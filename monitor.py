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

偵測策略（前一層失敗才往下走），每個款式分開判斷，任一款有貨就算有貨：
  第 1 層：頁面內嵌的 SHOPLINE 商品 JSON（app.value('product', JSON.parse(...))）
  第 2 層：JSON-LD 的 offers.availability
  第 3 層：SHOPLINE /api/product/products.json
  都抓不到 → 記為 error（不再用「貨到通知我」關鍵字，因為它是模板固定文字）
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


# ------------------------------------------------------ 款式（variation）解析
#
# 商品現在有多個款式（例如 4 個顏色），只要「其中任一個」有貨就算有貨。
# 每個款式各自判斷，結果存成 [{"name": ..., "in_stock": True/False}, ...]

LANG_KEYS = ["zh-hant", "zh-TW", "zh_tw", "zh", "en"]

# 只想盯特定款式時，在 workflow 設 WATCH_VARIANTS="關鍵字1,關鍵字2"；留空 = 全部款式
WATCH_VARIANTS = [k.strip() for k in os.environ.get("WATCH_VARIANTS", "").split(",") if k.strip()]


def pick_translation(t):
    if isinstance(t, str):
        return t
    if isinstance(t, dict):
        for k in LANG_KEYS:
            if t.get(k):
                return t[k]
        for v in t.values():
            if isinstance(v, str) and v:
                return v
    return None


def variant_name(v, idx):
    parts = []
    ft = v.get("fields_translations")
    if isinstance(ft, dict):
        names = None
        for k in LANG_KEYS:
            if isinstance(ft.get(k), list):
                names = ft[k]
                break
        if names is None:
            names = next((x for x in ft.values() if isinstance(x, list)), [])
        parts = [str(n) for n in names if n]
    if not parts and isinstance(v.get("fields"), list):
        for f in v["fields"]:
            if isinstance(f, dict):
                n = pick_translation(f.get("name_translations")) or f.get("name")
                if n:
                    parts.append(str(n))
    if not parts:
        n = pick_translation(v.get("title_translations")) or v.get("title") or v.get("name")
        if n:
            parts.append(str(n))
    return " / ".join(parts) or v.get("sku") or f"款式 {idx + 1}"


def variant_in_stock(v):
    """True/False；看不到任何庫存欄位回傳 None。"""
    if v.get("unlimited_quantity") is True:
        return True
    if v.get("out_of_stock_orderable") is True:      # 允許缺貨下單（預購）
        return True
    for key in ("total_orderable_quantity", "quantity", "stock", "inventory_quantity"):
        q = v.get(key)
        if isinstance(q, (int, float)):
            return q > 0
    if isinstance(v.get("available"), bool):
        return v["available"]
    if isinstance(v.get("sold_out"), bool):
        return not v["sold_out"]
    return None


def variants_from_product(p):
    """從 SHOPLINE 商品物件取出每個款式的庫存。抓不到回傳 None。"""
    if not isinstance(p, dict):
        return None
    raw = p.get("variations") or p.get("variants") or []
    out = []
    for i, v in enumerate(raw):
        if not isinstance(v, dict):
            continue
        s = variant_in_stock(v)
        if s is not None:
            out.append({"name": variant_name(v, i), "in_stock": s})
    if out:
        return out
    # 沒有款式的單一商品：直接看商品本身
    s = variant_in_stock(p)
    if s is not None:
        name = pick_translation(p.get("title_translations")) or p.get("title") or "商品"
        return [{"name": str(name), "in_stock": s}]
    return None


