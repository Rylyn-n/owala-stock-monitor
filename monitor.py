#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Owala 水壺庫存監控 —— GitHub Actions 版本

設計成「跑一次就結束」（run-once），不是常駐迴圈。
每次執行：
  1. 判斷商品目前是有貨還是售完
  2. 讀取 data/stock_status.json 取得上一次的狀態
  3. 只在「售完 → 有貨」這個轉折發 LINE 通知（有 alerted 旗標防止重複轟炸）
  4. 把結果寫回 JSON，由 workflow commit 回 repo

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
HISTORY_LIMIT = 500          # 歷史記錄保留筆數，避免 JSON 無限長大
REQUEST_TIMEOUT = 20
TPE = timezone(timedelta(hours=8))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9"}

SOLD_OUT_KEYWORDS = []

LINE_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_TO = os.environ.get("LINE_TO", "")


def now_iso():
    return datetime.now(TPE).isoformat(timespec="seconds")


def log(msg):
    print(f"[{now_iso()}] {msg}", flush=True)


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

    # 把巢狀結構攤平，找出所有 variant 物件
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
        # 不同 SHOPLINE 版本欄位名稱不一樣，能抓到哪個就用哪個
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

def check_via_html(url):
    """回傳 True(有貨) / False(售完)，抓不到頁面就丟例外。"""
    r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    html = r.text

    hits = [k for k in SOLD_OUT_KEYWORDS if k in html]
    if hits:
        log(f"HTML 出現售完訊號：{', '.join(hits)}")
        return False

    if "加入購物車" in html:
        log("HTML 只找到「加入購物車」，判定有貨")
        return True

    # 兩種訊號都沒有，很可能是頁面結構改了或被擋，當成售完比較安全
    log("HTML 找不到任何庫存訊號，保守判定為售完")
    return False


def detect():
    result = check_via_api(PRODUCT_URL)
    if result is None:
        result = check_via_html(PRODUCT_URL)
    return result


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

def load_state():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"current_status": "unknown", "last_checked": None,
                "alerted": False, "consecutive_errors": 0, "history": []}


def save_state(state):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    log(f"狀態已寫入 {DATA_FILE}")


# ---------------------------------------------------------------- 主流程

def main():
    state = load_state()
    prev = state.get("current_status", "unknown")

    try:
        in_stock = detect()
        status = "in_stock" if in_stock else "sold_out"
        state["consecutive_errors"] = 0
    except Exception as e:
        log(f"檢查失敗：{type(e).__name__}: {e}")
        status = "error"
        state["consecutive_errors"] = state.get("consecutive_errors", 0) + 1
        # 連續失敗 6 次（約 1 小時）才通知，避免單次網路抖動就吵人
        if state["consecutive_errors"] == 6:
            send_line("庫存監控連續失敗多次，可能是頁面結構改了或被擋，請確認。")

    log(f"上次：{prev} → 這次：{status}")

    # 只在「售完 → 有貨」的轉折通知；alerted 旗標在回到售完時歸零
    if status == "in_stock" and not state.get("alerted", False):
        if send_line("Owala Sway 30oz 高爾夫球款現在有貨了，手刀去結帳。"):
            state["alerted"] = True
    elif status == "sold_out":
        state["alerted"] = False

    ts = now_iso()
    if status != "error":
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
