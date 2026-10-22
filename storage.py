"""SQLite persistence and workflow rules. Every operation uses a short transaction."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from core import FIELDS, key, normalize, score_pair


DATA_DIR = Path(os.getenv("APP_DATA_DIR", Path(__file__).parent / "runtime")).resolve()
DB_PATH = DATA_DIR / "records.sqlite3"
ROLES = ("contributor", "reviewer", "admin")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    with connection() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL, role TEXT NOT NULL
          CHECK(role IN ('contributor','reviewer','admin')),
          locked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources (
          id INTEGER PRIMARY KEY, kind TEXT NOT NULL, filename TEXT NOT NULL,
          file_path TEXT, sha256 TEXT UNIQUE,
          raw_text TEXT NOT NULL DEFAULT '',
          claimed_type TEXT NOT NULL CHECK(claimed_type IN ('community','official')),
          verified_official INTEGER NOT NULL DEFAULT 0,
          provider_role TEXT NOT NULL, purpose TEXT NOT NULL,
          contact TEXT NOT NULL DEFAULT '', submitted_by INTEGER REFERENCES users(id),
          import_status TEXT NOT NULL DEFAULT 'complete', import_error TEXT NOT NULL DEFAULT '',
          extraction_mode TEXT NOT NULL DEFAULT 'manual',
          created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS records (
          id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id),
          raw_text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
          CHECK(status IN ('pending','approved','rejected','merged')),
          canonical_id INTEGER REFERENCES records(id), fields_json TEXT NOT NULL DEFAULT '{}',
          full_name TEXT NOT NULL DEFAULT '', name_key TEXT NOT NULL DEFAULT '',
          hometown_key TEXT NOT NULL DEFAULT '', unit_key TEXT NOT NULL DEFAULT '',
          identifier_key TEXT NOT NULL DEFAULT '', birth_year TEXT NOT NULL DEFAULT '',
          death_date TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS record_status ON records(status,id);
        CREATE INDEX IF NOT EXISTS record_name ON records(name_key,status);
        CREATE INDEX IF NOT EXISTS record_birth_place ON records(birth_year,hometown_key,status);
        CREATE INDEX IF NOT EXISTS record_unit ON records(unit_key,status);
        CREATE INDEX IF NOT EXISTS record_identifier ON records(identifier_key,status);
        CREATE INDEX IF NOT EXISTS record_source ON records(source_id);
        CREATE TABLE IF NOT EXISTS facts (
          id INTEGER PRIMARY KEY, record_id INTEGER NOT NULL REFERENCES records(id),
          source_id INTEGER NOT NULL REFERENCES sources(id), field TEXT NOT NULL,
          raw_value TEXT NOT NULL, proposed_value TEXT NOT NULL, suggestion TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
          reviewed_by INTEGER REFERENCES users(id), reviewed_at TEXT);
        CREATE INDEX IF NOT EXISTS fact_record ON facts(record_id);
        CREATE TABLE IF NOT EXISTS record_extras (
          id INTEGER PRIMARY KEY, record_id INTEGER NOT NULL REFERENCES records(id),
          source_id INTEGER NOT NULL REFERENCES sources(id), label TEXT NOT NULL,
          raw_value TEXT NOT NULL, reviewed INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS extra_record ON record_extras(record_id);
        CREATE TABLE IF NOT EXISTS source_extras (
          id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES sources(id),
          label TEXT NOT NULL, raw_value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS comments (
          id INTEGER PRIMARY KEY, record_id INTEGER NOT NULL REFERENCES records(id),
          author_id INTEGER NOT NULL REFERENCES users(id), text TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending'
          CHECK(status IN ('pending','approved','rejected')),
          created_at TEXT NOT NULL, reviewed_by INTEGER REFERENCES users(id));
        CREATE INDEX IF NOT EXISTS comment_record ON comments(record_id,status);
        CREATE TABLE IF NOT EXISTS links (
          a INTEGER NOT NULL REFERENCES records(id), b INTEGER NOT NULL REFERENCES records(id),
          label TEXT NOT NULL CHECK(label IN ('same','related','different')),
          reviewer_id INTEGER NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
          PRIMARY KEY(a,b), CHECK(a<b));
        CREATE VIRTUAL TABLE IF NOT EXISTS record_fts USING fts5(body, tokenize='unicode61 remove_diacritics 2');
        """)


