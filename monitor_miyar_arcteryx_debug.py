# -*- coding: utf-8 -*-
"""
混合抓取（更稳）：
1) 尝试 /products.json 分页；
2) 若不可用，回退抓取 /collections/all?page=N 的 HTML，解析其中的 /products/<handle>；
3) 对每个 handle 拉 /products/<handle>.js，做 Arc'teryx 过滤与变体级监控。

通知：
- 🔔 上新提醒 miyar
- 🔔 补货提醒 miyar（仅“缺货→到货”）
- 🔔 价格变化 miyar
- 文末带 🔗 直达链接，右侧缩略图（embed thumbnail）

日志：
- 抓取来源、数量、每步统计、snapshot 读写绝对路径
"""
import json, os, re, time, traceback
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Set
from urllib.parse import urljoin, urlparse

import requests

BASE = "https://store.miyaradventures.com/"
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
SNAPSHOT_PATH = os.environ.get("SNAPSHOT_PATH", "snapshot.json")
USER_AGENT = "Mozilla/5.0 (compatible; MiyarArcMonitor/1.1; +https://github.com)"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})

def log(msg: str):
    print(f"[DEBUG] {msg}", flush=True)

# ---------------- 数据模型 ----------------
@dataclass
class VariantState:
    id: int
    title: str
    option1: Optional[str]
    option2: Optional[str]
    option3: Optional[str]
    sku: Optional[str]
    price: float
    available: bool
    inventory_quantity: Optional[int]

@dataclass
class ProductState:
    handle: str
    title: str
    vendor: Optional[str]
    url: str
    image: Optional[str]
    variants: Dict[str, VariantState]

Snapshot = Dict[str, ProductState]

# ---------------- 工具函数 ----------------
def money_to_float(x) -> float:
    try:
        if x is None:
            return 0.0
        if isinstance(x, (int, float)):
            if isinstance(x, int) and x > 1000:
                return round(x / 100.0, 2)
            return float(x)
        s = str(x).strip().replace("$", "").replace(",", "")
        return round(float(s), 2)
    except Exception:
        return 0.0

def try_get(d, *keys, default=None):
    cur = d
    for k in keys:
        if isinstance(cur, dict):
            cur = cur.get(k)
        elif isinstance(cur, list) and isinstance(k, int):
            cur = cur[k] if 0 <= k < len(cur) else None
        else:
            return default
        if cur is None:
            return default
    return cur

def get_json(url: str, retries: int = 3, timeout: int = 20):
    for i in range(retries):
        try:
            r = SESSION.get(url, timeout=timeout)
            if r.status_code == 200:
                ct = (r.headers.get("Content-Type") or "")
                if "json" in ct or url.endswith(".js") or url.endswith(".json"):
                    return r.json()
                log(f"get_json warn content-type for {url}: {ct}")
                return None
            else:
                log(f"get_json {url} -> HTTP {r.status_code}")
                if r.status_code in (403, 404):
                    return None
        except Exception as e:
            log(f"get_json exception {url}: {e}")
        time.sleep(1.0 * (i + 1))
    return None

def get_text(url: str, retries: int = 3, timeout: int = 20) -> Optional[str]:
    for i in range(retries):
        try:
            r = SESSION.get(url, timeout=timeout)
            if r.status_code == 200:
                return r.text
            else:
                log(f"get_text {url} -> HTTP {r.status_code}")
                if r.status_code in (403, 404):
                    return None
        except Exception as e:
            log(f"get_text exception {url}: {e}")
        time.sleep(1.0 * (i + 1))
    return None

# ---------------- 抓取来源 A：products.json ----------------
def fetch_products_via_products_json(limit: int = 250) -> List[dict]:
    out = []
    page = 1
    while True:
        url = urljoin(BASE, f"/products.json?limit={limit}&page={page}")
        data = get_json(url)
        if not data or not data.get("products"):
            break
        out.extend(data["products"])
        log(f"/products.json page={page} -> {len(data['products'])} items")
        page += 1
        if page > 40:
            log("Stop at page>40 safety guard")
            break
        time.sleep(0.4)
    log(f"/products.json total: {len(out)}")
    return out

# ---------------- 抓取来源 B：collections/all HTML 爬取 ----------------
PRODUCT_LINK_RE = re.compile(r'href=["\'](/products/[^"\']+)["\']', re.IGNORECASE)

