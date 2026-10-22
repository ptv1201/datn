# Kho tư liệu hỗ trợ tìm kiếm liệt sĩ · MVP

Ứng dụng Streamlit/Python này hiện thực hóa các luồng trong **“Phân tích nghiệp vụ_Phạm Thường Vi.pdf”**. Dữ liệu mẫu trong `samples/` hoàn toàn hư cấu; không biểu thị một liệt sĩ hay ngôi mộ có thật.

## Nghiệp vụ và dữ liệu

| Nhu cầu | Cách hoạt động |
| --- | --- |
| Bài đăng chứa nhiều người | LLM trả về `people[]`. Mỗi người có một hồ sơ và tập dữ kiện riêng, dùng chung nguồn bài đăng. |
| Thiếu thông tin | Trường vắng mặt là `NULL` lúc trích xuất và để trống khi duyệt; không suy đoán. |
| Nội dung ngoài mẫu | `extra_fields` gắn đúng cá nhân; `post_extras` giữ chi tiết chưa thể quy cho người nào; luôn lưu bản gốc. |
| Nhiều định dạng | Văn bản nhập trực tiếp; CSV, XLSX, TXT, DOCX, PDF có lớp chữ; PDF scan và ảnh qua OCR trước khi trích xuất. |
| Nguyên bản và nguồn | `sources` lưu tệp/bài đăng gốc, người cung cấp, mục đích, liên hệ; `facts` lưu giá trị gốc, đề xuất chuẩn hóa, nguồn, người duyệt. |
| Chuẩn hóa | Ngày, khoảng trắng, tên đơn vị/địa danh theo từ điển; so khớp mờ chỉ **gợi ý**, không tự áp dụng. |
| Tìm kiếm và kiểm duyệt | Chỉ hồ sơ đã duyệt xuất hiện trong tìm kiếm FTS5; phân biệt nguồn chính thống **đã xác minh** với nguồn cộng đồng/chưa xác minh. |
| Đối sánh | Lấy ứng viên bằng các khóa có chỉ mục, so độ tương đồng, người kiểm duyệt gắn nhãn trùng/liên quan/khác; chỉ gộp sau khi xác nhận trùng. |
| Trao đổi | Người dùng gửi bình luận, người kiểm duyệt duyệt rồi nội dung mới được tìm kiếm. |
| Đánh giá | Bộ nhãn vàng riêng cho trích xuất LLM/quy tắc, chuẩn hóa, phát hiện trùng, gợi ý Top-K. Không có điểm số giả. |

Luồng bài đăng: **nội dung gốc → LLM với Pydantic Structured Output → nhiều cá nhân + thông tin mở rộng → chờ duyệt → hồ sơ tra cứu**. Câu chuyện hoặc thông tin không rõ gắn với người nào giữ tại nguồn. Một bài không xác định được cá nhân vẫn lưu và xuất hiện trong mục “Bài đăng & bổ sung hồ sơ”; người kiểm duyệt có thể thêm từng cá nhân từ cùng một bài.

## Chạy trên máy cá nhân

Yêu cầu Python 3.12+, Tesseract OCR với `vie` và `eng` (cho ảnh/PDF scan). Nếu chỉ thử text, CSV/XLSX hoặc PDF có chữ, chưa cần Tesseract. Cài Tesseract và gói ngôn ngữ tiếng Việt bằng trình quản lý gói của hệ điều hành; kiểm tra `tesseract --list-langs` có `vie` và `eng`.

### Windows Command Prompt (CMD)

Mở CMD và thay đường dẫn ví dụ bằng thư mục dự án thực tế:

```cmd
cd /d "C:\duong-dan\martyr_records"
python -m venv .venv
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt
set "OPENAI_API_KEY=API_KEY_CUA_BAN"
```

Chỉ tạo tài khoản quản trị **một lần**, nếu chưa có:

```cmd
python manage.py init-admin admin
```

Khởi động ứng dụng trong cùng cửa sổ CMD:

```cmd
python -m streamlit run app.py
```

`set` chỉ có hiệu lực trong cửa sổ CMD hiện tại; sau khi mở cửa sổ mới, kích hoạt lại môi trường và đặt lại khóa. Không đưa API key thật vào mã nguồn. Khi chỉ cập nhật bản sửa, không cần tạo lại tài khoản quản trị hoặc xóa cơ sở dữ liệu.

### Nếu dùng PowerShell

```powershell
cd "C:\duong-dan\martyr_records"
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:OPENAI_API_KEY="<API key riêng của bạn>"
python -m streamlit run app.py
```

### Nếu dùng Linux/macOS

```bash
cd martyr_records
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export OPENAI_API_KEY="<API key riêng của bạn>"
python -m streamlit run app.py
```

