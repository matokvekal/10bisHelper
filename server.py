"""Local server for the 10bis dish search.  Run:  python server.py [--headed] [--port 8000]"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.request import urlopen

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

import auth
import history
import paths
import patterns
import scraper

BASE_DIR = paths.RES_DIR
APP_ID = "10bis-dish-search"
app = FastAPI(title="10bis dish search")
app.add_middleware(GZipMiddleware, minimum_size=1000)

HEADED = False  # set by --headed
SERVER: Optional[uvicorn.Server] = None
# Per-launch secret embedded in the served page; required by /api/shutdown so other
# websites (which can't read our page) can't close the app.
SESSION_TOKEN = secrets.token_urlsafe(24)
LOCAL_HOSTS = {"localhost", "127.0.0.1"}

refresh_state: dict[str, Any] = {
    "running": False, "done": 0, "total": 0, "current_restaurant": "",
    "phase": None,  # starting | listing | authenticating | menus | saving | done | error | cancelled
    "started_at": None, "finished_at": None, "error": None, "login_required": False, "meta": None,
}
auth_state: dict[str, Any] = {
    "logged_in": None, "needs_otp": False, "in_progress": False, "message": "", "checked_at": None,
}
state_lock = threading.Lock()
otp_queue: "queue.Queue[str]" = queue.Queue()
job_threads: list[threading.Thread] = []
active_jobs: set = set()
exiting = threading.Event()


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ------------------------------------------------------------------ OTP bridge

async def ui_otp_provider() -> str:
    """Called by the login flow: flag the UI to show the code modal and wait for POST /api/auth/otp."""
    while not otp_queue.empty():
        otp_queue.get_nowait()
    auth_state.update(needs_otp=True, message="הזינו את הקוד שקיבלתם")
    if refresh_state["running"]:
        refresh_state["phase"] = "authenticating"
    try:
        return await asyncio.to_thread(otp_queue.get, True, auth.OTP_TIMEOUT)
    except queue.Empty:
        raise RuntimeError("לא הוזן קוד בזמן")
    finally:
        auth_state["needs_otp"] = False
        if refresh_state["running"]:
            refresh_state["phase"] = "listing"


def run_job(coro: Any) -> Any:
    """asyncio.run() that cancel_jobs() can interrupt from another thread (on Exit)."""
    async def wrapper() -> Any:
        key = (asyncio.get_running_loop(), asyncio.current_task())
        active_jobs.add(key)
        try:
            return await coro
        finally:
            active_jobs.discard(key)
    return asyncio.run(wrapper())


def cancel_jobs() -> None:
    for loop, task in list(active_jobs):
        loop.call_soon_threadsafe(task.cancel)
    otp_queue.put("")  # release a login that's waiting for a code


# ------------------------------------------------------------------ refresh

def _run_refresh() -> None:
    cfg = scraper.load_config()

    def progress(done: int, total: int, name: str) -> None:
        # The scraper reports (0, 0) while loading the restaurant list, then once per menu;
        # after the last menu it closes the browser and writes data.json/data.xlsx.
        phase = "menus" if total and done < total else "saving" if total else "listing"
        refresh_state.update(done=done, total=total, current_restaurant=name, phase=phase)

    try:
        if not (cfg.get("search_url") or "").strip():
            cfg["search_url"] = _discover_address()
            scraper.save_config(cfg)
        meta = run_job(scraper.scrape_async(cfg, progress, ui_otp_provider,
                                            headed=HEADED or cfg.get("headed"), log=log))
        refresh_state.update(meta=meta, error=None, phase="done")
        log(f"refresh done: {meta['dish_count']} dishes, {meta['error_count']} errors")
    except scraper.LoginRequired as e:
        refresh_state.update(error=str(e), login_required=True, phase="error")
        auth_state.update(logged_in=False, message="🔐 נדרשת התחברות")
    except asyncio.CancelledError:
        log("refresh cancelled")
        refresh_state.update(error="הרענון הופסק", phase="cancelled")
    except Exception as e:
        traceback.print_exc()
        refresh_state.update(error=str(e) or e.__class__.__name__, phase="error")
    finally:
        refresh_state.update(running=False, finished_at=datetime.now().isoformat(timespec="seconds"))


def _discover_address() -> str:
    """No address yet (first run / switched user): take it from the user's own 10bis account."""
    refresh_state["phase"] = "address"
    url = run_job(auth.discover_search_url(log=log))
    if url is None:
        raise scraper.LoginRequired("נדרשת התחברות ל-10bis")
    if not url:  # account has no default address: let the user pick one on 10bis itself
        refresh_state["phase"] = "address_manual"
        url = run_job(auth.discover_search_url(headed=True, log=log, timeout=300))
    if not url:
        raise RuntimeError("לא נמצאה כתובת משלוח בחשבון 10bis – בחרו כתובת באתר 10bis ונסו שוב")
    log("delivery address taken from the 10bis account")
    return url


