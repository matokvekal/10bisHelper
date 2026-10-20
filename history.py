"""Restaurant-terms history: one row per restaurant per snapshot.

A snapshot is a single SearchRestaurants call (no menus), cheap enough to take every
30 minutes. The rows feed patterns.py, which learns time-of-day rules such as
"free delivery only before 10:00" or "no deliveries after 12:00".

CLI:  python history.py            take one snapshot now (for Windows Task Scheduler)
      python history.py --stats    print what has been collected so far
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

import paths
import scraper
from scraper import find_list, hhmm, num, pick

DB_PATH = paths.APP_DIR / "history.db"
KEEP_DAYS = 60

COLUMNS = {
    "ts": "TEXT", "day": "TEXT", "weekday": "INT", "minute": "INT",   # weekday: 0 = Sunday (like JS)
    "restaurant_id": "INT", "name": "TEXT", "logo": "TEXT",
    "open_now": "INT", "terminal": "INT", "temp_closed": "TEXT",
    "fee": "REAL", "min_order": "REAL", "discount_pct": "REAL", "coupon_min": "REAL",
    "disc_fee": "REAL", "disc_fee_min": "REAL", "eta": "INT", "future": "INT", "pool": "INT",
    "mode": "TEXT", "open_time": "TEXT", "close_time": "TEXT", "announcement": "TEXT",
}


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute(f"CREATE TABLE IF NOT EXISTS snap ({', '.join(f'{k} {t}' for k, t in COLUMNS.items())})")
    con.execute("CREATE INDEX IF NOT EXISTS snap_rest ON snap(restaurant_id, day)")
    return con


def _flag(v: Any) -> Optional[int]:
    return None if v is None else int(bool(v))


def row_from_raw(r: dict, now: datetime) -> Optional[dict]:
    """Raw SearchRestaurants item -> history row (None for non-delivery restaurants)."""
    rid = pick(r, "restaurantId", "id", "RestaurantId")
    if rid is None or pick(r, "isDeliveryEnabled", default=True) is False or pick(r, "isActive", default=True) is False:
        return None
    tags = pick(r, "tags", default=[]) or []
    coupon = next((t for t in tags if isinstance(t, dict)
                   and str(pick(t, "type", default="")).lower() == "discountcoupon"), None)
    return {
        "ts": now.isoformat(timespec="seconds"), "day": now.date().isoformat(),
        "weekday": (now.weekday() + 1) % 7, "minute": now.hour * 60 + now.minute,
        "restaurant_id": rid,
        "name": pick(r, "restaurantName", "name", default="") or "",
        "logo": pick(r, "restaurantLogoUrl", "logoUrl", default="") or "",
        "open_now": int(bool(pick(r, "isOpenNow", "openNow", default=False))),
        "terminal": _flag(pick(r, "isTerminalActive")),
        "temp_closed": str(pick(r, "tempClosedReason", default="") or ""),
        "fee": num(pick(r, "deliveryFeeAmount", "deliveryFee", default=0)) or 0,
        "min_order": num(pick(r, "minimumPriceForOrder", "minimumForASAPOrder", default=0)) or 0,
        "discount_pct": num(pick(r, "discountPercent", default=0)) or 0,
        "coupon_min": num(pick(coupon, "minimumPriceForDiscount", default=0)) or 0 if coupon else 0,
        "disc_fee": num(pick(r, "discountedDeliveryFee")),
        "disc_fee_min": num(pick(r, "minOrderTotalForDeliveryFeeDiscount", default=0)) or 0,
        "eta": num(pick(r, "deliveryTimeInMinutes", "deliveryTime")),
        "future": _flag(pick(r, "futureOrdersAvailable")),
        "pool": _flag(pick(r, "hasActivePoolRule")),
        "mode": str(pick(r, "defaultDeliveryRuleType", default="") or ""),
        "open_time": hhmm(pick(r, "openTime")), "close_time": hhmm(pick(r, "closeTime")),
        "announcement": str(pick(r, "announcement", default="") or "").strip(),
    }


def append_snapshot(data: Any, now: Optional[datetime] = None) -> int:
    """Store every restaurant of a raw SearchRestaurants answer. Returns the row count."""
    now = now or datetime.now()
    rows = [x for r in find_list(data, "restaurantsList", "restaurants", "RestaurantsList", "items")
            if isinstance(r, dict) and (x := row_from_raw(r, now))]
    if not rows:
        return 0
    with connect() as con:
        cols = list(COLUMNS)
        con.executemany(f"INSERT INTO snap ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        [[x[c] for c in cols] for x in rows])
        con.execute("DELETE FROM snap WHERE day < ?", ((now - timedelta(days=KEEP_DAYS)).date().isoformat(),))
    return len(rows)


def load_rows(days: int = KEEP_DAYS) -> list[dict]:
    if not DB_PATH.exists():
        return []
    since = (datetime.now() - timedelta(days=days)).date().isoformat()
    with connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM snap WHERE day >= ? ORDER BY ts", (since,))]


def stats() -> dict:
    if not DB_PATH.exists():
        return {"snapshots": 0, "days": 0, "first": None, "last": None}
    with connect() as con:
        r = con.execute("SELECT COUNT(DISTINCT ts), COUNT(DISTINCT day), MIN(ts), MAX(ts) FROM snap").fetchone()
    return {"snapshots": r[0], "days": r[1], "first": r[2], "last": r[3]}


async def snapshot_async(cfg: dict, headed: Optional[bool] = None,
                         log: Callable[[str], None] = print) -> int:
    """One SearchRestaurants call -> history rows. Same engine choice as the full scrape."""
    url = (cfg.get("search_url") or "").strip()
    if not url:
        raise ValueError("אין עדיין כתובת משלוח – הריצו רענון נתונים פעם אחת")
    headed = cfg.get("headed") if headed is None else headed
    fetcher = None
    if cfg.get("engine", "playwright") == "playwright":
        try:
            from auth import Browser
            fetcher = Browser(headed=bool(headed), log=log)
            await fetcher.__aenter__()
        except Exception as e:  # Playwright missing, or the profile is in use by another process
            log(f"Playwright unavailable ({e!s:.200}); snapshot via curl_cffi")
            fetcher = None
    if fetcher is None:
        fetcher = await scraper.CurlFetcher().__aenter__()
    try:
        status, data, final_url = await fetcher.get_json(url)
        if scraper.is_auth_failure(status, final_url, data) and fetcher.name == "playwright" \
                and await fetcher.try_auto_login(cfg.get("login_method", "otp"), None):
            status, data, final_url = await fetcher.get_json(url)
        if scraper.is_auth_failure(status, final_url, data):
            raise scraper.LoginRequired("נדרשת התחברות ל-10bis")
        if status != 200:
            raise RuntimeError(f"SearchRestaurants החזיר {status}")
    finally:
        await fetcher.__aexit__(None, None, None)
    n = append_snapshot(data)
    if not n:
        raise RuntimeError("לא נמצאו מסעדות בתשובה")
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description="restaurant-terms snapshot")
    ap.add_argument("--stats", action="store_true", help="print collection stats and exit")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not args.stats:
        try:
            n = asyncio.run(snapshot_async(scraper.load_config()))
        except scraper.LoginRequired as e:
            print(f"{e}. הריצו: python auth.py --login")
            sys.exit(2)
        print(f"snapshot: {n} restaurants")
    print(json.dumps(stats(), ensure_ascii=False))


if __name__ == "__main__":
    main()