Có thể đổi mô hình bằng `OPENAI_MODEL`, mặc định `gpt-4.1-mini`.

Mở **http://localhost:8501** trên máy đang chạy. Không có tài khoản/mật khẩu mặc định; bước `init-admin` yêu cầu nhập mật khẩu kín. Tài khoản đăng ký trên giao diện bắt đầu là `contributor`; admin cấp quyền `reviewer`. Thư mục `runtime/` lưu SQLite và tệp gốc; không xóa khi muốn giữ dữ liệu.

### Docker, chạy riêng trên localhost

```cmd
copy .env.example .env
notepad .env
docker compose up --build -d
docker compose exec app python manage.py init-admin admin
```

Điền khóa vào `.env`, lưu và đóng trình soạn thảo trước khi chạy `docker compose`. Trên Linux/macOS, dùng `cp .env.example .env` để sao chép tệp.

Sau đó mở **http://127.0.0.1:8501**. Dockerfile cài Tesseract và gói tiếng Việt. `compose.yaml` chỉ mở cổng trên địa chỉ loopback; để truy cập từ máy khác cần hạ tầng và thiết lập xác thực, TLS, sao lưu, kiểm soát quyền và xử lý dữ liệu cá nhân phù hợp. Không đăng công khai kho tài liệu/bài đăng thật từ bản MVP này.

## Nhập dữ liệu lớn

Giao diện chấp nhận từng tệp **tối đa 500 MB**. Đối với các bộ nhiều tệp hoặc danh sách cỡ hàng trăm nghìn hàng, chạy trực tiếp trên máy chủ:

```cmd
python manage.py import "C:\DuLieu\danh_sach" --mode auto --type community
python manage.py import "C:\DuLieu\tai_lieu.pdf" --mode llm --type official
python manage.py import "C:\DuLieu\bang_lon.csv" --mode rules
```

- `auto`: CSV/XLSX có cột họ tên/mã... được ánh xạ trực tiếp, **ô văn bản tự do và các tệp văn bản/PDF/ảnh** được LLM phân tích. `llm`: gọi LLM cho mọi dòng/trang, phù hợp dữ liệu ít cấu trúc nhưng có thể chậm và phát sinh chi phí lớn. `rules`: chỉ dùng tên cột và nhãn rõ ràng, phù hợp danh sách lớn cần nạp trước rồi đánh giá thủ công.
- CSV dùng Pandas `chunksize=250`, nhận diện dấu phân cách phẩy/chấm phẩy/tab/`|`; XLSX dùng `openpyxl` chế độ `read_only`. Mỗi lô ghi SQLite trong một giao dịch. Tệp đã nạp hoàn tất được nhận diện bằng SHA-256 và không nạp trùng. Nguồn lỗi `partial` có **0 bản ghi đã lưu** được cùng người gửi thử lại từ đầu; nguồn đã lưu một phần dữ liệu không tự khởi động lại, tránh tạo bản ghi trùng.
- PDF được xử lý từng trang; trang không có lớp chữ và ảnh được OCR. Tài liệu scan muốn nhận dạng tiếng Việt cần `vie.traineddata`. Kiểm tra kỹ các ký tự OCR trước khi duyệt.
- SQLite bật WAL, khóa truy vấn và FTS5. Đây là bản thử nghiệm cho một máy chủ với số người ghi đồng thời thấp. Chưa có cam kết thông lượng ở 600.000 hồ sơ; nên đo trên tập dữ liệu thật rồi cân nhắc PostgreSQL, hàng đợi tác vụ và lưu trữ tệp riêng khi lên sản phẩm.
- Nguồn nạp ở `--mode rules` có thể được người kiểm duyệt xem mẫu rồi duyệt theo nguồn: tối đa 2.500 mục mỗi lần trên giao diện. Với nguồn rất lớn, xem mẫu trên giao diện rồi dùng `python manage.py approve-source ID --reviewer TAI_KHOAN --reviewed-sample`. Chỉ thêm `--verify-official` khi đã xác minh loại nguồn. Các nguồn LLM không có nút duyệt lô.

Các cột được nhận diện gồm họ tên, mã, năm sinh, quê quán, đơn vị, cấp bậc, ngày nhập ngũ, ngày/nơi/lý do hy sinh, nơi an táng, vị trí mộ, di vật, lời kể. Cột không nhận diện sẽ vào “Thông tin mở rộng”; dữ liệu gốc không bị bỏ. Từ điển ví dụ trong `data/` chỉ để minh họa, cần thẩm định tên địa danh lịch sử theo mốc thời gian trước khi dùng thật.

### Kiểm tra giới hạn 500 MB

