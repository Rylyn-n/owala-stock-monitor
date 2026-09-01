#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Owala 水壺庫存監控 —— GitHub Actions 版本

設計成「跑一次就結束」（run-once），不是常駐迴圈。
每次執行：
  1. 判斷商品目前是有貨還是售完
  2. 順便抓商品標題、縮圖、價格
  3. 只在「售完 → 有貨」這個轉折發 LINE 通知（alerted 旗標防止重複轟炸）
  4. 更新累計統計（總檢查次數、有貨次數、上次有貨區間）
  5. 把結果寫回 data/stock_status.json，由 workflow commit 回 repo

偵測策略（由準到粗，前一層失敗才往下走）：
  第 1 層：SHOPLINE 內部 JSON API，直接讀 variant 的庫存欄位
  第 2 層：HTML 關鍵字 —— 「貨到通知我」出現 = 售完
"""

import os
import sys
import json
import re
from datetime import datetime, timezone, timedelta

import requests

# ---------------------------------------------------------------- 設定

PRODUCT_URL = os.environ.get(
    "PRODUCT_URL",
    "https://www.finders.com.tw/products/owala-sway-30oz-golf",
)
DATA_FILE = os.path.join(os.path.dirname(__file__), "data", "stock_status.json")
HISTORY_LIMIT = 1000         # 約一週份（每 10 分鐘一次）
REQUEST_TIMEOUT = 20
TPE = timezone(timedelta(hours=8))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9"}

SOLD_OUT_KEYWORDS = ["貨到通知我", "補貨通知", "已售完", "售完"]

LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_TO = os.environ.get("LINE_TO", "")


def now_iso():
    return datetime.now(TPE).isoformat(timespec="seconds")


def log(msg):
    print(f"[{now_iso()}] {msg}", flush=True)


# ------------------------------------------------------------ 抓取頁面

def fetch_html(url):
    """回傳 HTML 字串；失敗回傳 None（不中斷流程，庫存還能靠 API 判斷）。"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return r.text
    except Exception as e:
        log(f"頁面讀取失敗：{type(e).__name__}: {e}")
        return None