def find_products(obj, depth=0):
    """在任意 JSON 裡找出看起來像商品（含 variations）的物件。"""
    if depth > 6:
        return
    if isinstance(obj, dict):
        if isinstance(obj.get("variations"), list) or isinstance(obj.get("variants"), list):
            yield obj
        for v in obj.values():
            yield from find_products(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from find_products(v, depth + 1)


# ---------------------------------------------------------- 第 1 層：頁面內嵌 JSON

JS_PARSE_RE = re.compile(r"JSON\.parse\(\s*'((?:\\.|[^'\\])*)'\s*\)", re.S)
JS_PARSE_RE2 = re.compile(r'JSON\.parse\(\s*"((?:\\.|[^"\\])*)"\s*\)', re.S)


def unescape_js(s):
    s = s.replace("\\'", "'")
    try:
        return json.loads('"' + s.replace('"', '\\"').replace('\\\\"', '\\"') + '"')
    except Exception:
        try:
            return s.encode("utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8")
        except Exception:
            return None


def check_via_embedded(html):
    """SHOPLINE 會把商品資料用 app.value('product', JSON.parse('...')) 塞在頁面裡。"""
    if not html:
        return None
    candidates = []
    for rx in (JS_PARSE_RE, JS_PARSE_RE2):
        for m in rx.finditer(html):
            body = m.group(1)
            if "variation" not in body and "quantity" not in body:
                continue
            text = unescape_js(body)
            if not text:
                continue
            try:
                candidates.append(json.loads(text))
            except Exception:
                continue
    # 也試 <script type="application/json"> 之類的資料區塊
    for m in re.finditer(r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>', html, re.S):
        try:
            candidates.append(json.loads(m.group(1)))
        except Exception:
            pass

    for c in candidates:
        for p in find_products(c):
            vs = variants_from_product(p)
            if vs:
                log(f"頁面內嵌資料找到 {len(vs)} 個款式")
                return vs
    log("頁面內嵌資料找不到款式庫存")
    return None


# ---------------------------------------------------------- 第 2 層：JSON-LD

def check_via_jsonld(html):
    """schema.org 的 offers.availability，每個款式一個 offer。"""
    if not html:
        return None
    out = []
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            if not isinstance(it, dict):
                continue
            offers = it.get("offers")
            if isinstance(offers, dict):
                offers = offers.get("offers") or [offers]
            if not isinstance(offers, list):
                continue
            for i, o in enumerate(offers):
                if not isinstance(o, dict) or "availability" not in o:
                    continue
                av = str(o["availability"])
                name = o.get("name") or o.get("sku") or it.get("name") or f"款式 {i + 1}"
                out.append({"name": str(name),
                            "in_stock": ("InStock" in av or "PreOrder" in av or "LimitedAvailability" in av)})
    if out:
        log(f"JSON-LD 找到 {len(out)} 個款式")
        return out
    return None


# ---------------------------------------------------------- 第 3 層：API

def check_via_api(url):
    m = re.match(r"(https?://[^/]+)/products/([^/?#]+)", url)
    if not m:
        return None
    base, handle = m.group(1), m.group(2)
    api = f"{base}/api/product/products.json?handle={handle}"
    try:
        r = requests.get(api, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            log(f"API 回應 {r.status_code}")
            return None
        data = r.json()
    except Exception as e:
        log(f"API 讀取失敗（{type(e).__name__}）")
        return None
    for p in find_products(data):
        vs = variants_from_product(p)
        if vs:
            log(f"API 找到 {len(vs)} 個款式")
            return vs
    log("API 有回應但看不到款式庫存")
    return None


def summarize(variants):
    """套用 WATCH_VARIANTS 篩選，回傳 (是否有貨, 有貨款式名稱清單)。"""
    watched = variants
    if WATCH_VARIANTS:
        watched = [v for v in variants if any(k in v["name"] for k in WATCH_VARIANTS)]
        if not watched:
            log(f"WATCH_VARIANTS={WATCH_VARIANTS} 沒對到任何款式，改看全部")
            watched = variants
    for v in variants:
        log(f"  {'✓ 有貨' if v['in_stock'] else '✗ 售完'}  {v['name']}")
    names = [v["name"] for v in watched if v["in_stock"]]
    return bool(names), names


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
    "variants": [],
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
    variants = None
    in_stock_names = []

    try:
        variants = (check_via_embedded(html)
                    or check_via_jsonld(html)
                    or check_via_api(PRODUCT_URL))
        if variants is None:
            # 注意：「貨到通知我」「售完」這些字是 SHOPLINE 頁面模板裡固定就有的，
            # 不論有沒有貨都會出現，所以不能拿來判斷，直接當成錯誤處理。
            raise RuntimeError("找不到任何款式庫存資料，可能是頁面結構改了")
        result, in_stock_names = summarize(variants)
        status = "in_stock" if result else "sold_out"
        state["consecutive_errors"] = 0
    except Exception as e:
        log(f"檢查失敗：{type(e).__name__}: {e}")
        status = "error"
        state["consecutive_errors"] = state.get("consecutive_errors", 0) + 1
        if state["consecutive_errors"] == 6:
            send_line("庫存監控連續失敗多次，可能是頁面結構改了或被擋，請確認。")

    if variants:
        state["variants"] = variants

    log(f"上次：{prev} → 這次：{status}")
    ts = now_iso()

    # 商品資訊：抓得到就更新，抓不到保留舊的
    info = extract_product(html)
    if info:
        state["product"] = {**state.get("product", {}), **info}

    # 通知：只在「售完 → 有貨」的轉折發一次
    if status == "in_stock" and not state.get("alerted", False):
        msg = "Owala Sway 30oz 高爾夫球款有貨了：" + "、".join(in_stock_names)
        if send_line(msg):
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
