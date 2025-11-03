# main.py
import logging, json, os, requests, re
from time import time
from datetime import datetime, timezone, timedelta
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ===== DB backend (Turso ↔︎ SQLite) =====
from db_backend import db_init, db_upsert, db_get, db_list_spx_keys, db_purge_expired, CACHE_TTL

# ===== Logging =====
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== Constants =====
API_URL = "https://us-central1-get-feedback-a0119.cloudfunctions.net/app/api/shopee/getOrderDetailsForCookie"
SPX_API_URL = "https://spx.vn/shipment/order/open/order/get_order_info"
VN_TZ = timezone(timedelta(hours=7))

# Cache RAM: key -> {"items":[...], "meta": {...}, "ts": int}
PRODUCT_CACHE: dict[str, dict] = {}

# ===== UI helpers =====
def build_menu():
    keyboard = [
        ['/start Bắt đầu'],
        ['/help Trợ giúp'],
        ['/balance Xem số dư'],
        ['/buy Mua gửi thường viên'],
        ['/list Danh sách SPX'],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def vnd(n: int | float) -> str:
    try: return f"{int(n):,}".replace(",", ".") + "đ"
    except: return f"{n}đ"

def ts_to_vn(ts: int | float) -> str:
    try: return datetime.fromtimestamp(int(ts), VN_TZ).strftime("%H:%M:%S • %d/%m/%Y")
    except: return str(ts)

def short_addr(address_text: str, max_len: int = 90) -> str:
    if not address_text: return ""
    s = " ".join(address_text.split())
    return s if len(s) <= max_len else s[:max_len-1] + "…"

# ===== Cache orchestration (RAM + DB) =====
def cache_store_from_order(order: dict):
    items = order.get("product_info") or []
    if not items: return
    meta = {"address": order.get("address") or {}}
    entry = {"items": items, "meta": meta, "ts": int(time())}
    oid = order.get("order_id"); tn = order.get("tracking_number")
    # RAM
    if oid: PRODUCT_CACHE[oid] = entry
    if tn:  PRODUCT_CACHE[tn]  = entry
    # DB
    if oid: db_upsert(oid, items, entry["ts"], meta)
    if tn:  db_upsert(tn, items, entry["ts"], meta)

def cache_get_all(key: str):
    if not key: return {"items": None, "meta": None}
    e = PRODUCT_CACHE.get(key)
    if e and int(time()) - int(e["ts"]) <= CACHE_TTL:
        return {"items": e.get("items"), "meta": e.get("meta")}
    items, meta = db_get(key)
    if items:
        PRODUCT_CACHE[key] = {"items": items, "meta": meta or {}, "ts": int(time())}
    return {"items": items, "meta": meta}

def cache_get(key: str):
    return cache_get_all(key).get("items")

# ===== Commands =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"Xin chào {user.first_name}! Bot lấy order Shopee (cookie) & tra SPX.\n"
        "• Gửi cookie Shopee (SPC...) để lưu sản phẩm + nơi nhận.\n"
        "• Gửi mã SPX (SPXVN...) để xem timeline; nếu đã có cache sẽ hiện sản phẩm & nơi nhận.\n"
        "• /list để liệt kê SPX gần đây.",
        reply_markup=build_menu()
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start, /help, /balance, /buy, /confirm, /list\n"
        "Gửi cookie Shopee để mình lưu dữ liệu; từ đó tra SPX sẽ có tên SP & nơi nhận.",
        reply_markup=build_menu()
    )

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Số dư hiện tại của bạn: 1.000đ", reply_markup=build_menu())

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bạn muốn mua gói gửi thường viên? Giá: 500đ.\nGửi /confirm để xác nhận.", reply_markup=build_menu())

async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Mua thành công! Số dư đã trừ.", reply_markup=build_menu())

# ===== Shopee API =====
def call_shopee_api(cookie_str: str) -> dict:
    if not (cookie_str.startswith('SPC') or ';' in cookie_str or '=' in cookie_str):
        return {'error': 'Cookie không hợp lệ (phải chứa SPC, ; hoặc =).'}
    payload = {"cookies": [cookie_str.strip()]}
    try:
        r = requests.post(API_URL, json=payload, headers={'Content-Type':'application/json'}, timeout=10)
        logger.info(f"Shopee status={r.status_code} body[:200]={r.text[:200]}...")
        if r.status_code != 200:
            return {'error': f'Status {r.status_code}: {r.text[:200]}'}
        data = r.json()
        if 'allOrderDetails' not in data:
            return {'error': "Thiếu 'allOrderDetails' trong response."}
        return data
    except requests.RequestException as e:
        return {'error': str(e)}
    except ValueError:
        return {'error': 'Response không phải JSON'}

def parse_orders_from_api(data: dict) -> list:
    res = []
    for order in data.get('allOrderDetails', []):
        if order.get('data') and order['data'].get('error') == 'DeadCookie':
            res.append({'noOrder': True})
            continue
        for od in order.get('orderDetails', []):
            od_copy = od.copy()
            od_copy['cookie'] = order.get('cookie')
            res.append(od_copy)
            try: cache_store_from_order(od_copy)
            except Exception as err: logger.warning(f"cache error: {err}")
    return res