def meta_content(html, key):
    """抓 <meta property/name="key" content="...">，兩種屬性順序都試。"""
    patterns = [
        rf'<meta[^>]+(?:property|name)=["\']{re.escape(key)}["\'][^>]*content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']{re.escape(key)}["\']',
    ]
    for p in patterns:
        m = re.search(p, html, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def extract_product(html):
    """從 HTML 取出商品標題、縮圖、價格。抓不到的欄位就留空。"""
    if not html:
        return {}

    info = {}
    title = meta_content(html, "og:title")
    if title:
        info["title"] = title

    image = meta_content(html, "og:image")
    if image:
        if image.startswith("//"):
            image = "https:" + image
        info["image"] = image

    price = meta_content(html, "product:price:amount")
    if not price:
        # 退而求其次，從 JSON-LD 的 offers 裡找
        m = re.search(r'"price"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?', html)
        if m:
            price = m.group(1)
    if price:
        try:
            info["price"] = int(float(price))
        except ValueError:
            info["price"] = price

    currency = meta_content(html, "product:price:currency")
    info["currency"] = currency or "TWD"

    log(f"商品資訊：{info.get('title', '（無標題）')} / "
        f"{info.get('price', '（無價格）')} / "
        f"{'有縮圖' if info.get('image') else '無縮圖'}")
    return info


# ---------------------------------------------------------- 第 1 層：API

def check_via_api(url):
    """
    SHOPLINE 站台通常有 /api/product/products.json?handle=xxx 這個端點。
    回傳 True(有貨) / False(售完) / None(這層判斷不出來)
    """
    m = re.match(r"(https?://[^/]+)/products/([^/?#]+)", url)
    if not m:
        return None
    base, handle = m.group(1), m.group(2)
    api = f"{base}/api/product/products.json?handle={handle}"

    try:
        r = requests.get(api, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            log(f"API 回應 {r.status_code}，改用關鍵字判斷")
            return None
        data = r.json()
    except Exception as e:
        log(f"API 讀取失敗（{type(e).__name__}），改用關鍵字判斷")
        return None

    items = data.get("items") or data.get("data") or data
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return None

    variants = []
    for it in items:
        if isinstance(it, dict):
            variants.extend(it.get("variations") or it.get("variants") or [it])

    available = None
    for v in variants:
        if not isinstance(v, dict):
            continue
        if v.get("out_of_stock_orderable") is True:
            available = True
        elif isinstance(v.get("available"), bool):
            available = available or v["available"]
        elif isinstance(v.get("quantity"), (int, float)):
            available = available or v["quantity"] > 0
        elif isinstance(v.get("stock"), (int, float)):
            available = available or v["stock"] > 0

    if available is None:
        log("API 有回應但看不到庫存欄位，改用關鍵字判斷")
        return None

    log(f"API 判斷結果：{'有貨' if available else '售完'}")
    return bool(available)


# ------------------------------------------------------ 第 2 層：關鍵字

def check_via_html(html):
    hits = [k for k in SOLD_OUT_KEYWORDS if k in html]
    if hits:
        log(f"HTML 出現售完訊號：{', '.join(hits)}")
        return False
    if "加入購物車" in html:
        log("HTML 只找到「加入購物車」，判定有貨")
        return True
    log("HTML 找不到任何庫存訊號，保守判定為售完")
    return False


# ---------------------------------------------------------------- LINE

def send_line(text):
    if not LINE_TOKEN or not LINE_TO:
        log("缺少 LINE_CHANNEL_ACCESS_TOKEN 或 LINE_TO，略過通知")
        return False

    payload = {
        "to": LINE_TO,
        "messages": [{
            "type": "flex",
            "altText": text,
            "contents": {
                "type": "bubble",
                "body": {
                    "type": "box", "layout": "vertical", "spacing": "md",
                    "contents": [
                        {"type": "text", "text": "補貨了", "weight": "bold",
                         "size": "xl", "color": "#1F7A5A"},
                        {"type": "text", "text": text, "wrap": True, "size": "sm"},
                        {"type": "text", "text": now_iso(), "size": "xs",
                         "color": "#999999"},
                    ],
                },
                "footer": {
                    "type": "box", "layout": "vertical",
                    "contents": [{
                        "type": "button", "style": "primary", "color": "#1F7A5A",
                        "action": {"type": "uri", "label": "前往商品頁",
                                   "uri": PRODUCT_URL},
                    }],
                },
            },
        }],
    }

    try:
        r = requests.post(
            "https://api.line.me/v2/bot/message/push",
            headers={"Authorization": f"Bearer {LINE_TOKEN}",
                     "Content-Type": "application/json"},
            json=payload, timeout=REQUEST_TIMEOUT,
        )
        if r.status_code == 200:
            log("LINE 通知已送出")
            return True
        log(f"LINE 通知失敗 {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log(f"LINE 通知發送例外：{type(e).__name__}: {e}")
    return False


# ---------------------------------------------------------------- 狀態

DEFAULT_STATE = {
    "current_status": "unknown",
    "last_checked": None,
    "alerted": False,
    "consecutive_errors": 0,
    "product_url": PRODUCT_URL,
    "product": {},
    "stats": {
        "total_checks": 0,
        "in_stock_checks": 0,
        "in_stock_events": 0,
        "status_since": None,
        "last_in_stock_start": None,
        "last_in_stock_end": None,
    },
    "history": [],
}


def load_state():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return json.loads(json.dumps(DEFAULT_STATE))

    # 舊版檔案沒有 stats / product，補上預設值
    for key, val in DEFAULT_STATE.items():
        state.setdefault(key, val)
    for key, val in DEFAULT_STATE["stats"].items():
        state["stats"].setdefault(key, val)
    return state


def save_state(state):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    log(f"狀態已寫入 {DATA_FILE}")


# ---------------------------------------------------------------- 主流程

def main():
    state = load_state()
    prev = state.get("current_status", "unknown")
    stats = state["stats"]

    html = fetch_html(PRODUCT_URL)

    try:
        result = check_via_api(PRODUCT_URL)
        if result is None:
            if html is None:
                raise RuntimeError("API 與頁面都讀取失敗")
            result = check_via_html(html)
        status = "in_stock" if result else "sold_out"
        state["consecutive_errors"] = 0
    except Exception as e:
        log(f"檢查失敗：{type(e).__name__}: {e}")
        status = "error"
        state["consecutive_errors"] = state.get("consecutive_errors", 0) + 1
        if state["consecutive_errors"] == 6:
            send_line("庫存監控連續失敗多次，可能是頁面結構改了或被擋，請確認。")

    log(f"上次：{prev} → 這次：{status}")
    ts = now_iso()

    # 商品資訊：抓得到就更新，抓不到保留舊的
    info = extract_product(html)
    if info:
        state["product"] = {**state.get("product", {}), **info}

    # 通知：只在「售完 → 有貨」的轉折發一次
    if status == "in_stock" and not state.get("alerted", False):
        if send_line("Owala Sway 30oz 高爾夫球款現在有貨了，手刀去結帳。"):
            state["alerted"] = True
    elif status == "sold_out":
        state["alerted"] = False

    # 累計統計
    if status != "error":
        stats["total_checks"] = stats.get("total_checks", 0) + 1
        if status == "in_stock":
            stats["in_stock_checks"] = stats.get("in_stock_checks", 0) + 1

        if status != prev:
            stats["status_since"] = ts
            if status == "in_stock":
                stats["in_stock_events"] = stats.get("in_stock_events", 0) + 1
                stats["last_in_stock_start"] = ts
                stats["last_in_stock_end"] = None
            elif prev == "in_stock":
                stats["last_in_stock_end"] = ts
        elif not stats.get("status_since"):
            stats["status_since"] = ts

        state["current_status"] = status

    state["last_checked"] = ts
    state["product_url"] = PRODUCT_URL

    history = state.get("history", [])
    history.insert(0, {"time": ts, "status": status})
    state["history"] = history[:HISTORY_LIMIT]

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