def digest_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 480_000)
    return f"pbkdf2_sha256$480000${salt.hex()}${digest.hex()}"


def create_user(username: str, password: str, role: str = "contributor") -> int:
    username = username.strip().lower()
    if role not in ROLES or not (3 <= len(username) <= 40) or not username.replace("_", "").isalnum():
        raise ValueError("Tên đăng nhập 3–40 ký tự chữ/số/_")
    if len(password) < 12:
        raise ValueError("Mật khẩu cần ít nhất 12 ký tự")
    with connection() as db:
        return int(db.execute("INSERT INTO users(username,password_hash,role,created_at) VALUES(?,?,?,?)",
                              (username, digest_password(password), role, now())).lastrowid)


def authenticate(username: str, password: str) -> dict | None:
    with connection() as db:
        row = db.execute("SELECT * FROM users WHERE username=? AND locked=0", (username.strip().lower(),)).fetchone()
    if row:
        try:
            _, iterations, salt, expected = row["password_hash"].split("$")
            actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
            if secrets.compare_digest(actual, bytes.fromhex(expected)):
                return {"id": row["id"], "username": row["username"], "role": row["role"]}
        except (ValueError, TypeError):
            pass
    return None


def require(user: dict | None, *roles: str) -> None:
    if not user or user.get("role") not in roles:
        raise PermissionError("Bạn không có quyền thực hiện thao tác này")
    with connection() as db:
        row = db.execute("SELECT role,locked FROM users WHERE id=?", (user["id"],)).fetchone()
    if row is None or row["locked"] or row["role"] not in roles:
        raise PermissionError("Tài khoản đã bị khóa hoặc quyền đã thay đổi")


def set_user_role(actor: dict, target_id: int, role: str, locked: bool) -> None:
    require(actor, "admin")
    if role not in ROLES or actor["id"] == target_id:
        raise ValueError("Không thể thay đổi chính tài khoản đang dùng")
    with connection() as db:
        db.execute("UPDATE users SET role=?,locked=? WHERE id=?", (role, int(locked), target_id))


