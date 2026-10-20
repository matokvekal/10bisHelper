"""10bis dish scraper.

Fetches the restaurant list (SearchRestaurants URL from config.json) and every
restaurant's menu, flattens them into one record per dish and writes
data.json (+ data.xlsx) atomically.

CLI:  python scraper.py [--engine playwright|curl] [--headed] [--limit N]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import paths

BASE_DIR = paths.APP_DIR
CONFIG_PATH = BASE_DIR / "config.json"
DATA_PATH = BASE_DIR / "data.json"
XLSX_PATH = BASE_DIR / "data.xlsx"
DEBUG_DIR = BASE_DIR / "debug"

SITE = "https://www.10bis.co.il"
MENU_URL = SITE + "/NextApi/GetRestaurantMenu?culture=he-IL&uiCulture=he&restaurantId={id}&deliveryMethod=delivery"
# Route taken from the 10bis SPA bundle:
#   /restaurants/menu/delivery/:restaurantId/:restaurantSlug/dish/:dishId
# Per-restaurant delivery rules for the user's address (public, no login): which delivery types are
# active now, ASAP minutes, when the shared ("Pool") delivery arrives, and today's scheduled slots.
DETAILS_URL = "https://api.10bis.co.il/api/v1/Restaurants/{id}?addressId={addressId}&longitude={longitude}&latitude={latitude}"
ORDER_URL = SITE +"/next/restaurants/menu/delivery/{id}/{slug}/dish/{dish_id}"
MENU_PAGE_URL = SITE + "/next/restaurants/menu/delivery/{id}/{slug}"

REQUEST_INTERVAL = 0.7   # seconds between request starts (global)
MAX_CONCURRENCY = 3
RETRIES = 2
DEBUG_MENU_DUMPS = 3     # raw menu dumps kept in /debug (plus every failed one)

DEFAULT_CONFIG: dict[str, Any] = {
    "search_url": "",
    "engine": "playwright",        # playwright | curl
    "login_method": "otp",         # otp | password | manual
    "headed": False,
    "auto_refresh_hours": 0,       # 0 = off
    # Restaurant-terms snapshots (history.py) for learning time-of-day patterns.
    "snapshot_minutes": 30,        # 0 = off
    "snapshot_from": "08:00",
    "snapshot_to": "23:00",
    # Restaurant-level limits, applied before menus are fetched. 0/empty = no limit.
    "max_distance_km": 0,
    "max_delivery_minutes": 0,
    "max_delivery_fee": 0,
    "only_open_now": False,
    "synonyms": "בורגר=המבורגר\nסושי=רול\nשווארמה=שוורמה=שאורמה",
}

ProgressCb = Callable[[int, int, str], None]
OtpProvider = Callable[[], Awaitable[str]]


class LoginRequired(Exception):
    """The site answered 401/403 or redirected to the login page."""


# ---------------------------------------------------------------- config

def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    atomic_write_text(CONFIG_PATH, json.dumps(cfg, ensure_ascii=False, indent=2))


def atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def dump_debug(name: str, data: Any) -> None:
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=1)
        (DEBUG_DIR / name).write_text(text, encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------- defensive field access

def pick(d: Any, *names: str, default: Any = None) -> Any:
    """First non-None value among several possible key names (case-insensitive)."""
    if not isinstance(d, dict):
        return default
    lower = {k.lower(): k for k in d}
    for n in names:
        k = lower.get(n.lower())
        if k is not None and d[k] is not None:
            return d[k]
    return default


def find_list(data: Any, *names: str) -> list:
    """Find a list under any of `names`, searching nested dicts (Data, data, ...)."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    v = pick(data, *names)
    if isinstance(v, list):
        return v
    for child in data.values():
        if isinstance(child, dict):
            found = find_list(child, *names)
            if found:
                return found
    return []


def num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f.is_integer() else round(f, 2)


def slugify(name: str) -> str:
    # Same as the site's own slug function (module 26025 in the SPA bundle).
    s = name.strip().replace(" ", "-")
    s = re.sub(r"[^\-\d\wא-ת]", "", s, flags=re.ASCII)
    return s.lower()


