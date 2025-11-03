import logging
import json
import os
import sqlite3
import requests
import re
from time import time
from datetime import datetime, timezone, timedelta
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ================== Logging ==================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ================== Constants ==================
API_URL = "https://us-central1-get-feedback-a0119.cloudfunctions.net/app/api/shopee/getOrderDetailsForCookie"
SPX_API_URL = "https://spx.vn/shipment/order/open/order/get_order_info"
VN_TZ = timezone(timedelta(hours=7))

# Cache trong RAM
PRODUCT_CACHE = {}  # key -> {"items": [...], "meta": {...}, "ts": int}
CACHE_TTL = 3 * 24 * 3600  # 3 ngày

# SQLite
DB_PATH = os.path.join(os.getcwd(), "orders.db")

# ================== UI Helpers ==================
def build_menu():
    keyboard = [
        ['/start Bắt đầu'],
        ['/help Trợ giúp'],
        ['/balance Xem số dư'],
        ['/buy Mua gửi thường viên'],
        ['/list Danh sách SPX']
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_emoji(key):
    emojis = {
        'ma_don_hang': '🆔', 'ngay_dat': '📅', 'dia_chi': '📍',
        'san_pham': '🛒', 'ma_van_don': '📦', 'van_chuyen': '🚚', 'trang_thai': '📊'
    }
    return emojis.get(key, '❓')

def vnd(n: int | float) -> str:
    try:
        return f"{int(n):,}".replace(",", ".") + "đ"
    except Exception:
        return f"{n}đ"

def ts_to_vn(ts: int | float) -> str:
    try:
        return datetime.fromtimestamp(int(ts), VN_TZ).strftime("%H:%M:%S • %d/%m/%Y")
    except Exception:
        return str(ts)

def short_addr(address_text: str, max_len: int = 90) -> str:
    if not address_text:
        return ""
    s = " ".join(address_text.split())
    return s if len(s) <= max_len else s[:max_len - 1] + "…"

# ================== SQLite Helpers ==================
def db_conn():
    return sqlite3.connect(DB_PATH)

def db_init():
    con = db_conn()
    try:
        con.execute("""
        CREATE TABLE IF NOT EXISTS product_cache (
            cache_key TEXT PRIMARY KEY,
            items_json TEXT NOT NULL,
            meta_json  TEXT,
            ts INTEGER NOT NULL
        )
        """)
        # migrate thêm cột meta_json nếu DB cũ
        try:
            con.execute("ALTER TABLE product_cache ADD COLUMN meta_json TEXT")
        except Exception:
            pass
        con.commit()
    finally:
        con.close()

def db_upsert(cache_key: str, items: list, ts: int | None = None, meta: dict | None = None):
    if not cache_key or not items:
        return
    if ts is None:
        ts = int(time())
    con = db_conn()
    try:
        con.execute(
            "INSERT INTO product_cache(cache_key, items_json, meta_json, ts) VALUES(?,?,?,?) "
            "ON CONFLICT(cache_key) DO UPDATE SET items_json=excluded.items_json, meta_json=excluded.meta_json, ts=excluded.ts",
            (cache_key, json.dumps(items, ensure_ascii=False), json.dumps(meta or {}, ensure_ascii=False), ts)
        )
        con.commit()
    finally:
        con.close()

def db_get(cache_key: str):
    if not cache_key:
        return None, None
    con = db_conn()
    try:
        cur = con.execute("SELECT items_json, meta_json, ts FROM product_cache WHERE cache_key=?", (cache_key,))
        row = cur.fetchone()
        if not row:
            return None, None
        items_json, meta_json, ts = row
        if int(time()) - int(ts) > CACHE_TTL:
            try:
                con.execute("DELETE FROM product_cache WHERE cache_key=?", (cache_key,))
                con.commit()
            except Exception:
                pass
            return None, None
        items = json.loads(items_json) if items_json else None
        meta = json.loads(meta_json) if meta_json else None
        return items, meta
    finally:
        con.close()

def db_list_spx_keys(limit: int = 50) -> list[str]:
    cutoff = int(time()) - CACHE_TTL
    con = db_conn()
    try:
        cur = con.execute(
            "SELECT cache_key FROM product_cache WHERE cache_key LIKE 'SPXVN%%' AND ts >= ? "
            "ORDER BY ts DESC LIMIT ?",
            (cutoff, limit),
        )
        rows = cur.fetchall()
        return [r[0] for r in rows]
    finally:
        con.close()

def db_purge_expired():
    con = db_conn()
    try:
        cutoff = int(time()) - CACHE_TTL
        con.execute("DELETE FROM product_cache WHERE ts < ?", (cutoff,))
        con.commit()
    finally:
        con.close()

# ================== Cache (RAM + SQLite) ==================
def cache_store_from_order(order: dict):
    """Lưu product_info + meta(address) theo order_id & tracking_number (RAM + SQLite)."""
    items = order.get("product_info") or []
    if not items:
        return
    meta = {"address": order.get("address") or {}}
    entry = {"items": items, "meta": meta, "ts": int(time())}

    oid = order.get("order_id")
    tn  = order.get("tracking_number")

    # RAM
    if oid:
        PRODUCT_CACHE[oid] = entry
    if tn:
        PRODUCT_CACHE[tn] = entry

    # SQLite
    if oid:
        db_upsert(oid, items, entry["ts"], meta)
    if tn:
        db_upsert(tn, items, entry["ts"], meta)

def cache_get_all(key: str):
    """Trả dict {'items':..., 'meta':...} từ RAM; fallback SQLite."""
    if not key:
        return {"items": None, "meta": None}
    e = PRODUCT_CACHE.get(key)
    if e and (int(time()) - int(e["ts"]) <= CACHE_TTL):
        return {"items": e.get("items"), "meta": e.get("meta")}
    items, meta = db_get(key)
    if items:
        PRODUCT_CACHE[key] = {"items": items, "meta": meta or {}, "ts": int(time())}
    return {"items": items, "meta": meta}

def cache_get(key: str):
    """Giữ tương thích cũ: chỉ trả items."""
    return cache_get_all(key).get("items")

# ================== Telegram Commands ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_text(
        f"Xin chào {user.first_name}! Bot lấy order Shopee (cookie) & tra cứu SPX.\n"
        "• Gửi cookie Shopee (SPC...) để lấy chi tiết đơn & lưu sản phẩm + nơi nhận (SQLite).\n"
        "• Gửi mã SPX như SPXVN05122704911B để xem timeline; nếu đã có cache, sẽ hiện luôn sản phẩm & nơi nhận.\n"
        "Gõ /help để xem hướng dẫn.",
        reply_markup=build_menu()
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "Các lệnh:\n"
        "/start - Bắt đầu bot\n"
        "/help - Trợ giúp\n"
        "/balance - Xem số dư\n"
        "/buy - Mua gửi thường viên\n"
        "/list - Danh sách SPX gần đây (SPX | Sản phẩm | Trạng thái | Nơi nhận)\n\n"
        "Cách dùng:\n"
        "• Gửi cookie Shopee (F12 > Cookies > SPC) → bot trả trạng thái, địa chỉ, sản phẩm, giá và lưu vào SQLite.\n"
        "• Gửi mã SPX → bot gọi API SPX & in timeline; nếu đã có cache, hiện thêm sản phẩm & nơi nhận.\n"
        "• TTL cache mặc định 3 ngày.\n"
    )
    await update.message.reply_text(help_text, reply_markup=build_menu())

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    balance_amount = 1000
    await update.message.reply_text(f"Số dư hiện tại của bạn: {vnd(balance_amount)}", reply_markup=build_menu())

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Bạn muốn mua gói gửi thường viên? Giá: 500đ.\nGửi /confirm để xác nhận.",
        reply_markup=build_menu()
    )