# ===== SPX API =====
def call_spx_api(tn: str) -> dict:
    try:
        r = requests.get(SPX_API_URL, params={"spx_tn": tn, "language_code": "vi"}, timeout=10)
        logger.info(f"SPX status={r.status_code} body[:200]={r.text[:200]}...")
        if r.status_code != 200: return {"error": f"SPX status {r.status_code}: {r.text[:120]}"}
        data = r.json()
        if data.get("retcode") != 0: return {"error": f"SPX retcode {data.get('retcode')}: {data.get('message')}"}
        return data
    except requests.RequestException as e:
        return {"error": str(e)}
    except ValueError:
        return {"error": "Response SPX không phải JSON"}

def format_spx_timeline(spx_json: dict) -> str:
    try:
        info = spx_json["data"]["sls_tracking_info"]
        tn = info.get("sls_tn") or ""
        client_order_id = info.get("client_order_id") or ""
        recs = info.get("records") or []
    except Exception:
        return "❌ Không đọc được dữ liệu SPX."

    recs_sorted = sorted(recs, key=lambda r: r.get("actual_time", 0), reverse=True)
    lines = [f"📦 **SPX: {tn}**" + (f"\n🆔 Đơn hàng: {client_order_id}" if client_order_id else "")]
    for r in recs_sorted[:8]:
        when = ts_to_vn(r.get("actual_time", 0))
        desc = (r.get("buyer_description") or r.get("description") or "").strip()
        loc = (r.get("current_location") or {}).get("location_name") or ""
        lines.append(f"• {when}\n  {desc}" + (f" — _{loc}_" if loc else ""))
    if not recs_sorted: lines.append("Không có cập nhật trạng thái.")
    return "\n".join(lines)

def get_latest_spx_status(spx_code: str) -> tuple[str, str]:
    data = call_spx_api(spx_code)
    if "error" in data: return ("—", "")
    try:
        recs = data["data"]["sls_tracking_info"].get("records") or []
        if not recs: return ("—", "")
        last = max(recs, key=lambda r: r.get("actual_time", 0))
        desc = (last.get("buyer_description") or last.get("description") or "").strip() or "—"
        when = ts_to_vn(last.get("actual_time", 0))
        return (desc, when)
    except Exception:
        return ("—", "")

