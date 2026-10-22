"""Run: python -m unittest discover -s tests -v"""
import csv
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import ExtractedPost, extract_post_llm
from evaluate import evaluate_llm_extraction, evaluate_normalization
import storage
from ingest import import_file


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_dir, self.old_path = storage.DATA_DIR, storage.DB_PATH
        storage.DATA_DIR = Path(self.temp.name) / "data"
        storage.DB_PATH = storage.DATA_DIR / "records.sqlite3"
        import ingest
        self.ingest = ingest
        self.ingest.DATA_DIR = storage.DATA_DIR
        storage.init_db()
        uid = storage.create_user("reviewer01", "a long demo password", "reviewer")
        self.reviewer = {"id": uid, "role": "reviewer", "username": "reviewer01"}
        uid = storage.create_user("provider01", "another long password")
        self.provider = {"id": uid, "role": "contributor", "username": "provider01"}

    def tearDown(self):
        storage.DATA_DIR, storage.DB_PATH = self.old_dir, self.old_path
        self.ingest.DATA_DIR = self.old_dir
        self.temp.cleanup()

    def test_multiple_people_missing_fields_and_extended_details(self):
        post = "Danh sách minh họa: liệt sĩ hư cấu An Văn Mẫu, quê Quảng Trị, giữ chiếc la bàn; liệt sĩ hư cấu Bình Văn Ví Dụ, đơn vị F304. Có ghi chú chưa rõ người: gần một cây cầu."
        example = ExtractedPost.model_validate({
            "people": [
                {"facts": {"full_name": "An Văn Mẫu", "hometown": "Quảng Trị", "relic": "chiếc la bàn"},
                 "extra_fields": [{"label": "Ghi chú", "value": "Hồ sơ hư cấu"}],
                 "evidence_excerpt": "An Văn Mẫu, quê Quảng Trị"},
                {"facts": {"full_name": "Bình Văn Ví Dụ", "military_unit": "F304"},
                 "extra_fields": [], "evidence_excerpt": "Bình Văn Ví Dụ, đơn vị F304"}],
            "post_extras": [{"label": "Vị trí chưa rõ", "value": "gần một cây cầu"}],
        })
        fake_client = SimpleNamespace(responses=SimpleNamespace(
            parse=lambda **kw: SimpleNamespace(output_parsed=example)))
        with patch.dict(sys.modules, {"openai": SimpleNamespace(OpenAI=lambda: fake_client)}):
            actual = extract_post_llm(post)
            metrics = evaluate_llm_extraction([{
                "raw_text": post,
                "expected_people_json": json.dumps([{
                    "full_name": "An Văn Mẫu", "hometown": "Quảng Trị", "relic": "chiếc la bàn"
                }, {"full_name": "Bình Văn Ví Dụ", "military_unit": "F304"}]),
            }])
        self.assertEqual(metrics["micro"]["F1"], 1.0)
        self.assertEqual(evaluate_normalization([{"field": "military_unit", "raw_value": "F304",
                                                 "expected": "Sư đoàn 304"}])["Accuracy"], 1.0)
        source, ids = storage.create_post(post, actual, self.provider, "Nhân chứng", "Cung cấp thông tin")
        self.assertEqual(len(ids), 2)
        first, second = storage.record(ids[0]), storage.record(ids[1])
        self.assertEqual([f["field"] for f in second["facts"]], ["full_name", "military_unit"])
        self.assertEqual(second["source_extras"][0]["raw_value"], "gần một cây cầu")
        self.assertEqual(first["extras"][0]["label"], "Ghi chú")
        self.assertEqual(storage.search(name="An Văn Mẫu")[1], 0)
        storage.approve_record(ids[0], self.reviewer)
        storage.approve_record(ids[1], self.reviewer)
        self.assertEqual(storage.search(name="An Văn Mẫu")[1], 1)
        self.assertEqual(json.loads(storage.record(ids[1])["fields_json"])["military_unit"], "Sư đoàn 304")
        self.assertNotIn("birth_year", json.loads(storage.record(ids[1])["fields_json"]))

    def test_batch_import_lookup_and_reviewed_merge(self):
        path = Path(self.temp.name) / "mock.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.DictWriter(stream, fieldnames=["Họ tên", "Năm sinh", "Quê quán", "Đơn vị", "Di vật", "Chú thích khác"])
            writer.writeheader()
            for i in range(1250):
                writer.writerow({"Họ tên": f"Nhân vật hư cấu {i}", "Năm sinh": "1940",
                                 "Quê quán": "Quảng Trị", "Đơn vị": "F304", "Di vật": "Vòng tay giả định",
                                 "Chú thích khác": "Dữ liệu tổng hợp"})
        info = import_file(path, mode="rules")  # structured columns avoid LLM requests
        self.assertEqual(info["count"], 1250)
        self.assertEqual(import_file(path, mode="rules")["duplicate_file"], True)
        rid = storage.pending(limit=1)[0]["id"]
        self.assertEqual(storage.record(rid)["extras"][0]["label"], "Chú thích khác")
        storage.approve_record(rid, self.reviewer)
        result, total = storage.search(keyword="vòng tay", hometown="Quảng Trị")
        self.assertEqual(total, 1)
        self.assertEqual(result[0]["id"], rid)
        self.assertEqual(storage.approve_source_batch(info["source_id"], self.reviewer,
                                                    reviewed_sample=True), 1249)
        self.assertEqual(storage.search(keyword="vòng tay")[1], 1250)
        self.assertEqual(storage.search(keyword="đơn vị")[1], 1250)
        # A second source with matching identity may be linked and merged after review.
        source2 = storage.create_source("post", "Nguồn khác", "community", "Thân nhân", "Tìm kiếm", "", self.provider["id"])
        rid2 = storage.add_records(source2, [("Thông tin khác", {"full_name": "Nhân vật hư cấu 0", "birth_year": "1940",
                                                               "hometown": "Quảng Trị"})])[0]
        storage.approve_record(rid2, self.reviewer)
        self.assertTrue(storage.find_similar(rid2))
        storage.label_pair(rid, rid2, "same", self.reviewer)
        storage.merge_records(rid, rid2, self.reviewer)
        self.assertEqual(storage.record(rid2)["status"], "merged")
        self.assertEqual(storage.record(rid2)["canonical_id"], rid)
        self.assertEqual(len(storage.record(rid)["facts"]), 8)

    def test_excel_pdf_image_free_text_split_into_people(self):
        import fitz
        from openpyxl import Workbook
        from PIL import Image

        output = ExtractedPost.model_validate({"people": [
            {"facts": {"full_name": "Nhân Vật Mẫu Một"},
             "extra_fields": [{"label": "Chi tiết", "value": "Vật minh họa"}],
             "evidence_excerpt": "Nhân Vật Mẫu Một"},
            {"facts": {"full_name": "Nhân Vật Mẫu Hai"},
             "extra_fields": [], "evidence_excerpt": "Nhân Vật Mẫu Hai"}],
            "post_extras": [{"label": "Không rõ ai", "value": "gần đường mòn"}]})
        xlsx = Path(self.temp.name) / "story.xlsx"
        workbook = Workbook()
        workbook.active.append(["Nội dung", "Ghi chú"])
        workbook.active.append(["Nhân Vật Mẫu Một và Nhân Vật Mẫu Hai trong câu chuyện minh họa.", "Hư cấu"])
        workbook.save(xlsx)
        pdf = Path(self.temp.name) / "story.pdf"
        doc = fitz.open()
        page = doc.new_page()
        page.insert_text((72, 72), "A fictional story about character one and character two for demonstration only.")
        doc.save(pdf)
        doc.close()
        image = Path(self.temp.name) / "story.png"
        Image.new("RGB", (300, 100), "white").save(image)
        with patch.object(self.ingest, "extract_post_llm", return_value=output), \
             patch.object(self.ingest, "ocr_image", return_value="Hai nhân vật trong ảnh"):
            for path in (xlsx, pdf, image):
                info = import_file(path, mode="auto")
                self.assertEqual(info["count"], 2, path.name)
                with storage.connection() as db:
                    rows = db.execute("SELECT id FROM records WHERE source_id=? ORDER BY id", (info["source_id"],)).fetchall()
                first = storage.record(rows[0]["id"])
                if path != xlsx:
                    self.assertEqual(first["extras"][-1]["label"], "Trích đoạn đối chiếu")
                self.assertTrue(any(e["label"].startswith("Chưa quy cho cá nhân") for e in first["extras"]))


if __name__ == "__main__":
    unittest.main()