async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Mua thành công! Số dư đã trừ.", reply_markup=build_menu())

# ================== Shopee API ==================
def call_shopee_api(cookie_str: str) -> dict:
    if not (cookie_str.startswith('SPC') or ';' in cookie_str or '=' in cookie_str):
        return {'error': 'Cookie không hợp lệ (phải chứa SPC, ; hoặc =). Copy từ browser Shopee.'}
    payload = {"cookies": [cookie_str.strip()]}
    headers = {'Content-Type': 'application/json'}
    try:
        response = requests.post(API_URL, json=payload, headers=headers, timeout=10)
        logger.info(f"Shopee Status: {response.status_code}, Response: {response.text[:200]}...")
        if response.status_code == 200:
            try:
                data = response.json()
                if data and 'allOrderDetails' in data:
                    return data
                return {'error': "Dữ liệu trả về từ API không có thuộc tính 'allOrderDetails'."}
            except json.JSONDecodeError:
                return {'error': 'Response không phải JSON', 'raw': response.text[:500]}
        else:
            return {'error': f'Status {response.status_code}: {response.text[:200]}. Kiểm tra cookie valid.'}
    except requests.exceptions.RequestException as e:
        logger.error(f"API request error: {e}")
        return {'error': str(e)}

def parse_orders_from_api(data: dict) -> list:
    new_orders = []
    if not data or 'allOrderDetails' not in data:
        return new_orders
    for order in data['allOrderDetails']:
        if order.get('data') and order['data'].get('error') == 'DeadCookie':
            od = {
                'order_id': 'DeadCookie',
                'tracking_number': 'DeadCookie',
                'tracking_info_description': 'DeadCookie',
                'address': {
                    'shipping_name': 'DeadCookie',
                    'shipping_phone': 'DeadCookie',
                    'shipping_address': 'DeadCookie',
                },
                'cookie': order.get('cookie'),
                'noOrder': True,
            }
            new_orders.append(od)
        else:
            for order_detail in order.get('orderDetails', []):
                order_detail_copy = order_detail.copy()
                order_detail_copy['cookie'] = order.get('cookie')
                new_orders.append(order_detail_copy)
                try:
                    cache_store_from_order(order_detail_copy)
                except Exception as err:
                    logger.warning(f"cache_store_from_order error: {err}")
    return new_orders