def hhmm(v: Any) -> str:
    m = re.search(r"(\d{1,2}):(\d{2})", str(v or ""))
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""


def in_window(t: str, start: str, end: str) -> bool:
    if not (t and start and end):
        return False
    if start == end:
        return True
    if start < end:
        return start <= t < end
    return t >= start or t < end      # crosses midnight, e.g. 18:00-02:00


# ---------------------------------------------------------------- address / limits

def parse_address(url: str, restaurants: Optional[list] = None) -> str:
    """Human-readable address from the SearchRestaurants URL params.

    The URL normally carries only ids and coordinates, so when no street/city
    name is present the city is taken from the restaurants closest to the user.
    """
    from collections import Counter
    from urllib.parse import parse_qs, urlparse
    q = {k.lower(): v[0] for k, v in parse_qs(urlparse(url or "").query).items() if v}
    street = q.get("streetname") or q.get("street") or ""
    city = q.get("cityname") or q.get("city") or ""
    house = q.get("housenumber", "")
    house = "" if house in ("0", "") or not street else house
    if not city and restaurants:
        near = sorted((r for r in restaurants if r.get("distance_km") is not None),
                      key=lambda r: r["distance_km"])[:15]
        cities = Counter(r["city"] for r in near if r.get("city"))
        city = cities.most_common(1)[0][0] if cities else ""
    parts = [" ".join(p for p in (street, house) if p), city]
    text = ", ".join(p for p in parts if p)
    if not text and q.get("latitude") and q.get("longitude"):
        text = f"{q['latitude']}, {q['longitude']}"
    return text


def apply_limits(restaurants: list[dict], cfg: dict) -> list[dict]:
    """Restaurant-level filters; a limit is skipped when the API gave no value for it."""
    max_km = num(cfg.get("max_distance_km")) or 0
    max_min = num(cfg.get("max_delivery_minutes")) or 0
    max_fee = num(cfg.get("max_delivery_fee")) or 0
    only_open = bool(cfg.get("only_open_now"))
    out = []
    for r in restaurants:
        if max_km and r["distance_km"] is not None and r["distance_km"] > max_km:
            continue
        if max_min and r["eta_min"] is not None and r["eta_min"] > max_min:
            continue
        if max_fee and r["delivery_fee"] > max_fee:
            continue
        if only_open and not r["open_now"]:
            continue
        out.append(r)
    return out


# ---------------------------------------------------------------- parsing

def _city_from_address(addr: Any) -> str:
    """'החרושת, 27 אור יהודה' -> 'אור יהודה'."""
    m = re.match(r"^.*?,\s*\d+\S*\s+(.+)$", str(addr or ""))
    return m.group(1).strip() if m else ""


