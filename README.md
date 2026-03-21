# 🏆 Rewards Bot

Automated Microsoft Rewards farming bot — **Python + Playwright**.

## ⚡ Quick Install

### Windows (recommended)
```
1. Install Python 3.10+ from python.org (check "Add to PATH")
2. Double-click setup.bat
3. Done!
```

### Manual
```bash
pip install -r requirements.txt
playwright install chromium
```

## 🚀 Usage

| Command | Description |
|---------|-------------|
| `python main.py` | Interactive CLI menu |
| `python main.py --web` | Web Dashboard at `localhost:8080` |
| `python main.py --auto` | Auto-run all tasks (for scheduler) |

### First Time
1. Run `python main.py --web`
2. Open `http://localhost:8080`
3. Click ⚙️ **Cài đặt** → **Thêm tài khoản** → add your Microsoft account
4. Click **Chạy tất cả** to start!

## ✨ Features

| Category | Details |
|----------|---------|
| 🔎 **Search** | Desktop, Mobile (CDP emulation), Edge — auto-calculated from API |
| 🎯 **Tasks** | Daily Set, Punch Cards, Promotions, Quizzes (6 types) |
| 🛡️ **Stealth** | playwright-stealth, fingerprint spoofing, Edge UA |
| 🧠 **Smart** | Google Trends queries, retry with backoff, credit probe |
| 💰 **Points** | CSV logging, auto-redeem, streak protection |
| 📩 **Notify** | Discord webhook + Telegram bot |
| ⏰ **Schedule** | Windows Task Scheduler integration |
| 🔐 **Security** | Fernet encrypted credentials, master password |
| 🌐 **Dashboard** | Web UI at `localhost:8080` with settings drawer |
| 🤖 **AI** | OpenRouter AI fallback for complex tasks |

## 📂 Structure

```
rewards-bot/
├── main.py                 # Entry point
├── setup.bat               # One-click Windows installer
├── requirements.txt        # Python dependencies
├── config/
│   ├── accounts.example.json   # Account template
│   ├── settings.json           # Bot settings (auto-created)
│   └── search_topics.txt       # Fallback search keywords
├── dashboard/
│   ├── index.html              # Web dashboard
│   └── style.css               # Dashboard styling
├── src/                        # Core modules (22 files)
└── data/                       # Runtime data (auto-created)
```

## ⚙️ Settings

All settings are managed via the web dashboard (⚙️ button). Key options:

| Setting | Default | Notes |
|---------|---------|-------|
| Headless | `false` | Hide browser window |
| Stealth | `true` | Anti-detection |
| Google Trends | `true` | Real trending queries |
| Block Images | `true` | Faster page loads |
| Streak Protection | `true` | Monitor daily streaks |
| AI Agent | `true` | OpenRouter fallback |
| Auto Redeem | `false` | Auto-redeem points |

## ⚠️ Disclaimer

This project is for **educational purposes only**. Automating Microsoft Rewards
may violate their Terms of Service and could result in account suspension.
Use at your own risk.