# ================== SPX API ==================
def call_spx_api(tracking_number: str) -> dict:
    try:
        resp = requests.get(
            SPX_API_URL,
            params={"spx_tn": tracking_number, "language_code": "vi"},
            timeout=10,
        )
        logger.info(f"SPX Status: {resp.status_code}, Body: {resp.text[:200]}...")
        if resp.status_code != 200:
            return {"error": f"SPX status {resp.status_code}: {resp.text[:120]}"}
        data = resp.json()
        if data.get("retcode") != 0:
            return {"error": f"SPX retcode {data.get('retcode')}: {data.get('message')}"}
        return data
    except requests.exceptions.RequestException as e:
        logger.error(f"SPX request error: {e}")
        return {"error": str(e)}
    except ValueError:
        return {"error": "Response SPX không phải JSON hợp lệ"}

def format_spx_timeline(spx_json: dict) -> str:
    try:
        info = spx_json["data"]["sls_tracking_info"]
        tn = info.get("sls_tn") or ""
        client_order_id = info.get("client_order_id") or ""
        records = info.get("records") or []
    except Exception:
        return "❌ Không đọc được dữ liệu SPX."
    records_sorted = sorted(records, key=lambda r: r.get("actual_time", 0), reverse=True)
    lines = []
    header = f"📦 **SPX: {tn}**"
    if client_order_id:
        header += f"\n🆔 Đơn hàng: {client_order_id}"
    lines.append(header)
    for r in records_sorted[:8]:
        when = ts_to_vn(r.get("actual_time", 0))
        desc = (r.get("buyer_description") or r.get("description") or "").strip()
        loc = (r.get("current_location", {}) or {}).get("location_name") or ""
        if loc:
            lines.append(f"• {when}\n  {desc} — _{loc}_")
        else:
            lines.append(f"• {when}\n  {desc}")
    if not records_sorted:
        lines.append("Không có cập nhật trạng thái.")
    return "\n".join(lines)

def get_latest_spx_status(spx_code: str) -> tuple[str, str]:
    data = call_spx_api(spx_code)
    if "error" in data:
        return ("—", "")
    try:
        recs = data["data"]["sls_tracking_info"].get("records") or []
        if not recs:
            return ("—", "")
        last = max(recs, key=lambda r: r.get("actual_time", 0))
        desc = (last.get("buyer_description") or last.get("description") or "").strip() or "—"
        when = ts_to_vn(last.get("actual_time", 0))
        return (desc, when)
    except Exception:
        return ("—", "")