def parse_restaurants(data: Any) -> list[dict]:
    out = []
    for r in find_list(data, "restaurantsList", "restaurants", "RestaurantsList", "items"):
        rid = pick(r, "restaurantId", "id", "RestaurantId")
        if rid is None:
            continue
        if pick(r, "isDeliveryEnabled", "deliveryEnabled", "isDelivery", default=True) is False:
            continue
        if pick(r, "isActive", default=True) is False:
            continue
        name = pick(r, "restaurantName", "name", "RestaurantName", default="")
        loc = pick(r, "localizationNames", default={}) or {}
        cuisines = pick(r, "restaurantCuisineTypes", "cuisineTypes", default={})
        if isinstance(cuisines, dict):
            cuisine = ", ".join(str(v) for v in cuisines.values())
        elif isinstance(cuisines, list):
            cuisine = ", ".join(str(v) for v in cuisines)
        else:
            cuisine = str(pick(r, "cuisineList", "cuisine", default="") or "")

        promo_parts, discount_pct = [], 0.0
        pct = num(pick(r, "discountPercent", "discountPercentage", default=0)) or 0
        tags = pick(r, "tags", default=[]) or []
        coupon = next((t for t in tags if isinstance(t, dict)
                       and str(pick(t, "type", default="")).lower() == "discountcoupon"), None)
        coupon_min = num(pick(coupon, "minimumPriceForDiscount", default=0)) or 0 if coupon else 0
        if pct:
            if coupon_min:
                promo_parts.append(f"{pct:g}% הנחה בהזמנה מעל {coupon_min:g} ₪")
            else:
                promo_parts.append(f"{pct:g}% הנחה")
                discount_pct = float(pct)
        disc_fee = num(pick(r, "discountedDeliveryFee", default=0)) or 0
        disc_fee_min = num(pick(r, "minOrderTotalForDeliveryFeeDiscount", default=0)) or 0
        if disc_fee_min:
            promo_parts.append(f"משלוח {disc_fee:g} ₪ בהזמנה מעל {disc_fee_min:g} ₪")

        start = hhmm(pick(r, "openTime", "deliveryStartTime", "startTime"))
        end = hhmm(pick(r, "closeTime", "deliveryEndTime", "endTime"))
        open_now = bool(pick(r, "isOpenNow", "openNow", "isOpen", default=False))
        # restaurant not active for deliveries now: terminal off or temp-closed
        if pick(r, "isTerminalActive", default=True) is False or r.get("tempClosedReason"):
            open_now = False
        now = datetime.now().strftime("%H:%M")
        out.append({
            "restaurant_id": rid,
            "restaurant_name": name,
            "restaurant_name_en": pick(loc, "en", default="") or "",
            "restaurant_logo": pick(r, "restaurantLogoUrl", "logoUrl", "logo", default=""),
            "cuisine": cuisine,
            "kosher": (lambda k: None if k is None else bool(k))(pick(r, "isKosher", "kosher")),
            "city": pick(r, "restaurantCityName", "cityName", default="") or _city_from_address(pick(r, "restaurantAddress", default="")),
            "distance_km": (lambda m: round(m / 1000, 2) if m is not None else None)(
                num(pick(r, "distanceFromUser", "distance"))),
            "eta_min": num(pick(r, "deliveryTimeInMinutes", "deliveryTime")),
            "rating": num(pick(r, "reviewsRankDecimal", "reviewsRank")),
            "review_count": num(pick(r, "numOfReviews", default=0)) or 0,
            "delivery_fee": num(pick(r, "deliveryFeeAmount", "deliveryFee", "deliveryPrice", default=0)) or 0,
            "min_order": num(pick(r, "minimumPriceForOrder", "minimumForASAPOrder", "minimumOrder", default=0)) or 0,
            "delivery_from": start,
            "delivery_to": end,
            "open_now": open_now,
            # Closed although now is inside today's hours -> closed for the day.
            "closed_today": (not open_now) and in_window(now, start, end),
            # "Pool" = shared office delivery only (order until closeTime, delivered together at
            # lunch); its open/close hours are that ordering window, not regular delivery.
            "delivery_mode": str(pick(r, "defaultDeliveryRuleType", default="") or ""),
            "pool_min": num(pick(r, "minimumForPooledOrder", default=0)) or 0,
            "closed_reason": str(pick(r, "tempClosedReason", default="") or ""),
            "discount_pct": discount_pct,
            "promo": " · ".join(promo_parts),
            # Free text; often holds time/day rules ("משלוחים מ-12:00", "בימי שישי...").
            "announcement": str(pick(r, "announcement", default="") or "").strip(),
            "slug": slugify(name),
        })
    return out