- Đặt tệp cấu hình tại `martyr_records\.streamlit\config.toml`, với `[server]` và `maxUploadSize = 500`.
- `ingest.py` đặt `MAX_WEB_MB = 500` và `MAX_WEB_BYTES = 500 * 1024 * 1024` để kiểm tra kích thước phía xử lý.
- Ô tải lên trong `app.py` lấy `max_upload_size` từ cùng hằng `MAX_WEB_MB`.
- Khởi động lại Streamlit sau khi thay cấu hình. Streamlit nhận tệp tải lên trong RAM; việc đọc CSV/XLSX theo lô chỉ giới hạn phần xử lý bảng, không làm biến mất bộ nhớ giữ tệp đã tải. Nếu RAM không đủ, dùng lệnh nhập tệp từ ổ đĩa.

### Lỗi `post_extras.5.value` khi nhập CSV/Excel

Lỗi này có nghĩa LLM trả về phần tử thứ 6 trong danh sách mở rộng với giá trị rỗng. `core.py` nay loại bỏ mục mở rộng có tên hoặc giá trị trống **trước khi** Pydantic kiểm tra từng phần tử; mục có dữ liệu vẫn giữ nguyên. Các lỗi sai kiểu dữ liệu khác vẫn được báo.

Ở chế độ `auto`, LLM được gọi nếu có cột văn bản tự do hoặc không ánh xạ được trường nào có dữ liệu trong dòng. Một CSV dùng dấu `;` nhưng được đọc như dấu `,` cũng có thể khiến tiêu đề cột không được nhận diện; bản sửa đã thêm nhận diện dấu phân cách. Muốn xác định chính xác vì sao một tệp cụ thể gọi LLM, cần xem hàng tiêu đề và một vài dòng mẫu của tệp đó.

Sau khi thay `core.py`, `ingest.py`, `app.py` và khởi động lại, đăng nhập cùng tài khoản đã nạp tệp rồi thử lại. Trường hợp báo `0 mục` sẽ dùng lại nguồn cũ và không yêu cầu xóa `records.sqlite3`. Nếu nguồn đã lưu trên 0 bản ghi, ứng dụng báo chưa hoàn tất và không tự nạp lại từ đầu.

## Đánh giá

Vào trang **Đánh giá** với tài khoản kiểm duyệt, tải CSV nhãn vàng UTF-8:

| Phép đo | Cột đầu vào | Ý nghĩa |
| --- | --- | --- |
| Trích xuất LLM | `raw_text,expected_people_json` | JSON là mảng cá nhân và các giá trị đúng; đo micro Precision, Recall, F1 theo bộ ba người/trường/giá trị. Mỗi dòng có thể chứa nhiều người. |
| Trích xuất quy tắc | `raw_text` và các cột mã trường như `full_name` | Đường cơ sở chỉ đo nhãn có cấu trúc. |
| Chuẩn hóa | `field,raw_value,expected` | Tỷ lệ khớp chính xác Accuracy. |
| Trùng lặp | `record_a,record_b,is_duplicate` | Cặp ID đã duyệt có nhãn 0/1; tính Precision, Recall, F1 cho ứng viên và ngưỡng 0,78. |
| Top-K | `query_id,relevant_ids` | ID liên quan cách nhau `|`; tính Precision@K, Recall@K và Top-K Accuracy. |

`samples/` có mẫu CSV để điền nhãn. Đánh giá LLM thực sự gọi API cho mỗi hàng nhãn vàng. Tập nhãn cần bao gồm cặp khó và trường hợp thiếu thông tin; chỉ đánh giá trên các cặp được cung cấp, không thể suy ra Recall cho toàn kho từ một tập nhãn không đầy đủ.

## Giới hạn của bản thử nghiệm

- Không xác minh danh tính liệt sĩ hoặc quan hệ ADN; những kết quả tương đồng chỉ là gợi ý cho người có thẩm quyền.
- LLM/OCR có thể nhầm người, ngày âm/dương, nơi chôn cất. Mọi đầu ra là **đề xuất chờ duyệt**, giữ bản gốc cạnh giá trị đã sửa; phần còn lại do mô hình trích xuất sẽ vào mở rộng, và toàn văn luôn còn để đối chiếu nếu mô hình bỏ sót.
- Ảnh và PDF scan có chất lượng kém làm giảm độ chính xác. PDF nhiều trang được tách theo trang; một câu chuyện kéo dài qua hai trang cần người kiểm duyệt đối chiếu tài liệu gốc.
- MVP dùng đăng nhập cơ bản và lưu tệp riêng trên máy chủ; cần gia cố bảo mật, phân quyền chi tiết, nhật ký kiểm toán, chính sách dữ liệu và kiểm thử tải trước khi triển khai với dữ liệu thật hoặc nhiều người dùng.

Kiểm tra tự động: `python -m unittest discover -s tests -v`.