def find_product_handles_from_html(html: str) -> Set[str]:
    handles: Set[str] = set()
    for m in PRODUCT_LINK_RE.finditer(html or ""):
        path = urlparse(m.group(1)).path
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2 and parts[0].lower() == "products":
            handles.add(parts[1])
    return handles

def crawl_collections_all(max_pages: int = 50) -> List[str]:
    all_handles: Set[str] = set()
    for page in range(1, max_pages + 1):
        url = urljoin(BASE, f"/collections/all?page={page}")
        html = get_text(url)
        if not html:
            log(f"/collections/all?page={page} no html, stop.")
            break
        found = find_product_handles_from_html(html)
        log(f"/collections/all?page={page} found handles: {len(found)} (unique so far {len(all_handles|found)})")
        prev_size = len(all_handles)
        all_handles |= found
        if len(all_handles) == prev_size:
            if page > 1:
                log("no new handles on this page, stop.")
                break
        time.sleep(0.5)
    return sorted(all_handles)

# ---------------- normalize ----------------
def normalize_product_from_products_json(p: dict) -> Optional[ProductState]:
    handle = p.get("handle")
    if not handle:
        return None
    url = urljoin(BASE, f"/products/{handle}")
    image = try_get(p, "images", 0, "src")
    variants: Dict[str, VariantState] = {}
    for v in p.get("variants", []):
        vid = str(v.get("id"))
        variants[vid] = VariantState(
            id=int(v.get("id")),
            title=v.get("title") or "",
            option1=v.get("option1"),
            option2=v.get("option2"),
            option3=v.get("option3"),
            sku=v.get("sku"),
            price=money_to_float(v.get("price")),
            available=bool(v.get("available", False)),
            inventory_quantity=v.get("inventory_quantity") if isinstance(v.get("inventory_quantity"), int) else None,
        )
    return ProductState(
        handle=handle, title=p.get("title") or "", vendor=p.get("vendor"),
        url=url, image=image, variants=variants
    )

def normalize_product_from_js(p: dict) -> Optional[ProductState]:
    handle = p.get("handle")
    if not handle:
        return None
    url = p.get("url") or urljoin(BASE, f"/products/{handle}")
    image = try_get(p, "images", 0)
    variants: Dict[str, VariantState] = {}
    for v in p.get("variants", []):
        vid = str(v.get("id"))
        variants[vid] = VariantState(
            id=int(v.get("id")),
            title=v.get("title") or "",
            option1=v.get("option1"),
            option2=v.get("option2"),
            option3=v.get("option3"),
            sku=v.get("sku"),
            price=money_to_float(v.get("price")),
            available=bool(v.get("available", False)),
            inventory_quantity=v.get("inventory_quantity") if isinstance(v.get("inventory_quantity"), int) else None,
        )
    return ProductState(
        handle=handle, title=p.get("title") or "", vendor=p.get("vendor"),
        url=url, image=image, variants=variants
    )

# ---------------- 识别品牌 ----------------
def is_arcteryx(title: str, vendor: Optional[str], tags=None) -> bool:
    t, v = (title or "").lower(), (vendor or "").lower()
    if "arc'teryx" in v or "arcteryx" in v:
        return True
    if "arc'teryx" in t or "arcteryx" in t:
        return True
    if tags and any("arcteryx" in str(tag).lower() for tag in tags):
        return True
    return False

# ---------------- 快照 IO ----------------
def load_snapshot() -> Snapshot:
    abspath = os.path.abspath(SNAPSHOT_PATH)
    if not os.path.exists(SNAPSHOT_PATH):
        log(f"snapshot not found at {abspath} (first run expected)")
        return {}
    try:
        with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        snap: Snapshot = {}
        for handle, pdata in raw.items():
            variants = {vid: VariantState(**v) for vid, v in pdata["variants"].items()}
            snap[handle] = ProductState(
                handle=pdata["handle"], title=pdata["title"], vendor=pdata.get("vendor"),
                url=pdata["url"], image=pdata.get("image"), variants=variants
            )
        log(f"snapshot loaded from {abspath} with {len(snap)} products")
        return snap
    except Exception as e:
        log(f"load_snapshot error: {e}\n{traceback.format_exc()}")
        return {}