def create_source(kind: str, filename: str, claimed_type: str, provider_role: str,
                  purpose: str, contact: str, submitted_by: int | None,
                  file_path: str | None = None, sha256: str | None = None,
                  raw_text: str = "", extraction_mode: str = "manual") -> int:
    if claimed_type not in ("community", "official"):
        raise ValueError("Nguồn không hợp lệ")
    with connection() as db:
        cur = db.execute("""INSERT INTO sources(kind,filename,file_path,sha256,raw_text,claimed_type,
                          provider_role,purpose,contact,submitted_by,extraction_mode,created_at)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (kind, filename[:255], file_path, sha256, raw_text[:120_000], claimed_type,
                          provider_role[:120], purpose[:120], contact[:255], submitted_by, extraction_mode, now()))
        return int(cur.lastrowid)


def source_by_sha(sha: str) -> int | None:
    with connection() as db:
        row = db.execute("SELECT id FROM sources WHERE sha256=?", (sha,)).fetchone()
    return row[0] if row else None


def mark_import(source_id: int, status: str, error: str = "") -> None:
    if status not in ("processing", "complete", "partial"):
        raise ValueError("Trạng thái nhập không hợp lệ")
    with connection() as db:
        db.execute("UPDATE sources SET import_status=?,import_error=? WHERE id=?",
                   (status, error[:500], source_id))


def add_records(source_id: int, items: list[tuple]) -> list[int]:
    """One transaction per bounded batch; no whole file is kept in memory."""
    ids = []
    with connection() as db:
        for item in items:
            raw_text, fields = item[:2]
            extras = item[2] if len(item) >= 3 else []
            rec_id = db.execute("INSERT INTO records(source_id,raw_text,created_at) VALUES(?,?,?)",
                                (source_id, raw_text[:120_000], now())).lastrowid
            ids.append(rec_id)
            for field, raw in fields.items():
                if field not in FIELDS or not str(raw).strip():
                    continue
                proposed, suggestion = normalize(field, str(raw))
                db.execute("""INSERT INTO facts(record_id,source_id,field,raw_value,proposed_value,suggestion)
                              VALUES(?,?,?,?,?,?)""",
                           (rec_id, source_id, field, str(raw)[:4000], proposed[:4000], suggestion[:4000]))
            for extra in extras:
                label = str(extra["label"]).strip()[:120]
                value = str(extra["value"]).strip()[:4000]
                if label and value:
                    db.execute("""INSERT INTO record_extras(record_id,source_id,label,raw_value)
                                  VALUES(?,?,?,?)""", (rec_id, source_id, label, value))
    return ids


def add_source_extras(source_id: int, items: list[dict[str, str]]) -> None:
    with connection() as db:
        for item in items:
            db.execute("INSERT INTO source_extras(source_id,label,raw_value) VALUES(?,?,?)",
                       (source_id, item["label"][:120], item["value"][:4000]))


def post_sources() -> list[dict]:
    with connection() as db:
        return [dict(row) for row in db.execute("""SELECT s.id,s.raw_text,s.created_at,
                  s.provider_role,COUNT(r.id) AS record_count FROM sources s
                  LEFT JOIN records r ON r.source_id=s.id WHERE s.kind='post'
                  GROUP BY s.id ORDER BY s.id DESC LIMIT 100""")]


def attach_person(source_id: int, raw_fields: dict[str, str], actor: dict) -> int:
    require(actor, "reviewer", "admin")
    with connection() as db:
        source = db.execute("SELECT raw_text,kind FROM sources WHERE id=?", (source_id,)).fetchone()
    if not source or source["kind"] != "post":
        raise ValueError("Không tìm thấy bài đăng")
    if not (raw_fields.get("full_name") or raw_fields.get("identifier")):
        raise ValueError("Cần tên hoặc mã để phân biệt cá nhân")
    return add_records(source_id, [(source["raw_text"], raw_fields)])[0]


def create_post(text: str, extracted, actor: dict, provider_role: str,
                purpose: str, contact: str = "") -> tuple[int, list[int]]:
    require(actor, *ROLES)
    if not (10 <= len(text.strip()) <= 50000):
        raise ValueError("Bài đăng cần từ 10 đến 50.000 ký tự")
    source_id = create_source("post", "Bài đăng người dùng", "community", provider_role,
                              purpose, contact, actor["id"], raw_text=text)
    items = []
    for person in extracted.people:
        fields = person.facts.facts()
        if not (fields.get("full_name") or fields.get("identifier")):
            # Without a distinguishing identity, keep the text under the source.
            continue
        extras = [item.model_dump() for item in person.extra_fields]
        if person.evidence_excerpt.strip():
            extras.append({"label": "Trích đoạn đối chiếu", "value": person.evidence_excerpt[:1000]})
        items.append((text, fields, extras))
    ids = add_records(source_id, items)
    add_source_extras(source_id, [field.model_dump() for field in extracted.post_extras])
    return source_id, ids


def record(record_id: int) -> dict | None:
    with connection() as db:
        item = db.execute("""SELECT r.*,s.kind,s.filename,s.file_path,s.claimed_type,s.verified_official,
                         s.provider_role,s.purpose,s.contact,s.raw_text AS source_raw_text,s.created_at AS source_created
                         FROM records r JOIN sources s ON s.id=r.source_id WHERE r.id=?""", (record_id,)).fetchone()
        if not item:
            return None
        result = dict(item)
        result["facts"] = [dict(row) for row in db.execute("SELECT * FROM facts WHERE record_id=? ORDER BY id", (record_id,))]
        result["extras"] = [dict(row) for row in db.execute("SELECT * FROM record_extras WHERE record_id=? ORDER BY id", (record_id,))]
        result["source_extras"] = [dict(row) for row in db.execute("SELECT * FROM source_extras WHERE source_id=? ORDER BY id", (result["source_id"],))]
        result["comments"] = [dict(row) for row in db.execute("""SELECT c.*,u.username FROM comments c JOIN users u ON u.id=c.author_id
                        WHERE record_id=? AND status='approved' ORDER BY c.id""", (record_id,))]
        return result


def _refresh_fts(db: sqlite3.Connection, record_id: int) -> None:
    item = db.execute("SELECT raw_text,status FROM records WHERE id=?", (record_id,)).fetchone()
    db.execute("DELETE FROM record_fts WHERE rowid=?", (record_id,))
    if item["status"] != "approved":
        return
    facts = db.execute("SELECT raw_value,proposed_value FROM facts WHERE record_id=? AND status='approved'", (record_id,))
    extras = db.execute("SELECT raw_value FROM record_extras WHERE record_id=? AND reviewed=1", (record_id,))
    comments = db.execute("SELECT text FROM comments WHERE record_id=? AND status='approved'", (record_id,))
    text = " ".join([item["raw_text"], *(v for row in facts for v in row),
                     *(row[0] for row in extras), *(row[0] for row in comments)])
    # FTS5 alone does not necessarily fold Vietnamese đ to d. Store the same
    # accent-free key used for user queries to make both spellings searchable.
    db.execute("INSERT INTO record_fts(rowid,body) VALUES(?,?)", (record_id, key(text[:500_000])))


def approve_record(record_id: int, actor: dict, overrides: dict[str, str] | None = None,
                   extra_edits: dict[str, str] | None = None,
                   verify_official: bool = False, reject: bool = False) -> None:
    require(actor, "reviewer", "admin")
    overrides = overrides or {}
    extra_edits = extra_edits or {}
    with connection() as db:
        _approve_in_tx(db, record_id, actor["id"], overrides, extra_edits, verify_official, reject)


def _approve_in_tx(db: sqlite3.Connection, record_id: int, actor_id: int, overrides: dict[str, str],
                   extra_edits: dict[str, str], verify_official: bool, reject: bool) -> None:
    item = db.execute("SELECT source_id,status FROM records WHERE id=?", (record_id,)).fetchone()
    if not item or item["status"] != "pending":
        raise ValueError("Hồ sơ không còn chờ duyệt")
    if reject:
        db.execute("UPDATE records SET status='rejected' WHERE id=?", (record_id,))
        db.execute("UPDATE facts SET status='rejected',reviewed_by=?,reviewed_at=? WHERE record_id=?",
                   (actor_id, now(), record_id))
        return
    facts = db.execute("SELECT * FROM facts WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
    values: dict[str, str] = {}
    for fact in facts:
        replacement = str(overrides.get(str(fact["id"]), fact["proposed_value"])).strip()[:4000]
        if replacement:
            values.setdefault(fact["field"], replacement)
        db.execute("""UPDATE facts SET proposed_value=?,status=?,reviewed_by=?,reviewed_at=? WHERE id=?""",
                   (replacement, "approved" if replacement else "rejected", actor_id, now(), fact["id"]))
    for extra in db.execute("SELECT id,raw_value FROM record_extras WHERE record_id=?", (record_id,)).fetchall():
        value = str(extra_edits.get(str(extra["id"]), extra["raw_value"])).strip()[:4000]
        db.execute("UPDATE record_extras SET raw_value=?,reviewed=? WHERE id=?",
                   (value, int(bool(value)) if value else -1, extra["id"]))
    db.execute("""UPDATE records SET status='approved',fields_json=?,full_name=?,name_key=?,
                hometown_key=?,unit_key=?,identifier_key=?,birth_year=?,death_date=? WHERE id=?""",
               (json.dumps(values, ensure_ascii=False), values.get("full_name", ""), key(values.get("full_name")),
                key(values.get("hometown")), key(values.get("military_unit")), key(values.get("identifier")),
                values.get("birth_year", ""), values.get("death_date", ""), record_id))
    if verify_official:
        db.execute("""UPDATE sources SET verified_official=1 WHERE id=? AND claimed_type='official'""", (item["source_id"],))
    _refresh_fts(db, record_id)


def batch_sources() -> list[dict]:
    with connection() as db:
        return [dict(row) for row in db.execute("""SELECT s.id,s.filename,s.claimed_type,
                 COUNT(r.id) AS remaining FROM sources s JOIN records r ON r.source_id=s.id
                 WHERE s.extraction_mode='rules' AND s.import_status='complete'
                 AND r.status='pending' GROUP BY s.id ORDER BY s.id DESC LIMIT 50""")]


def batch_sample(source_id: int, count: int = 5) -> list[int]:
    with connection() as db:
        return [row[0] for row in db.execute("""SELECT id FROM records WHERE source_id=? AND status='pending'
               ORDER BY id LIMIT ?""", (source_id, count))]


def approve_source_batch(source_id: int, actor: dict, *, reviewed_sample: bool,
                         verify_official: bool = False, max_records: int = 2500) -> int:
    require(actor, "reviewer", "admin")
    if not reviewed_sample:
        raise ValueError("Cần kiểm tra mẫu hồ sơ và nguồn trước khi duyệt lô")
    with connection() as db:
        source = db.execute("SELECT extraction_mode,import_status,claimed_type FROM sources WHERE id=?", (source_id,)).fetchone()
    if not source or source["extraction_mode"] != "rules" or source["import_status"] != "complete":
        raise ValueError("Chỉ duyệt lô nguồn có cấu trúc đã nạp hoàn tất bằng quy tắc")
    if verify_official and source["claimed_type"] != "official":
        raise ValueError("Nguồn này không khai báo là chính thống")
    processed = 0
    while processed < max_records:
        with connection() as db:
            rows = db.execute("SELECT id FROM records WHERE source_id=? AND status='pending' ORDER BY id LIMIT ?",
                              (source_id, min(250, max_records - processed))).fetchall()
            for row in rows:
                _approve_in_tx(db, row["id"], actor["id"], {}, {}, verify_official, False)
        processed += len(rows)
        if not rows:
            break
    return processed


def pending(limit: int = 50, offset: int = 0) -> list[dict]:
    with connection() as db:
        return [dict(row) for row in db.execute("""SELECT r.id,r.raw_text,r.created_at,s.filename,
               s.claimed_type,s.provider_role,COALESCE((SELECT proposed_value FROM facts
               WHERE record_id=r.id AND field='full_name' LIMIT 1),'') AS name
               FROM records r JOIN sources s ON s.id=r.source_id
               WHERE r.status='pending' ORDER BY r.id LIMIT ? OFFSET ?""", (limit, offset))]


def stats() -> dict:
    with connection() as db:
        return {status: db.execute("SELECT COUNT(*) FROM records WHERE status=?", (status,)).fetchone()[0]
                for status in ("pending", "approved", "merged")}


def find_similar(record_id: int, limit: int = 10) -> list[dict]:
    item = record(record_id)
    if not item:
        return []
    values = json.loads(item["fields_json"]) if item["status"] == "approved" else {
        fact["field"]: fact["proposed_value"] for fact in item["facts"]}
    name_key, birth, hometown_key, unit_key, identifier_key = (key(values.get("full_name")),
        values.get("birth_year", ""), key(values.get("hometown")),
        key(values.get("military_unit")), key(values.get("identifier")))
    if not (name_key or identifier_key or (birth and (hometown_key or unit_key))):
        return []
    with connection() as db:
        rows = db.execute("""SELECT id,fields_json,full_name,status FROM records
              WHERE id<>? AND status='approved' AND (
                (name_key<>'' AND name_key=?) OR
                (identifier_key<>'' AND identifier_key=?) OR
                (birth_year<>'' AND birth_year=? AND hometown_key<>'' AND hometown_key=?) OR
                (birth_year<>'' AND birth_year=? AND unit_key<>'' AND unit_key=?))
              ORDER BY CASE WHEN identifier_key<>'' AND identifier_key=? THEN 0
                            WHEN name_key<>'' AND name_key=? THEN 1 ELSE 2 END,
                       id DESC LIMIT 300""",
              (record_id, name_key, identifier_key, birth, hometown_key, birth, unit_key,
               identifier_key, name_key)).fetchall()
    result = []
    for row in rows:
        score, evidence = score_pair(values, json.loads(row["fields_json"]))
        if evidence >= 2 and score >= .64:
            result.append({"id": row["id"], "full_name": row["full_name"],
                           "score": score, "evidence": evidence})
    return sorted(result, key=lambda x: -x["score"])[:limit]


def label_pair(a: int, b: int, label: str, actor: dict) -> None:
    require(actor, "reviewer", "admin")
    if a == b or label not in ("same", "related", "different"):
        raise ValueError("Nhãn không hợp lệ")
    x, y = sorted((a, b))
    with connection() as db:
        db.execute("""INSERT INTO links(a,b,label,reviewer_id,created_at) VALUES(?,?,?,?,?)
             ON CONFLICT(a,b) DO UPDATE SET label=excluded.label,reviewer_id=excluded.reviewer_id,
             created_at=excluded.created_at""", (x, y, label, actor["id"], now()))


def merge_records(primary: int, secondary: int, actor: dict) -> None:
    """A human-confirmed merge retains every original fact and source."""
    require(actor, "reviewer", "admin")
    if primary == secondary:
        raise ValueError("Chọn hai hồ sơ khác nhau")
    with connection() as db:
        rows = db.execute("SELECT id,status FROM records WHERE id IN (?,?)", (primary, secondary)).fetchall()
        if len(rows) != 2 or any(row["status"] != "approved" for row in rows):
            raise ValueError("Chỉ có thể gộp hai hồ sơ đã duyệt")
        x, y = sorted((primary, secondary))
        row = db.execute("SELECT label FROM links WHERE a=? AND b=?", (x, y)).fetchone()
        if not row or row["label"] != "same":
            raise ValueError("Người kiểm duyệt cần xác nhận trùng trước khi gộp")
        db.execute("UPDATE facts SET record_id=? WHERE record_id=?", (primary, secondary))
        db.execute("UPDATE record_extras SET record_id=? WHERE record_id=?", (primary, secondary))
        db.execute("UPDATE comments SET record_id=? WHERE record_id=?", (primary, secondary))
        db.execute("UPDATE records SET status='merged',canonical_id=? WHERE id=?", (primary, secondary))
        _refresh_fts(db, primary)
        _refresh_fts(db, secondary)


def add_comment(record_id: int, text: str, actor: dict) -> None:
    require(actor, *ROLES)
    if not text.strip() or len(text) > 4000:
        raise ValueError("Bình luận cần có nội dung và tối đa 4.000 ký tự")
    with connection() as db:
        if not db.execute("SELECT 1 FROM records WHERE id=? AND status='approved'", (record_id,)).fetchone():
            raise ValueError("Hồ sơ chưa được duyệt")
        db.execute("INSERT INTO comments(record_id,author_id,text,created_at) VALUES(?,?,?,?)",
                   (record_id, actor["id"], text.strip(), now()))


def comment_queue() -> list[dict]:
    with connection() as db:
        return [dict(row) for row in db.execute("""SELECT c.*,u.username FROM comments c
               JOIN users u ON u.id=c.author_id WHERE c.status='pending' ORDER BY c.id LIMIT 100""")]


def review_comment(comment_id: int, accept: bool, actor: dict) -> None:
    require(actor, "reviewer", "admin")
    with connection() as db:
        row = db.execute("SELECT record_id,status FROM comments WHERE id=?", (comment_id,)).fetchone()
        if not row or row["status"] != "pending":
            raise ValueError("Bình luận không còn chờ duyệt")
        db.execute("UPDATE comments SET status=?,reviewed_by=? WHERE id=?",
                   ("approved" if accept else "rejected", actor["id"], comment_id))
        _refresh_fts(db, row["record_id"])


def search(keyword: str = "", name: str = "", hometown: str = "", unit: str = "",
           birth_year: str = "", death_date: str = "", source_type: str = "all",
           provider_role: str = "", limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
    where = ["r.status='approved'"]
    params: list = []
    join = "JOIN sources s ON s.id=r.source_id"
    words = key(keyword).split()[:10]
    if words:
        join += " JOIN record_fts ON record_fts.rowid=r.id"
        where.append("record_fts MATCH ?")
        params.append(" AND ".join('"' + w[:64] + '"*' for w in words))
    for col, value in (("name_key", name), ("hometown_key", hometown), ("unit_key", unit)):
        if value.strip():
            where.append(f"r.{col} LIKE ? ESCAPE '\\'")
            safe = key(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append("%" + safe + "%")
    if birth_year.strip():
        where.append("r.birth_year=?")
        params.append(birth_year.strip())
    if death_date.strip():
        where.append("r.death_date=?")
        params.append(normalize("death_date", death_date)[0])
    if source_type == "official":
        where.append("s.verified_official=1")
    elif source_type == "community":
        where.append("s.verified_official=0")
    if provider_role.strip():
        where.append("s.provider_role=?")
        params.append(provider_role.strip())
    sql = f"FROM records r {join} WHERE " + " AND ".join(where)
    with connection() as db:
        count = db.execute("SELECT count(*) " + sql, params).fetchone()[0]
        rows = db.execute("""SELECT r.id,r.full_name,r.fields_json,r.raw_text,s.filename,
                           s.provider_role,s.verified_official """ + sql +
                          " ORDER BY r.id DESC LIMIT ? OFFSET ?", (*params, min(limit, 100), max(offset, 0))).fetchall()
    return [dict(row) for row in rows], count


def users() -> list[dict]:
    with connection() as db:
        return [dict(row) for row in db.execute("SELECT id,username,role,locked,created_at FROM users ORDER BY id")]
