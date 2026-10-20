"""Learn time-of-day rules from history rows (history.py).

Per restaurant and day group (Sun–Thu / Fri / Sat):
  1. grid: 30-min slot -> the value seen in that slot, per day
  2. a slot's value is the majority across days, kept only if seen on >= MIN_DAYS days
     and on >= AGREE of them (otherwise unknown)
  3. baseline = the restaurant's usual value; runs of slots that differ become rules
Pure functions over rows, no I/O.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Optional

SLOT = 30
SLOTS = 24 * 60 // SLOT
MIN_DAYS = 2
AGREE = 0.7
GROUPS = [("א׳–ה׳", (0, 1, 2, 3, 4)), ("שישי", (5,)), ("שבת", (6,))]
# Fields compared against their baseline. avail (open & taking orders) is handled separately.
FIELDS = ("fee", "discount_pct", "min_order", "eta", "disc_fee_min", "future")


def fmt(minute: int) -> str:
    return f"{minute // 60 % 24:02d}:{minute % 60:02d}"


def to_min(t: str) -> Optional[int]:
    try:
        h, m = t.split(":")
        return int(h) * 60 + int(m)
    except (AttributeError, ValueError):
        return None


def money(v: float) -> str:
    return f"₪{v:g}"


def derive(r: dict) -> dict:
    return {
        "avail": int(bool(r["open_now"]) and r["terminal"] != 0 and not r["temp_closed"]),
        "fee": r["fee"], "discount_pct": r["discount_pct"], "min_order": r["min_order"],
        "eta": r["eta"], "disc_fee_min": r["disc_fee_min"], "future": r["future"],
    }


def slot_majority(rows: list[dict]) -> dict[str, list]:
    """field -> per slot [value, support_days, observed_days] (value None = unknown)."""
    per_day: dict[tuple, dict] = {}   # (day, slot) -> derived values of the latest snapshot in it
    for r in rows:
        per_day[(r["day"], r["minute"] // SLOT)] = derive(r)
    by_slot: dict[int, list[dict]] = defaultdict(list)
    for (_, s), v in per_day.items():
        by_slot[s].append(v)
    out: dict[str, list] = {}
    for f in ("avail",) + FIELDS:
        col = []
        for s in range(SLOTS):
            vals = [v[f] for v in by_slot.get(s, []) if v[f] is not None]
            if len(vals) < MIN_DAYS:
                col.append([None, 0, len(vals)])
                continue
            val, n = Counter(vals).most_common(1)[0]
            col.append([val if n / len(vals) >= AGREE else None, n, len(vals)])
        out[f] = col
    return out


def runs(col: list, keep) -> list[tuple[int, int, Any, int]]:
    """Maximal runs of known slots whose value passes keep(); one unknown slot inside a run is
    bridged. Returns (first_slot, last_slot, value, min_support)."""
    out, cur = [], None
    for s, (v, n, _) in enumerate(col):
        if v is not None and keep(v):
            if cur and cur[2] == v and s - cur[1] <= 2:
                cur = [cur[0], s, v, min(cur[3], n)]
            else:
                if cur:
                    out.append(tuple(cur))
                cur = [s, s, v, n]
        elif v is not None and cur:
            out.append(tuple(cur))
            cur = None
    if cur:
        out.append(tuple(cur))
    return out


def when(a: int, b: int, first: int, last: int) -> tuple[str, str]:
    """Hebrew time phrase for slots a..b inside the known range first..last: (plain, negated)."""
    start, end = fmt(a * SLOT), fmt((b + 1) * SLOT)
    if a <= first and b < last:
        return f"לפני {end}", f"רק החל מ-{end}"
    if b >= last and a > first:
        return f"אחרי {start}", f"רק עד {start}"
    return f"בין {start}–{end}", f"לא בין {start}–{end}"


def rule(field: str, kind: str, text: str, a: int, b: int, support: int, group: tuple) -> dict:
    label, days = group
    return {"field": field, "kind": kind, "text": text, "from": a * SLOT, "to": (b + 1) * SLOT,
            "days": list(days), "days_label": label, "support": support}


def avail_rules(col: list, open_t: Optional[int], close_t: Optional[int], group: tuple) -> list[dict]:
    known = [s for s, (v, _, _) in enumerate(col) if v is not None]
    on = [s for s in known if col[s][0] == 1]
    if not on:
        return []
    out, first_on, last_on = [], on[0], on[-1]
    # Closed slots inside the declared hours, after/before/between the slots it was open.
    in_hours = lambda s: open_t is None or close_t is None or (
        open_t <= s * SLOT < close_t if open_t < close_t else s * SLOT >= open_t or s * SLOT < close_t)
    for a, b, _, n in runs(col, lambda v: v == 0):
        if not in_hours(a):
            continue
        if a > last_on:
            out.append(rule("avail", "stop", f"אין משלוחים אחרי {fmt(a * SLOT)}"
                            + (f" (לפי השעות פתוח עד {fmt(close_t)})" if close_t is not None else ""), a, b, n, group))
        elif b < first_on:
            out.append(rule("avail", "start", f"משלוחים בפועל רק מ-{fmt((b + 1) * SLOT)}"
                            + (f" (לפי השעות נפתח ב-{fmt(open_t)})" if open_t is not None else ""), a, b, n, group))
        else:
            out.append(rule("avail", "stop", f"לא מקבל הזמנות בין {fmt(a * SLOT)}–{fmt((b + 1) * SLOT)}", a, b, n, group))
    return out


def value_rules(field: str, col: list, avail: list, group: tuple) -> list[dict]:
    # Only judge slots where the restaurant is (or may be) open: closed-time values are noise.
    col = [c if avail[s][0] != 0 else [None, 0, 0] for s, c in enumerate(col)]
    known = [s for s, (v, _, _) in enumerate(col) if v is not None]
    if len(known) < 2:
        return []
    weights = Counter()
    for s in known:
        weights[col[s][0]] += 1
    base = weights.most_common(1)[0][0]
    first, last = known[0], known[-1]
    out = []
    for a, b, v, n in runs(col, lambda v: v != base):
        if a == first and b == last:
            continue   # no baseline to compare with
        plain, neg = when(a, b, first, last)
        if field == "fee":
            text, kind = (f"משלוח חינם {plain} (בד״כ {money(base)})", "free") if v == 0 else \
                (f"משלוח {money(v)} {plain} (בד״כ {money(base)})", "deal" if v < base else "info")
        elif field == "discount_pct":
            if v == 0:
                text = f"{base:g}% הנחה – {neg}" if neg.startswith("לא") else f"{base:g}% הנחה {neg}"
            else:
                text = f"{v:g}% הנחה {plain}" + (f" (בד״כ {base:g}%)" if base else "")
            kind = "deal"
        elif field == "min_order":
            text, kind = f"מינימום להזמנה {money(v)} {plain} (בד״כ {money(base)})", "info" if v > base else "deal"
        elif field == "eta":
            if abs(v - base) < 15:
                continue
            text, kind = f"זמן משלוח ~{v:g} דק׳ {plain} (בד״כ {base:g})", "info"
        elif field == "disc_fee_min":
            text = f"משלוח מוזל בהזמנה מעל {money(v)} {plain}" if v else f"בלי הנחת משלוח {plain}"
            kind = "deal" if v else "info"
        elif field == "future":
            text, kind = (f"אין הזמנה מראש {plain}", "stop") if not v else (f"הזמנה מראש אפשרית {plain}", "info")
        else:
            continue
        out.append(rule(field, kind, text, a, b, n, group))
    return out


def analyse(rows: list[dict]) -> dict:
    by_rest: dict[Any, list[dict]] = defaultdict(list)
    for r in rows:
        by_rest[r["restaurant_id"]].append(r)
    restaurants = []
    for rid, rr in by_rest.items():
        last = rr[-1]
        hours = Counter((r["open_time"], r["close_time"]) for r in rr).most_common(1)[0][0]
        open_t, close_t = to_min(hours[0]), to_min(hours[1])
        rules, timeline = [], {}
        for group in GROUPS:
            g_rows = [r for r in rr if r["weekday"] in group[1]]
            if len({r["day"] for r in g_rows}) < MIN_DAYS:
                continue
            maj = slot_majority(g_rows)
            rules += avail_rules(maj["avail"], open_t, close_t, group)
            for f in FIELDS:
                rules += value_rules(f, maj[f], maj["avail"], group)
            timeline[group[0]] = {f: [c[0] for c in maj[f]] for f in ("avail", "fee", "discount_pct", "min_order")}
        if rules:
            rules.sort(key=lambda x: (x["days"][0], x["from"]))
            restaurants.append({"id": rid, "name": last["name"], "logo": last["logo"],
                                "hours": f"{hours[0]}–{hours[1]}" if hours[0] else "",
                                "announcement": last["announcement"], "rules": rules, "timeline": timeline})
    restaurants.sort(key=lambda x: -max(r["support"] for r in x["rules"]))
    days = defaultdict(set)
    for r in rows:
        for label, wd in GROUPS:
            if r["weekday"] in wd:
                days[label].add(r["day"])
    return {"coverage": {"snapshots": len({r["ts"] for r in rows}), "days": len({r["day"] for r in rows}),
                         "days_by_group": {k: len(v) for k, v in days.items()},
                         "first": min((r["ts"] for r in rows), default=None),
                         "last": max((r["ts"] for r in rows), default=None),
                         "analysed": len(by_rest), "min_days": MIN_DAYS},
            "restaurants": restaurants}
