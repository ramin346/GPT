import asyncio
import json
import logging
import math
import os
import time

import aiosqlite
import httpx
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

import updater

load_dotenv()
logging.basicConfig(level=logging.INFO)

# ───────────────────────── تنظیمات ─────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
API_BASE = os.environ.get("API_BASE", "https://canboso.com/api/v2/telegram-buyer")
API_KEY = os.environ["API_KEY"]
# اگر سایت از هدر دیگری استفاده می‌کند (مثلاً X-API-Key) اینجا عوض کن
AUTH_HEADER = os.environ.get("AUTH_HEADER", "Authorization")
AUTH_PREFIX = os.environ.get("AUTH_PREFIX", "Bearer ")
PRICE_MULTIPLIER = float(os.environ.get("PRICE_MULTIPLIER", "1"))  # تبدیل قیمت تأمین‌کننده به تومان
DEFAULT_MARKUP = float(os.environ.get("DEFAULT_MARKUP", "20"))  # درصد سود
CARD_NUMBER = os.environ.get("CARD_NUMBER", "0000-0000-0000-0000")
CARD_OWNER = os.environ.get("CARD_OWNER", "نام صاحب کارت")
CURRENCY = os.environ.get("CURRENCY", "تومان")
SUPPORT = os.environ.get("SUPPORT", "@your_support")
DB_PATH = os.environ.get("DB_PATH", "bot.db")
PAGE_SIZE = 8
# هر چند دقیقه یک‌بار از گیت‌هاب آپدیت چک شود (0 = خاموش)
AUTO_UPDATE_MINUTES = int(os.environ.get("AUTO_UPDATE_MINUTES", "10"))

router = Router()
db: aiosqlite.Connection
user_locks: dict[int, asyncio.Lock] = {}
_products_cache = {"ts": 0.0, "data": []}


