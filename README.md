# Giám Sát Bài Nợ 24/24 - Bản Độc Lập

Hệ thống giám sát bài nợ Facebook tự động 24/24, quét view Reels Facebook, tự động kiểm tra trạng thái bài đăng, tự động cảnh báo 0 comment và gửi thông báo qua Telegram / Web UI.

## 🚀 Tính năng chính
- **Giám sát độc lập 24/24**: Tự động quét và theo dõi lượt xem, tương tác của các bài viết và Reels theo danh sách cấu hình.
- **Tích hợp Python Runtime Portable**: Đã đóng gói sẵn môi trường Python 3.12 đầy đủ thư viện cần thiết, sẵn sàng khởi chạy trên mọi máy tính Windows mà không cần cài đặt thêm.
- **Tự động kích hoạt Playwright**: Tự động kiểm tra và tải trình duyệt Chromium offline khi khởi chạy lần đầu.
- **Giao diện Web UI trực quan**: Quản lý cấu hình, xem trạng thái bài nợ theo thời gian thực tại `http://127.0.0.1:5005`.

## 🛠 Hướng dẫn sử dụng
1. **Khởi chạy ứng dụng**:
   - Nhấp đúp vào file `Chay_Giam_Sat.bat` hoặc `GiamSatBaiNo.exe`.
   - Lần đầu chạy nếu chưa có trình duyệt Chromium, script sẽ tự động tải Chromium về thư mục `ms-playwright`.
2. **Mở giao diện Web**:
   - Truy cập vào đường dẫn: `http://127.0.0.1:5005`
3. **Cấu hình dữ liệu**:
   - Tinh chỉnh file `config.json` (API Key, Token, thời gian quét).
   - Thêm danh sách proxy vào file `proxies.txt` (nếu có sử dụng).

---
*Phát triển và đóng gói bởi đội ngũ Traffic Master.*
