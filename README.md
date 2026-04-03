# 🤖 AutoBing - Bot Tự Động Giải Quyết Microsoft Rewards

AutoBing là một phần mềm giúp bạn tự động tìm kiếm, làm khảo sát và gom điểm trên Microsoft Rewards một cách tự động, giúp tiết kiệm thời gian. Phần mềm được thiết kế vô cùng đơn giản với giao diện điều khiển bằng Web, người không am hiểu lập trình (no code) vẫn có thể dùng dễ dàng.

---

## 📥 1. Hướng Dẫn Cài Đặt (Chỉ làm duy nhất 1 lần)

Bạn chỉ cần máy tính chạy bằng hệ điều hành Windows, mọi thứ cấu hình rườm rà bot sẽ tự làm giúp bạn.

1. Tải công cụ AutoBing về máy và giải nén ra một thư mục.
2. Mở thư mục vừa giải nén, tìm và **nhấp đúp chuột vào file `setup.bat`** (Có biểu tượng bánh răng).
3. Một cửa sổ màu đen sẽ hiển thị. Việc của bạn lúc này là Đợi! Nó sẽ tự động kiểm tra, tải Python và các trình duyệt thu nhỏ cần thiết. Quá trình có thể tốn từ 2-5 phút tùy tốc độ mạng.
4. Khi quá trình hoàn tất, màn hình sẽ báo chữ **"Setup Complete!"** và yêu cầu bạn bấm phím bất kỳ để tắt. Khâu cài đặt đã xong!

---

## 🚀 2. Cách Khởi Động Bot Hằng Ngày

Khi muốn treo máy cày điểm, bạn thực hiện qua 2 bước dướn đây:

1. **Nhấp đúp chuột vào file `start_web.bat`**. 
   *(Lúc này một cửa sổ đen sẽ hiện ra, hãy **thu nhỏ** nó xuống chứ đừng bấm tắt chéo (X) nhé, tắt nó đi là bot sẽ dừng).*
   
2. Bot sẽ tự động bật trình duyệt web máy tính lên và truy cập vào Bảng Điều Khiển ở địa chỉ: http://localhost:23900
   
3. **Tại Bảng Điều Khiển này, bạn có thể:**
   - **Thêm tài khoản**: Điền Email và Mật khẩu của tài khoản Microsoft Rewards.
   - **Bắt đầu**: Bấm nút Play/Start trên giao diện để bot tự động mở các cửa sổ web ảo lên và tự động thu thập điểm số.
   - **Theo dõi**: Bạn có thể xem hôm nay bot cày được bao nhiêu điểm ở mục thống kê.
   - **Cài đặt tốc độ**: Khuyến khích chỉnh thời gian nghỉ giữa các lần search từ 3 - 8 giây để Microsoft không phát hiện là bot.

---

## ⚙️ 3. Sử Dụng Cùng GPM Login (Nâng Cao - Tùy chọn dành cho cày nhiều tài khoản)

Nếu bạn dùng phần mềm nuôi nick **GPM Login** để chống bị khoá acc, AutoBing đã tích hợp sẵn tính năng kết nối thẳng với GPM!
- Mở app GPM Login > Cài đặt chung > **Bật GPM API** (địa chỉ mặc định thường là `http://127.0.0.1:9495`).
- Mở bảng điều khiển AutoBing (http://localhost:23900) > Vào tab **Cài đặt (Settings)** > Bật tính năng tích hợp GPM Login và kiểm tra xem dòng GPM API URL có khớp ở trên chưa.
- Mở tab **Tài khoản** > Thêm ID Profile của GPM (chuỗi mã nằm trong GPM Login) vào kế bên Email tương ứng.
- **Xong!** Lúc này bot sẽ tự động bật đúng cái cửa sổ hồ sơ chứa IP của riêng tài khoản đó lền và bắt đầu làm nhiệm vụ.

---

## 🛡️ 4. Lời Khuyên An Toàn Chống Khóa (Ban) Tài Khoản

* Thay vì cài đặt Bot chạy với tốc độ chóng mặt (ví dụ chưa tới nửa giây đã click), hãy để mặc định thời gian giãn cách (Delay) giống như một người thật đang tự tay gõ máy đọc báo.
* Ở những lần khởi động lấy điểm đầu tiên của tài khoản mới, Microsoft có thể đòi bạn nhập mã OTP gửi về Email, hoặc đòi giải một mã Captcha bằng hình ảnh. Nhiệm vụ của bạn chỉ là giải tay lần đầu đó. Khi đã "quen mặt", bot sẽ chạy tự động về sau.
* Đừng lợi dụng bot cày quá nhiều tài khoản trên cùng 1 mạng (IP) duy nhất. Nếu cày công nghiệp, bạn cần phải có proxy hoặc GPM profile cho từng nick (Acc).