def save_snapshot(snap: Snapshot):
    try:
        serializable = {
            h: {
                "handle": p.handle,
                "title": p.title,
                "vendor": p.vendor,
                "url": p.url,
                "image": p.image,
                "variants": {vid: asdict(v) for vid, v in p.variants.items()},
            }
            for h, p in snap.items()
        }
        abspath = os.path.abspath(SNAPSHOT_PATH)
        log(f"writing snapshot to {abspath}")
        with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
        size = os.path.getsize(SNAPSHOT_PATH)
        log(f"snapshot written OK: {abspath} ({size} bytes)")
    except Exception as e:
        log(f"save_snapshot error: {e}\n{traceback.format_exc()}")

# ---------------- Discord（embed，miyar 文案 + 直达链接） ----------------
def send_embed(description: str, thumb: Optional[str]):
    if not DISCORD_WEBHOOK:
        log("[NO WEBHOOK] printing instead:\n" + description)
        return
    embed = {
        "title": "🔔 通知 miyar",
        "color": 0x2B65EC,
        "description": description.strip()
    }
    if thumb:
        embed["thumbnail"] = {"url": thumb}
    try:
        r = SESSION.post(DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=20)
        if r.status_code >= 300:
            log(f"Discord HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log(f"Discord error: {e}")

def format_inventory(p: ProductState) -> str:
    counts: Dict[str, int] = {}
    for v in p.variants.values():
        size = v.option2 or v.option1 or "N/A"
        qty = v.inventory_quantity if isinstance(v.inventory_quantity, int) else (1 if v.available else 0)
        counts[size] = counts.get(size, 0) + max(0, int(qty))
    return " | ".join(f"{k}:{v}" for k, v in counts.items()) or "无"

def link_line(p: ProductState) -> str:
    return f"🔗 [直达链接]({p.url})"

def desc_new(p: ProductState) -> str:
    anyv = next(iter(p.variants.values()))
    price_line = f"• 价格：CA$ {anyv.price:.0f}" if anyv.price == int(anyv.price) else f"• 价格：CA$ {anyv.price:.2f}"
    return (
        f"🔔 上新提醒 miyar\n"
        f"• 名称：{p.title}\n"
        f"• 货号：{anyv.sku or '未知'}\n"
        f"• 颜色：{anyv.option1 or '未知'}\n"
        f"{price_line}\n"
        f"🧾 库存信息：{format_inventory(p)}\n\n"
        f"{link_line(p)}\n\n"
        "（右侧商品缩略图）"
    )

def desc_restock(p: ProductState, v: VariantState) -> str:
    price_line = f"• 价格：CA$ {v.price:.0f}" if v.price == int(v.price) else f"• 价格：CA$ {v.price:.2f}"
    return (
        f"🔔 补货提醒 miyar\n"
        f"• 名称：{p.title}\n"
        f"• 货号：{v.sku or '未知'}\n"
        f"• 颜色：{v.option1 or '未知'}\n"
        f"{price_line}\n"
        f"🧾 库存信息：{v.option2 or 'N/A'}:{v.inventory_quantity if isinstance(v.inventory_quantity, int) else (1 if v.available else 0)}\n\n"
        f"{link_line(p)}\n\n"
        "（右侧商品缩略图）"
    )

def desc_price_change(p: ProductState, vold: VariantState, vnew: VariantState) -> str:
    return (
        f"🔔 价格变化 miyar\n"
        f"• 名称：{p.title}\n"
        f"• 货号：{vnew.sku or '未知'}\n"
        f"• 颜色：{vnew.option1 or '未知'}\n"
        f"• 价格：CA$ {vold.price:.2f} → CA$ {vnew.price:.2f}\n"
        f"🧾 库存信息：{vnew.option2 or 'N/A'}:{vnew.inventory_quantity if isinstance(vnew.inventory_quantity, int) else (1 if vnew.available else 0)}\n\n"
        f"{link_line(p)}\n\n"
        "（右侧商品缩略图）"
    )

# ---------------- 构建最新快照（混合抓取） ----------------
def build_snapshot() -> Snapshot:
    snap: Snapshot = {}

    # A. 先试 products.json
    products = fetch_products_via_products_json()
    if products:
        log("Use /products.json path")
        for p in products:
            if not is_arcteryx(p.get("title",""), p.get("vendor"), p.get("tags", [])):
                continue
            ps = normalize_product_from_products_json(p)
            if not ps:
                continue
            # 用 .js 精准补齐 available / inventory_quantity / image
            js = get_json(urljoin(BASE, f"/products/{ps.handle}.js"))
            if js:
                jsn = normalize_product_from_js(js)
                if jsn:
                    ps.image = jsn.image or ps.image
                    for vid, v in ps.variants.items():
                        if vid in jsn.variants:
                            jsv = jsn.variants[vid]
                            v.available = jsv.available
                            if isinstance(jsv.inventory_quantity, int):
                                v.inventory_quantity = jsv.inventory_quantity
            snap[ps.handle] = ps
        log(f"Snapshot via products.json: {len(snap)}")
        if snap:
            return snap

    # B. 回退：爬 collections/all
    log("Fallback to /collections/all crawl")
    handles = crawl_collections_all(max_pages=50)
    log(f"handles from collections/all: {len(handles)}")
    ok, skipped = 0, 0
    for i, h in enumerate(handles, 1):
        js = get_json(urljoin(BASE, f"/products/{h}.js"))
        if not js:
            skipped += 1
            continue
        if not is_arcteryx(js.get("title",""), js.get("vendor"), js.get("tags", [])):
            skipped += 1
            continue
        ps = normalize_product_from_js(js)
        if not ps or not ps.variants:
            skipped += 1
            continue
        snap[ps.handle] = ps
        ok += 1
        if i % 25 == 0:
            log(f"handled {i}/{len(handles)} -> ok={ok}, skipped={skipped}")
        time.sleep(0.25)
    log(f"Snapshot via collections: ok={ok}, skipped={skipped}, total={len(snap)}")
    return snap

# ---------------- Diff & 推送 ----------------
def diff_and_report(old: Snapshot, new: Snapshot):
    # 新商品
    for handle, p in new.items():
        if handle not in old:
            log(f"[NEW PRODUCT] {p.title} ({handle})")
            send_embed(desc_new(p), p.image)

    # 变体新增 / 价格变化 / 仅“缺货→到货” / 库存增加
    for handle, pnew in new.items():
        pold = old.get(handle)
        if not pold:
            continue

        # 新增变体 -> 作为上新
        for vid, vnew in pnew.variants.items():
            if vid not in pold.variants:
                log(f"[NEW VARIANT] {pnew.title} ({handle}) vid={vid}")
                send_embed(desc_new(pnew), pnew.image)

        for vid, vnew in pnew.variants.items():
            vold = pold.variants.get(vid)
            if not vold:
                continue

            # 价格变化
            if abs((vnew.price or 0) - (vold.price or 0)) > 1e-6:
                log(f"[PRICE] {pnew.title} ({handle}) {vold.price}->{vnew.price} (vid={vid})")
                send_embed(desc_price_change(pnew, vold, vnew), pnew.image)

            # 仅“缺货→到货”
            if (not bool(vold.available)) and bool(vnew.available):
                log(f"[RESTOCK] {pnew.title} ({handle}) vid={vid} now available")
                send_embed(desc_restock(pnew, vnew), pnew.image)

            # 库存数量增加
            if isinstance(vnew.inventory_quantity, int) and isinstance(vold.inventory_quantity, int):
                if vnew.inventory_quantity > vold.inventory_quantity:
                    log(f"[QTY UP] {pnew.title} ({handle}) vid={vid} {vold.inventory_quantity}->{vnew.inventory_quantity}")
                    send_embed(desc_restock(pnew, vnew), pnew.image)

# ---------------- 主入口 ----------------
def list_dir(label: str):
    try:
        print(f"[DEBUG] {label} | CWD={os.getcwd()} | files={os.listdir('.')}")
    except Exception as e:
        print(f"[DEBUG] list_dir error: {e}")

def main():
    log(f"START cwd={os.getcwd()}")
    log(f"ENV SNAPSHOT_PATH={SNAPSHOT_PATH} -> abs={os.path.abspath(SNAPSHOT_PATH)}")
    list_dir("BEFORE RUN")

    old = load_snapshot()
    new = build_snapshot()
    log(f"products found (new snapshot size) = {len(new)}")
    diff_and_report(old, new)

    # 一定写快照（首次必须落盘）
    save_snapshot(new)
    list_dir("AFTER SAVE")

    log(f"Done. Tracked: {len(new)} products")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FATAL: {e}\n{traceback.format_exc()}")
        raise
