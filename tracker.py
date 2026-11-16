#!/usr/bin/env python3
"""機票價格追蹤器：從 Google Flights 抓指定日期航線的最低價，記錄走勢並在降價時推播通知。

用法：
    python tracker.py                # 正常執行（抓價 + 通知 + 寫入紀錄）
    python tracker.py --dry-run      # 抓價並寫入，但通知只印在畫面上
    python tracker.py --mock         # 用假資料測試（不連網），通知只印在畫面上
    python tracker.py --test-notify  # 只發一則測試通知，確認手機收得到

通知管道（用環境變數設定，可同時開）：
    NTFY_TOPIC            ntfy 主題名稱（手機裝 ntfy App 訂閱同一個名稱）
    NTFY_SERVER           選填，預設 https://ntfy.sh
    TELEGRAM_BOT_TOKEN    Telegram 機器人 token
    TELEGRAM_CHAT_ID      你的 Telegram chat id
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "docs" / "data"
HISTORY_PATH = DATA_DIR / "history.json"
ALERTS_PATH = DATA_DIR / "alerts.json"
TAIPEI = timezone(timedelta(hours=8))
TOP_N = 8          # 每次記錄最便宜的前幾個航班
MAX_ALERTS = 100   # 通知紀錄最多保留幾則

ALERT_LOG: list[dict] = []


# ---------------------------------------------------------------------------
# 抓資料
# ---------------------------------------------------------------------------

def _get(obj, *names, default=None):
    """依序嘗試多個屬性名稱（不同版本的 fast-flights 欄位名稱略有不同）。"""
    for name in names:
        if obj is None:
            return default
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _parse_price(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(round(value))
    digits = re.sub(r"[^\d.]", "", str(value))
    try:
        return int(round(float(digits))) if digits else None
    except ValueError:
        return None


def _fmt_time(value) -> str:
    """把各種時間表示法轉成 'MM-DD HH:MM' 字串。"""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%m-%d %H:%M")
    d = _get(value, "date")
    t = _get(value, "time")
    if isinstance(t, (tuple, list)) and t:
        hh, mm = [int(v or 0) for v in (list(t) + [0, 0])[:2]]
        prefix = ""
        if isinstance(d, (tuple, list)) and len(d) >= 3:
            prefix = f"{int(d[1]):02d}-{int(d[2]):02d} "
        return f"{prefix}{hh:02d}:{mm:02d}"
    return str(value)


def _airport_code(value) -> str:
    code = _get(value, "code", "iata")
    return str(code) if code else (str(value) if value else "")


def normalize_flight(item) -> dict | None:
    price = _parse_price(_get(item, "price", "total_price"))
    if not price:
        return None
    airlines = _get(item, "airlines", "airline", "name", default=[])
    if isinstance(airlines, (list, tuple)):
        airlines = " / ".join(dict.fromkeys(str(a) for a in airlines))
    legs = _get(item, "flights", "legs", "segments", default=None) or []
    if not isinstance(legs, (list, tuple)):
        legs = []
    first, last = (legs[0], legs[-1]) if legs else (item, item)
    numbers = []
    for leg in legs:
        num = _get(leg, "flight_number", "number", "flight_no")
        code = _get(leg, "airline_code", "carrier")
        if num:
            numbers.append(f"{code or ''}{num}")
    return {
        "price": price,
        "airline": str(airlines or ""),
        "flight_no": " ".join(numbers),
        "dep": _fmt_time(_get(first, "departure", "departure_time", "depart")),
        "arr": _fmt_time(_get(last, "arrival", "arrival_time", "arrive")),
        "stops": max(len(legs) - 1, 0) if legs else _get(item, "stops"),
    }


def fetch_google_flights(route: dict, currency: str) -> list[dict]:
    from fast_flights import FlightQuery, Passengers, create_query, get_flights

    def leg(d, a, b):
        kw = dict(date=d, from_airport=a, to_airport=b)
        if route.get("max_stops") is not None:
            kw["max_stops"] = int(route["max_stops"])
        return FlightQuery(**kw)

    round_trip = route.get("trip") == "round-trip" and route.get("return_date")
    legs = [leg(route["date"], route["from"], route["to"])]
    if round_trip:
        legs.append(leg(route["return_date"], route["to"], route["from"]))
    query = create_query(
        flights=legs,
        trip="round-trip" if round_trip else "one-way",
        seat=route.get("seat", "economy"),
        passengers=Passengers(adults=int(route.get("adults", 1))),
        language="en-US",
        currency=currency,
    )
    try:  # faster-flights 可以要求 Google 依價格排序、回傳最便宜的完整列表
        from fast_flights import ShoppingOptions
        result = get_flights(query, shopping=ShoppingOptions(ranking_mode="cheapest", result_sort="price"))
    except (ImportError, TypeError):
        result = get_flights(query)
    if os.environ.get("TRACKER_DEBUG"):
        seq = result if isinstance(result, (list, tuple)) else _get(result, "flights", default=[]) or []
        print(f"[debug] result type={type(result).__name__} count={len(seq)} metadata={repr(_get(result, 'metadata'))[:600]}")
        for i, it in enumerate(list(seq)[:6]):
            legs = _get(it, "flights", default=[]) or []
            print(f"[debug] item{i}: price={_get(it, 'price')} airlines={_get(it, 'airlines')} "
                  f"route={'-'.join([_airport_code(_get(l, 'from_airport')) for l in legs] + [_airport_code(_get(legs[-1], 'to_airport'))] if legs else [])} "
                  f"leg0={repr(legs[0])[:300] if legs else ''}")
    items = _get(result, "flights", default=result)
    # 多段查詢時，有些版本回傳「每段一組結果」的 list，取第一段（價格為整趟總價）
    if isinstance(items, (list, tuple)) and items and _get(items[0], "price") is None \
            and _get(items[0], "flights") is not None:
        items = _get(items[0], "flights")
    flights = [f for f in (normalize_flight(i) for i in (items or [])) if f]
    if route.get("max_stops") == 0:
        flights = [f for f in flights if not f.get("stops")]
    flights.sort(key=lambda f: f["price"])
    return flights


def fetch_mock(route: dict, currency: str, prev_price: int | None) -> list[dict]:
    base = prev_price or 4200
    price = max(1800, int(base * random.uniform(0.9, 1.08)))
    airlines = ["Thai AirAsia", "Thai Lion Air", "Thai Vietjet", "EVA Air"]
    out = []
    for i, name in enumerate(airlines):
        out.append({
            "price": price + i * random.randint(150, 900),
            "airline": name,
            "flight_no": "",
            "dep": f"{route['date'][5:]} {2 + i * 4:02d}:45",
            "arr": f"{route['date'][5:]} {6 + i * 4:02d}:50",
            "stops": 0 if i < 3 else 1,
        })
    return sorted(out, key=lambda f: f["price"])


# ---------------------------------------------------------------------------
# 判斷要不要通知：回傳 [(類型, 說明), ...]
# ---------------------------------------------------------------------------

def money(n: int, currency: str = "TWD") -> str:
    return f"NT${n:,}" if currency == "TWD" else f"{currency} {n:,}"


def decide_alerts(route: dict, history: dict, current: dict) -> list[tuple[str, str]]:
    rules = route.get("alerts", {})
    points = [p for p in history.get("points", []) if p.get("min")]
    price = current["price"]
    cur = history.get("currency", "TWD")
    reasons: list[tuple[str, str]] = []

    if not points:
        return [("start", f"開始追蹤，目前最低 {money(price, cur)}")]

    last = points[-1]["min"]
    low = min(p["min"] for p in points)
    diff = last - price

    if rules.get("notify_new_low", True) and low - price >= rules.get("new_low_min_diff", 100):
        reasons.append(("new_low", f"歷史新低！比之前最低 {money(low, cur)} 再便宜 {money(low - price, cur)}"))

    drop_pct = rules.get("drop_pct")
    drop_abs = rules.get("drop_abs")
    if rules.get("notify_drop", True) and diff > 0:
        hit_pct = drop_pct is not None and diff / last * 100 >= drop_pct
        hit_abs = drop_abs is not None and diff >= drop_abs
        if hit_pct or hit_abs:
            reasons.append(("drop", f"比上次便宜 {money(diff, cur)}（-{diff / last * 100:.1f}%）"))

    target = rules.get("target_price")
    if target and price <= target < last:
        reasons.append(("target", f"跌破目標價 {money(target, cur)}"))

    rise = rules.get("notify_rise_pct")
    if rise and diff < 0 and -diff / last * 100 >= rise:
        reasons.append(("rise", f"漲價 {money(-diff, cur)}（+{-diff / last * 100:.1f}%），想買要趁早"))

    return reasons


# ---------------------------------------------------------------------------
# 通知
# ---------------------------------------------------------------------------

def _post_json(url: str, payload: dict) -> None:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def channels() -> dict:
    topic = os.environ.get("NTFY_TOPIC") or ""
    return {
        "ntfy": bool(topic),
        "ntfy_topic_hint": (topic[:6] + "••••") if topic else "",
        "telegram": bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")),
    }


def notify(title: str, message: str, click_url: str | None, dry_run: bool, *,
           kind: str = "info", route: dict | None = None, price: int | None = None) -> None:
    ALERT_LOG.append({
        "t": datetime.now(TAIPEI).isoformat(timespec="minutes"),
        "kind": kind, "title": title, "message": message, "price": price,
        "route_id": route["id"] if route else None,
        "route_name": route["name"] if route else None,
    })
    if dry_run:
        print(f"\n[通知（測試模式，未送出）] {title}\n{message}\n")
        return
    sent = False
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        payload = {"topic": topic, "title": title, "message": message,
                   "tags": ["airplane"], "priority": 4}
        if click_url:
            payload["click"] = click_url
        try:
            _post_json(server, payload)
            sent = True
        except Exception as e:  # noqa: BLE001
            print(f"ntfy 發送失敗：{e}", file=sys.stderr)
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        text = f"✈️ {title}\n{message}" + (f"\n{click_url}" if click_url else "")
        try:
            _post_json(f"https://api.telegram.org/bot{token}/sendMessage",
                       {"chat_id": chat, "text": text, "disable_web_page_preview": True})
            sent = True
        except Exception as e:  # noqa: BLE001
            print(f"Telegram 發送失敗：{e}", file=sys.stderr)
    if not sent:
        print(f"[沒有設定任何通知管道] {title}\n{message}")


def google_flights_url(route: dict) -> str:
    if route.get("trip") == "round-trip" and route.get("return_date"):
        q = (f"Flights from {route['from']} to {route['to']} on {route['date']} "
             f"returning {route['return_date']}")
    else:
        q = f"Flights from {route['from']} to {route['to']} on {route['date']} one way"
    return "https://www.google.com/travel/flights?q=" + urllib.parse.quote(q)


def page_url(route_id: str | None = None) -> str | None:
    repo = os.environ.get("GITHUB_REPOSITORY")  # 例如 owner/flight-price-tracker
    if not repo or "/" not in repo:
        return None
    owner, name = repo.split("/", 1)
    return f"https://{owner.lower()}.github.io/{name}/" + (f"#/route/{route_id}" if route_id else "")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default
    return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def flush_alerts() -> None:
    if not ALERT_LOG:
        return
    alerts = load_json(ALERTS_PATH, [])
    alerts = list(reversed(ALERT_LOG)) + alerts
    save_json(ALERTS_PATH, alerts[:MAX_ALERTS])


def run(args) -> int:
    config = load_json(CONFIG_PATH, None)
    if not config:
        print("找不到 config.json", file=sys.stderr)
        return 1
    currency = config.get("currency", "TWD")
    history = load_json(HISTORY_PATH, {"routes": {}})
    history.setdefault("routes", {})
    now = datetime.now(TAIPEI)
    today = now.date()
    dry_run = args.dry_run or args.mock
    exit_code = 0
    config_ids = {r["id"] for r in config["routes"]}

    # 從 config 刪掉的航線，紀錄也一併移除
    for rid in list(history["routes"]):
        if rid not in config_ids:
            del history["routes"][rid]

    for route in config["routes"]:
        rid = route["id"]
        rh = history["routes"].setdefault(rid, {"points": []})
        rh.update({
            "name": route["name"], "from": route["from"], "to": route["to"],
            "date": route["date"], "trip": route.get("trip", "one-way"),
            "return_date": route.get("return_date"),
            "adults": route.get("adults", 1), "max_stops": route.get("max_stops"),
            "currency": currency, "alerts": route.get("alerts", {}),
            "target_price": route.get("alerts", {}).get("target_price"),
            "google_url": google_flights_url(route),
            "active": route.get("active", True),
        })
        if date.fromisoformat(route["date"]) < today:
            rh["ended"] = True
            print(f"[{rid}] 出發日已過，略過")
            continue
        if not route.get("active", True):
            print(f"[{rid}] 已暫停，略過")
            continue

        prev = next((p["min"] for p in reversed(rh["points"]) if p.get("min")), None)
        try:
            flights = (fetch_mock(route, currency, prev) if args.mock
                       else fetch_google_flights(route, currency))
            if not flights:
                raise RuntimeError("沒有抓到任何航班")
        except Exception as e:  # noqa: BLE001
            exit_code = 2
            rh["error_streak"] = rh.get("error_streak", 0) + 1
            rh["last_error"] = f"{now.isoformat(timespec='minutes')} {type(e).__name__}: {e}"[:300]
            print(f"[{rid}] 抓取失敗：{e}", file=sys.stderr)
            limit = config.get("notify_on_error_after", 3)
            if limit and rh["error_streak"] == limit:
                notify(f"連續 {limit} 次抓不到資料", f"{route['name']}\n{rh['last_error']}",
                       page_url(rid), dry_run, kind="error", route=route)
            continue

        if rh.get("error_streak", 0) >= config.get("notify_on_error_after", 3):
            notify("已恢復抓取", f"{route['name']} 又抓得到價格了", page_url(rid), dry_run,
                   kind="recovered", route=route, price=flights[0]["price"])
        rh["error_streak"] = 0
        best = flights[0]
        reasons = decide_alerts(route, rh, best)
        rh["points"].append({
            "t": now.isoformat(timespec="minutes"),
            "min": best["price"],
            "airline": best["airline"],
            "dep": best["dep"],
            "count": len(flights),
        })
        rh["latest"] = flights[:TOP_N]
        print(f"[{rid}] 最低 {best['price']:,} {currency}（{best['airline']} {best['dep']}），"
              f"共 {len(flights)} 筆")

        if reasons:
            pax = route.get("adults", 1)
            kinds = [k for k, _ in reasons]
            kind = next((k for k in ("start", "new_low", "target", "drop", "rise") if k in kinds), kinds[0])
            headline = {"start": "開始追蹤", "new_low": f"歷史新低 {money(best['price'], currency)}",
                        "target": f"跌破目標價 {money(best['price'], currency)}",
                        "drop": f"降價 {money(best['price'], currency)}",
                        "rise": f"漲價 {money(best['price'], currency)}"}[kind]
            body = "\n".join(text for _, text in reasons) + (
                f"\n目前最低：{money(best['price'], currency)}"
                f"{'（' + str(pax) + ' 人總價）' if pax > 1 else ''}"
                f"\n{best['airline']} {best['dep']} → {best['arr']}"
            )
            notify(f"{route['name']}｜{headline}", body, page_url(rid) or rh["google_url"],
                   dry_run, kind=kind, route=route, price=best["price"])

    history["updated"] = now.isoformat(timespec="minutes")
    history["status"] = {
        "channels": channels(),
        "interval": config.get("interval_label", "每 6 小時"),
        "source": "Google Flights",
        "mock": bool(args.mock),
    }
    save_json(HISTORY_PATH, history)
    flush_alerts()
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description="Google Flights 機票價格追蹤")
    parser.add_argument("--dry-run", action="store_true", help="通知只印出、不送出")
    parser.add_argument("--mock", action="store_true", help="用假資料測試，不連網")
    parser.add_argument("--test-notify", action="store_true", help="發一則測試通知")
    args = parser.parse_args()
    if args.test_notify:
        notify("機票追蹤測試通知", "收到這則訊息代表通知設定成功 ✈️", page_url(), False, kind="test")
        flush_alerts()
        history = load_json(HISTORY_PATH, {"routes": {}})
        history.setdefault("status", {})["channels"] = channels()
        save_json(HISTORY_PATH, history)
        return 0
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