def start_refresh() -> bool:
    with state_lock:
        if refresh_state["running"] or auth_state["in_progress"] or snapshot_state["running"] or exiting.is_set():
            return False
        refresh_state.update(running=True, done=0, total=0, current_restaurant="", error=None,
                             login_required=False, meta=None, phase="starting",
                             started_at=datetime.now().isoformat(timespec="seconds"), finished_at=None)
    start_job(_run_refresh)
    return True


def start_job(target: Any, *args: Any) -> None:
    t = threading.Thread(target=target, args=args, daemon=True)
    job_threads[:] = [j for j in job_threads if j.is_alive()] + [t]
    t.start()


def auto_refresh_loop() -> None:
    while True:
        time.sleep(60)
        hours = float(scraper.load_config().get("auto_refresh_hours") or 0)
        if hours <= 0 or refresh_state["running"]:
            continue
        last = scraper.load_data().get("meta", {}).get("last_updated")
        age = (datetime.now() - datetime.fromisoformat(last)).total_seconds() if last else 1e9
        if age >= hours * 3600 and not refresh_state["login_required"]:
            log("auto refresh")
            start_refresh()


# ------------------------------------------------------------------ history snapshots

snapshot_state: dict[str, Any] = {"running": False, "last_error": None}


def take_snapshot() -> bool:
    """One SearchRestaurants call -> history.db. Skipped while a refresh or login holds the browser."""
    with state_lock:
        if snapshot_state["running"] or refresh_state["running"] or auth_state["in_progress"] or exiting.is_set():
            return False
        snapshot_state["running"] = True
    try:
        n = run_job(history.snapshot_async(scraper.load_config(), headed=HEADED or None, log=log))
        snapshot_state["last_error"] = None
        log(f"history snapshot: {n} restaurants")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        snapshot_state["last_error"] = str(e) or e.__class__.__name__
        log(f"history snapshot failed: {snapshot_state['last_error']}")
    finally:
        snapshot_state["running"] = False
    return True


def snapshot_loop() -> None:
    while True:
        time.sleep(60)
        cfg = scraper.load_config()
        every = float(cfg.get("snapshot_minutes") or 0)
        now = datetime.now()
        if every <= 0 or not (cfg.get("search_url") or "").strip() or refresh_state["login_required"] \
                or not scraper.in_window(now.strftime("%H:%M"), cfg.get("snapshot_from") or "00:00",
                                         cfg.get("snapshot_to") or "00:00"):
            continue
        last = history.stats()["last"]   # includes snapshots taken by a refresh or the CLI
        if not last or (now - datetime.fromisoformat(last)).total_seconds() >= every * 60 - 30:
            take_snapshot()


# ------------------------------------------------------------------ auth jobs

def _run_auth(job: str, method: str = "otp") -> None:
    try:
        if job == "check":
            ok = run_job(auth.check_status())
        else:
            auth_state["message"] = "פותח דפדפן להתחברות…" if method == "manual" else "מתחבר…"
            ok = run_job(auth.run_login(method, HEADED, ui_otp_provider, log=log))
        auth_state.update(logged_in=ok, message="מחובר/ת ✓" if ok else
                          ("לא מחובר/ת" if job == "check" else "ההתחברות נכשלה – ראו תיקיית debug"))
        if ok:
            refresh_state["login_required"] = False
    except asyncio.CancelledError:
        auth_state.update(message="בוטל")
    except Exception as e:
        traceback.print_exc()
        auth_state.update(logged_in=False, message=f"שגיאה: {e}")
    finally:
        auth_state.update(in_progress=False, needs_otp=False,
                          checked_at=datetime.now().isoformat(timespec="seconds"))


