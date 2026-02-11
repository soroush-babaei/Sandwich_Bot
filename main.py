import logging
import io
import datetime
import sqlite3
import os
from typing import Optional
from PIL import Image, ImageDraw, ImageFont
import arabic_reshaper
from bidi.algorithm import get_display
try:
    import jdatetime  # برای نمایش تاریخ شمسی
except Exception:
    jdatetime = None
from geopy.geocoders import Nominatim
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
import textwrap
# بارگذاری متغیرهای محیطی از فایل .env در صورت وجود
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# --- تنظیمات ---
# امنیت: توکن را از متغیر محیطی بخوانید (در فایل .env ست شود)
TOKEN = os.getenv("TOKEN")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0") or 0)  # آیدی عددی ادمین

OPEN_TIME = datetime.time(11, 0)
CLOSE_TIME = datetime.time(23, 59)

DB_DIR = os.path.join(os.path.dirname(__file__), 'data')
DB_PATH = os.path.join(DB_DIR, 'bot.db')
# مسیر فونت فارسی (کنار پروژه قرار دهید: fonts/Vazir.ttf)
FONT_PATH = os.path.join(os.path.dirname(__file__), 'fonts', 'Vazir.ttf')

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
geolocator = Nominatim(user_agent="food_bot", timeout=5)

# --- Database Helpers ---

def _get_conn():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        # مشتری‌ها: آخرین اطلاعات ذخیره شده برای هر تلگرام آیدی
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                name TEXT,
                phone TEXT,
                address TEXT,
                plaque TEXT,
                unit TEXT,
                lat REAL,
                lon REAL,
                updated_at TEXT
            )
            """
        )
        # سفارش‌ها
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                customer_name TEXT,
                phone TEXT,
                address TEXT,
                lat REAL,
                lon REAL,
                total INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        # آیتم‌های سفارش
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                unit_price INTEGER NOT NULL,
                qty INTEGER NOT NULL,
                total INTEGER NOT NULL,
                FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE CASCADE
            )
            """
        )