# ================== Input Handler ==================
async def handle_input_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    input_text = (update.message.text or "").strip()
    if not input_text:
        await update.message.reply_text("Vui lòng gửi cookie, mã SPX hoặc text đơn hàng hợp lệ.", reply_markup=build_menu())
        return

    # --- 1) SPX code ---
    spx_match = re.search(r"\bSPXVN[A-Z0-9]{8,}\b", input_text, re.IGNORECASE)
    if spx_match:
        spx_tn = spx_match.group(0).upper()
        await update.message.reply_text(f"🔎 Đang tra SPX: {spx_tn} ...")
        spx_data = call_spx_api(spx_tn)
        if "error" in spx_data:
            await update.message.reply_text(f"❌ Lỗi SPX: {spx_data['error']}", reply_markup=build_menu())
            return

        timeline = format_spx_timeline(spx_data)

        # Map sang sản phẩm + nơi nhận
        try:
            info = spx_data["data"]["sls_tracking_info"]
            client_order_id = info.get("client_order_id") or ""
            sls_tn = info.get("sls_tn") or ""
        except Exception:
            client_order_id, sls_tn = "", ""

        cached = {"items": None, "meta": None}
        for key in (client_order_id, sls_tn, spx_tn):
            if key:
                cached = cache_get_all(key)
                if cached.get("items"):
                    break

        items = cached.get("items")
        meta = cached.get("meta") or {}
        address = (meta.get("address") or {})
        recv_name = address.get("shipping_name") or ""
        recv_phone = address.get("shipping_phone") or ""
        recv_addr = short_addr(address.get("shipping_address") or "")

        if items:
            # render sản phẩm + nơi nhận
            lines = [timeline]
            lines.append("\n🛒 **SẢN PHẨM**")
            for i, p in enumerate(items[:3], 1):
                name = p.get("name") or "N/A"
                model = p.get("model_name") or "—"
                amount = p.get("amount", 1) or 1
                price_raw = p.get("order_price", 0)
                if isinstance(price_raw, (int, float)):
                    if price_raw > 1_000_000_000:
                        unit_vnd = price_raw // 100_000
                    elif price_raw > 10_000:
                        unit_vnd = price_raw // 100
                    else:
                        unit_vnd = price_raw
                else:
                    unit_vnd = 0
                price_txt = f"{amount}×{vnd(unit_vnd)}" if unit_vnd else f"x{amount}"
                lines.append(f"{i}. {name} ({model}) — {price_txt}")

            # nơi nhận (dễ nhìn: 2 dòng)
            if recv_name or recv_phone or recv_addr:
                lines.append("\n📍 **NƠI NHẬN**")
                who = " • ".join([x for x in [recv_name, recv_phone] if x])
                if who:
                    lines.append(who)
                if recv_addr:
                    lines.append(recv_addr)

            await update.message.reply_text("\n".join(lines), reply_markup=build_menu())
        else:
            await update.message.reply_text(
                f"{timeline}\n\n"
                "ℹ️ Chưa có thông tin sản phẩm/nơi nhận cho mã này.\n"
                "👉 Gửi cookie Shopee (SPC...) của đơn tương ứng một lần để mình lưu (SQLite), "
                "từ lần sau nhập mã SPX sẽ hiện đủ thông tin.",
                reply_markup=build_menu()
            )
        return

    # --- 2) Cookie Shopee ---
    if ';' in input_text or input_text.startswith('SPC'):
        await update.message.reply_text("🔄 Đang gọi API Shopee...")
        api_data = call_shopee_api(input_text)
        if 'error' in api_data:
            error_msg = f"❌ Lỗi API: {api_data['error']}"
            if 'raw' in api_data:
                error_msg += f"\nChi tiết: {api_data['raw']}"
            await update.message.reply_text(error_msg, reply_markup=build_menu())
            return

        new_orders = parse_orders_from_api(api_data)
        if not new_orders:
            await update.message.reply_text("Không có order details từ API. Thử cookie khác!", reply_markup=build_menu())
            return

        order = new_orders[0]

        if order.get("tracking_number") == "Đang chờ":
            await update.message.reply_text("❌ Tài khoản đã bị cấm hoặc cookie hết hạn.", reply_markup=build_menu())
            return
        if order.get('noOrder'):
            await update.message.reply_text("❌ DeadCookie - Cookie hết hạn.", reply_markup=build_menu())
            return

        # Lưu cache (sản phẩm + nơi nhận)
        try:
            cache_store_from_order(order)
            db_purge_expired()
        except Exception as err:
            logger.warning(f"Caching error: {err}")

        # ----- Render kết quả -----
        lines = []
        trang_thai = order.get('tracking_info_description', 'Đơn hàng đang trong quá trình vận chuyển')
        ma_don_hang = order.get('order_id', 'N/A')
        thoi_gian_dat_hang = order.get('order_time') or "28 Tháng 10, 2025 16:12:33"

        lines.append(f"Tình trạng: {trang_thai}")
        lines.append(f"Mã đơn hàng: {ma_don_hang}")
        lines.append(f"Thời gian đặt hàng: {thoi_gian_dat_hang}\n")

        address = order.get('address', {}) or {}
        ten_nhan = address.get('shipping_name', 'N/A')
        raw_phone = address.get('shipping_phone', 'N/A')
        if isinstance(raw_phone, str) and raw_phone.startswith('84') and len(raw_phone) > 2:
            sdt_fmt = f"(+84) {raw_phone[2:]}"
        else:
            sdt_fmt = raw_phone
        dia_chi = address.get('shipping_address', 'N/A')
        lines.append("📦 ĐỊA CHỈ NHẬN HÀNG")
        lines.append(f"{ten_nhan}")
        lines.append(f"{sdt_fmt}")
        lines.append(f"{dia_chi}\n")

        product_info = (order.get('product_info') or [{}])[0] if order.get('product_info') else {}
        ten_san_pham = product_info.get('name', 'N/A')
        phan_loai = product_info.get('model_name', 'N/A')
        item_id = product_info.get('item_id', '')
        shop_id = product_info.get('shop_id', '')
        lien_ket = f"https://shopee.vn/product/{shop_id}/{item_id}" if item_id and shop_id else 'N/A'
        lines.append("🛍 SẢN PHẨM 1")
        lines.append(f"Tên sản phẩm: {ten_san_pham}")
        lines.append(f"Phân loại: {phan_loai}")
        lines.append(f"Liên kết: {lien_ket}\n")

        don_vi_van_chuyen = "SPX Express" if (order.get('tracking_number') or "").startswith('SPXVN') else 'N/A'
        shipping_method = order.get('shipping_method') or "Nhanh (Thanh toán khi nhận hàng)"
        ma_van_don = order.get('tracking_number', 'N/A')
        thong_tin = order.get('tracking_info_description', 'N/A')
        lines.append("🚚 ĐƠN VỊ VẬN CHUYỂN")
        lines.append(f"{shipping_method}")
        lines.append(f"Đơn vị vận chuyển: {don_vi_van_chuyen}")
        lines.append(f"Mã vận đơn: {ma_van_don}")
        lines.append(f"Thông tin: {thong_tin}\n")

        amount = product_info.get('amount', 1) or 1
        order_price_raw = product_info.get('order_price', 0)
        if isinstance(order_price_raw, (int, float)):
            if order_price_raw > 1_000_000_000:
                unit_price_vnd = order_price_raw // 100_000
            elif order_price_raw > 10_000:
                unit_price_vnd = order_price_raw // 100
            else:
                unit_price_vnd = order_price_raw
        else:
            unit_price_vnd = 0
        tong_tien = int(unit_price_vnd) * amount
        lines.append(f"💵 Vui lòng thanh toán {vnd(tong_tien)} khi nhận hàng")
        lines.append("\nGửi cookie khác hoặc nhập mã SPX để kiểm tra!")

        await update.message.reply_text("\n".join(lines), reply_markup=build_menu())
        return

    # --- 3) Fallback: parse text đơn hàng ---
    await handle_order_text_fallback(update, context)