def parse_menu(data: Any, rest: dict) -> list[dict]:
    dishes: dict[Any, dict] = {}
    for cat in find_list(data, "categoriesList", "categories", "menuCategories"):
        cat_name = pick(cat, "categoryName", "name", default="") or ""
        popular = str(pick(cat, "categoryType", default="")).lower() == "popular"
        for d in find_list(cat, "dishList", "dishes", "items"):
            did = pick(d, "dishId", "id")
            if did is None:
                continue
            if did in dishes:
                # Dish already seen in the "popular" category: prefer the real one.
                if not popular and dishes[did]["_popular"]:
                    dishes[did]["category"] = cat_name
                    dishes[did]["_popular"] = False
                continue
            base = num(pick(d, "dishPrice", "price", "Price", default=0)) or 0
            if base <= 0:      # notes / "scan me" placeholders, not real dishes
                continue
            orig = num(pick(d, "originalPrice", "priceBeforeDiscount", "fullPrice"))
            price, promo = base, rest["promo"]
            if orig and orig > base:
                promo = promo or "מבצע"
            elif rest["discount_pct"]:
                orig = base
                price = num(round(base * (1 - rest["discount_pct"] / 100), 2))
            else:
                orig = None
            dishes[did] = {
                "restaurant_id": rest["restaurant_id"],
                "restaurant_name": rest["restaurant_name"],
                "restaurant_logo": rest["restaurant_logo"],
                "cuisine": rest["cuisine"],
                "kosher": rest["kosher"],
                "dish_id": did,
                "dish_name": (pick(d, "dishName", "name", default="") or "").strip(),
                "description": (pick(d, "dishDescription", "description", default="") or "").strip(),
                "category": cat_name,
                "image_url": pick(d, "dishImageUrl", "imageUrl", "image", default="") or "",
                "price": price,
                "original_price": orig,
                "promo": promo,
                "announcement": rest["announcement"],
                "delivery_fee": rest["delivery_fee"],
                "min_order": rest["min_order"],
                "delivery_from": rest["delivery_from"],
                "delivery_to": rest["delivery_to"],
                "open_now": rest["open_now"],
                "eta_min": rest["eta_min"],
                "distance_km": rest["distance_km"],
                "rating": rest["rating"],
                "review_count": rest["review_count"],
                "closed_today": rest["closed_today"],
                "delivery_mode": rest["delivery_mode"],
                "pool_min": rest["pool_min"],
                "closed_reason": rest["closed_reason"],
                "order_url": ORDER_URL.format(id=rest["restaurant_id"], slug=rest["slug"] or "r", dish_id=did),
                "_popular": popular,
            }
    for v in dishes.values():
        del v["_popular"]
    return list(dishes.values())


def parse_details(data: Any) -> dict:
    """Arrival-relevant facts from api.10bis.co.il/api/v1/Restaurants/{id}."""
    d = data.get("Data", data) if isinstance(data, dict) else {}
    rules = [r for r in d.get("deliveryRules") or [] if isinstance(r, dict)]
    active = {str(r.get("type")) for r in rules if r.get("isActiveNow")}
    pt = d.get("poolDeliveryTime") or {}
    h, m = int(pt.get("hours") or 0), int(pt.get("minutes") or 0)
    today = datetime.now().strftime("%d/%m/%Y")
    slots = []
    for day in d.get("availableFutureDatesAndTimes") or []:
        if day.get("date") == today:
            for label in (day.get("timesWithTimeRanges") or {}).values():
                mt = re.match(r"\s*(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", str(label or ""))
                if mt:
                    slots.append([mt[1].zfill(5), mt[2].zfill(5)])
    asap = next((r for r in rules if r.get("type") == "Asap"), {})
    free_over = num(asap.get("minimumOrderForDeliveryFeeDiscount"))
    return {
        "asap_now": "Asap" in active,
        "pool_now": "Pool" in active,
        "asap_min": num(d.get("asapDeliveryTimeInMinutes")),
        "pool_at": f"{h:02d}:{m:02d}" if (h or m) else "",
        "slots": slots,
        # e.g. Arcaffe: ₪8 delivery, free from ₪100
        "free_over": free_over if free_over and asap.get("deliveryFeeAfterDiscount") == 0 else None,
    }


# ---------------------------------------------------------------- fetchers

class RateLimiter:
    def __init__(self, interval: float):
        self.interval, self.last, self.lock = interval, 0.0, asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            delay = self.last + self.interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self.last = time.monotonic()


class CurlFetcher:
    """curl_cffi with a Chrome TLS fingerprint. No login support."""

    name = "curl"

    async def __aenter__(self):
        from curl_cffi.requests import AsyncSession
        self.session = AsyncSession(impersonate="chrome", timeout=30)
        return self

    async def __aexit__(self, *exc):
        await self.session.close()

    async def get_json(self, url: str) -> tuple[int, Any, str]:
        r = await self.session.get(url, allow_redirects=True)
        try:
            data = r.json()
        except ValueError:
            data = r.text
        return r.status_code, data, str(r.url)


