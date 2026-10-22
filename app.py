"""Run with: streamlit run app.py"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st

from core import FIELDS, extract_post_llm
from evaluate import (evaluate_extraction, evaluate_llm_extraction, evaluate_normalization,
                      evaluate_pairs, evaluate_ranking)
from ingest import MAX_WEB_MB, SUPPORTED, import_file
from storage import (DATA_DIR, add_comment, approve_record, approve_source_batch, attach_person, authenticate,
                     batch_sample, batch_sources, comment_queue, create_post, create_user, find_similar, init_db,
                     label_pair, merge_records, pending, record, require, review_comment,
                     search, set_user_role, stats, post_sources, users)

st.set_page_config(page_title="Tra cứu tư liệu liệt sĩ", page_icon="🕯️", layout="wide")
init_db()

st.markdown("""<style>
.block-container{max-width:1320px;padding-top:1.4rem}
h1,h2,h3{color:#17383e;letter-spacing:-.025em}
.kicker{color:#0a777b;font-weight:700;letter-spacing:.12em;font-size:.75rem;text-transform:uppercase}
.muted{color:#586a70;font-size:.92rem}
[data-testid="stMetric"]{background:#fff;border:1px solid #dbe5e6;padding:1rem;border-radius:.65rem}
[data-testid="stSidebar"]{background:#e9f0f0}
</style>""", unsafe_allow_html=True)


def badge(text: str) -> str:
    return f"`{text}`"


def redact(text: str) -> str:
    """Do not display contact details from raw material to anonymous visitors."""
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email ẩn]", text)
    return re.sub(r"(?<!\d)(?:\+?84|0)(?:[\s.\-]*\d){9,10}(?!\d)", "[số liên hệ ẩn]", text)


def session_user() -> dict | None:
    return st.session_state.get("user")


def auth_sidebar() -> None:
    user = session_user()
    if user:
        st.sidebar.success(f"{user['username']} · {user['role']}")
        if st.sidebar.button("Đăng xuất"):
            st.session_state.pop("user", None)
            st.rerun()
        return
    with st.sidebar.expander("Đăng nhập để đóng góp", expanded=False):
        with st.form("login"):
            username = st.text_input("Tên đăng nhập")
            password = st.text_input("Mật khẩu", type="password")
            if st.form_submit_button("Đăng nhập", width="stretch"):
                user = authenticate(username, password)
                if user:
                    st.session_state.user = user
                    st.rerun()
                st.error("Thông tin đăng nhập không đúng hoặc tài khoản bị khóa")
    with st.sidebar.expander("Tạo tài khoản người cung cấp"):
        with st.form("register"):
            username = st.text_input("Tên đăng nhập", key="reg_name")
            password = st.text_input("Mật khẩu từ 12 ký tự", type="password", key="reg_pass")
            if st.form_submit_button("Tạo tài khoản", width="stretch"):
                try:
                    create_user(username, password)
                    st.success("Đã tạo tài khoản. Hãy đăng nhập.")
                except (ValueError, sqlite3.IntegrityError) as exc:
                    st.error(str(exc))


def record_view(record_id: int, *, reviewer: bool = False) -> None:
    item = record(record_id)
    if not item or (item["status"] != "approved" and not reviewer):
        st.warning("Hồ sơ chưa được công bố hoặc không tồn tại")
        return
    values = json.loads(item["fields_json"])
    if item["status"] == "pending":
        values = {f["field"]: f["proposed_value"] for f in item["facts"]}
    title = values.get("full_name") or "Chưa rõ họ tên"
    st.subheader(f"{title}  ·  #{item['id']}")
    st.caption(f"Nguồn: {item['filename']} · Người cung cấp: {item['provider_role']} · "
               + ("Nguồn chính thống đã xác minh" if item["verified_official"] else "Nguồn cộng đồng / chưa xác minh nguồn"))
    left, right = st.columns([1, 1], gap="large")
    with left:
        st.markdown("#### Dữ liệu đã chuẩn hóa" if item["status"] == "approved" else "#### Dữ liệu đề xuất")
        st.dataframe(pd.DataFrame([{"Trường": label, "Giá trị": values.get(field, "")}
                                   for field, label in FIELDS.items()]), hide_index=True, width="stretch")
        st.markdown("#### Thông tin mở rộng")
        extras = [e for e in item["extras"] if reviewer or e["reviewed"] == 1]
        if extras:
            st.dataframe(pd.DataFrame([{"Mục": e["label"], "Chi tiết": e["raw_value"] if reviewer else redact(e["raw_value"])}
                                       for e in extras]), hide_index=True, width="stretch")
        else:
            st.caption("Chưa có thông tin ngoài các trường chính.")
    with right:
        st.markdown("#### Tài liệu và dữ kiện gốc")
        with st.expander("Xem nội dung bài đăng / tài liệu", expanded=True):
            st.text(redact(item["raw_text"]) if not reviewer else item["raw_text"])
        if item["facts"]:
            st.dataframe(pd.DataFrame([{"Trường": FIELDS[f["field"]], "Giá trị gốc": f["raw_value"],
                                        "Giá trị chuẩn": f["proposed_value"], "Nguồn #": f["source_id"]}
                                       for f in item["facts"] if reviewer or f["status"] == "approved"]),
                         hide_index=True, width="stretch")
        if item["source_extras"]:
            st.caption("Ghi chú của bài đăng, chưa quy cho cá nhân nào:")
            for extra in item["source_extras"]:
                st.write(f"**{extra['label']}:** {extra['raw_value'] if reviewer else redact(extra['raw_value'])}")
        if reviewer and item["contact"]:
            st.caption(f"Liên hệ riêng (chỉ người kiểm duyệt): {item['contact']}")
    if item["status"] == "approved":
        st.markdown("#### Trao đổi")
        if item["comments"]:
            for comment in item["comments"]:
                st.markdown(f"**{comment['username']}** · {comment['created_at'][:10]}  \n{redact(comment['text'])}")
        else:
            st.caption("Chưa có bình luận đã duyệt.")
        if session_user():
            with st.form(f"comment_{record_id}"):
                comment = st.text_area("Chia sẻ thêm thông tin / đặt câu hỏi", max_chars=4000)
                if st.form_submit_button("Gửi bình luận để kiểm duyệt"):
                    try:
                        add_comment(record_id, comment, session_user())
                        st.success("Đã gửi bình luận, chờ kiểm duyệt.")
                    except (ValueError, PermissionError) as exc:
                        st.error(str(exc))


def search_page() -> None:
    st.markdown('<div class="kicker">Kho tư liệu · Tra cứu</div>', unsafe_allow_html=True)
    st.title("Tìm kiếm thông tin liệt sĩ")
    st.caption("Kết quả chỉ gồm hồ sơ đã kiểm duyệt. Dữ liệu gốc và nguồn được hiển thị để đối chiếu.")
    s = stats()
    a, b, c = st.columns(3)
    a.metric("Hồ sơ có thể tìm", s["approved"])
    b.metric("Đang chờ xác minh", s["pending"])
    c.metric("Đã đối chiếu và gộp", s["merged"])
    with st.form("filters"):
        keyword = st.text_input("Tìm trong nội dung, lời kể, di vật", placeholder="Ví dụ: nơi an táng, vật nhận dạng...")
        col = st.columns(3)
        name = col[0].text_input("Họ tên")
        hometown = col[1].text_input("Quê quán")
        unit = col[2].text_input("Đơn vị")
        col2 = st.columns(4)
        birth = col2[0].text_input("Năm sinh")
        death = col2[1].text_input("Ngày hy sinh")
        source = col2[2].selectbox("Loại nguồn", ["Tất cả", "Nguồn chính thống đã xác minh", "Nguồn cộng đồng / chưa xác minh"])
        provider = col2[3].selectbox("Người cung cấp", ["Tất cả", "Cựu chiến binh", "Nhân chứng", "Thân nhân", "Người quản lý nghĩa trang", "Khác"])
        st.form_submit_button("Tìm kiếm", type="primary")
    source_type = {"Tất cả": "all", "Nguồn chính thống đã xác minh": "official",
                   "Nguồn cộng đồng / chưa xác minh": "community"}[source]
    offset = st.number_input("Trang kết quả", min_value=1, value=1, step=1) - 1
    try:
        results, total = search(keyword, name, hometown, unit, birth, death,
                                source_type, "" if provider == "Tất cả" else provider,
                                limit=20, offset=int(offset) * 20)
    except sqlite3.OperationalError as exc:
        st.error(f"Không thể tìm kiếm: {exc}")
        return
    st.markdown(f"**{total:,} kết quả** · Hiển thị tối đa 20 hồ sơ mỗi trang")
    if not results:
        st.info("Chưa có kết quả. Hãy thử bớt điều kiện hoặc nạp và duyệt dữ liệu.")
    for hit in results:
        values = json.loads(hit["fields_json"])
        with st.container(border=True):
            st.markdown(f"### {values.get('full_name') or 'Chưa rõ họ tên'} · #{hit['id']}")
            st.caption(" · ".join(v for v in [values.get("birth_year", ""), values.get("hometown", ""),
                                           values.get("military_unit", ""), hit["filename"]] if v))
            st.markdown(badge("Nguồn xác minh" if hit["verified_official"] else "Nguồn cộng đồng"))
            with st.expander("Xem hồ sơ và bản gốc"):
                record_view(hit["id"])


def submit_page() -> None:
    st.title("Cung cấp tư liệu")
    st.caption("Mỗi người được trích xuất từ bài đăng sẽ có một hồ sơ chờ duyệt riêng.")
    user = session_user()
    if not user:
        st.info("Đăng nhập hoặc tạo tài khoản ở thanh bên để gửi tư liệu.")
        return
    try:
        require(user, "contributor", "reviewer", "admin")
    except PermissionError as exc:
        st.error(str(exc))
        return
    tab_post, tab_file = st.tabs(["Bài đăng / lời kể", "Nhập tệp"])
    with tab_post:
        with st.form("post_form"):
            provider = st.selectbox("Bạn tham gia với vai trò", ["Thân nhân", "Cựu chiến binh", "Nhân chứng", "Người quản lý nghĩa trang", "Khác"])
            purpose = st.selectbox("Mục đích", ["Cung cấp thông tin", "Tìm kiếm", "Cung cấp và tìm kiếm"])
            contact = st.text_input("Thông tin liên hệ riêng (nếu muốn)", help="Chỉ người kiểm duyệt được xem trường liên hệ này.")
            raw_post = st.text_area("Nội dung bài đăng", height=220, placeholder="Dán bài đăng vào đây. Có thể nhắc tới nhiều liệt sĩ...")
            submitted = st.form_submit_button("Dùng LLM trích xuất và xem trước", type="primary")
        if submitted:
            st.session_state.pop("post_preview", None)
            if not os.getenv("OPENAI_API_KEY"):
                st.error("Chưa cấu hình OPENAI_API_KEY cho chức năng LLM. Xem README để thiết lập.")
            elif not (10 <= len(raw_post.strip()) <= 50000):
                st.error("Bài đăng cần từ 10 đến 50.000 ký tự.")
            else:
                try:
                    with st.spinner("Đang tách từng cá nhân và các trường thông tin..."):
                        parsed = extract_post_llm(raw_post)
                    st.session_state.post_preview = (raw_post, provider, purpose, contact, parsed)
                except Exception as exc:
                    st.error(f"Trích xuất LLM thất bại: {exc}")
        if "post_preview" in st.session_state:
            raw, provider, purpose, contact, parsed = st.session_state.post_preview
            st.markdown(f"#### Xem trước: {len(parsed.people)} cá nhân được đề xuất")
            with st.expander("Nội dung bài đăng sẽ lưu"):
                st.text(raw)
            if not parsed.people:
                st.warning("LLM chưa xác định được cá nhân riêng biệt. Bài đăng vẫn có thể lưu để người kiểm duyệt xem.")
            for index, person in enumerate(parsed.people, 1):
                with st.expander(f"Cá nhân {index}: {person.facts.full_name or person.facts.identifier or 'chưa rõ'}", expanded=True):
                    st.dataframe(pd.DataFrame([{"Trường": label, "Giá trị": person.facts.facts().get(field, "")}
                                               for field, label in FIELDS.items()]), hide_index=True, width="stretch")
                    st.write("Thông tin mở rộng:", [f"{x.label}: {x.value}" for x in person.extra_fields])
                    st.caption(f"Trích đoạn: {person.evidence_excerpt}")
            if parsed.post_extras:
                st.write("Chi tiết chưa quy cho cá nhân:", [f"{x.label}: {x.value}" for x in parsed.post_extras])
            st.warning("Đây là bản trích xuất tự động. Nội dung chỉ xuất hiện trong tìm kiếm sau khi người kiểm duyệt xác minh.")
            if st.button("Lưu bài đăng và các hồ sơ chờ duyệt", type="primary"):
                try:
                    source_id, ids = create_post(raw, parsed, user, provider, purpose, contact)
                    del st.session_state.post_preview
                    st.success(f"Đã lưu bài #{source_id} với {len(ids)} hồ sơ cá nhân chờ duyệt.")
                except (ValueError, PermissionError) as exc:
                    st.error(str(exc))
    with tab_file:
        st.caption(f"CSV và XLSX được đọc theo lô. Với dữ liệu lớn hơn {MAX_WEB_MB} MB, sử dụng lệnh nhập thư mục trong README.")
        file = st.file_uploader("Chọn CSV, XLSX, PDF, ảnh, TXT hoặc DOCX",
                                type=[s.lstrip(".") for s in sorted(SUPPORTED)],
                                max_upload_size=MAX_WEB_MB)
        claimed = st.selectbox("Loại nguồn khai báo", ["Cộng đồng", "Chính thống (cần xác minh tài liệu)"])
        provider = st.selectbox("Vai trò người cung cấp tệp", ["Thân nhân", "Cựu chiến binh", "Nhân chứng", "Người quản lý nghĩa trang", "Khác"], key="file_provider")
        mode = st.selectbox("Phương pháp trích xuất", ["LLM cho văn bản tự do; đọc trực tiếp cột có sẵn",
                                                        "LLM cho mọi dòng / trang", "Chỉ dùng cột và nhãn có sẵn"])
        if st.button("Nạp và đưa vào hàng chờ", disabled=file is None, type="primary"):
            mode_code = "rules" if mode.startswith("Chỉ") else "llm" if mode.startswith("LLM cho mọi") else "auto"
            if mode_code != "rules" and not os.getenv("OPENAI_API_KEY"):
                st.error("Chưa cấu hình OPENAI_API_KEY.")
            else:
                suffix = Path(file.name).suffix.lower()
                temp = DATA_DIR / "tmp"
                temp.mkdir(parents=True, exist_ok=True)
                path = None
                try:
                    with tempfile.NamedTemporaryFile(dir=temp, suffix=suffix, delete=False) as dst:
                        path = Path(dst.name)
                        file.seek(0)  # Re-clicking after a failed import must reread the file.
                        shutil.copyfileobj(file, dst, length=1024 * 1024)
                    info = import_file(path, original_name=Path(file.name).name,
                                       claimed_type="official" if claimed.startswith("Chính") else "community",
                                       provider_role=provider, user_id=user["id"],
                                       mode=mode_code, web_upload=True)
                    if info["duplicate_file"]:
                        if info.get("import_status") == "complete":
                            st.info(f"Tệp này đã nạp hoàn tất tại nguồn #{info['source_id']}. Không tạo bản ghi trùng.")
                        else:
                            st.warning(f"Nguồn #{info['source_id']} đã tồn tại nhưng chưa nạp hoàn tất "
                                       f"({info.get('stored_count', 0)} bản ghi đã lưu). Lần này không tạo thêm bản ghi.")
                    else:
                        st.success(f"Đã thêm {info['count']} mục chờ duyệt · nguồn #{info['source_id']}" +
                                   (" · Đã thử lại nguồn từng bị lỗi" if info.get("retried") else ""))
                except Exception as exc:
                    st.error(str(exc))
                finally:
                    if path:
                        path.unlink(missing_ok=True)


def review_page() -> None:
    st.title("Kiểm duyệt và đối sánh")
    user = session_user()
    try:
        require(user, "reviewer", "admin")
    except PermissionError:
        st.error("Trang này dành cho người kiểm duyệt.")
        return
    t_records, t_batches, t_posts, t_comments, t_pairs = st.tabs(
        ["Hồ sơ chờ duyệt", "Duyệt nguồn có cấu trúc", "Bài đăng & bổ sung hồ sơ", "Bình luận", "Đối sánh"])
    with t_records:
        queue = pending()
        if not queue:
            st.info("Không có hồ sơ chờ duyệt trong 50 mục đầu.")
        else:
            choice = st.selectbox("Chọn hồ sơ", queue, format_func=lambda r: f"#{r['id']} · {r['name'] or 'Chưa rõ tên'} · {r['filename']}")
            item = record(choice["id"])
            record_view(choice["id"], reviewer=True)
            suggestions = find_similar(choice["id"])
            if suggestions:
                st.info("Có thể liên quan: " + "; ".join(f"#{s['id']} {s['full_name']} ({s['score']:.0%})" for s in suggestions))
            with st.form(f"approve_{choice['id']}"):
                st.markdown("#### Sửa giá trị chuẩn hóa trước khi duyệt")
                overrides = {}
                for f in item["facts"]:
                    overrides[str(f["id"])] = st.text_area(
                        f"{FIELDS[f['field']]} · gốc: {f['raw_value'][:120]}",
                        value=f["proposed_value"], key=f"fact_{f['id']}", height=80)
                    if f["suggestion"]:
                        st.caption(f"Gợi ý chưa áp dụng: {f['suggestion']}")
                extra_edits = {}
                for extra in item["extras"]:
                    extra_edits[str(extra["id"])] = st.text_input(
                        f"Mở rộng · {extra['label']}", value=extra["raw_value"], key=f"extra_{extra['id']}")
                verified = st.checkbox("Đã kiểm tra tài liệu và xác nhận đây là nguồn chính thống",
                                       disabled=item["claimed_type"] != "official")
                accept, reject = st.columns(2)
                accepted = accept.form_submit_button("Duyệt và đưa vào tìm kiếm", type="primary")
                rejected = reject.form_submit_button("Từ chối hồ sơ")
                if accepted or rejected:
                    try:
                        approve_record(choice["id"], user, overrides, extra_edits, verified, reject=rejected)
                        st.success("Đã cập nhật hàng chờ.")
                        st.rerun()
                    except (ValueError, PermissionError) as exc:
                        st.error(str(exc))
    with t_batches:
        st.caption("Chỉ áp dụng cho tệp nhập ở chế độ cột/nhãn rõ ràng (`rules`) đã nạp hết; dữ liệu LLM cần xem từng hồ sơ.")
        sources = batch_sources()
        if not sources:
            st.info("Không có nguồn phù hợp để duyệt lô.")
        else:
            source = st.selectbox("Nguồn chờ duyệt", sources,
                                  format_func=lambda s: f"#{s['id']} · {s['filename']} · {s['remaining']:,} hồ sơ")
            st.markdown("**Một số hồ sơ đầu để kiểm tra đối chiếu:**")
            for sample_id in batch_sample(source["id"]):
                with st.expander(f"Hồ sơ #{sample_id}"):
                    record_view(sample_id, reviewer=True)
            with st.form("bulk_approval"):
                reviewed = st.checkbox("Tôi đã kiểm tra mẫu và đối chiếu với tệp nguồn")
                verified = st.checkbox("Tôi đã xác minh đây là nguồn chính thống",
                                       disabled=source["claimed_type"] != "official")
                if st.form_submit_button("Duyệt tối đa 2.500 hồ sơ tiếp theo", type="primary"):
                    try:
                        count = approve_source_batch(source["id"], user,
                                                     reviewed_sample=reviewed, verify_official=verified)
                        st.success(f"Đã duyệt {count:,} hồ sơ. Lặp lại để duyệt phần còn lại.")
                    except (ValueError, PermissionError) as exc:
                        st.error(str(exc))
    with t_posts:
        posts = post_sources()
        if not posts:
            st.info("Chưa có bài đăng.")
        for post in posts:
            with st.expander(f"Bài #{post['id']} · {post['provider_role']} · {post['record_count']} hồ sơ · {post['created_at'][:10]}"):
                st.text(post["raw_text"])
                with st.form(f"assign_{post['id']}"):
                    name = st.text_input("Họ tên được nhắc rõ trong bài")
                    identifier = st.text_input("Mã phân biệt nếu có")
                    if st.form_submit_button("Tạo một hồ sơ chờ duyệt"):
                        try:
                            rid = attach_person(post["id"], {"full_name": name, "identifier": identifier}, user)
                            st.success(f"Đã tạo hồ sơ #{rid}. Có thể tiếp tục thêm người từ cùng bài.")
                            st.rerun()
                        except (ValueError, PermissionError) as exc:
                            st.error(str(exc))
    with t_comments:
        comments = comment_queue()
        if not comments:
            st.info("Chưa có bình luận chờ duyệt.")
        for comment in comments:
            with st.container(border=True):
                st.write(f"Hồ sơ #{comment['record_id']} · {comment['username']}: {comment['text']}")
                left, right = st.columns(2)
                for accept, pressed in ((True, left.button("Duyệt", key=f"yes_{comment['id']}")),
                                        (False, right.button("Từ chối", key=f"no_{comment['id']}"))):
                    if pressed:
                        review_comment(comment["id"], accept, user)
                        st.rerun()
    with t_pairs:
        st.caption("Đối sánh chỉ gợi ý. Cần ghi nhận quyết định của người kiểm duyệt trước khi gộp.")
        rid = st.number_input("ID hồ sơ cần đối sánh", min_value=1, step=1)
        source_record = record(int(rid))
        if source_record:
            matches = find_similar(int(rid))
            if not matches:
                st.info("Không có ứng viên vượt ngưỡng lọc trong các trường hiện có.")
            for match in matches:
                st.write(f"#{match['id']} · {match['full_name']} · điểm {match['score']:.0%} · {match['evidence']} trường tương đồng")
        with st.form("pair_decision"):
            other = st.number_input("ID hồ sơ thứ hai", min_value=1, step=1)
            label = st.selectbox("Kết luận", ["Trùng nhau", "Có liên quan", "Khác nhau"])
            choice = st.radio("Thao tác", ["Lưu kết luận", "Lưu kết luận và gộp vào hồ sơ thứ nhất"], horizontal=True)
            if st.form_submit_button("Xác nhận"):
                try:
                    code = {"Trùng nhau": "same", "Có liên quan": "related", "Khác nhau": "different"}[label]
                    if choice.startswith("Lưu kết luận và") and code != "same":
                        raise ValueError("Chỉ gộp sau khi kết luận trùng nhau")
                    label_pair(int(rid), int(other), code, user)
                    if choice.startswith("Lưu kết luận và"):
                        merge_records(int(rid), int(other), user)
                    st.success("Đã lưu kết luận" + (" và gộp hồ sơ" if choice.startswith("Lưu kết luận và") else ""))
                except (ValueError, PermissionError, sqlite3.IntegrityError) as exc:
                    st.error(str(exc))


def evaluation_page() -> None:
    st.title("Đánh giá chất lượng")
    try:
        require(session_user(), "reviewer", "admin")
    except PermissionError:
        st.error("Trang này dành cho người kiểm duyệt.")
        return
    st.caption("Tải bộ nhãn vàng CSV để tính điểm thật trên tập đã gán nhãn, không phải số liệu mẫu.")
    kind = st.selectbox("Phép đo", ["Trích xuất trường · Precision / Recall / F1", "Chuẩn hóa · Accuracy",
                                    "Phát hiện trùng · Precision / Recall / F1", "Gợi ý tương đồng · Precision@K / Recall@K / Top-K Accuracy"])
    method = st.radio("Cách trích xuất", ["LLM (nhiều người trong một bài)", "Quy tắc (đường cơ sở)"], horizontal=True) if kind.startswith("Trích xuất") else ""
    schemas = {
        "Trích xuất": ("raw_text,expected_people_json; mỗi ô JSON là danh sách các cá nhân và các trường đúng. "
                       "Ví dụ được cung cấp trong samples/.") if method.startswith("LLM") else
                       "raw_text,full_name,birth_year,... (một văn bản và các trường đúng theo cột)",
        "Chuẩn hóa": "field,raw_value,expected",
        "Phát hiện": "record_a,record_b,is_duplicate (0 hoặc 1; ID hồ sơ đã duyệt)",
        "Gợi ý": "query_id,relevant_ids (các ID đúng, cách nhau bằng dấu |)",
    }
    st.info(next(value for prefix, value in schemas.items() if kind.startswith(prefix)))
    gold = st.file_uploader("CSV nhãn vàng UTF-8", type=["csv"], key="evaluation_gold")
    k = st.number_input("K", 1, 50, 5) if kind.startswith("Gợi ý") else 5
    if st.button("Chạy đánh giá", disabled=gold is None, type="primary"):
        try:
            dataset = pd.read_csv(gold, dtype=str, keep_default_na=False).to_dict("records")
            if kind.startswith("Trích xuất"):
                if not dataset or "raw_text" not in dataset[0]:
                    raise ValueError("Thiếu cột raw_text")
                if method.startswith("LLM"):
                    if not os.getenv("OPENAI_API_KEY") or "expected_people_json" not in dataset[0]:
                        raise ValueError("Cần OPENAI_API_KEY và cột expected_people_json")
                    result = evaluate_llm_extraction(dataset)
                else:
                    result = evaluate_extraction(dataset)
            elif kind.startswith("Chuẩn hóa"):
                result = evaluate_normalization(dataset)
            elif kind.startswith("Phát hiện"):
                result = evaluate_pairs(dataset)
            else:
                result = evaluate_ranking(dataset, int(k))
            st.json(result)
        except (ValueError, KeyError, sqlite3.Error) as exc:
            st.error(f"Bộ nhãn không hợp lệ: {exc}")


def admin_page() -> None:
    st.title("Quản trị tài khoản")
    try:
        require(session_user(), "admin")
    except PermissionError:
        st.error("Trang này dành cho quản trị viên.")
        return
    for person in users():
        with st.container(border=True):
            st.write(f"#{person['id']} · **{person['username']}** · {person['role']}" + (" · Đã khóa" if person["locked"] else ""))
            if person["id"] == session_user()["id"]:
                continue
            with st.form(f"role_{person['id']}"):
                role = st.selectbox("Quyền", ["contributor", "reviewer", "admin"],
                                    index=["contributor", "reviewer", "admin"].index(person["role"]))
                locked = st.checkbox("Khóa tài khoản", value=bool(person["locked"]))
                if st.form_submit_button("Lưu quyền"):
                    set_user_role(session_user(), person["id"], role, locked)
                    st.rerun()


st.sidebar.markdown("## 🕯️ Kho tư liệu")
st.sidebar.caption("Truy vết nguồn · Kiểm duyệt · Đối sánh")
auth_sidebar()
nav = ["Tra cứu", "Cung cấp tư liệu"]
if session_user() and session_user()["role"] in ("reviewer", "admin"):
    nav += ["Kiểm duyệt", "Đánh giá"]
if session_user() and session_user()["role"] == "admin":
    nav += ["Quản trị"]
page = st.sidebar.radio("Đi tới", nav)
{"Tra cứu": search_page, "Cung cấp tư liệu": submit_page, "Kiểm duyệt": review_page,
 "Đánh giá": evaluation_page, "Quản trị": admin_page}[page]()
st.sidebar.caption("Phân tích tự động hỗ trợ tra cứu; xác minh danh tính và ADN thuộc cơ quan có thẩm quyền.")