# ================== Fallback Parser ==================
async def handle_order_text_fallback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    order_text = (update.message.text or "").strip()
    patterns = {
        'ma_don_hang': r'Mã đơn hàng:(\d+|[A-Z0-9]+)',
        'ngay_dat': r'(\d{1,2} Tháng \d{1,2}, 20\d{2} \d{1,2}:\d{2}:\d{2})',
        'dia_chi': r'Địa chỉ giao hàng\n(.*?)Thủ?,',
        'san_pham': r'([\w\s]+)\nLiên kết:',
        'ma_van_don': r'Mã vận đơn: ([\w\d]+)',
        'van_chuyen': r'Đơn vị vận chuyển ([\w\s]+)',
        'trang_thai': r'Tình trạng: ([\w\s]+)',
        'ma_tracking': r'^([A-Z0-9]{20,})'
    }

    extracted = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, order_text, re.DOTALL | re.IGNORECASE)
        extracted[key] = match.group(1).strip() if match else 'N/A'

    if all(v == 'N/A' for v in extracted.values()):
        await update.message.reply_text("Không parse được từ text. Hãy gửi cookie Shopee hoặc mã SPX.", reply_markup=build_menu())
        return

    tracking_status = "Đã giao" if "SPX" in extracted.get('ma_van_don', '') else "Đang vận chuyển"

    lines = ["📦 **Summary từ Text**:\n"]
    for key, value in extracted.items():
        if key == 'ma_tracking':
            lines.append(f"🔍 Tracking: {value[:30]}...")
        else:
            lines.append(f"{get_emoji(key)} {value}")
    lines.append(f"📊 Tracking: {tracking_status}\n\nGửi cookie hoặc mã SPX để dùng API!")

    await update.message.reply_text("\n".join(lines), reply_markup=build_menu())

