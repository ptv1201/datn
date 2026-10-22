"""Offline administration and bulk importer. Run `python manage.py --help`."""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from ingest import SUPPORTED, import_file
from storage import approve_source_batch, authenticate, batch_sample, create_user, init_db


def main() -> None:
    parser = argparse.ArgumentParser(description="Quản trị và nạp hồ sơ")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("init-admin", help="Tạo quản trị viên đầu tiên (nhập mật khẩu kín)")
    admin.add_argument("username")
    bulk = sub.add_parser("import", help="Nạp một tệp hoặc cả thư mục, theo lô")
    bulk.add_argument("path", type=Path)
    bulk.add_argument("--type", choices=("community", "official"), default="community")
    bulk.add_argument("--provider-role", default="Nguồn nhập")
    bulk.add_argument("--mode", choices=("auto", "rules", "llm"), default="auto")
    approve = sub.add_parser("approve-source", help="Duyệt từng lô của nguồn nhập bằng quy tắc, sau khi kiểm tra mẫu")
    approve.add_argument("source_id", type=int)
    approve.add_argument("--reviewer", required=True, help="Tài khoản kiểm duyệt hoặc quản trị")
    approve.add_argument("--reviewed-sample", action="store_true", help="Đã kiểm tra các hàng mẫu và tài liệu gốc")
    approve.add_argument("--verify-official", action="store_true", help="Đã kiểm tra tư cách nguồn chính thống")
    args = parser.parse_args()
    init_db()
    if args.command == "init-admin":
        password = getpass.getpass("Mật khẩu quản trị (>=12 ký tự): ")
        confirm = getpass.getpass("Nhập lại mật khẩu: ")
        if password != confirm:
            parser.error("Mật khẩu không khớp")
        create_user(args.username, password, role="admin")
        print("Đã tạo tài khoản quản trị.")
        return
    if args.command == "approve-source":
        if not args.reviewed_sample:
            parser.error("Cần --reviewed-sample sau khi xem mẫu hồ sơ trong giao diện")
        actor = authenticate(args.reviewer, getpass.getpass("Mật khẩu kiểm duyệt: "))
        if not actor:
            parser.error("Đăng nhập không hợp lệ")
        print("Mẫu hồ sơ:", batch_sample(args.source_id))
        total = 0
        while count := approve_source_batch(args.source_id, actor, reviewed_sample=True,
                                            verify_official=args.verify_official, max_records=2500):
            total += count
            print(f"Đã duyệt {total} hồ sơ", flush=True)
        print("Hoàn thành.")
        return
    if not args.path.exists():
        parser.error("Không tìm thấy đường dẫn")
    files = ([args.path] if args.path.is_file() else sorted(
        p for p in args.path.rglob("*") if p.is_file() and not p.is_symlink()
        and p.suffix.lower() in SUPPORTED))
    total = failures = 0
    for path in files:
        try:
            info = import_file(path, claimed_type=args.type, provider_role=args.provider_role, mode=args.mode)
            total += info["count"]
            print(f"{path.name}: +{info['count']} bản ghi; nguồn #{info['source_id']}", flush=True)
        except (RuntimeError, ValueError) as exc:
            failures += 1
            print(f"{path.name}: LỖI {exc}", file=sys.stderr, flush=True)
    print(f"Hoàn thành: {total} bản ghi chờ duyệt; {failures} tệp lỗi.")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