def is_auth_failure(status: int, final_url: str, data: Any) -> bool:
    return status in (401, 403) or "login" in final_url.lower() or (
        isinstance(data, str) and "<html" in data[:500].lower() and "login" in data.lower())


# ---------------------------------------------------------------- scrape

async def scrape_async(cfg: dict, progress: Optional[ProgressCb] = None,
                       otp_provider: Optional[OtpProvider] = None,
                       headed: Optional[bool] = None, limit: int = 0,
                       log: Callable[[str], None] = print) -> dict:
    url = (cfg.get("search_url") or "").strip()
    if not url:
        raise ValueError("חסר קישור SearchRestaurants – יש להדביק אותו בהגדרות")
    progress = progress or (lambda d, t, n: None)
    headed = cfg.get("headed") if headed is None else headed

    fetcher = None
    if cfg.get("engine", "playwright") == "playwright":
        try:
            from auth import Browser
            fetcher = Browser(headed=bool(headed), log=log)
            await fetcher.__aenter__()
        except Exception as e:  # Playwright missing / browser not installed
            log(f"Playwright unavailable ({e!s:.200}); falling back to curl_cffi")
            fetcher = None
    if fetcher is None:
        fetcher = await CurlFetcher().__aenter__()

    try:
        progress(0, 0, "טוען רשימת מסעדות…")
        status, data, final_url = await fetcher.get_json(url)
        if is_auth_failure(status, final_url, data):
            relogged = False
            if fetcher.name == "playwright":
                relogged = await fetcher.try_auto_login(cfg.get("login_method", "otp"), otp_provider)
                if relogged:
                    status, data, final_url = await fetcher.get_json(url)
            if not relogged or is_auth_failure(status, final_url, data):
                raise LoginRequired("נדרשת התחברות ל-10bis")
        dump_debug("search_raw.json", data)
        if status != 200:
            raise RuntimeError(f"SearchRestaurants החזיר {status}")
        restaurants = parse_restaurants(data)
        try:  # the restaurant list is also a free history snapshot
            import history
            history.append_snapshot(data)
        except Exception as e:
            log(f"history snapshot skipped: {e}")
        if not restaurants:
            raise RuntimeError("לא נמצאו מסעדות – ייתכן שהקישור לא תקין או שמבנה התשובה השתנה (ראו debug/search_raw.json)")
        address = parse_address(url, restaurants)
        found = len(restaurants)
        restaurants = apply_limits(restaurants, cfg)
        log(f"{found} delivery restaurants at '{address}', {len(restaurants)} after limits")
        if not restaurants:
            raise RuntimeError(f"נמצאו {found} מסעדות, אך אף אחת לא עומדת במגבלות ההגדרות – הרחיבו אותן")
        if limit:
            restaurants = restaurants[:limit]

        total, done, errors = len(restaurants), 0, 0
        all_dishes: list[dict] = []
        limiter, sem = RateLimiter(REQUEST_INTERVAL), asyncio.Semaphore(MAX_CONCURRENCY)
        progress(0, total, "")

        async def one(i: int, rest: dict) -> None:
            nonlocal done, errors
            async with sem:
                last_err = ""
                for attempt in range(RETRIES + 1):
                    await limiter.wait()
                    try:
                        st, menu, fu = await fetcher.get_json(MENU_URL.format(id=rest["restaurant_id"]))
                        if is_auth_failure(st, fu, menu):
                            raise LoginRequired("נדרשת התחברות ל-10bis")
                        if st != 200 or not isinstance(menu, dict):
                            raise RuntimeError(f"HTTP {st}")
                        if i < DEBUG_MENU_DUMPS:
                            dump_debug(f"menu_{rest['restaurant_id']}.json", menu)
                        all_dishes.extend(parse_menu(menu, rest))
                        last_err = ""
                        break
                    except LoginRequired:
                        raise
                    except Exception as e:
                        last_err = f"{e!s:.200}"
                        await asyncio.sleep(1.5 * (attempt + 1))
                if last_err:
                    errors += 1
                    log(f"menu {rest['restaurant_id']} ({rest['restaurant_name']}) failed: {last_err}")
                    if fetcher.name == "playwright":
                        await fetcher.debug_snapshot(f"fail_{rest['restaurant_id']}")
                done += 1
                progress(done, total, rest["restaurant_name"])

        from urllib.parse import parse_qs, urlparse
        q = {k.lower(): v[0] for k, v in parse_qs(urlparse(url).query).items() if v}
        addr = {k: q.get(k.lower(), "") for k in ("addressId", "longitude", "latitude")}
        details: dict[str, dict] = {}
        det_limiter, det_sem = RateLimiter(REQUEST_INTERVAL), asyncio.Semaphore(MAX_CONCURRENCY)

        async def details_one(det: Any, rest: dict) -> None:
            # Nice-to-have (arrival times): one try, never fails the scrape.
            async with det_sem:
                await det_limiter.wait()
                try:
                    r = await det.get(DETAILS_URL.format(id=rest["restaurant_id"], **addr))
                    if r.status_code == 200:
                        details[str(rest["restaurant_id"])] = parse_details(r.json())
                except Exception as e:
                    log(f"details {rest['restaurant_id']} failed: {e!s:.120}")

        async def all_details() -> None:
            if not all(addr.values()):
                return
            async with CurlFetcher() as det:
                await asyncio.gather(*(details_one(det.session, r) for r in restaurants))

        await asyncio.gather(asyncio.gather(*(one(i, r) for i, r in enumerate(restaurants))), all_details())
    finally:
        await fetcher.__aexit__(None, None, None)

    if not all_dishes:
        raise RuntimeError("לא נאספו מנות – הנתונים הקודמים נשמרו")
    meta = {
        "last_updated": datetime.now().isoformat(timespec="seconds"),
        "restaurant_count": len({d["restaurant_id"] for d in all_dishes}),
        "restaurants_scanned": total,
        "restaurants_in_area": found,
        "address": address,
        "dish_count": len(all_dishes),
        "error_count": errors,
        "engine": fetcher.name,
        "details_count": len(details),
    }
    save_data(meta, all_dishes, details)
    return meta