def get_saved_customer(telegram_id: int) -> Optional[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT * FROM customers WHERE telegram_id = ?", (telegram_id,))
        row = cur.fetchone()
    if row:
        return dict(row)
    return None


def save_or_update_customer(telegram_id: int, name: Optional[str], phone: Optional[str],
                            address: Optional[str], plaque: Optional[str], unit: Optional[str],
                            lat: Optional[float], lon: Optional[float]):
    now = datetime.datetime.now().isoformat(timespec='seconds')
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM customers WHERE telegram_id = ?", (telegram_id,))
        exists = cur.fetchone() is not None
        if exists:
            cur.execute(
                """
                UPDATE customers SET name=?, phone=?, address=?, plaque=?, unit=?, lat=?, lon=?, updated_at=?
                WHERE telegram_id=?
                """,
                (name, phone, address, plaque, unit, lat, lon, now, telegram_id)
            )
        else:
            cur.execute(
                """
                INSERT INTO customers (telegram_id, name, phone, address, plaque, unit, lat, lon, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (telegram_id, name, phone, address, plaque, unit, lat, lon, now)
            )


def create_order_record(telegram_id: int, cart: list, customer: dict, lat: Optional[float], lon: Optional[float]) -> int:
    total = sum(i['total'] for i in cart)
    created_at = datetime.datetime.now().isoformat(timespec='seconds')
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO orders (telegram_id, customer_name, phone, address, lat, lon, total, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                telegram_id,
                customer.get('name') or customer.get('full_name'),
                customer.get('phone'),
                customer.get('address'),
                lat,
                lon,
                total,
                created_at,
            ),
        )
        order_id = cur.lastrowid
        for i in cart:
            cur.execute(
                """
                INSERT INTO order_items (order_id, item_name, unit_price, qty, total)
                VALUES (?, ?, ?, ?, ?)
                """,
                (order_id, i['name'], i['price'], i['qty'], i['total'])
            )
    return order_id


def get_total_sales() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(SUM(total), 0) AS s FROM orders")
        row = cur.fetchone()
    return int(row[0] if row and row[0] is not None else 0)


def get_today_sales() -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COALESCE(SUM(total), 0) FROM orders WHERE DATE(created_at) = DATE('now','localtime')"
        )
        row = cur.fetchone()
    return int(row[0] if row and row[0] is not None else 0)


def get_last_orders(limit: int = 5) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT id, customer_name, total, created_at FROM orders ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


# --- بررسی ساعت کاری ---

def _is_open_now(now: datetime.datetime | None = None) -> bool:
    now = now or datetime.datetime.now()
    current = now.time()
    if OPEN_TIME <= CLOSE_TIME:
        return OPEN_TIME <= current <= CLOSE_TIME
    return current >= OPEN_TIME or current <= CLOSE_TIME

# --- مراحل گفتگو (States) ---
(CATEGORY, ITEM, QUANTITY, CHECKOUT, CONFIRM_ORDER, EDIT_CART,
 CHANGE_QTY, SWAP_CATEGORY, SWAP_ITEM, NAME, CHOOSE_ADDRESS_METHOD,
 WAIT_FOR_ADDRESS, CONFIRM_GPS_ADDRESS, PLAQUE, UNIT, CONTACT,
 FINAL_CHECK, EDIT_INFO_SELECT, EDIT_SPECIFIC_FIELD) = range(19)

MENU_DATA = {
    "🥪 ساندویچ‌ها": {"بندری مخصوص": 95000, "فلافل ویژه": 65000, "هات‌داگ تنوری": 110000},
    "🥤 نوشیدنی": {"نوشابه": 25000, "دوغ": 20000},
    "🍟 پیش‌غذا": {"سیب‌زمینی": 70000, "قارچ سوخاری": 85000}
}

# --- 🎨 تابع ساخت فاکتور ---

def create_invoice_image(cart, customer_info):
    width = 600
    base_height = 300
    item_height = 50
    footer_height = 150
    height = base_height + (len(cart) * item_height) + footer_height

    img = Image.new('RGB', (width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)

    try:
        # استفاده از فونت فارسی محلی. اگر موجود نبود، به فونت پیش‌فرض می‌افتد.
        font_header = ImageFont.truetype(FONT_PATH, 32)
        font_title = ImageFont.truetype(FONT_PATH, 26)
        font_item = ImageFont.truetype(FONT_PATH, 22)
        font_bold_lg = ImageFont.truetype(FONT_PATH, 30)
        font_bold_md = ImageFont.truetype(FONT_PATH, 24)
    except Exception:
        logging.warning("Font not found at %s; falling back to default PIL font (may not support Farsi)", FONT_PATH)
        font_header = font_title = font_item = font_bold_lg = font_bold_md = ImageFont.load_default()

    def draw_fa(text, x, y, font, align="right", color=(0, 0, 0)):
        reshaped = arabic_reshaper.reshape(str(text))
        bidi = get_display(reshaped)
        bbox = draw.textbbox((0, 0), bidi, font=font)
        text_width = bbox[2] - bbox[0]
        if align == "right":
            draw.text((x - text_width, y), bidi, fill=color, font=font)
        elif align == "center":
            draw.text((x - (text_width / 2), y), bidi, fill=color, font=font)
        else:
            draw.text((x, y), bidi, fill=color, font=font)

    draw_fa("📋 فست‌فود آنلاین", width / 2, 20, font_header, align="center")
    now = datetime.datetime.now()
    date_str = now.strftime("%Y-%m-%d %H:%M")
    draw_fa(f"تاریخ: {date_str}", width / 2, 60, font_item, align="center")
    draw.line([(30, 95), (width - 30, 95)], fill=0, width=2)

    y = 110
    right_margin = width - 40
    draw_fa(f"مشتری: {customer_info['name']}", right_margin, y, font_title, align="right")
    y += 35
    draw_fa(f"تلفن: {customer_info['phone']}", right_margin, y, font_title, align="right")
    y += 35

    addr_label = "آدرس: "
    full_address = f"{addr_label}{customer_info['address']}"
    wrapper = textwrap.TextWrapper(width=45)
    addr_lines = wrapper.wrap(full_address)
    for line in addr_lines:
        draw_fa(line, right_margin, y, font_title, align="right")
        y += 30

    y += 15
    draw.line([(30, y), (width - 30, y)], fill=0, width=2)
    y += 10

    col_name = width - 40
    col_qty = width / 2 + 30
    col_fee = width / 2 - 80
    col_total = 40

    draw_fa("نام کالا", col_name, y, font_bold_md, align="right")
    draw_fa("تعداد", col_qty, y, font_bold_md, align="center")
    draw_fa("فی", col_fee, y, font_bold_md, align="center")
    draw_fa("جمع کل", col_total, y, font_bold_md, align="left")

    y += 35
    draw.line([(30, y), (width - 30, y)], fill=0, width=1)
    y += 15

    total_sum = 0
    for item in cart:
        draw_fa(item['name'], col_name, y, font_item, align="right")
        draw_fa(str(item['qty']), col_qty, y, font_item, align="center")
        draw_fa(f"{item['price']:,}", col_fee, y, font_item, align="center")
        draw_fa(f"{item['total']:,}", col_total, y, font_item, align="left")
        total_sum += item['total']
        y += 45

    draw.line([(30, y), (width - 30, y)], fill=0, width=3)
    y += 20

    draw_fa("مبلغ نهایی قابل پرداخت:", width - 40, y, font_bold_md, align="right")
    draw_fa(f"{total_sum:,} تومان", 40, y, font_bold_lg, align="left")

    y += 80
    draw_fa("نوش جان! از خرید شما متشکریم.", width / 2, y, font_item, align="center")

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf


# --- تابع کمکی برای نمایش فاکتور نهایی ---
async def show_final_check(update, context):
    cart = context.user_data.get('cart', [])
    if not cart:
        return await start(update, context)

    total_price = sum(i['total'] for i in cart)

    msg = "🧾 **پیش‌فاکتور نهایی**\n"
    msg += "➖➖➖➖➖➖➖➖➖➖\n"
    msg += f"👤 **مشتری:** {context.user_data['full_name']}\n"
    msg += f"📱 **تلفن:** {context.user_data['phone']}\n"
    msg += f"📍 **آدرس:**\n{context.user_data['address']}\n"
    msg += "➖➖➖➖➖➖➖➖➖➖\n"
    msg += "🍕 **سفارشات:**\n"
    for i in cart:
        msg += f"▫️ {i['name']} ({i['qty']} عدد) : {i['total']:,} ت\n"
    msg += "➖➖➖➖➖➖➖➖➖➖\n"
    msg += f"💰 **مبلغ کل: {total_price:,} تومان**"

    kb = [
        ["✅ تایید و ارسال فاکتور"],
        ["🛍 تغییر در کالاها", "✍️ ویرایش مشخصات"],
    ]
    await update.message.reply_text(
        msg,
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        parse_mode='Markdown',
    )
    return FINAL_CHECK


# --- شروع و منو ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_open_now():
        await update.message.reply_text("⏰ متأسفانه الان خارج از ساعات کاری هستیم.\nساعات کاری: 11:00 تا 23:59")
        return ConversationHandler.END

    if update.effective_user.id == ADMIN_CHAT_ID:
        await update.message.reply_text(
            "👨‍🍳 پنل مدیریت:",
            reply_markup=ReplyKeyboardMarkup([["📊 گزارش فروش", "📅 فروش امروز"], ["🧾 ۵ سفارش آخر"]], resize_keyboard=True),
        )
        return ConversationHandler.END

    context.user_data['cart'] = context.user_data.get('cart', [])
    keyboard = [[cat] for cat in MENU_DATA.keys()]
    if context.user_data['cart']:
        keyboard.append(["🛒 سبد خرید"])

    await update.message.reply_text(
        "سلام! به فست‌فود آنلاین خوش اومدی 🍔\nچی میل داری؟",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
    )
    return CATEGORY


async def select_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = update.message.text
    if cat == "🛒 سبد خرید":
        return await checkout_choice(update, context)
    if cat not in MENU_DATA:
        return CATEGORY
    context.user_data['temp_cat'] = cat
    items = [[item] for item in MENU_DATA[cat].keys()] + [["🔙 منوی اصلی"]]
    await update.message.reply_text(
        f"از منوی {cat} انتخاب کن:",
        reply_markup=ReplyKeyboardMarkup(items, resize_keyboard=True),
    )
    return ITEM


async def select_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    item = update.message.text
    if item == "🔙 منوی اصلی":
        return await start(update, context)
    cat = context.user_data.get('temp_cat')
    if not cat or item not in MENU_DATA[cat]:
        return ITEM
    context.user_data['temp_item_name'] = item
    context.user_data['temp_item_price'] = MENU_DATA[cat][item]
    await update.message.reply_text(
        f"تعداد {item}؟",
        reply_markup=ReplyKeyboardMarkup([["1", "2", "3"], ["4", "5", "6"]], resize_keyboard=True),
    )
    return QUANTITY


async def select_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    qty = update.message.text
    if not qty.isdigit():
        return QUANTITY
    context.user_data['cart'].append(
        {
            'name': context.user_data['temp_item_name'],
            'price': context.user_data['temp_item_price'],
            'qty': int(qty),
            'total': context.user_data['temp_item_price'] * int(qty),
        }
    )
    await update.message.reply_text(
        "✅ به سبد اضافه شد.",
        reply_markup=ReplyKeyboardMarkup([["➕ ادامه سفارش"], ["🛒 تکمیل خرید"]], resize_keyboard=True),
    )
    return CHECKOUT


async def checkout_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "➕ ادامه سفارش":
        return await start(update, context)
    cart = context.user_data.get('cart', [])
    if not cart:
        await update.message.reply_text("سبد خرید خالیه!")
        return await start(update, context)

    invoice = "🛒 **سبد خرید شما:**\n\n"
    for i in cart:
        invoice += f"🔸 {i['name']} ({i['qty']} عدد) - {i['total']:,}\n"
    invoice += f"\n💰 جمع کل: {sum(x['total'] for x in cart):,} تومان"

    kb = [["✅ ثبت نهایی"], ["✏️ تغییر در کالاها"], ["❌ لغو"]]
    await update.message.reply_text(
        invoice,
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        parse_mode='Markdown',
    )
    return CONFIRM_ORDER


# --- بخش تغییر کالاها ---
async def edit_cart_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = []
    for i in context.user_data['cart']:
        kb.append([f"❌ حذف {i['name']}", f"🔄 تعویض {i['name']}"])
        kb.append([f"✏️ تعداد {i['name']}"])
    kb.append(["🔙 بازگشت"])
    await update.message.reply_text(
        "چه تغییری مد نظرته؟",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return EDIT_CART


async def edit_logic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "🔙 بازگشت":
        if 'phone' in context.user_data:
            return await show_final_check(update, context)
        return await checkout_choice(update, context)

    for idx, item in enumerate(context.user_data['cart']):
        if item['name'] in text:
            context.user_data['edit_idx'] = idx

            if "❌ حذف" in text:
                context.user_data['cart'].pop(idx)
                if 'phone' in context.user_data:
                    return await show_final_check(update, context)
                return await checkout_choice(update, context)

            elif "✏️ تعداد" in text:
                await update.message.reply_text(
                    f"تعداد جدید {item['name']}:",
                    reply_markup=ReplyKeyboardMarkup([["1", "2", "3", "4"]], resize_keyboard=True),
                )
                return CHANGE_QTY

            elif "🔄 تعویض" in text:
                kb = [[cat] for cat in MENU_DATA.keys()]
                await update.message.reply_text(
                    "با چی میخوای عوضش کنی؟",
                    reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
                )
                return SWAP_CATEGORY

    return EDIT_CART


async def change_qty_logic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.text.isdigit():
        return CHANGE_QTY
    idx = context.user_data['edit_idx']
    qty = int(update.message.text)
    item = context.user_data['cart'][idx]
    item['qty'] = qty
    item['total'] = item['price'] * qty

    if 'phone' in context.user_data:
        return await show_final_check(update, context)
    return await checkout_choice(update, context)


async def swap_category_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cat = update.message.text
    if cat not in MENU_DATA:
        return EDIT_CART
    context.user_data['swap_cat'] = cat
    items = [[item] for item in MENU_DATA[cat].keys()]
    await update.message.reply_text(
        "محصول جدید رو انتخاب کن:",
        reply_markup=ReplyKeyboardMarkup(items, resize_keyboard=True),
    )
    return SWAP_ITEM


async def swap_item_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    new_name = update.message.text
    cat = context.user_data['swap_cat']
    idx = context.user_data['edit_idx']

    price = MENU_DATA[cat][new_name]
    old_qty = context.user_data['cart'][idx]['qty']
    context.user_data['cart'][idx] = {
        'name': new_name,
        'price': price,
        'qty': old_qty,
        'total': price * old_qty,
    }

    await update.message.reply_text(f"✅ کالا با {new_name} تعویض شد.")

    if 'phone' in context.user_data:
        return await show_final_check(update, context)
    return await checkout_choice(update, context)


# --- دریافت اطلاعات کاربر ---
async def start_address_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = int(update.effective_user.id)
    saved = get_saved_customer(user_id)
    if saved and saved.get('address'):
        plaque_unit = []
        if saved.get('plaque'):
            plaque_unit.append(f"پلاک: {saved['plaque']}")
        if saved.get('unit'):
            plaque_unit.append(f"واحد: {saved['unit']}")
        suffix = (" | " + " | ".join(plaque_unit)) if plaque_unit else ""
        kb = [["📍 ارسال به آدرس قبلی"], ["✏️ آدرس جدید"]]
        await update.message.reply_text(
            f"آیا به این آدرس ارسال شود؟\n{saved['address']}{suffix}",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return NAME
    await update.message.reply_text("لطفاً نام خود را وارد کنید:", reply_markup=ReplyKeyboardRemove())
    return NAME


async def handle_name_or_saved(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if text == "📍 ارسال به آدرس قبلی":
        user_id = int(update.effective_user.id)
        saved = get_saved_customer(user_id)
        if saved:
            context.user_data['full_name'] = saved.get('name') or ''
            context.user_data['address'] = saved.get('address') or ''
            if saved.get('plaque') or saved.get('unit'):
                suffix = []
                if saved.get('plaque'):
                    suffix.append(f"پلاک: {saved['plaque']}")
                if saved.get('unit'):
                    suffix.append(f"واحد: {saved['unit']}")
                context.user_data['address'] = f"{context.user_data['address']} | {' | '.join(suffix)}"
            if saved.get('lat') and saved.get('lon'):
                context.user_data['lat'] = saved.get('lat')
                context.user_data['lon'] = saved.get('lon')
        kb = [[KeyboardButton("📱 تایید شماره تماس", request_contact=True)]]
        await update.message.reply_text(
            "شماره تماس جهت هماهنگی:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return CONTACT

    if text == "✏️ آدرس جدید":
        await update.message.reply_text("نام خود را وارد کنید:", reply_markup=ReplyKeyboardRemove())
        return NAME

    context.user_data['full_name'] = text
    kb = [[KeyboardButton("📍 ارسال لوکیشن (GPS)", request_location=True)], ["✍️ تایپ دستی آدرس"]]
    await update.message.reply_text(
        "لطفاً آدرس خود را ارسال کنید:",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return CHOOSE_ADDRESS_METHOD


async def handle_location_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.location:
        try:
            lat = update.message.location.latitude
            lon = update.message.location.longitude
            context.user_data['lat'], context.user_data['lon'] = lat, lon
            location = geolocator.reverse(f"{lat}, {lon}")
            if location and location.address:
                context.user_data['temp_address'] = location.address
                kb = [["✅ آدرس صحیح است"], ["❌ ویرایش دستی"]]
                await update.message.reply_text(
                    f"آدرس دریافتی:\n{location.address}",
                    reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
                )
                return CONFIRM_GPS_ADDRESS
        except Exception:
            pass

    await update.message.reply_text("آدرس دقیق را تایپ کنید (خیابان، کوچه، پلاک):", reply_markup=ReplyKeyboardRemove())
    return WAIT_FOR_ADDRESS


async def confirm_gps_logic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text == "✅ آدرس صحیح است":
        context.user_data['address'] = context.user_data['temp_address']
        await update.message.reply_text("پلاک:")
        return PLAQUE
    await update.message.reply_text("آدرس دقیق را تایپ کنید:")
    return WAIT_FOR_ADDRESS


async def save_manual_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['address'] = update.message.text
    await update.message.reply_text("پلاک:")
    return PLAQUE


async def get_plaque(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['plaque'] = update.message.text
    await update.message.reply_text("واحد:")
    return UNIT


async def get_unit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['unit'] = update.message.text
    kb = [[KeyboardButton("📱 ارسال شماره تماس", request_contact=True)]]
    await update.message.reply_text(
        "لطفاً شماره تماس خود را ارسال کنید:",
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
    )
    return CONTACT


async def save_contact_and_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.contact:
        return CONTACT
    context.user_data['phone'] = update.message.contact.phone_number

    if 'plaque' in context.user_data:
        full = f"{context.user_data['address']} | پلاک: {context.user_data['plaque']} | واحد: {context.user_data['unit']}"
        context.user_data['address'] = full

    return await show_final_check(update, context)


# --- انتخاب ویرایش اطلاعات خاص ---
async def edit_info_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "🔙 بازگشت":
        return await show_final_check(update, context)

    if text == "👤 نام":
        context.user_data['editing_field'] = 'full_name'
        await update.message.reply_text("نام جدید را وارد کنید:", reply_markup=ReplyKeyboardRemove())
    elif text == "🏠 آدرس":
        context.user_data['editing_field'] = 'address'
        await update.message.reply_text("آدرس جدید را وارد کنید:", reply_markup=ReplyKeyboardRemove())
    elif text == "📞 تلفن":
        context.user_data['editing_field'] = 'phone'
        kb = [[KeyboardButton("📱 ارسال شماره جدید", request_contact=True)]]
        await update.message.reply_text(
            "شماره جدید را ارس��ل کنید:",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )

    return EDIT_SPECIFIC_FIELD


async def save_specific_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data['editing_field']
    if field == 'phone':
        val = update.message.contact.phone_number if update.message.contact else update.message.text
    else:
        val = update.message.text

    context.user_data[field] = val
    await update.message.reply_text("✅ اطلاعات بروز شد.")
    return await show_final_check(update, context)


# --- ثبت نهایی و ارسال به ادمین ---
async def final_submit_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    if not _is_open_now():
        await update.message.reply_text(
            "⏰ متأسفانه الان خارج از ساعات کاری هستیم.\nساعات کاری: 11:00 تا 23:59",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ConversationHandler.END

    if text == "🛍 تغییر در کالاها":
        return await edit_cart_menu(update, context)

    if text == "✍️ ویرایش مشخصات":
        kb = [["👤 نام", "🏠 آدرس"], ["📞 تلفن", "🔙 بازگشت"]]
        await update.message.reply_text(
            "کدام بخش را ویرایش می‌کنید؟",
            reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True),
        )
        return EDIT_INFO_SELECT

    if text == "✅ تایید و ارسال فاکتور":
        user_id = int(update.effective_user.id)
        cart = context.user_data['cart']
        total = sum(i['total'] for i in cart)

        # ذخیره/بروزرسانی مشتری
        full_name = context.user_data.get('full_name')
        phone = context.user_data.get('phone')
        address = context.user_data.get('address')
        plaque = context.user_data.get('plaque') if 'plaque' in context.user_data else None
        unit = context.user_data.get('unit') if 'unit' in context.user_data else None
        lat = context.user_data.get('lat') if 'lat' in context.user_data else None
        lon = context.user_data.get('lon') if 'lon' in context.user_data else None
        save_or_update_customer(user_id, full_name, phone, address, plaque, unit, lat, lon)

        # داده برای فاکتور و ثبت سفارش
        customer_for_invoice = {
            'name': full_name,
            'address': address,
            'phone': phone,
        }

        try:
            # ثبت سفارش و آیتم‌ها در دیتابیس
            order_id = create_order_record(user_id, cart, customer_for_invoice, lat, lon)

            # 1. تولید عکس فاکتور
            invoice_img = create_invoice_image(cart, customer_for_invoice)
            await context.bot.send_photo(chat_id=ADMIN_CHAT_ID, photo=invoice_img, caption=f"📄 فاکتور سفارش جدید\n#Order{order_id}")

            # 2. ارسال متن جداگانه
            text_order = (
                f"🔔 سفارش جدید!\n\n"
                f"👤 {customer_for_invoice['name']}\n"
                f"📞 {customer_for_invoice['phone']}\n"
                f"🏠 {customer_for_invoice['address']}\n\n"
                f"🛒 اقلام:\n"
            )
            for i in cart:
                text_order += f"- {i['name']} ({i['qty']} عدد)\n"
            text_order += f"\n💰 مجموع: {total:,} تومان\n#Order{order_id}"

            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text_order)

            # 3. ارسال لوکیشن جداگانه (فقط اگر باشد)
            if lat is not None and lon is not None:
                await context.bot.send_location(chat_id=ADMIN_CHAT_ID, latitude=lat, longitude=lon)

        except Exception as e:
            logging.error(f"Error: {e}")
            await context.bot.send_message(ADMIN_CHAT_ID, "سفارش جدید ثبت شد ولی در تولید/ثبت فاکتور خطایی رخ داد.")

        await update.message.reply_text(
            "🎉 سفارش شما با موفقیت ثبت شد!\nبه زودی با شما تماس می‌گیریم. نوش جان! ❤️",
            reply_markup=ReplyKeyboardRemove(),
        )
        context.user_data.clear()
        return ConversationHandler.END


# --- اجرا ---
def main():
    init_db()

    app = Application.builder().token(TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CATEGORY: [MessageHandler(filters.TEXT, select_category)],
            ITEM: [MessageHandler(filters.TEXT, select_item)],
            QUANTITY: [MessageHandler(filters.TEXT, select_quantity)],
            CHECKOUT: [MessageHandler(filters.TEXT, checkout_choice)],
            CONFIRM_ORDER: [
                MessageHandler(filters.Regex("✅"), start_address_process),
                MessageHandler(filters.Regex("✏️"), edit_cart_menu),
                MessageHandler(filters.Regex("❌"), start),
            ],
            EDIT_CART: [MessageHandler(filters.TEXT, edit_logic)],
            CHANGE_QTY: [MessageHandler(filters.TEXT, change_qty_logic)],
            SWAP_CATEGORY: [MessageHandler(filters.TEXT, swap_category_select)],
            SWAP_ITEM: [MessageHandler(filters.TEXT, swap_item_select)],
            NAME: [MessageHandler(filters.TEXT, handle_name_or_saved)],
            CHOOSE_ADDRESS_METHOD: [MessageHandler(filters.LOCATION | filters.TEXT, handle_location_choice)],
            WAIT_FOR_ADDRESS: [MessageHandler(filters.TEXT, save_manual_address)],
            CONFIRM_GPS_ADDRESS: [MessageHandler(filters.TEXT, confirm_gps_logic)],
            PLAQUE: [MessageHandler(filters.TEXT, get_plaque)],
            UNIT: [MessageHandler(filters.TEXT, get_unit)],
            CONTACT: [MessageHandler(filters.CONTACT, save_contact_and_review)],
            FINAL_CHECK: [MessageHandler(filters.TEXT, final_submit_handler)],
            EDIT_INFO_SELECT: [MessageHandler(filters.TEXT, edit_info_select)],
            EDIT_SPECIFIC_FIELD: [MessageHandler(filters.TEXT | filters.CONTACT, save_specific_field)],
        },
        fallbacks=[CommandHandler("start", start)],
    )
    app.add_handler(conv)

    async def send_sales_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
        total_sales = get_total_sales()
        await update.message.reply_text(f"فروش کل: {total_sales:,} تومان")

    async def send_today_sales(update: Update, context: ContextTypes.DEFAULT_TYPE):
        today_sales = get_today_sales()
        await update.message.reply_text(f"📅 فروش امروز: {today_sales:,} تومان")

    async def send_last_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
        rows = get_last_orders(5)
        if not rows:
            await update.message.reply_text("هنوز سفارشی ثبت نشده است.")
            return
        lines = ["🧾 ۵ سفارش آخر:"]
        for idx, r in enumerate(rows, start=1):
            name = r.get('customer_name') or '-'
            total = f"{int(r.get('total') or 0):,}"
            created_raw = r.get('created_at') or ''
            created = created_raw
            # تلاش برای تبدیل تاریخ میلادی به شمسی در صورت نصب بودن jdatetime
            if created_raw and jdatetime is not None:
                try:
                    dt = datetime.datetime.fromisoformat(created_raw)
                    jdt = jdatetime.datetime.fromgregorian(datetime=dt)
                    created = jdt.strftime("%Y/%m/%d %H:%M")
                except Exception:
                    created = created_raw
            lines.append(f"{idx}) #Order{r['id']} | {name} | {total} تومان | {created}")
        await update.message.reply_text("\n".join(lines))

    # هندلرهای ادمین (فقط برای ADMIN_CHAT_ID)
    app.add_handler(MessageHandler(filters.User(ADMIN_CHAT_ID) & filters.Regex("📊 گزارش فروش"), send_sales_report))
    app.add_handler(MessageHandler(filters.User(ADMIN_CHAT_ID) & filters.Regex("📅 فروش امروز"), send_today_sales))
    app.add_handler(MessageHandler(filters.User(ADMIN_CHAT_ID) & filters.Regex("🧾 ۵ سفارش آخر"), send_last_orders))

    print("Bot is running...")
    app.run_polling()


if __name__ == '__main__':
    main()