# ================== /list Command ==================
async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    spx_keys = db_list_spx_keys(limit=50)
    if not spx_keys:
        cutoff = int(time()) - CACHE_TTL
        spx_keys = [
            k for k, v in PRODUCT_CACHE.items()
            if k.startswith("SPXVN") and int(v.get("ts", 0)) >= cutoff
        ][:50]

    if not spx_keys:
        await update.message.reply_text(
            "Chưa có SPX nào trong cache. Hãy gửi cookie Shopee trước, rồi tra SPX.",
            reply_markup=build_menu()
        )
        return

    lines = ["📋 **Danh sách SPX gần đây** (tối đa 50)\n"]
    max_rows = 20
    count = 0

    for spx in spx_keys:
        if count >= max_rows:
            lines.append(f"\n… và {len(spx_keys) - max_rows} mã khác")
            break

        cached = cache_get_all(spx)
        items = cached.get("items") or []
        meta  = cached.get("meta") or {}
        addr  = meta.get("address") or {}

        # tên sản phẩm (sản phẩm đầu)
        name = "N/A"
        if items:
            name = (items[0].get("name") or "N/A").strip()

        # nơi nhận (gọn 1–2 dòng)
        recv_name = addr.get("shipping_name") or ""
        recv_phone = addr.get("shipping_phone") or ""
        recv_addr = short_addr(addr.get("shipping_address") or "")
        who = " • ".join([x for x in [recv_name, recv_phone] if x])
        where = recv_addr

        status, when = get_latest_spx_status(spx)
        when_txt = f" — {when}" if when else ""

        # Block hiển thị
        lines.append(f"• {spx}")
        lines.append(f"  🛒 {name}")
        lines.append(f"  🟢 {status}{when_txt}")
        if who or where:
            lines.append(f"  📍 {who}" if who else "  📍")
            if where:
                lines.append(f"     {where}")
        lines.append("")  # dòng trống phân cách
        count += 1

    text_out = "\n".join(lines)
    if len(text_out) > 3800:
        text_out = text_out[:3800] + "\n…(đã rút gọn)"
    await update.message.reply_text(text_out, reply_markup=build_menu())

# ================== Main ==================
def main() -> None:
    db_init()
    db_purge_expired()

    # Lấy token từ ENV thay vì hard-code
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Missing TELEGRAM_TOKEN env")

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("balance", balance))
    application.add_handler(CommandHandler("buy", buy))
    application.add_handler(CommandHandler("confirm", confirm))
    application.add_handler(CommandHandler("list", list_cmd))  # NEW

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_input_text))

    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
