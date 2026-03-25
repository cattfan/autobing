# 🏆 Rewards Bot

Automated Microsoft Rewards farming bot — **Python + Playwright**.

## ⚡ Cài đặt

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

## 🚀 Cách chạy

| File | Mô tả |
|------|-------|
| `start_web.bat` | Mở Web Dashboard tại `localhost:8080` |
| `start_cmd.bat` | Chạy trực tiếp trong CMD (menu CLI) |

### Command line
| Lệnh | Mô tả |
|-------|-------|
| `python main.py` | Web Dashboard (mặc định) |
| `python main.py --cli` | Menu CLI tương tác |
| `python main.py --auto` | Chạy tự động tất cả (dùng cho lập lịch) |

### Lần đầu
1. Chạy `start_web.bat`
2. Browser tự mở `http://localhost:8080`
3. Click ⚙️ **Cài đặt** → **Thêm tài khoản** → thêm tài khoản Microsoft
4. Click **Chạy tất cả** để bắt đầu!

## ✨ Tính năng

| Nhóm | Chi tiết |
|------|----------|
| 🔎 **Tìm kiếm** | Desktop, Mobile (CDP emulation), Edge — tự tính từ API |
| 🎯 **Nhiệm vụ** | Daily Set, Punch Cards, Promotions, Quizzes (6 loại) |
| 🌐 **Edge Streak** | Duyệt Edge 30 phút/ngày — native Win32 (SendInput) |
| 🛡️ **Stealth** | playwright-stealth, fingerprint spoofing, Edge UA |
| 🧠 **Thông minh** | Google Trends queries, retry tự động, credit probe |
| 💰 **Điểm** | CSV logging, auto-redeem, bảo vệ streak |
| 📩 **Thông báo** | Discord webhook + Telegram bot |
| ⏰ **Lập lịch** | Windows Task Scheduler |
| 🌐 **Dashboard** | Web UI tại `localhost:8080` |
| 🤖 **AI** | OpenRouter AI fallback cho task phức tạp |

## 📂 Cấu trúc

```
autofarmbing/
├── main.py                 # Entry point
├── start_web.bat           # Chạy Web Dashboard
├── start_cmd.bat           # Chạy CLI trong CMD
├── setup.bat               # Cài đặt tự động
├── requirements.txt        # Dependencies
├── config/
│   ├── accounts.json.enc   # Tài khoản (tự tạo)
│   ├── settings.json       # Cài đặt (tự tạo)
│   └── search_topics.txt   # Từ khóa tìm kiếm fallback
├── dashboard/
│   ├── index.html          # Web dashboard
│   └── style.css           # Dashboard styling
├── src/                    # Core modules
└── data/                   # Runtime data (tự tạo)
```

## ⚙️ Cài đặt

Tất cả cài đặt qua Web Dashboard (⚙️). Các tuỳ chọn chính:

| Cài đặt | Mặc định | Ghi chú |
|----------|----------|---------|
| Chạy ẩn (Headless) | `false` | Ẩn cửa sổ browser |
| Stealth | `true` | Chống phát hiện |
| Google Trends | `true` | Dùng từ khóa trending thật |
| Chặn hình ảnh | `true` | Tải trang nhanh hơn |
| Bảo vệ streak | `true` | Giám sát streak hàng ngày |
| AI Agent | `true` | OpenRouter fallback |
| Tự đổi thưởng | `false` | Tự động đổi điểm |

## ⚠️ Disclaimer

Dự án này chỉ dùng cho mục đích **học tập**. Tự động hoá Microsoft Rewards
có thể vi phạm Điều khoản Dịch vụ và có thể bị khoá tài khoản.
Sử dụng tự chịu rủi ro.