# ───────────────────────── دیتابیس ─────────────────────────
async def init_db():
    global db
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, username TEXT, balance INTEGER DEFAULT 0,
            created INTEGER);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, product_id TEXT,
            product_name TEXT, cost INTEGER, price INTEGER, status TEXT,
            result TEXT, created INTEGER);
        CREATE TABLE IF NOT EXISTS topups(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER,
            status TEXT, created INTEGER);
        """
    )
    await db.commit()


async def get_markup() -> float:
    cur = await db.execute("SELECT value FROM settings WHERE key='markup'")
    row = await cur.fetchone()
    return float(row["value"]) if row else DEFAULT_MARKUP


async def set_markup(v: float):
    await db.execute("INSERT OR REPLACE INTO settings VALUES('markup', ?)", (str(v),))
    await db.commit()


async def ensure_user(uid: int, username: str | None):
    await db.execute(
        "INSERT OR IGNORE INTO users(id, username, balance, created) VALUES(?,?,0,?)",
        (uid, username, int(time.time())),
    )
    await db.commit()


async def balance_of(uid: int) -> int:
    cur = await db.execute("SELECT balance FROM users WHERE id=?", (uid,))
    row = await cur.fetchone()
    return row["balance"] if row else 0


async def debit(uid: int, amount: int) -> bool:
    cur = await db.execute(
        "UPDATE users SET balance=balance-? WHERE id=? AND balance>=?", (amount, uid, amount)
    )
    await db.commit()
    return cur.rowcount == 1


async def credit(uid: int, amount: int):
    await db.execute("UPDATE users SET balance=balance+? WHERE id=?", (amount, uid))
    await db.commit()


# ───────────────────────── API تأمین‌کننده ─────────────────────────
def _headers():
    return {AUTH_HEADER: f"{AUTH_PREFIX}{API_KEY}", "Accept": "application/json"}


def normalize_product(p: dict) -> dict:
    """ساختار پاسخ API را به فرمت داخلی تبدیل می‌کند. اگر نام فیلدها فرق داشت اینجا اصلاح کن."""
    pid = p.get("id") or p.get("product_id") or p.get("uuid")
    name = p.get("name") or p.get("title") or str(pid)
    cost = p.get("price") or p.get("cost") or p.get("amount") or 0
    stock = p.get("stock")
    if stock is None:
        stock = p.get("quantity", p.get("count", p.get("available")))
    return {
        "id": str(pid),
        "name": str(name),
        "desc": p.get("description") or p.get("desc") or "",
        "cost": float(cost),
        "stock": stock,
    }


async def fetch_products(force: bool = False) -> list[dict]:
    if not force and time.time() - _products_cache["ts"] < 60 and _products_cache["data"]:
        return _products_cache["data"]
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{API_BASE}/products", headers=_headers())
        r.raise_for_status()
        data = r.json()
    if isinstance(data, dict):
        data = data.get("data") or data.get("products") or data.get("items") or []
    items = [normalize_product(p) for p in data]
    items = [p for p in items if p["stock"] is None or p["stock"] is True or (p["stock"] not in (0, False, "0"))]
    _products_cache.update(ts=time.time(), data=items)
    return items


async def get_product(pid: str) -> dict | None:
    for p in await fetch_products():
        if p["id"] == pid:
            return p
    return None


async def sell_price(p: dict) -> int:
    markup = await get_markup()
    return int(math.ceil(p["cost"] * PRICE_MULTIPLIER * (1 + markup / 100)))


def base_cost(p: dict) -> int:
    return int(math.ceil(p["cost"] * PRICE_MULTIPLIER))


async def api_purchase(pid: str, order_id: int):
    """بدنه‌ی درخواست را طبق داکیومنت Swagger تنظیم کن."""
    payload = {"product_id": pid, "quantity": 1, "order_id": str(order_id)}
    async with httpx.AsyncClient(timeout=45) as client:
        r = await client.post(f"{API_BASE}/purchase", json=payload, headers=_headers())
    return r


def extract_delivery(data) -> str:
    if isinstance(data, dict):
        for k in ("data", "result", "items", "accounts", "delivery", "codes"):
            if k in data and data[k]:
                return extract_delivery(data[k])
        return json.dumps(data, ensure_ascii=False, indent=2)
    if isinstance(data, list):
        return "\n".join(extract_delivery(x) for x in data)
    return str(data)


# ───────────────────────── کیبوردها ─────────────────────────
def main_menu(uid: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="🛍 محصولات", callback_data="products:0")
    kb.button(text="💰 کیف پول", callback_data="wallet")
    kb.button(text="➕ شارژ حساب", callback_data="topup")
    kb.button(text="📦 سفارش‌های من", callback_data="orders")
    kb.button(text="☎️ پشتیبانی", callback_data="support")
    if uid in ADMIN_IDS:
        kb.button(text="⚙️ پنل ادمین", callback_data="admin")
    kb.adjust(1, 2, 2, 1) if uid in ADMIN_IDS else kb.adjust(1, 2, 2)
    return kb.as_markup()


def back_kb(to="home"):
    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 بازگشت", callback_data=to)
    return kb.as_markup()


def fmt(n: int) -> str:
    return f"{n:,}"


# ───────────────────────── هندلرهای کاربر ─────────────────────────
@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await ensure_user(m.from_user.id, m.from_user.username)
    await m.answer("سلام 👋\nبه فروشگاه خوش اومدی. یکی از گزینه‌ها رو انتخاب کن:",
                   reply_markup=main_menu(m.from_user.id))


@router.callback_query(F.data == "home")
async def home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("منوی اصلی:", reply_markup=main_menu(c.from_user.id))
    await c.answer()


@router.callback_query(F.data == "support")
async def support(c: CallbackQuery):
    await c.message.edit_text(f"برای پشتیبانی پیام بده: {SUPPORT}", reply_markup=back_kb())
    await c.answer()


@router.callback_query(F.data == "wallet")
async def wallet(c: CallbackQuery):
    bal = await balance_of(c.from_user.id)
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ شارژ حساب", callback_data="topup")
    kb.button(text="🔙 بازگشت", callback_data="home")
    kb.adjust(1)
    await c.message.edit_text(f"💰 موجودی شما: {fmt(bal)} {CURRENCY}", reply_markup=kb.as_markup())
    await c.answer()


@router.callback_query(F.data.startswith("products:"))
async def products(c: CallbackQuery):
    page = int(c.data.split(":")[1])
    try:
        items = await fetch_products()
    except Exception:
        logging.exception("products fetch failed")
        await c.answer("خطا در دریافت محصولات. کمی بعد تلاش کن.", show_alert=True)
        return
    if not items:
        await c.message.edit_text("فعلاً محصولی موجود نیست.", reply_markup=back_kb())
        return
    total_pages = max(1, math.ceil(len(items) / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    kb = InlineKeyboardBuilder()
    for p in items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
        price = await sell_price(p)
        kb.button(text=f"{p['name']} — {fmt(price)}", callback_data=f"view:{p['id']}")
    kb.adjust(1)
    nav = InlineKeyboardBuilder()
    if page > 0:
        nav.button(text="◀️ قبلی", callback_data=f"products:{page-1}")
    nav.button(text=f"{page+1}/{total_pages}", callback_data="noop")
    if page < total_pages - 1:
        nav.button(text="بعدی ▶️", callback_data=f"products:{page+1}")
    kb.attach(nav)
    kb.row(InlineKeyboardButton(text="🔙 بازگشت", callback_data="home"))
    await c.message.edit_text("🛍 محصولات موجود:", reply_markup=kb.as_markup())
    await c.answer()


@router.callback_query(F.data == "noop")
async def noop(c: CallbackQuery):
    await c.answer()


@router.callback_query(F.data.startswith("view:"))
async def view_product(c: CallbackQuery):
    p = await get_product(c.data.split(":", 1)[1])
    if not p:
        await c.answer("این محصول دیگر موجود نیست.", show_alert=True)
        return
    price = await sell_price(p)
    text = f"🛍 {p['name']}\n\n💵 قیمت: {fmt(price)} {CURRENCY}"
    if p["desc"]:
        text += f"\n\n{p['desc']}"
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ خرید", callback_data=f"buy:{p['id']}")
    kb.button(text="🔙 بازگشت", callback_data="products:0")
    kb.adjust(1)
    await c.message.edit_text(text, reply_markup=kb.as_markup())
    await c.answer()


@router.callback_query(F.data.startswith("buy:"))
async def confirm_buy(c: CallbackQuery):
    p = await get_product(c.data.split(":", 1)[1])
    if not p:
        await c.answer("این محصول دیگر موجود نیست.", show_alert=True)
        return
    price = await sell_price(p)
    bal = await balance_of(c.from_user.id)
    if bal < price:
        kb = InlineKeyboardBuilder()
        kb.button(text="➕ شارژ حساب", callback_data="topup")
        kb.button(text="🔙 بازگشت", callback_data=f"view:{p['id']}")
        kb.adjust(1)
        await c.message.edit_text(
            f"موجودی کافی نیست.\nقیمت: {fmt(price)}\nموجودی شما: {fmt(bal)} {CURRENCY}",
            reply_markup=kb.as_markup())
        await c.answer()
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ تأیید و پرداخت", callback_data=f"pay:{p['id']}")
    kb.button(text="❌ انصراف", callback_data=f"view:{p['id']}")
    kb.adjust(2)
    await c.message.edit_text(
        f"خرید «{p['name']}» به مبلغ {fmt(price)} {CURRENCY} از کیف پول شما کسر می‌شود. تأیید می‌کنی؟",
        reply_markup=kb.as_markup())
    await c.answer()


@router.callback_query(F.data.startswith("pay:"))
async def pay(c: CallbackQuery, bot: Bot):
    uid = c.from_user.id
    lock = user_locks.setdefault(uid, asyncio.Lock())
    if lock.locked():
        await c.answer("درخواست قبلی در حال انجام است...", show_alert=True)
        return
    async with lock:
        p = await get_product(c.data.split(":", 1)[1])
        if not p:
            await c.answer("این محصول دیگر موجود نیست.", show_alert=True)
            return
        price = await sell_price(p)
        cost = base_cost(p)
        if not await debit(uid, price):
            await c.answer("موجودی کافی نیست.", show_alert=True)
            return
        cur = await db.execute(
            "INSERT INTO orders(user_id, product_id, product_name, cost, price, status, created)"
            " VALUES(?,?,?,?,?,'pending',?)",
            (uid, p["id"], p["name"], cost, price, int(time.time())))
        await db.commit()
        order_id = cur.lastrowid
        await c.message.edit_text("⏳ در حال پردازش سفارش...")
        await c.answer()
        try:
            r = await api_purchase(p["id"], order_id)
        except httpx.TimeoutException:
            await db.execute("UPDATE orders SET status='unknown' WHERE id=?", (order_id,))
            await db.commit()
            await c.message.edit_text(
                f"سفارش #{order_id} در حال بررسی است. نتیجه به‌زودی اعلام می‌شود یا با پشتیبانی تماس بگیر.",
                reply_markup=back_kb())
            await notify_admins(bot, f"⚠️ تایم‌اوت در خرید. سفارش #{order_id} کاربر {uid} بررسی دستی لازم دارد.")
            return
        except Exception:
            logging.exception("purchase network error")
            await refund(order_id, uid, price)
            await c.message.edit_text("خطا در ارتباط. مبلغ به کیف پولت برگشت.", reply_markup=back_kb())
            return

        ok = r.status_code < 300
        try:
            body = r.json()
        except Exception:
            body = r.text
        if isinstance(body, dict) and body.get("success") is False:
            ok = False

        if ok:
            result = extract_delivery(body)
            await db.execute("UPDATE orders SET status='done', result=? WHERE id=?", (result, order_id))
            await db.commit()
            await c.message.edit_text(
                f"✅ سفارش #{order_id} با موفقیت انجام شد.\n\n{result}", reply_markup=back_kb())
        else:
            await refund(order_id, uid, price)
            logging.error("purchase failed %s %s", r.status_code, body)
            await c.message.edit_text(
                "❌ خرید انجام نشد و مبلغ به کیف پولت برگشت.", reply_markup=back_kb())
            await notify_admins(bot, f"❌ خرید ناموفق سفارش #{order_id}\nکد: {r.status_code}\n{str(body)[:500]}")


async def refund(order_id: int, uid: int, price: int):
    await credit(uid, price)
    await db.execute("UPDATE orders SET status='failed' WHERE id=?", (order_id,))
    await db.commit()


async def notify_admins(bot: Bot, text: str):
    for a in ADMIN_IDS:
        try:
            await bot.send_message(a, text)
        except Exception:
            pass


@router.callback_query(F.data == "orders")
async def my_orders(c: CallbackQuery):
    cur = await db.execute(
        "SELECT id, product_name, price, status FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10",
        (c.from_user.id,))
    rows = await cur.fetchall()
    icons = {"done": "✅", "failed": "❌", "pending": "⏳", "unknown": "⚠️"}
    text = "📦 آخرین سفارش‌ها:\n\n" + ("\n".join(
        f"{icons.get(r['status'], '•')} #{r['id']} {r['product_name']} — {fmt(r['price'])}" for r in rows
    ) if rows else "هنوز سفارشی نداری.")
    await c.message.edit_text(text, reply_markup=back_kb())
    await c.answer()


# ───────────────────────── شارژ حساب ─────────────────────────
class TopUp(StatesGroup):
    amount = State()
    receipt = State()


@router.callback_query(F.data == "topup")
async def topup(c: CallbackQuery, state: FSMContext):
    await state.set_state(TopUp.amount)
    await c.message.edit_text(f"مبلغ شارژ را به {CURRENCY} (فقط عدد) بفرست:", reply_markup=back_kb())
    await c.answer()


@router.message(TopUp.amount)
async def topup_amount(m: Message, state: FSMContext):
    txt = (m.text or "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")).replace(",", "")
    if not txt.isdigit() or int(txt) <= 0:
        await m.answer("لطفاً فقط عدد معتبر بفرست.")
        return
    await state.update_data(amount=int(txt))
    await state.set_state(TopUp.receipt)
    await m.answer(
        f"مبلغ {fmt(int(txt))} {CURRENCY} را به کارت زیر واریز کن و عکس رسید را بفرست:\n\n"
        f"💳 {CARD_NUMBER}\n👤 {CARD_OWNER}", reply_markup=back_kb())


@router.message(TopUp.receipt, F.photo)
async def topup_receipt(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    amount = data["amount"]
    cur = await db.execute(
        "INSERT INTO topups(user_id, amount, status, created) VALUES(?,?,'pending',?)",
        (m.from_user.id, amount, int(time.time())))
    await db.commit()
    tid = cur.lastrowid
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ تأیید", callback_data=f"ta:{tid}")
    kb.button(text="❌ رد", callback_data=f"tr:{tid}")
    for a in ADMIN_IDS:
        try:
            await bot.send_photo(
                a, m.photo[-1].file_id,
                caption=f"درخواست شارژ #{tid}\nکاربر: {m.from_user.id} (@{m.from_user.username})\nمبلغ: {fmt(amount)} {CURRENCY}",
                reply_markup=kb.as_markup())
        except Exception:
            pass
    await state.clear()
    await m.answer("رسید ثبت شد. بعد از تأیید، حسابت شارژ می‌شود.", reply_markup=main_menu(m.from_user.id))


@router.message(TopUp.receipt)
async def topup_need_photo(m: Message):
    await m.answer("لطفاً عکس رسید را بفرست.")


@router.callback_query(F.data.startswith(("ta:", "tr:")))
async def topup_decide(c: CallbackQuery, bot: Bot):
    if c.from_user.id not in ADMIN_IDS:
        await c.answer("دسترسی نداری.", show_alert=True)
        return
    action, tid = c.data.split(":")
    cur = await db.execute("SELECT * FROM topups WHERE id=?", (int(tid),))
    t = await cur.fetchone()
    if not t or t["status"] != "pending":
        await c.answer("این درخواست قبلاً بررسی شده.", show_alert=True)
        return
    if action == "ta":
        await credit(t["user_id"], t["amount"])
        await db.execute("UPDATE topups SET status='approved' WHERE id=?", (t["id"],))
        msg, note = f"✅ شارژ {fmt(t['amount'])} {CURRENCY} تأیید شد.", "تأیید شد ✅"
    else:
        await db.execute("UPDATE topups SET status='rejected' WHERE id=?", (t["id"],))
        msg, note = "❌ درخواست شارژ رد شد. با پشتیبانی تماس بگیر.", "رد شد ❌"
    await db.commit()
    try:
        await bot.send_message(t["user_id"], msg)
    except Exception:
        pass
    await c.message.edit_caption(caption=(c.message.caption or "") + f"\n\n{note}")
    await c.answer()


# ───────────────────────── پنل ادمین ─────────────────────────
class AdminStates(StatesGroup):
    markup = State()
    manual = State()


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


@router.callback_query(F.data == "admin")
async def admin_panel(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("دسترسی نداری.", show_alert=True)
        return
    await state.clear()
    markup = await get_markup()
    kb = InlineKeyboardBuilder()
    kb.button(text=f"📈 تغییر سود (الان {markup:g}%)", callback_data="adm_markup")
    kb.button(text="📊 آمار", callback_data="adm_stats")
    kb.button(text="💳 شارژ دستی کاربر", callback_data="adm_manual")
    kb.button(text="🔄 به‌روزرسانی لیست محصولات", callback_data="adm_refresh")
    kb.button(text="⬆️ آپدیت ربات از گیت‌هاب", callback_data="adm_update")
    kb.button(text="🔙 بازگشت", callback_data="home")
    kb.adjust(1)
    await c.message.edit_text(
        f"⚙️ پنل ادمین\nنسخه: {updater.current_version()}", reply_markup=kb.as_markup())
    await c.answer()


@router.callback_query(F.data == "adm_markup")
async def adm_markup(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return
    await state.set_state(AdminStates.markup)
    await c.message.edit_text("درصد سود جدید را بفرست (مثلاً 25):", reply_markup=back_kb("admin"))
    await c.answer()


@router.message(AdminStates.markup)
async def adm_markup_set(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    try:
        v = float((m.text or "").replace("%", "").strip())
        if v < 0:
            raise ValueError
    except ValueError:
        await m.answer("عدد معتبر بفرست.")
        return
    await set_markup(v)
    await state.clear()
    await m.answer(f"✅ سود روی {v:g}% تنظیم شد.", reply_markup=main_menu(m.from_user.id))


@router.callback_query(F.data == "adm_stats")
async def adm_stats(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return
    u = await (await db.execute("SELECT COUNT(*) n, COALESCE(SUM(balance),0) b FROM users")).fetchone()
    o = await (await db.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(price),0) s, COALESCE(SUM(price-cost),0) p "
        "FROM orders WHERE status='done'")).fetchone()
    unk = await (await db.execute("SELECT COUNT(*) n FROM orders WHERE status='unknown'")).fetchone()
    await c.message.edit_text(
        f"👥 کاربران: {u['n']}\n💰 مجموع موجودی کاربران: {fmt(u['b'])}\n\n"
        f"🧾 سفارش موفق: {o['n']}\n💵 فروش کل: {fmt(o['s'])}\n📈 سود کل: {fmt(o['p'])} {CURRENCY}\n"
        f"⚠️ سفارش نامشخص: {unk['n']}",
        reply_markup=back_kb("admin"))
    await c.answer()


@router.callback_query(F.data == "adm_refresh")
async def adm_refresh(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return
    try:
        items = await fetch_products(force=True)
        await c.answer(f"{len(items)} محصول دریافت شد.", show_alert=True)
    except Exception as e:
        await c.answer(f"خطا: {e}"[:190], show_alert=True)


@router.callback_query(F.data == "adm_manual")
async def adm_manual(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return
    await state.set_state(AdminStates.manual)
    await c.message.edit_text("فرمت: «آیدی‌کاربر مبلغ» مثل  123456 50000\n(مبلغ منفی = کسر)",
                              reply_markup=back_kb("admin"))
    await c.answer()


@router.message(AdminStates.manual)
async def adm_manual_do(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    try:
        uid, amount = (int(x) for x in (m.text or "").split())
    except ValueError:
        await m.answer("فرمت اشتباه است.")
        return
    await ensure_user(uid, None)
    await credit(uid, amount)
    await state.clear()
    await m.answer(f"✅ انجام شد. موجودی جدید: {fmt(await balance_of(uid))}",
                   reply_markup=main_menu(m.from_user.id))


# ───────────────────────── آپدیت خودکار ─────────────────────────
_last_update_error = ""


async def run_update(bot: Bot, manual: bool = False) -> tuple[str, str]:
    global _last_update_error
    status, msg = await asyncio.to_thread(updater.check_and_update)
    if status == "updated":
        await notify_admins(bot, f"✅ آپدیت جدید دریافت شد. در حال ریستارت...\n\n{msg[:800]}")
        # صبر کن تا خریدِ در حال انجام تمام شود (حداکثر ۶۰ ثانیه)
        for _ in range(60):
            if not any(lk.locked() for lk in user_locks.values()):
                break
            await asyncio.sleep(1)
        try:
            await bot.session.close()
        except Exception:
            pass
        updater.restart()
    elif status == "error":
        if manual or msg != _last_update_error:
            _last_update_error = msg
            if not manual:
                await notify_admins(bot, f"❌ خطا در آپدیت خودکار:\n{msg[:800]}")
    else:
        _last_update_error = ""
    return status, msg


async def auto_update_loop(bot: Bot):
    if AUTO_UPDATE_MINUTES <= 0:
        logging.info("auto update disabled")
        return
    await asyncio.sleep(30)
    while True:
        try:
            await run_update(bot)
        except Exception:
            logging.exception("auto update loop error")
        await asyncio.sleep(AUTO_UPDATE_MINUTES * 60)


async def _manual_update_reply(bot: Bot, send):
    status, msg = await run_update(bot, manual=True)
    if status == "none":
        await send(f"✅ ربات به‌روز است. نسخه: {updater.current_version()}")
    elif status == "error":
        await send(f"❌ خطا در آپدیت:\n{msg[:800]}")


@router.callback_query(F.data == "adm_update")
async def adm_update(c: CallbackQuery, bot: Bot):
    if not is_admin(c.from_user.id):
        await c.answer("دسترسی نداری.", show_alert=True)
        return
    await c.answer("در حال بررسی آپدیت...")
    await _manual_update_reply(bot, c.message.answer)


@router.message(Command("update"))
async def cmd_update(m: Message, bot: Bot):
    if not is_admin(m.from_user.id):
        return
    await m.answer("⏳ در حال بررسی آپدیت...")
    await _manual_update_reply(bot, m.answer)


# ───────────────────────── اجرا ─────────────────────────
async def main():
    await init_db()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    asyncio.create_task(auto_update_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
