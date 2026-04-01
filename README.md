# Rewards Bot

Tự động hóa Microsoft Rewards — **Python + Playwright**.

> **Chưa có tài khoản Microsoft Rewards?** Đăng ký tại đây:
> https://rewards.bing.com/welcome?rh=CE9698B&ref=rafsrchae

## Cài đặt

### Windows
```
1. Cài Python 3.10+ từ python.org (tick "Add to PATH")
2. Double-click setup.bat
3. Xong!
```

### Thủ công
```bash
pip install -r requirements.txt
playwright install chromium
```

## Cách chạy

| File | Mô tả |
|------|-------|
| `start_web.bat` | Mở Web Dashboard tại `localhost:23900` |
| `start_cmd.bat` | Chạy trực tiếp trong CMD (menu CLI) |

### Command line
| Lệnh | Mô tả |
|-------|-------|
| `python main.py` | Web Dashboard (mặc định) |
| `python main.py --cli` | Menu CLI tương tác |
| `python main.py --auto` | Chạy tự động tất cả (dùng cho lập lịch) |

### Lần đầu
1. Chạy `start_web.bat`
2. Trình duyệt tự mở `http://localhost:23900`
3. Vào **Cài đặt** → **Thêm tài khoản** → thêm tài khoản Microsoft
4. Click **Chạy tất cả** để bắt đầu!

## Tính năng

| Nhóm | Chi tiết |
|------|----------|
| **Tìm kiếm** | Desktop, Mobile (CDP emulation), Edge — tự tính từ API |
| **Nhiệm vụ** | Daily Set, Punch Cards, Promotions, Quizzes (6 loại) |
| **Edge Streak** | Duyệt Edge 30 phút/ngày — native Win32, không chiếm chuột |
| **Stealth** | playwright-stealth, fingerprint spoofing, Edge UA |
| **Thông minh** | Google Trends queries, retry tự động, credit probe |
| **Điểm** | CSV logging, auto-redeem, bảo vệ streak |
| **Thông báo** | Discord webhook + Telegram bot |
| **Lập lịch** | Windows Task Scheduler |
| **Dashboard** | Web UI tại `localhost:23900` |
| **AI** | OpenRouter AI fallback cho task phức tạp |

## Cấu trúc

```
autobing/
├── main.py                 # Điểm khởi chạy
├── start_web.bat           # Chạy Web Dashboard
├── start_cmd.bat           # Chạy CLI trong CMD
├── setup.bat               # Cài đặt tự động
├── requirements.txt        # Thư viện cần thiết
├── config/
│   ├── accounts.json.enc   # Tài khoản (mã hoá, tự tạo)
│   ├── settings.json       # Cài đặt (tự tạo)
│   └── search_topics.txt   # Từ khoá tìm kiếm fallback
├── dashboard/
│   ├── index.html          # Giao diện web dashboard
│   └── style.css           # Kiểu dáng dashboard
├── src/                    # Mã nguồn chính
└── data/                   # Dữ liệu runtime (tự tạo)
```

## Cài đặt chi tiết

Tất cả cài đặt thông qua Web Dashboard. Các tuỳ chọn chính:

| Cài đặt | Mặc định | Ghi chú |
|----------|----------|---------|
| Chạy ẩn (Headless) | `false` | Ẩn cửa sổ trình duyệt |
| Stealth | `true` | Chống phát hiện |
| Google Trends | `true` | Dùng từ khoá trending thật |
| Chặn hình ảnh | `true` | Tải trang nhanh hơn |
| Bảo vệ streak | `true` | Giám sát chuỗi ngày |
| AI Agent | `true` | OpenRouter fallback |
| Tự đổi thưởng | `false` | Tự động đổi điểm |

## Cảnh báo

Dự án này chỉ dùng cho mục đích **học tập**. Tự động hoá Microsoft Rewards
có thể vi phạm Điều khoản Dịch vụ và có thể bị khoá tài khoản.
Sử dụng tự chịu rủi ro.

## Liên kết giới thiệu

Nếu bạn thấy tool này hữu ích, hãy đăng ký Microsoft Rewards qua link:

https://rewards.bing.com/welcome?rh=CE9698B&ref=rafsrchae