# ===== Text handler =====
async def handle_input_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    # 1) SPX code
    spx_match = re.search(r"\bSPXVN[A-Z0-9]{8,}\b", text, re.IGNORECASE)
    if spx_match:
        spx_tn = spx_match.group(0).upper()
        await update.message.reply_text(f"🔎 Đang tra SPX: {spx_tn} ...")
        spx_data = call_spx_api(spx_tn)
        if "error" in spx_data:
            await update.message.reply_text(f"❌ Lỗi SPX: {spx_data['error']}", reply_markup=build_menu()); return

        timeline = format_spx_timeline(spx_data)

        # map sang sản phẩm + nơi nhận
        info = spx_data.get("data", {}).get("sls_tracking_info", {})
        client_order_id = info.get("client_order_id") or ""
        sls_tn = info.get("sls_tn") or ""

        cached = {"items": None, "meta": None}
        for key in (client_order_id, sls_tn, spx_tn):
            if key:
                cached = cache_get_all(key)
                if cached.get("items"): break

        items = cached.get("items") or []
        meta = cached.get("meta") or {}
        addr = (meta.get("address") or {})
        who = " • ".join([x for x in [addr.get("shipping_name") or "", addr.get("shipping_phone") or ""] if x])
        where = short_addr(addr.get("shipping_address") or "")

        if items:
            lines = [timeline, "\n🛒 **SẢN PHẨM**"]
            for i, p in enumerate(items[:3], 1):
                name = p.get("name") or "N/A"
                model = p.get("model_name") or "—"
                amount = p.get("amount", 1) or 1
                raw = p.get("order_price", 0)
                if isinstance(raw, (int, float)):
                    unit = raw//100_000 if raw>1_000_000_000 else (raw//100 if raw>10_000 else raw)
                else:
                    unit = 0
                price_txt = f"{amount}×{vnd(unit)}" if unit else f"x{amount}"
                lines.append(f"{i}. {name} ({model}) — {price_txt}")
            if who or where:
                lines += ["\n📍 **NƠI NHẬN**", who if who else "", where if where else ""]
            await update.message.reply_text("\n".join([x for x in lines if x]), reply_markup=build_menu())
        else:
            await update.message.reply_text(
                f"{timeline}\n\nℹ️ Chưa có sản phẩm/nơi nhận cho mã này.\n"
                "👉 Gửi cookie Shopee (SPC...) của đơn tương ứng để mình lưu, lần sau tra SPX sẽ hiện đầy đủ.",
                reply_markup=build_menu()
            )
        return

    # 2) Cookie Shopee
    if ';' in text or text.startswith('SPC'):
        await update.message.reply_text("🔄 Đang gọi API Shopee...")
        data = call_shopee_api(text)
        if 'error' in data:
            await update.message.reply_text(f"❌ Lỗi API: {data['error']}", reply_markup=build_menu()); return
        orders = parse_orders_from_api(data)
        if not orders:
            await update.message.reply_text("Không có order details từ API. Thử cookie khác!", reply_markup=build_menu()); return

        od = orders[0]
        if od.get("tracking_number") == "Đang chờ":
            await update.message.reply_text("❌ Tài khoản đã bị cấm hoặc cookie hết hạn.", reply_markup=build_menu()); return
        if od.get("noOrder"):
            await update.message.reply_text("❌ DeadCookie - Cookie hết hạn.", reply_markup=build_menu()); return

        try: db_purge_expired()
        except Exception as err: logger.warning(f"purge error: {err}")

        # Render gọn kết quả chính
        lines = []
        status = od.get('tracking_info_description', 'Đơn hàng đang trong quá trình vận chuyển')
        order_id = od.get('order_id', 'N/A')
        order_time = od.get('order_time') or "—"
        lines += [f"Tình trạng: {status}", f"Mã đơn hàng: {order_id}", f"Thời gian đặt hàng: {order_time}\n"]

        addr = od.get('address', {}) or {}
        name = addr.get('shipping_name', 'N/A')
        phone = addr.get('shipping_phone', 'N/A')
        if isinstance(phone, str) and phone.startswith('84') and len(phone) > 2: phone = f"(+84) {phone[2:]}"
        address = addr.get('shipping_address', 'N/A')
        lines += ["📦 ĐỊA CHỈ NHẬN HÀNG", name, phone, address, ""]

        p = (od.get('product_info') or [{}])[0]
        pname = p.get('name', 'N/A'); model = p.get('model_name', 'N/A')
        item_id = p.get('item_id', ''); shop_id = p.get('shop_id', '')
        link = f"https://shopee.vn/product/{shop_id}/{item_id}" if item_id and shop_id else 'N/A'
        lines += ["🛍 SẢN PHẨM 1", f"Tên sản phẩm: {pname}", f"Phân loại: {model}", f"Liên kết: {link}", ""]

        carrier = "SPX Express" if (od.get('tracking_number') or "").startswith('SPXVN') else 'N/A'
        ship_method = od.get('shipping_method') or "Nhanh (Thanh toán khi nhận hàng)"
        tracking = od.get('tracking_number', 'N/A')
        lines += ["🚚 ĐƠN VỊ VẬN CHUYỂN", ship_method, f"Đơn vị vận chuyển: {carrier}", f"Mã vận đơn: {tracking}", f"Thông tin: {status}", ""]

        amount = p.get('amount', 1) or 1
        raw = p.get('order_price', 0)
        if isinstance(raw, (int, float)):
            unit = raw//100_000 if raw>1_000_000_000 else (raw//100 if raw>10_000 else raw)
        else:
            unit = 0
        total = int(unit) * amount
        lines.append(f"💵 Vui lòng thanh toán {vnd(total)} khi nhận hàng")
        lines.append("\nGửi cookie khác hoặc nhập mã SPX để kiểm tra!")

        await update.message.reply_text("\n".join(lines), reply_markup=build_menu())
        return

    # 3) fallback
    await update.message.reply_text("Vui lòng gửi cookie Shopee hoặc mã SPX (SPXVN...)", reply_markup=build_menu())

# ===== /list =====
async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    spx_keys = db_list_spx_keys(limit=50)
    if not spx_keys:
        cutoff = int(time()) - CACHE_TTL
        spx_keys = [k for k, v in PRODUCT_CACHE.items() if k.startswith("SPXVN") and int(v.get("ts",0)) >= cutoff][:50]
    if not spx_keys:
        await update.message.reply_text("Chưa có SPX nào trong cache. Gửi cookie Shopee trước, rồi tra SPX.", reply_markup=build_menu()); return

    lines, max_rows = ["📋 **Danh sách SPX gần đây** (tối đa 50)\n"], 20
    for idx, spx in enumerate(spx_keys):
        if idx >= max_rows:
            lines.append(f"\n… và {len(spx_keys) - max_rows} mã khác"); break

        cached = cache_get_all(spx)
        items = cached.get("items") or []
        meta = cached.get("meta") or {}
        addr = meta.get("address") or {}
        name = (items[0].get("name") or "N/A").strip() if items else "N/A"
        who = " • ".join([x for x in [addr.get("shipping_name") or "", addr.get("shipping_phone") or ""] if x])
        where = short_addr(addr.get("shipping_address") or "")

        status, when = get_latest_spx_status(spx)
        when_txt = f" — {when}" if when else ""

        lines += [f"• {spx}", f"  🛒 {name}", f"  🟢 {status}{when_txt}"]
        if who or where:
            lines.append(f"  📍 {who}".rstrip())
            if where: lines.append(f"     {where}")
        lines.append("")

    out = "\n".join(lines)
    if len(out) > 3800: out = out[:3800] + "\n…(đã rút gọn)"
    await update.message.reply_text(out, reply_markup=build_menu())
