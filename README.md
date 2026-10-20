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

## Option 1 – Use the ready-made app (no Python)

1. Download `MaBaLi.zip` (from the repo's Releases page, or build it yourself – see below).
2. Right-click the ZIP → **Extract All** to a permanent folder (e.g. Documents). Don't run it from inside the ZIP.
3. Double-click **`MaBaLi.exe`**. The app opens in your browser.
   If Windows shows *"Windows protected your PC"*: **More info → Run anyway** (the EXE isn't code-signed).
4. On first run a 10Bis window opens – log in to your account as usual. Your delivery address and all dishes load automatically (a few minutes).

Requires Microsoft Edge or Google Chrome (already on every Windows PC). Hebrew end-user instructions: [`READ-ME-FIRST.txt`](READ-ME-FIRST.txt).

## Option 2 – Run from source

Requires **Python 3.11+** on Windows.

```bash
git clone https://github.com/matokvekal/10bisHelper.git
cd 10bisHelper
pip install -r requirements.txt
python -m playwright install chromium
python server.py
```

The UI opens at http://localhost:8000. On first run, log in to your 10Bis account in the window that opens; your delivery address is taken from the account, then press **🔄 רענון נתונים** to collect the menus (a few minutes).

Optional: copy `.env.example` to `.env` and fill it in to enable automatic login (OTP or password).

Command-line options: `--port 8000`, `--headed` (show the browser window – first login / debugging), `--no-browser` (don't open the UI), `--snapshot` (take one time-pattern sample and exit).

## Build the Windows app (EXE)

Builds a standalone app that runs on PCs **without Python**.

**You need:** Windows 10/11 and Python 3.11+ on `PATH` (check with `python --version`). Nothing else – the script installs everything it needs.

**Steps:**

1. Close the app if it's running (⏻ in the app, or end `MaBaLi.exe` in Task Manager) – the build replaces the `dist` folder.
2. Open a Command Prompt or PowerShell in the project folder and run:

   ```bat
   build.bat
   ```

   On the first run this takes a few minutes. The script:
   - creates an isolated virtual environment `.build-venv` (so only this app's packages are bundled),
   - installs `requirements.txt` + PyInstaller into it,
   - deletes old `build\` / `dist\` folders and runs PyInstaller (`--windowed --onedir`, bundling `index.html`),
   - copies `READ-ME-FIRST.txt` next to the EXE and zips the result.

3. When it prints `Done: dist\MaBaLi.zip`, the output is:

   | Path | What it is |
   | --- | --- |
   | `dist\MaBaLi\MaBaLi.exe` | The app – double-click to run |
   | `dist\MaBaLi\_internal\` | Bundled Python runtime and libraries (keep next to the EXE) |
   | `dist\MaBaLi.zip` | The whole folder zipped – this is the file to share |

4. To share it, upload `dist\MaBaLi.zip` as a GitHub Release asset (or send it directly). Your personal data is **not** included in the build.

**Rebuild after every code change** – the EXE contains its own copy of `index.html` and the Python code, so edits to the source don't show up in it until you run `build.bat` again.

**Troubleshooting:**
- `'python' is not recognized` → install Python from python.org and tick *"Add python.exe to PATH"*.
- `BUILD FAILED` / access denied on `dist` → the app is still running; close it and run `build.bat` again.
- The EXE starts but nothing opens → see `%LOCALAPPDATA%\MaBaLi\app.log`.
- Fresh rebuild from scratch → delete `.build-venv`, `build` and `dist`, then run `build.bat`.

## Keep collecting time patterns while the app is closed (optional)

The app samples restaurant terms every 30 minutes (08:00–23:00, adjustable in ⚙️ Settings) while it's running. To keep sampling when it's closed, add a Windows Task Scheduler task that runs every 30 minutes:

```
"C:\path\to\MaBaLi\MaBaLi.exe" --snapshot
```

(or `python server.py --snapshot` from source). Patterns start appearing after about 2 days and become reliable after about 5.

## Privacy

Everything runs on your own computer. Your login session, address, settings and collected data stay local
(project folder when run from source, `%LOCALAPPDATA%\MaBaLi` for the EXE) and are excluded from git
(`.env`, `config.json`, `data.*`, `history.db`, `browser_profile/`, `debug/`). Favorites, hidden dishes and search history live in the browser's localStorage.

## Project layout

| File | Purpose |
| --- | --- |
| `server.py` | Local FastAPI server: UI, data, refresh, login, reviews, snapshot and patterns endpoints |
| `scraper.py` | Collects restaurants, menus and delivery rules for your address |
| `history.py` | Stores restaurant-terms samples (SQLite) for pattern learning |
| `patterns.py` | Learns time-of-day rules from the samples |
| `auth.py` | 10Bis login (OTP / password / manual) via Playwright |
| `paths.py` | Where user data lives (source vs. EXE) |
| `index.html` | The whole UI (single file, no build step) |
| `build.bat` | PyInstaller build of the Windows app |
| `READ-ME-FIRST.txt` | Hebrew instructions for end users, shipped inside the ZIP |

## Disclaimer

Personal, non-commercial tool. Not affiliated with, endorsed by, or supported by 10bis. It uses your own account and the same requests the 10bis website makes; use it responsibly and at your own risk.
