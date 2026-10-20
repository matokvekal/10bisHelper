"""Playwright engine for 10bis: persistent browser session, login flows, JSON fetching.

CLI:
  python auth.py --login [--method otp|password|manual] [--headed]
  python auth.py --status
  python auth.py --logout
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import paths

BASE_DIR = paths.APP_DIR
PROFILE_DIR = BASE_DIR / "browser_profile"
DEBUG_DIR = BASE_DIR / "debug"

try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

# ------------------------------------------------------------------ selectors
# Everything that depends on the 10bis page layout lives here.
# Each entry is a list of alternatives tried in order:
#   ("role", <aria role>, <accessible name>) | ("placeholder", text) | ("text", text) | ("css", selector)
SELECTORS: dict[str, Any] = {
    "home_url": "https://www.10bis.co.il/next/",
    "user_api": "https://www.10bis.co.il/NextApi/GetUserV2",
    "open_login": [("role", "button", "התחברות"), ("text", "התחברות")],
    "identifier_input": [("placeholder", "מה המייל שלך"), ("css", "input[type=email]"),
                         ("css", "input[type=tel]"), ("css", "input[name=email]")],
    "continue_button": [("role", "button", "קדימה"), ("role", "button", "המשך"),
                        ("role", "button", "התחברות")],
    "password_input": [("css", "input[type=password]")],
    "otp_input": [("css", "input[autocomplete='one-time-code']"),
                  ("css", "input[inputmode='numeric']"), ("placeholder", "קוד"),
                  ("css", "input[maxlength='1']"), ("css", "input[type=tel]")],
    "submit_button": [("role", "button", "כניסה"), ("role", "button", "התחברות"),
                      ("role", "button", "אישור"), ("role", "button", "קדימה"),
                      ("role", "button", "המשך"), ("role", "button", "שליחה")],
    "use_password_link": [("text", "כניסה עם סיסמה"), ("text", "התחברות עם סיסמה")],
}

OTP_TIMEOUT = 300          # seconds to wait for the user to type the code
MANUAL_LOGIN_TIMEOUT = 300

# A persistent profile directory can only be open by one browser at a time.
PROFILE_LOCK = threading.Lock()

Log = Callable[[str], None]
OtpProvider = Callable[[], Awaitable[str]]


def credentials() -> dict[str, bool]:
    return {k: bool(os.getenv(f"TENBIS_{k.upper()}")) for k in ("email", "password", "phone")}


class Browser:
    """Async context manager around one persistent Chromium context."""

    name = "playwright"

    def __init__(self, headed: bool = False, log: Log = print):
        self.headed, self.log = headed, log
        self._pw = self.ctx = self.page = None
        self._page_lock = asyncio.Lock()

    async def __aenter__(self) -> "Browser":
        from playwright.async_api import async_playwright
        if not PROFILE_LOCK.acquire(timeout=5):
            raise RuntimeError("הדפדפן כבר בשימוש (סריקה או התחברות פעילה)")
        try:
            self._pw = await async_playwright().start()
            # The EXE ships no Chromium: drive the Edge/Chrome already installed on the PC.
            for channel in (("msedge", "chrome") if paths.FROZEN else (None,)):
                try:
                    self.ctx = await self._pw.chromium.launch_persistent_context(
                        str(PROFILE_DIR), channel=channel, headless=not self.headed, locale="he-IL",
                        viewport={"width": 1280, "height": 900})
                    break
                except Exception:
                    if channel == "chrome":
                        raise RuntimeError("לא נמצא דפדפן Edge או Chrome במחשב")
            self.page = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
        except BaseException:
            await self._close()
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        await self._close()

    async def _close(self) -> None:
        try:
            if self.ctx:
                await self.ctx.close()
            if self._pw:
                await self._pw.stop()
        finally:
            self.ctx = self._pw = None
            if PROFILE_LOCK.locked():
                PROFILE_LOCK.release()

    # -------------------------------------------------------------- fetching

    async def get_json(self, url: str) -> tuple[int, Any, str]:
        """GET via the context's request API (shares cookies); if blocked,
        retry with fetch() from inside the page (real browser fingerprint)."""
        r = await self.ctx.request.get(url, timeout=30000)
        status, final = r.status, r.url
        try:
            data = await r.json()
        except Exception:
            data = await r.text()
        if status in (401, 403) or not isinstance(data, (dict, list)):
            st2, data2 = await self._page_fetch(url)
            if st2 == 200 and isinstance(data2, (dict, list)):
                return st2, data2, url
        return status, data, final

    async def _page_fetch(self, url: str, method: str = "GET") -> tuple[int, Any]:
        async with self._page_lock:
            if not self.page.url.startswith("https://www.10bis.co.il"):
                await self.page.goto(SELECTORS["home_url"], wait_until="domcontentloaded", timeout=60000)
        res = await self.page.evaluate(
            """async ([url, method]) => {
                const r = await fetch(url, {method, credentials: 'include',
                    headers: {'Content-Type': 'application/json'}, body: method === 'POST' ? '{}' : undefined});
                const t = await r.text();
                try { return [r.status, JSON.parse(t)]; } catch { return [r.status, t]; }
            }""", [url, method])
        return res[0], res[1]

    async def is_logged_in(self) -> bool:
        try:
            r = await self.ctx.request.post(SELECTORS["user_api"], data={}, timeout=30000)
            data = await r.json()
        except Exception:
            try:
                _, data = await self._page_fetch(SELECTORS["user_api"], "POST")
            except Exception:
                return False
        user = (data or {}).get("Data") or {}
        return bool(user.get("userId")) if isinstance(user, dict) else False

    async def debug_snapshot(self, name: str) -> None:
        try:
            DEBUG_DIR.mkdir(exist_ok=True)
            await self.page.screenshot(path=str(DEBUG_DIR / f"{name}.png"), full_page=True)
            (DEBUG_DIR / f"{name}.html").write_text(await self.page.content(), encoding="utf-8")
        except Exception:
            pass

    # -------------------------------------------------------------- login

    async def _find(self, key: str, timeout: float = 0):
        """First visible locator among the alternatives in SELECTORS[key]."""
        deadline = time.monotonic() + timeout
        while True:
            for alt in SELECTORS[key]:
                kind = alt[0]
                if kind == "role":
                    loc = self.page.get_by_role(alt[1], name=alt[2], exact=True)
                elif kind == "placeholder":
                    loc = self.page.get_by_placeholder(alt[1])
                elif kind == "text":
                    loc = self.page.get_by_text(alt[1], exact=False)
                else:
                    loc = self.page.locator(alt[1])
                for i in range(await loc.count()):
                    if await loc.nth(i).is_visible():
                        return loc.nth(i)
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.5)

    async def _click_submit(self, exclude: str = "") -> None:
        btn = await self._find("submit_button", 3)
        if btn and exclude and (await btn.inner_text()).strip() == exclude:
            btn = None
        if btn:
            await btn.click()
        else:
            await self.page.keyboard.press("Enter")

    async def _wait_logged_in(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self.is_logged_in():
                return True
            await asyncio.sleep(2)
        return False

    async def login(self, method: str = "otp", otp_provider: Optional[OtpProvider] = None) -> bool:
        if await self.is_logged_in():
            self.log("כבר מחובר/ת")
            return True
        if method == "manual":
            return await self.manual_login()
        ident = os.getenv("TENBIS_EMAIL") or os.getenv("TENBIS_PHONE")
        if not ident:
            raise RuntimeError("חסר TENBIS_EMAIL (או TENBIS_PHONE) בקובץ .env")
        password = os.getenv("TENBIS_PASSWORD")
        if method == "password" and not password:
            raise RuntimeError("חסר TENBIS_PASSWORD בקובץ .env")
        try:
            await self.page.goto(SELECTORS["home_url"], wait_until="domcontentloaded", timeout=60000)
            opener = await self._find("open_login", 20)
            if not opener:
                raise RuntimeError("לא נמצא כפתור התחברות")
            await opener.click()
            field = await self._find("identifier_input", 15)
            if not field:
                raise RuntimeError("לא נמצא שדה מייל")
            await field.fill(ident)
            cont = await self._find("continue_button", 5)
            await (cont.click() if cont else self.page.keyboard.press("Enter"))
            await asyncio.sleep(2)

            if method == "password":
                link = await self._find("use_password_link", 3)
                if link:
                    await link.click()
            pwd = await self._find("password_input", 5) if method == "password" else None
            if pwd:
                await pwd.fill(password)
                await self._click_submit()
            else:
                otp = await self._find("otp_input", 20)
                if not otp:
                    raise RuntimeError("לא נמצא שדה סיסמה/קוד")
                if not otp_provider:
                    raise RuntimeError("נדרש קוד חד-פעמי")
                self.log("ממתין לקוד חד-פעמי…")
                code = (await asyncio.wait_for(otp_provider(), OTP_TIMEOUT)).strip()
                await otp.click()
                await self.page.keyboard.type(code, delay=80)
                await asyncio.sleep(1)
                if not await self.is_logged_in():
                    await self._click_submit()
            ok = await self._wait_logged_in(20)
            if not ok:
                await self.debug_snapshot("login_failed")
            return ok
        except Exception:
            await self.debug_snapshot("login_error")
            raise

    async def manual_login(self) -> bool:
        if not self.headed:
            raise RuntimeError("התחברות ידנית דורשת חלון דפדפן גלוי")
        await self.page.goto(SELECTORS["home_url"], wait_until="domcontentloaded", timeout=60000)
        self.log("התחברו בחלון הדפדפן שנפתח…")
        return await self._wait_logged_in(MANUAL_LOGIN_TIMEOUT)

    async def try_auto_login(self, method: str, otp_provider: Optional[OtpProvider]) -> bool:
        """Re-login after session expiry. Only password login can run unattended."""
        if method != "password" or not credentials()["password"]:
            return False
        try:
            return await self.login("password", otp_provider)
        except Exception as e:
            self.log(f"auto re-login failed: {e}")
            return False


def logout() -> None:
    if not PROFILE_LOCK.acquire(timeout=5):
        raise RuntimeError("הדפדפן בשימוש כרגע – נסו שוב בסיום הסריקה")
    try:
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)
    finally:
        PROFILE_LOCK.release()


async def check_status(headed: bool = False) -> bool:
    async with Browser(headed=headed) as b:
        return await b.is_logged_in()


async def run_login(method: str, headed: bool, otp_provider: Optional[OtpProvider], log: Log = print) -> bool:
    async with Browser(headed=headed or method == "manual", log=log) as b:
        return await b.login(method, otp_provider)


async def discover_search_url(headed: bool = False, log: Log = print, timeout: float = 30) -> Optional[str]:
    """Open 10bis as the logged-in user and capture the SearchRestaurants request the site
    itself makes for the account's delivery address (what used to be copied from DevTools).
    Returns None when not logged in, "" when the site made no such request in time."""
    async with Browser(headed=headed, log=log) as b:
        if not await b.is_logged_in():
            return None
        found: asyncio.Future = asyncio.get_running_loop().create_future()

        def on_request(req: Any) -> None:
            if "searchrestaurants" in req.url.lower() and not found.done():
                found.set_result(req.url)

        b.page.on("request", on_request)
        for url in (SELECTORS["home_url"], SELECTORS["home_url"] + "restaurants/search"):
            try:
                await b.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                log(f"discover: {url} failed: {e!s:.120}")
            try:
                return await asyncio.wait_for(asyncio.shield(found), timeout)
            except asyncio.TimeoutError:
                continue
        return ""


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="10bis login helper")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--login", action="store_true")
    g.add_argument("--status", action="store_true")
    g.add_argument("--logout", action="store_true")
    ap.add_argument("--method", choices=["otp", "password", "manual"], default="otp")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    if args.logout:
        logout()
        print("נותקת – browser_profile נמחק")
    elif args.status:
        print("מחובר/ת ✓" if asyncio.run(check_status(args.headed)) else "לא מחובר/ת")
    else:
        async def cli_otp() -> str:
            return (await asyncio.to_thread(input, "הקלד/י את הקוד שקיבלת: ")).strip()
        ok = asyncio.run(run_login(args.method, args.headed, cli_otp))
        print("התחברת בהצלחה ✓" if ok else "ההתחברות נכשלה – ראו debug/")


if __name__ == "__main__":
    main()
