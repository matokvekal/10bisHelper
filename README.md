===================================================
**10Bis Viewer – A Better Way to View Your 10Bis Information**

A lightweight desktop application that makes it easier to access, view, and understand your 10Bis account information.

Provides a clear, user-friendly interface for reviewing 10Bis data, with simple navigation and data refresh capabilities.

Designed to run locally on Windows with minimal setup, using your own 10Bis account.
====================================================

# 10Bis Viewer – A Better Way to View Your 10Bis Information

A lightweight desktop application that makes it easier to access, view, and understand your 10Bis account information.

Provides a clear, user-friendly interface for reviewing 10Bis data, with simple navigation and data refresh capabilities.

Designed to run locally on Windows with minimal setup, using your own 10Bis account.

> מה בא לי – טבלה אחת של כל המנות מכל המסעדות שמשלוחות אליך ב-10bis, עם חיפוש וסינון חכמים.

## Features

- **One table of every dish** from every restaurant that delivers to your 10Bis address
- **Hebrew-aware search** (final letters, niqqud, leading ה, synonyms) with **"but not"** exclusions (`פיצה -פטריות`) and the last 10 searches saved
- **Filters:** price range (with or without delivery), kosher, delivery type (regular / shared office delivery), open now, free delivery, promotions, no drinks/extras
- **"Order at" / "Receive by" times:** computes when each dish would actually arrive (regular delivery, scheduled slot, or shared delivery) and hides what can't make it in time
- **Time-dependent deals and notes** parsed from restaurant announcements ("deliveries from 12:00", "Fridays only"…), evaluated against the hour you pick
- **Favorites ♥**, **hide a dish forever**, and **"doesn't deliver to me"** per restaurant – all stored locally in the browser
- **Dish photo preview** and **latest reviews** on hover, loaded on demand
- **📈 Time patterns:** samples restaurant terms every 30 minutes and learns each restaurant's time-of-day rules ("free delivery before 10:00", "no deliveries after 12:00"…) – needs a few days of collection
- Day / night mode, CSV export, works on mobile screen widths

## Run from source

Requires Python 3.11+ on Windows.

```bash
pip install -r requirements.txt
python -m playwright install chromium
python server.py
```

The UI opens at http://localhost:8000. On first run, log in to your 10Bis account in the window that opens; your delivery address is taken from the account, then press **🔄 רענון נתונים** to collect the menus (a few minutes).

Optional: copy `.env.example` to `.env` to enable automatic login (OTP or password).

## Build the Windows app

```bat
build.bat
```

Creates `dist\MaBaLi\MaBaLi.exe` and `dist\MaBaLi.zip`, which run on PCs without Python. End-user instructions (Hebrew) are in `READ-ME-FIRST.txt`, which is copied into the ZIP.

## Privacy

Everything runs on your own computer. Your login session, address, settings and collected data stay local
(project folder when run from source, `%LOCALAPPDATA%\MaBaLi` for the EXE) and are excluded from git
(`.env`, `config.json`, `data.*`, `browser_profile/`, `debug/`). Favorites, hidden dishes and search history live in the browser's localStorage.

## Project layout

| File | Purpose |
| --- | --- |
| `server.py` | Local FastAPI server: UI, data, refresh, login and reviews endpoints |
| `scraper.py` | Collects restaurants, menus and delivery rules for your address |
| `auth.py` | 10Bis login (OTP / password / manual) via Playwright |
| `paths.py` | Where user data lives (source vs. EXE) |
| `index.html` | The whole UI (single file, no build step) |
| `build.bat` | PyInstaller build of the Windows app |

## Disclaimer

Personal, non-commercial tool. Not affiliated with, endorsed by, or supported by 10bis. It uses your own account and the same requests the 10bis website makes; use it responsibly and at your own risk.