def start_auth(job: str, method: str = "otp") -> bool:
    with state_lock:
        if auth_state["in_progress"] or refresh_state["running"] or exiting.is_set():
            return False
        auth_state.update(in_progress=True, message="")
    start_job(_run_auth, job, method)
    return True


# ------------------------------------------------------------------ routes

@app.get("/")
def index() -> HTMLResponse:
    html = (BASE_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("</head>", f'<meta name="app-token" content="{SESSION_TOKEN}">\n</head>', 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/api/data")
def get_data() -> JSONResponse:
    return JSONResponse(scraper.load_data(), headers={"Cache-Control": "no-cache"})


@app.get("/data.xlsx")
def get_xlsx() -> FileResponse:
    if not scraper.XLSX_PATH.exists():
        raise HTTPException(404, "אין עדיין קובץ Excel")
    return FileResponse(scraper.XLSX_PATH, filename="10bis-dishes.xlsx")


REVIEWS_URL = scraper.SITE + "/NextApi/GetRestaurantReviews?culture=he-IL&uiCulture=he&restaurantId={id}"
REVIEWS_TTL = 3600
reviews_cache: dict[int, tuple[float, list]] = {}


@app.get("/api/reviews/{restaurant_id}")
def get_reviews(restaurant_id: int) -> dict:
    """Latest reviews of one restaurant, fetched on demand (UI hover) – public, no login needed."""
    hit = reviews_cache.get(restaurant_id)
    if hit and time.time() - hit[0] < REVIEWS_TTL:
        return {"reviews": hit[1]}
    from curl_cffi import requests as curl
    try:
        r = curl.get(REVIEWS_URL.format(id=restaurant_id), impersonate="chrome", timeout=15)
        items = (r.json().get("Data") or {}).get("reviewsList") or []
    except Exception as e:
        raise HTTPException(502, f"לא ניתן לטעון ביקורות: {e}")
    reviews = [{"name": x.get("shortUserName") or "", "date": (x.get("reviewDateString") or "").split(" ")[0],
                "text": (x.get("reviewText") or "").strip(),
                "stars": (x.get("reviewRank") or 0) / 2}   # 10bis ranks 0-10
               for x in items]
    reviews_cache[restaurant_id] = (time.time(), reviews)
    return {"reviews": reviews}


@app.post("/api/refresh")
def post_refresh() -> JSONResponse:
    if not start_refresh():
        return JSONResponse({"ok": False, "error": "סריקה או התחברות כבר פועלת"}, status_code=409)
    return JSONResponse({"ok": True}, status_code=202)


@app.get("/api/refresh/status")
def refresh_status() -> dict:
    return refresh_state


@app.get("/api/patterns")
def get_patterns() -> JSONResponse:
    res = patterns.analyse(history.load_rows())
    res["coverage"]["snapshot_error"] = snapshot_state["last_error"]
    return JSONResponse(res, headers={"Cache-Control": "no-cache"})


@app.post("/api/snapshot")
def post_snapshot() -> JSONResponse:
    if refresh_state["running"] or auth_state["in_progress"] or snapshot_state["running"]:
        return JSONResponse({"ok": False, "error": "סריקה או התחברות כבר פועלת"}, status_code=409)
    start_job(take_snapshot)
    return JSONResponse({"ok": True}, status_code=202)


@app.get("/api/config")
def get_config() -> dict:
    cfg = scraper.load_config()
    cfg["address"] = scraper.parse_address(cfg.get("search_url", "")) or scraper.load_data().get("meta", {}).get("address", "")
    return cfg


@app.post("/api/config")
def post_config(body: dict = Body(...)) -> dict:
    cfg = scraper.load_config()
    allowed = set(scraper.DEFAULT_CONFIG)
    url = body.get("search_url")
    if url is not None and url.strip() and "searchrestaurants" not in url.lower():
        raise HTTPException(400, "הקישור חייב להיות כתובת SearchRestaurants של 10bis")
    old_url = (cfg.get("search_url") or "").strip()
    updates = {k: v for k, v in body.items() if k in allowed}
    for k in ("max_distance_km", "max_delivery_minutes", "max_delivery_fee"):
        if k in updates:
            try:
                updates[k] = max(0.0, float(updates[k] or 0))
            except (TypeError, ValueError):
                raise HTTPException(400, "ערך מגבלה לא תקין")
    if "only_open_now" in updates:
        updates["only_open_now"] = bool(updates["only_open_now"])
    if "snapshot_minutes" in updates:
        try:
            updates["snapshot_minutes"] = max(0.0, float(updates["snapshot_minutes"] or 0))
        except (TypeError, ValueError):
            raise HTTPException(400, "ערך מרווח דגימה לא תקין")
    for k in ("snapshot_from", "snapshot_to"):
        if k in updates:
            updates[k] = scraper.hhmm(updates[k]) or scraper.DEFAULT_CONFIG[k]
    cfg.update(updates)
    scraper.save_config(cfg)
    if (cfg.get("search_url") or "").strip() != old_url:
        # Dishes of the previous address must not be shown for the new one.
        for p in (scraper.DATA_PATH, scraper.XLSX_PATH):
            p.unlink(missing_ok=True)
    return cfg


def login_method(cfg: dict) -> str:
    """OTP/password need credentials in .env; without them (a fresh EXE install) the user
    logs in themselves in a visible 10bis window."""
    m = cfg.get("login_method", "otp")
    creds = auth.credentials()
    return "manual" if m in ("otp", "password") and not (creds["email"] or creds["phone"]) else m


@app.get("/api/auth/status")
def get_auth_status() -> dict:
    cfg = scraper.load_config()
    return {**auth_state, "method": login_method(cfg), "engine": cfg.get("engine"),
            "credentials": auth.credentials(), "login_required": refresh_state["login_required"]}


@app.post("/api/auth/login")
def post_login(body: Optional[dict] = Body(None)) -> JSONResponse:
    body = body or {}
    job = "check" if body.get("check_only") else "login"
    method = body.get("method") or login_method(scraper.load_config())
    if method not in ("otp", "password", "manual"):
        raise HTTPException(400, "שיטת התחברות לא מוכרת")
    if not start_auth(job, method):
        return JSONResponse({"ok": False, "error": "פעולה אחרת כבר פועלת"}, status_code=409)
    return JSONResponse({"ok": True}, status_code=202)


@app.post("/api/auth/otp")
def post_otp(body: dict = Body(...)) -> dict:
    code = str(body.get("code", "")).strip()
    if not code:
        raise HTTPException(400, "קוד ריק")
    if not auth_state["needs_otp"]:
        raise HTTPException(409, "אין התחברות שממתינה לקוד")
    otp_queue.put(code)
    return {"ok": True}


@app.post("/api/auth/logout")
def post_logout() -> dict:
    if refresh_state["running"] or auth_state["in_progress"]:
        raise HTTPException(409, "יש פעולה פעילה – נסו שוב בסיומה")
    try:
        auth.logout()
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    # Switching user: the next login's address (and dishes) come from the new account.
    cfg = scraper.load_config()
    cfg["search_url"] = ""
    scraper.save_config(cfg)
    for p in (scraper.DATA_PATH, scraper.XLSX_PATH):
        p.unlink(missing_ok=True)
    auth_state.update(logged_in=False, message="נותקת")
    return {"ok": True}


@app.get("/api/ping")
def ping() -> dict:
    return {"app": APP_ID}


@app.post("/api/shutdown")
def post_shutdown(request: Request) -> dict:
    host = (request.headers.get("host") or "").rsplit(":", 1)[0]
    token = request.headers.get("x-app-token") or ""
    if host not in LOCAL_HOSTS or not secrets.compare_digest(token, SESSION_TOKEN):
        raise HTTPException(403, "forbidden")
    log("exit requested from UI")
    threading.Thread(target=request_exit, daemon=True).start()
    return {"ok": True}


# ------------------------------------------------------------------ process lifecycle

def request_exit() -> None:
    """Cancel a running scan/login (its `async with Browser` closes Edge and Playwright),
    wait for it to wind down, then stop the HTTP server."""
    exiting.set()
    time.sleep(0.3)  # let the HTTP response go out first
    # Last resort if something hangs; kill_children_on_exit() still reaps child processes.
    threading.Timer(25, os._exit, args=(0,)).start()
    cancel_jobs()
    for t in list(job_threads):
        t.join(timeout=15)
    if SERVER:
        SERVER.should_exit = True


def kill_children_on_exit() -> None:
    """Put this process in a Windows Job that kills every child (Playwright driver, Edge)
    when we exit – even on a crash or a forced kill – so nothing is left running."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("Basic", BasicLimits), ("IoInfo", ctypes.c_ulonglong * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    JOB_OBJECT_LIMIT_BREAKAWAY_OK, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x800, 0x2000
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    info = ExtendedLimits()
    info.Basic.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK
    ok = job and k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info))         and k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(k32.GetCurrentProcess()))
    if not ok:
        log(f"job object unavailable (error {ctypes.get_last_error()})")
    globals()["_JOB"] = job  # the handle must stay open for the life of the process


def open_ui(url: str) -> None:
    """Open the UI in the user's browser *outside* our job, so Exit doesn't close their browser."""
    if os.name == "nt":
        CREATE_BREAKAWAY_FROM_JOB, CREATE_NO_WINDOW = 0x01000000, 0x08000000
        try:
            subprocess.Popen(["cmd", "/c", "start", "", url],
                             creationflags=CREATE_BREAKAWAY_FROM_JOB | CREATE_NO_WINDOW)
            return
        except OSError:
            pass
    webbrowser.open(url)


def running_instance(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
            return json.load(r).get("app") == APP_ID
    except Exception:
        return False


def port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def show_error(msg: str) -> None:
    log(f"startup failed: {msg}")
    if paths.FROZEN and os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "MaBaLi", 0x10)  # MB_ICONERROR


def redirect_output() -> None:
    """A windowed EXE has no console (sys.stdout is None): log to app.log next to the EXE."""
    if sys.stdout is not None:
        return
    log_path = paths.APP_DIR / "app.log"
    if log_path.exists() and log_path.stat().st_size > 1_000_000:
        log_path.unlink()
    sys.stdout = sys.stderr = open(log_path, "a", encoding="utf-8", buffering=1)


def main() -> None:
    global HEADED
    redirect_output()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--headed", action="store_true", help="show the browser window (first login / debugging)")
    ap.add_argument("--no-browser", action="store_true", help="don't open the UI automatically")
    ap.add_argument("--snapshot", action="store_true",
                    help="take one restaurant-terms snapshot and exit (for Windows Task Scheduler)")
    args = ap.parse_args()
    HEADED = args.headed
    if args.snapshot:
        n = asyncio.run(history.snapshot_async(scraper.load_config(), log=log))
        log(f"history snapshot: {n} restaurants")
        return

    # Double-clicking the EXE again just reopens the UI of the copy that's already running.
    for port in range(args.port, args.port + 10):
        if running_instance(port):
            if not args.no_browser:
                open_ui(f"http://localhost:{port}")
            return
        if port_free(port):
            break
    else:
        show_error(f"לא ניתן להפעיל: הפורטים {args.port}-{args.port + 9} תפוסים על ידי תוכנות אחרות.")
        return
    if port != args.port:
        log(f"port {args.port} is used by another program; using {port}")

    global SERVER
    kill_children_on_exit()
    threading.Thread(target=auto_refresh_loop, daemon=True).start()
    threading.Thread(target=snapshot_loop, daemon=True).start()
    url = f"http://localhost:{port}"
    if not args.no_browser:
        threading.Timer(1.2, open_ui, args=(url,)).start()
    log(f"serving on {url}")
    SERVER = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           timeout_graceful_shutdown=3))
    SERVER.run()
    if not SERVER.started and not exiting.is_set():
        show_error(f"לא ניתן להפעיל את השרת על פורט {port}.\nפרטים: {paths.APP_DIR / 'app.log'}")
    log("stopped")
    os._exit(0)  # don't wait for a scan thread; the job object closes its browser


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        show_error(f"שגיאה בהפעלה:\n{e}")
        sys.exit(1)
