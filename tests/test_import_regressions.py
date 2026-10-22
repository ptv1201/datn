"""Offline regressions for empty LLM extras, CSV parsing and safe retries."""
import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from core import ExtractedPost
import ingest
import storage


class ExtraValidationTests(unittest.TestCase):
    def test_empty_sixth_extra_and_nested_placeholders(self):
        payload = {
            "people": [{"facts": {"full_name": "Nhân Vật Mẫu"},
                        "extra_fields": [{"label": "Diện mạo", "value": " "},
                                         {"label": None, "value": ""},
                                         {"label": "Ghi chú", "value": "Dữ liệu hư cấu"}]}],
            "post_extras": [{"label": f"Mục {i}", "value": "Có dữ liệu"} for i in range(5)]
                           + [{"label": "Ghi chú", "value": ""}],
        }
        parsed = ExtractedPost.model_validate(payload)
        self.assertEqual(len(parsed.post_extras), 5)
        self.assertEqual(parsed.people[0].extra_fields[0].value, "Dữ liệu hư cấu")
        self.assertEqual(len(parsed.people[0].extra_fields), 1)
        self.assertIsNone(parsed.people[0].facts.birth_year)
        self.assertEqual(ExtractedPost.model_validate({"post_extras": None}).post_extras, [])

    def test_invalid_nonempty_data_is_still_rejected(self):
        with self.assertRaises(ValidationError):
            ExtractedPost.model_validate({"post_extras": [{"label": "Sai kiểu", "value": ["x"]}]})


class ImportRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.original = storage.DATA_DIR, storage.DB_PATH, ingest.DATA_DIR
        storage.DATA_DIR = self.root / "runtime"
        storage.DB_PATH = storage.DATA_DIR / "records.sqlite3"
        ingest.DATA_DIR = storage.DATA_DIR
        storage.init_db()
        self.owner = storage.create_user("owner01", "long test password 01")
        self.other = storage.create_user("owner02", "long test password 02")
        self.path = self.root / "sample.csv"
        with self.path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";")
            writer.writerow(["Họ tên", "Năm sinh", "Ghi chú khác"])
            writer.writerow(["Nhân Vật Mẫu Một", "1941", "Dữ liệu hư cấu, có dấu phẩy"])
            writer.writerow(["Nhân Vật Mẫu Hai", "", "Ghi chú khác"])

    def tearDown(self):
        storage.DATA_DIR, storage.DB_PATH, ingest.DATA_DIR = self.original
        self.tmp.cleanup()

    def test_semicolon_csv_maps_columns_without_calling_llm(self):
        with patch.object(ingest, "extract_post_llm", side_effect=AssertionError("Unnecessary LLM call")):
            info = ingest.import_file(self.path, mode="auto", user_id=self.owner)
        self.assertEqual(info["count"], 2)
        row = storage.record(storage.pending(limit=1)[0]["id"])
        facts = {f["field"]: f["raw_value"] for f in row["facts"]}
        self.assertEqual(facts["full_name"], "Nhân Vật Mẫu Một")
        self.assertEqual(row["extras"][0]["raw_value"], "Dữ liệu hư cấu, có dấu phẩy")

    def test_zero_record_failure_can_retry_under_same_source(self):
        with patch.object(ingest, "iterate_content", side_effect=ValueError("Invalid empty model extra")):
            with self.assertRaisesRegex(RuntimeError, "0 mục"):
                ingest.import_file(self.path, mode="auto", user_id=self.owner)
        blocked = ingest.import_file(self.path, mode="rules", user_id=self.other)
        self.assertTrue(blocked["duplicate_file"])
        self.assertEqual(blocked["import_status"], "partial")
        success = ingest.import_file(self.path, mode="rules", user_id=self.owner)
        self.assertTrue(success["retried"])
        self.assertEqual(success["source_id"], blocked["source_id"])
        self.assertEqual(success["count"], 2)
        self.assertTrue(ingest.import_file(self.path, mode="rules", user_id=self.owner)["duplicate_file"])
        with storage.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sources").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM records").fetchone()[0], 2)
            row = db.execute("SELECT import_status,extraction_mode FROM sources").fetchone()
            self.assertEqual(tuple(row), ("complete", "rules"))

    def test_failure_with_committed_records_does_not_restart(self):
        def fail_after_batch(path, mode):
            yield "Mẫu một", {"full_name": "Nhân Vật Mẫu Một"}, []
            yield "Mẫu hai", {"full_name": "Nhân Vật Mẫu Hai"}, []
            raise ValueError("Failure after a committed batch")

        with patch.object(ingest, "iterate_content", side_effect=fail_after_batch), patch.object(ingest, "BATCH_SIZE", 2):
            with self.assertRaisesRegex(RuntimeError, "2 mục"):
                ingest.import_file(self.path, mode="auto", user_id=self.owner)
        result = ingest.import_file(self.path, mode="rules", user_id=self.owner)
        self.assertTrue(result["duplicate_file"])
        self.assertEqual(result["stored_count"], 2)
        self.assertEqual(result["import_status"], "partial")
        with storage.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM records").fetchone()[0], 2)


class UploadLimitTests(unittest.TestCase):
    def test_500mb_boundary_without_allocating_a_500mb_file(self):
        class GeneratedStream:
            def __init__(self, size):
                self.remaining = size

            def read(self, size):
                size = min(size, self.remaining)
                self.remaining -= size
                return b"x" * size

        class Sink:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def write(self, block):
                return len(block)

        with tempfile.TemporaryDirectory() as tmp, patch.object(ingest, "DATA_DIR", Path(tmp)), \
                patch.object(Path, "open", return_value=Sink()), patch.object(Path, "unlink") as unlink:
            ingest.copy_to_store(GeneratedStream(500 * 1024 * 1024), "demo.csv", ingest.MAX_WEB_BYTES)
            with self.assertRaisesRegex(ValueError, "Vượt giới hạn"):
                ingest.copy_to_store(GeneratedStream(500 * 1024 * 1024 + 1), "demo.csv", ingest.MAX_WEB_BYTES)
            unlink.assert_called_once()


if __name__ == "__main__":
    unittest.main()