def save_data(meta: dict, dishes: list[dict], restaurants: Optional[dict] = None) -> None:
    atomic_write_text(DATA_PATH, json.dumps({"meta": meta, "dishes": dishes, "restaurants": restaurants or {}},
                                            ensure_ascii=False, separators=(",", ":")))
    try:
        import pandas as pd
        tmp = XLSX_PATH.with_name("data.tmp.xlsx")
        pd.DataFrame(dishes).to_excel(tmp, index=False)
        os.replace(tmp, XLSX_PATH)
    except Exception as e:  # xlsx is a bonus; never fail the scrape for it
        print(f"xlsx export skipped: {e}")


def load_data() -> dict:
    if DATA_PATH.exists():
        return json.loads(DATA_PATH.read_text(encoding="utf-8"))
    return {"meta": {}, "dishes": []}


def main() -> None:
    ap = argparse.ArgumentParser(description="10bis dish scraper")
    ap.add_argument("--engine", choices=["playwright", "curl"])
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--limit", type=int, default=0, help="only the first N restaurants (testing)")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    cfg = load_config()
    if args.engine:
        cfg["engine"] = args.engine

    async def cli_otp() -> str:
        return (await asyncio.to_thread(input, "הקלד/י את הקוד שקיבלת: ")).strip()

    def cli_progress(d: int, t: int, name: str) -> None:
        if t:
            print(f"\r{d}/{t}  {name[:40]:<40}", end="", flush=True)

    try:
        meta = asyncio.run(scrape_async(cfg, cli_progress, cli_otp,
                                        headed=args.headed or None, limit=args.limit))
    except LoginRequired as e:
        print(f"\n{e}. הריצו: python auth.py --login")
        sys.exit(2)
    print("\n", json.dumps(meta, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
