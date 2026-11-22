import asyncio
import html
import io
import logging
import os
import re
import time

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()

import updater  # noqa: E402

logging.basicConfig(level=logging.INFO)

# ───────────────────────── تنظیمات ─────────────────────────
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
CARD_NUMBER = os.environ.get("CARD_NUMBER", "0000-0000-0000-0000")
CARD_OWNER = os.environ.get("CARD_OWNER", "نام صاحب کارت")
CURRENCY = os.environ.get("CURRENCY", "تومان")
SUPPORT = os.environ.get("SUPPORT", "@your_support")
DB_PATH = os.environ.get("DB_PATH", "shop.db")
LOW_STOCK = int(os.environ.get("LOW_STOCK", "3"))  # هشدار به ادمین وقتی موجودی کمتر یا مساوی این عدد شد
AUTO_UPDATE_MINUTES = int(os.environ.get("AUTO_UPDATE_MINUTES", "10"))  # 0 = خاموش

router = Router()  # کاربران
admin = Router()  # فقط ادمین‌ها
admin.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

db: aiosqlite.Connection
user_locks: dict[int, asyncio.Lock] = {}
purchase_lock = asyncio.Lock()


# ───────────────────────── دیتابیس ─────────────────────────
async def init_db():
    global db
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, username TEXT, balance INTEGER DEFAULT 0, created INTEGER);
        CREATE TABLE IF NOT EXISTS categories(
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, sort INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT, category_id INTEGER, name TEXT NOT NULL,
            description TEXT DEFAULT '', price INTEGER DEFAULT 0, cost INTEGER DEFAULT 0,
            active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS stock(
            id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER, content TEXT NOT NULL,
            sold INTEGER DEFAULT 0, sold_to INTEGER, sold_at INTEGER, order_id INTEGER);
        CREATE INDEX IF NOT EXISTS idx_stock_pid ON stock(product_id, sold);
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, product_id INTEGER,
            product_name TEXT, price INTEGER, cost INTEGER, content TEXT, created INTEGER);
        CREATE TABLE IF NOT EXISTS topups(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER,
            status TEXT, created INTEGER);
        """
    )
    await db.commit()


async def q1(sql: str, args: tuple = ()):
    cur = await db.execute(sql, args)
    return await cur.fetchone()


async def qa(sql: str, args: tuple = ()):
    cur = await db.execute(sql, args)
    return await cur.fetchall()


async def ex(sql: str, args: tuple = ()):
    cur = await db.execute(sql, args)
    await db.commit()
    return cur


async def ensure_user(uid: int, username: str | None):
    await ex(
        "INSERT OR IGNORE INTO users(id, username, balance, created) VALUES(?,?,0,?)",
        (uid, username, int(time.time())),
    )


async def balance_of(uid: int) -> int:
    row = await q1("SELECT balance FROM users WHERE id=?", (uid,))
    return row["balance"] if row else 0


async def credit(uid: int, amount: int):
    await ensure_user(uid, None)
    await ex("UPDATE users SET balance=balance+? WHERE id=?", (amount, uid))


async def stock_count(pid: int) -> int:
    row = await q1("SELECT COUNT(*) n FROM stock WHERE product_id=? AND sold=0", (pid,))
    return row["n"]


# ───────────────────────── ابزارهای کمکی ─────────────────────────
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def parse_int(text: str | None) -> int | None:
    t = (text or "").translate(_DIGITS).replace(",", "").replace("،", "").strip()
    return int(t) if t.isdigit() else None


def fmt(n: int) -> str:
    return f"{n:,}"


def kb(*rows):
    """هر ردیف: لیستی از (متن، callback_data)"""
    b = InlineKeyboardBuilder()
    for row in rows:
        b.row(*[InlineKeyboardButton(text=t, callback_data=d) for t, d in row])
    return b.as_markup()


async def edit(c: CallbackQuery, text: str, markup=None, parse_mode: str | None = None):
    try:
        await c.message.edit_text(text, reply_markup=markup, parse_mode=parse_mode)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            await c.message.answer(text, reply_markup=markup, parse_mode=parse_mode)


def split_items(text: str) -> list[str]:
    """هر خط = یک آیتم. اگر خطی فقط «---» باشد، آیتم‌ها با آن جدا می‌شوند (برای آیتم چندخطی)."""
    text = text.replace("\r\n", "\n").strip()
    if re.search(r"^\s*---\s*$", text, flags=re.M):
        parts = re.split(r"^\s*---\s*$", text, flags=re.M)
    else:
        parts = text.split("\n")
    return [p.strip() for p in parts if p.strip()]


def main_menu(uid: int):
    rows = [
        [("🛍 محصولات", "shop")],
        [("💰 کیف پول", "wallet"), ("➕ شارژ حساب", "topup")],
        [("📦 سفارش‌های من", "orders"), ("☎️ پشتیبانی", "support")],
    ]
    if uid in ADMIN_IDS:
        rows.append([("⚙️ پنل ادمین", "admin")])
    return kb(*rows)


BACK_HOME = kb([("🔙 بازگشت", "home")])


# ───────────────────────── بخش کاربر ─────────────────────────
@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await ensure_user(m.from_user.id, m.from_user.username)
    await m.answer("سلام 👋\nبه فروشگاه خوش اومدی. یکی از گزینه‌ها رو انتخاب کن:",
                   reply_markup=main_menu(m.from_user.id))


@router.callback_query(F.data == "home")
async def home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(c, "منوی اصلی:", main_menu(c.from_user.id))
    await c.answer()


@router.callback_query(F.data == "noop")
async def noop(c: CallbackQuery):
    await c.answer()


@router.callback_query(F.data == "support")
async def support(c: CallbackQuery):
    await edit(c, f"برای پشتیبانی پیام بده: {SUPPORT}", BACK_HOME)
    await c.answer()


@router.callback_query(F.data == "wallet")
async def wallet(c: CallbackQuery):
    bal = await balance_of(c.from_user.id)
    await edit(c, f"💰 موجودی شما: {fmt(bal)} {CURRENCY}",
               kb([("➕ شارژ حساب", "topup")], [("🔙 بازگشت", "home")]))
    await c.answer()


@router.callback_query(F.data == "shop")
async def shop(c: CallbackQuery, state: FSMContext):
    await state.clear()
    cats = await qa(
        "SELECT c.* FROM categories c WHERE EXISTS "
        "(SELECT 1 FROM products p WHERE p.category_id=c.id AND p.active=1) ORDER BY c.sort, c.id")
    if not cats:
        await edit(c, "فعلاً محصولی موجود نیست.", BACK_HOME)
        await c.answer()
        return
    rows = [[(f"📂 {x['name']}", f"cat:{x['id']}")] for x in cats]
    rows.append([("🔙 بازگشت", "home")])
    await edit(c, "یک دسته را انتخاب کن:", kb(*rows))
    await c.answer()


PRODUCTS_WITH_QTY = (
    "SELECT p.*, (SELECT COUNT(*) FROM stock s WHERE s.product_id=p.id AND s.sold=0) AS qty "
    "FROM products p "
)


@router.callback_query(F.data.startswith("cat:"))
async def category(c: CallbackQuery):
    cid = int(c.data.split(":")[1])
    cat = await q1("SELECT * FROM categories WHERE id=?", (cid,))
    if not cat:
        await c.answer("این دسته وجود ندارد.", show_alert=True)
        return
    prods = await qa(PRODUCTS_WITH_QTY + "WHERE p.category_id=? AND p.active=1 ORDER BY p.id", (cid,))
    rows = []
    for p in prods:
        label = f"{p['name']} — {fmt(p['price'])}" + ("" if p["qty"] > 0 else "  ❌ ناموجود")
        rows.append([(label, f"prod:{p['id']}")])
    rows.append([("🔙 بازگشت", "shop")])
    await edit(c, f"📂 {cat['name']}", kb(*rows))
    await c.answer()


@router.callback_query(F.data.startswith("prod:"))
async def product_view(c: CallbackQuery):
    pid = int(c.data.split(":")[1])
    p = await q1(PRODUCTS_WITH_QTY + "WHERE p.id=? AND p.active=1", (pid,))
    if not p:
        await c.answer("این محصول دیگر موجود نیست.", show_alert=True)
        return
    text = f"🛍 {p['name']}\n\n💵 قیمت: {fmt(p['price'])} {CURRENCY}\n"
    text += "✅ موجود" if p["qty"] > 0 else "❌ ناموجود"
    if p["description"]:
        text += f"\n\n{p['description']}"
    rows = []
    if p["qty"] > 0:
        rows.append([("✅ خرید", f"buy:{pid}")])
    rows.append([("🔙 بازگشت", f"cat:{p['category_id']}")])
    await edit(c, text, kb(*rows))
    await c.answer()


@router.callback_query(F.data.startswith("buy:"))
async def confirm_buy(c: CallbackQuery):
    pid = int(c.data.split(":")[1])
    p = await q1(PRODUCTS_WITH_QTY + "WHERE p.id=? AND p.active=1", (pid,))
    if not p or p["qty"] <= 0:
        await c.answer("این محصول ناموجود است.", show_alert=True)
        return
    bal = await balance_of(c.from_user.id)
    if bal < p["price"]:
        await edit(
            c, f"موجودی کافی نیست.\nقیمت: {fmt(p['price'])}\nموجودی شما: {fmt(bal)} {CURRENCY}",
            kb([("➕ شارژ حساب", "topup")], [("🔙 بازگشت", f"prod:{pid}")]))
        await c.answer()
        return
    await edit(
        c, f"خرید «{p['name']}» به مبلغ {fmt(p['price'])} {CURRENCY} از کیف پول شما کسر می‌شود. تأیید می‌کنی؟",
        kb([("✅ تأیید و پرداخت", f"pay:{pid}"), ("❌ انصراف", f"prod:{pid}")]))
    await c.answer()


async def do_purchase(uid: int, pid: int) -> tuple[str, dict | None]:
    """کسر موجودی + برداشتن یک آیتم از انبار + ثبت سفارش، همه پشت یک قفل."""
    async with purchase_lock:
        p = await q1("SELECT * FROM products WHERE id=? AND active=1", (pid,))
        if not p:
            return "gone", None
        item = await q1(
            "SELECT id, content FROM stock WHERE product_id=? AND sold=0 ORDER BY id LIMIT 1", (pid,))
        if not item:
            return "nostock", None
        try:
            cur = await db.execute(
                "UPDATE users SET balance=balance-? WHERE id=? AND balance>=?",
                (p["price"], uid, p["price"]))
            if cur.rowcount != 1:
                await db.rollback()
                return "nofunds", None
            cur = await db.execute(
                "INSERT INTO orders(user_id, product_id, product_name, price, cost, content, created)"
                " VALUES(?,?,?,?,?,?,?)",
                (uid, pid, p["name"], p["price"], p["cost"], item["content"], int(time.time())))
            oid = cur.lastrowid
            await db.execute(
                "UPDATE stock SET sold=1, sold_to=?, sold_at=?, order_id=? WHERE id=?",
                (uid, int(time.time()), oid, item["id"]))
            await db.commit()
        except Exception:
            await db.rollback()
            logging.exception("purchase failed")
            return "error", None
        remaining = await stock_count(pid)
        return "ok", {"order_id": oid, "name": p["name"], "content": item["content"], "remaining": remaining}


def delivery_text(order_id: int, name: str, content: str) -> str:
    return (f"✅ سفارش #{order_id} با موفقیت انجام شد.\n\n🛍 {html.escape(name)}\n\n"
            f"<code>{html.escape(content)}</code>")


@router.callback_query(F.data.startswith("pay:"))
async def pay(c: CallbackQuery, bot: Bot):
    uid = c.from_user.id
    lock = user_locks.setdefault(uid, asyncio.Lock())
    if lock.locked():
        await c.answer("درخواست قبلی در حال انجام است...", show_alert=True)
        return
    async with lock:
        pid = int(c.data.split(":")[1])
        status, info = await do_purchase(uid, pid)
        if status == "ok":
            await edit(c, delivery_text(info["order_id"], info["name"], info["content"]),
                       BACK_HOME, parse_mode="HTML")
            await c.answer()
            if info["remaining"] <= LOW_STOCK:
                await notify_admins(
                    bot, f"⚠️ موجودی «{info['name']}» رو به اتمامه: {info['remaining']} عدد باقی مانده.")
        elif status == "nofunds":
            await c.answer("موجودی کافی نیست.", show_alert=True)
        elif status == "nostock":
            await edit(c, "متأسفانه این محصول همین الان ناموجود شد.", BACK_HOME)
            await c.answer()
        elif status == "gone":
            await c.answer("این محصول دیگر موجود نیست.", show_alert=True)
        else:
            await c.answer("خطای داخلی. دوباره تلاش کن.", show_alert=True)


async def notify_admins(bot: Bot, text: str):
    for a in ADMIN_IDS:
        try:
            await bot.send_message(a, text)
        except Exception:
            pass


@router.callback_query(F.data == "orders")
async def my_orders(c: CallbackQuery):
    rows = await qa(
        "SELECT id, product_name FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10", (c.from_user.id,))
    if not rows:
        await edit(c, "هنوز سفارشی نداری.", BACK_HOME)
    else:
        btns = [[(f"#{r['id']} — {r['product_name']}", f"order:{r['id']}")] for r in rows]
        btns.append([("🔙 بازگشت", "home")])
        await edit(c, "📦 آخرین سفارش‌ها (برای دیدن محصول روی سفارش بزن):", kb(*btns))
    await c.answer()


@router.callback_query(F.data.startswith("order:"))
async def order_view(c: CallbackQuery):
    oid = int(c.data.split(":")[1])
    o = await q1("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, c.from_user.id))
    if not o:
        await c.answer("سفارش پیدا نشد.", show_alert=True)
        return
    await edit(c, delivery_text(o["id"], o["product_name"], o["content"]),
               kb([("🔙 بازگشت", "orders")]), parse_mode="HTML")
    await c.answer()


# ───────────────────────── شارژ حساب ─────────────────────────
class TopUp(StatesGroup):
    amount = State()
    receipt = State()


@router.callback_query(F.data == "topup")
async def topup(c: CallbackQuery, state: FSMContext):
    await state.set_state(TopUp.amount)
    await edit(c, f"مبلغ شارژ را به {CURRENCY} (فقط عدد) بفرست:", BACK_HOME)
    await c.answer()


@router.message(TopUp.amount)
async def topup_amount(m: Message, state: FSMContext):
    amount = parse_int(m.text)
    if not amount or amount <= 0:
        await m.answer("لطفاً فقط عدد معتبر بفرست.")
        return
    await state.update_data(amount=amount)
    await state.set_state(TopUp.receipt)
    await m.answer(
        f"مبلغ {fmt(amount)} {CURRENCY} را به کارت زیر واریز کن و عکس رسید را بفرست:\n\n"
        f"💳 {CARD_NUMBER}\n👤 {CARD_OWNER}", reply_markup=BACK_HOME)


@router.message(TopUp.receipt, F.photo)
async def topup_receipt(m: Message, state: FSMContext, bot: Bot):
    amount = (await state.get_data())["amount"]
    await ensure_user(m.from_user.id, m.from_user.username)
    cur = await ex("INSERT INTO topups(user_id, amount, status, created) VALUES(?,?,'pending',?)",
                   (m.from_user.id, amount, int(time.time())))
    tid = cur.lastrowid
    markup = kb([("✅ تأیید", f"ta:{tid}"), ("❌ رد", f"tr:{tid}")])
    for a in ADMIN_IDS:
        try:
            await bot.send_photo(
                a, m.photo[-1].file_id,
                caption=(f"درخواست شارژ #{tid}\nکاربر: {m.from_user.id} (@{m.from_user.username})\n"
                         f"مبلغ: {fmt(amount)} {CURRENCY}"),
                reply_markup=markup)
        except Exception:
            pass
    await state.clear()
    await m.answer("رسید ثبت شد. بعد از تأیید، حسابت شارژ می‌شود.", reply_markup=main_menu(m.from_user.id))


@router.message(TopUp.receipt)
async def topup_need_photo(m: Message):
    await m.answer("لطفاً عکس رسید را بفرست.")


# کاربر عادی که دکمه‌های ادمین را بزند
@router.callback_query(F.data.startswith(("admin", "adm_", "acat", "aprod", "astock", "aedit", "ta:", "tr:")))
async def not_admin(c: CallbackQuery):
    await c.answer("دسترسی نداری.", show_alert=True)


# ───────────────────────── پنل ادمین ─────────────────────────
class Flow(StatesGroup):
    new_cat = State()
    ren_cat = State()
    p_name = State()
    p_price = State()
    p_cost = State()
    p_desc = State()
    add_stock = State()
    edit_value = State()
    manual = State()


@admin.callback_query(F.data == "admin")
async def admin_panel(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(
        c, f"⚙️ پنل ادمین\nنسخه: {updater.current_version()}",
        kb([("📂 دسته‌ها و محصولات", "adm_cats")],
           [("📊 آمار", "adm_stats"), ("💳 شارژ دستی کاربر", "adm_manual")],
           [("⬆️ آپدیت ربات از گیت‌هاب", "adm_update")],
           [("🔙 بازگشت", "home")]))
    await c.answer()


# ── دسته‌ها ──
@admin.callback_query(F.data == "adm_cats")
async def adm_cats(c: CallbackQuery, state: FSMContext):
    await state.clear()
    cats = await qa("SELECT * FROM categories ORDER BY sort, id")
    rows = [[(f"📂 {x['name']}", f"acat:{x['id']}")] for x in cats]
    rows.append([("➕ دسته‌ی جدید", "acat_new")])
    rows.append([("🔙 بازگشت", "admin")])
    text = "📂 دسته‌ها:" if cats else "هنوز دسته‌ای نساختی. با «➕ دسته‌ی جدید» شروع کن (مثلاً Fastly یا ChatGPT)."
    await edit(c, text, kb(*rows))
    await c.answer()


@admin.callback_query(F.data == "acat_new")
async def acat_new(c: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.new_cat)
    await edit(c, "اسم دسته‌ی جدید را بفرست (مثلاً ChatGPT):", kb([("🔙 انصراف", "adm_cats")]))
    await c.answer()


@admin.message(Flow.new_cat)
async def new_cat_name(m: Message, state: FSMContext):
    name = (m.text or "").strip()
    if not name:
        await m.answer("اسم معتبر بفرست.")
        return
    cur = await ex("INSERT INTO categories(name) VALUES(?)", (name,))
    await state.clear()
    await m.answer(f"✅ دسته‌ی «{name}» ساخته شد.", reply_markup=kb(
        [("➕ افزودن محصول به این دسته", f"aprod_new:{cur.lastrowid}")],
        [("📂 همه‌ی دسته‌ها", "adm_cats")]))


async def category_admin_view(cid: int):
    cat = await q1("SELECT * FROM categories WHERE id=?", (cid,))
    if not cat:
        return None, None
    prods = await qa(PRODUCTS_WITH_QTY + "WHERE p.category_id=? ORDER BY p.id", (cid,))
    rows = [[(f"{p['name']} — {fmt(p['price'])} (موجودی {p['qty']})", f"aprod:{p['id']}")] for p in prods]
    rows += [
        [("➕ محصول جدید", f"aprod_new:{cid}")],
        [("✏️ تغییر نام دسته", f"acat_ren:{cid}"), ("🗑 حذف دسته", f"acat_del:{cid}")],
        [("🔙 بازگشت", "adm_cats")],
    ]
    return f"📂 {cat['name']}\nمحصولات این دسته:", kb(*rows)


@admin.callback_query(F.data.startswith("acat:"))
async def acat(c: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = await category_admin_view(int(c.data.split(":")[1]))
    if not text:
        await c.answer("دسته پیدا نشد.", show_alert=True)
        return
    await edit(c, text, markup)
    await c.answer()


@admin.callback_query(F.data.startswith("acat_ren:"))
async def acat_ren(c: CallbackQuery, state: FSMContext):
    cid = int(c.data.split(":")[1])
    await state.set_state(Flow.ren_cat)
    await state.update_data(cid=cid)
    await edit(c, "اسم جدید دسته را بفرست:", kb([("🔙 انصراف", f"acat:{cid}")]))
    await c.answer()


@admin.message(Flow.ren_cat)
async def ren_cat_name(m: Message, state: FSMContext):
    name = (m.text or "").strip()
    if not name:
        await m.answer("اسم معتبر بفرست.")
        return
    cid = (await state.get_data())["cid"]
    await ex("UPDATE categories SET name=? WHERE id=?", (name, cid))
    await state.clear()
    text, markup = await category_admin_view(cid)
    await m.answer("✅ تغییر کرد.\n\n" + text, reply_markup=markup)


@admin.callback_query(F.data.startswith("acat_del:"))
async def acat_del(c: CallbackQuery):
    cid = int(c.data.split(":")[1])
    await edit(c, "⚠️ با حذف دسته، همه‌ی محصولات و موجودی‌های فروش‌نرفته‌ی آن هم پاک می‌شود. مطمئنی؟",
               kb([("🗑 بله، حذف شود", f"acat_delok:{cid}"), ("❌ انصراف", f"acat:{cid}")]))
    await c.answer()


@admin.callback_query(F.data.startswith("acat_delok:"))
async def acat_delok(c: CallbackQuery):
    cid = int(c.data.split(":")[1])
    await db.execute("DELETE FROM stock WHERE product_id IN (SELECT id FROM products WHERE category_id=?)", (cid,))
    await db.execute("DELETE FROM products WHERE category_id=?", (cid,))
    await db.execute("DELETE FROM categories WHERE id=?", (cid,))
    await db.commit()
    await c.answer("حذف شد.")
    cats = await qa("SELECT * FROM categories ORDER BY sort, id")
    rows = [[(f"📂 {x['name']}", f"acat:{x['id']}")] for x in cats]
    rows += [[("➕ دسته‌ی جدید", "acat_new")], [("🔙 بازگشت", "admin")]]
    await edit(c, "📂 دسته‌ها:", kb(*rows))


# ── محصولات ──
@admin.callback_query(F.data.startswith("aprod_new:"))
async def aprod_new(c: CallbackQuery, state: FSMContext):
    cid = int(c.data.split(":")[1])
    await state.set_state(Flow.p_name)
    await state.update_data(cid=cid)
    await edit(c, "اسم محصول را بفرست (مثلاً «اکانت ChatGPT Plus یک‌ماهه»):",
               kb([("🔙 انصراف", f"acat:{cid}")]))
    await c.answer()


@admin.message(Flow.p_name)
async def p_name(m: Message, state: FSMContext):
    name = (m.text or "").strip()
    if not name:
        await m.answer("اسم معتبر بفرست.")
        return
    await state.update_data(name=name)
    await state.set_state(Flow.p_price)
    await m.answer(f"قیمت فروش را به {CURRENCY} بفرست (فقط عدد):")


@admin.message(Flow.p_price)
async def p_price(m: Message, state: FSMContext):
    v = parse_int(m.text)
    if v is None or v <= 0:
        await m.answer("عدد معتبر بفرست.")
        return
    await state.update_data(price=v)
    await state.set_state(Flow.p_cost)
    await m.answer("قیمت خرید (تمام‌شده) هر عدد را بفرست تا سود محاسبه شود. اگر نمی‌خوای، 0 بفرست:")


@admin.message(Flow.p_cost)
async def p_cost(m: Message, state: FSMContext):
    v = parse_int(m.text)
    if v is None:
        await m.answer("عدد معتبر بفرست (یا 0).")
        return
    await state.update_data(cost=v)
    await state.set_state(Flow.p_desc)
    await m.answer("توضیحات محصول را بفرست (برای ردکردن، علامت - بفرست):")


@admin.message(Flow.p_desc)
async def p_desc(m: Message, state: FSMContext):
    d = (m.text or "").strip()
    desc = "" if d == "-" else d
    data = await state.get_data()
    cur = await ex(
        "INSERT INTO products(category_id, name, description, price, cost) VALUES(?,?,?,?,?)",
        (data["cid"], data["name"], desc, data["price"], data["cost"]))
    await state.clear()
    text, markup = await product_admin_view(cur.lastrowid)
    await m.answer("✅ محصول ساخته شد. حالا موجودی (اکانت/کد) را اضافه کن.\n\n" + text, reply_markup=markup)


async def product_admin_view(pid: int):
    p = await q1(
        "SELECT p.*, c.name AS cname FROM products p JOIN categories c ON c.id=p.category_id WHERE p.id=?", (pid,))
    if not p:
        return None, None
    qty = await stock_count(pid)
    sold = (await q1("SELECT COUNT(*) n FROM stock WHERE product_id=? AND sold=1", (pid,)))["n"]
    profit = f"\n📈 سود هر فروش: {fmt(p['price'] - p['cost'])}" if p["cost"] else ""
    text = (f"🛍 {p['name']}\n📂 دسته: {p['cname']}\n💵 قیمت فروش: {fmt(p['price'])} {CURRENCY}\n"
            f"🏷 قیمت خرید: {fmt(p['cost'])}{profit}\n📦 موجودی: {qty}\n✅ فروخته‌شده: {sold}")
    if p["description"]:
        text += f"\n\n📝 {p['description']}"
    markup = kb(
        [("➕ افزودن موجودی", f"astock_add:{pid}")],
        [("🧹 پاک‌کردن موجودی فروش‌نرفته", f"astock_clear:{pid}")],
        [("✏️ نام", f"aedit:name:{pid}"), ("💵 قیمت فروش", f"aedit:price:{pid}")],
        [("🏷 قیمت خرید", f"aedit:cost:{pid}"), ("📝 توضیحات", f"aedit:desc:{pid}")],
        [("🗑 حذف محصول", f"aprod_del:{pid}")],
        [("🔙 بازگشت", f"acat:{p['category_id']}")],
    )
    return text, markup


@admin.callback_query(F.data.startswith("aprod:"))
async def aprod(c: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = await product_admin_view(int(c.data.split(":")[1]))
    if not text:
        await c.answer("محصول پیدا نشد.", show_alert=True)
        return
    await edit(c, text, markup)
    await c.answer()


@admin.callback_query(F.data.startswith("aedit:"))
async def aedit(c: CallbackQuery, state: FSMContext):
    _, field, pid = c.data.split(":")
    labels = {"name": "اسم جدید", "price": f"قیمت فروش جدید ({CURRENCY})",
              "cost": "قیمت خرید جدید", "desc": "توضیحات جدید (برای پاک‌کردن - بفرست)"}
    await state.set_state(Flow.edit_value)
    await state.update_data(pid=int(pid), field=field)
    await edit(c, f"{labels[field]} را بفرست:", kb([("🔙 انصراف", f"aprod:{pid}")]))
    await c.answer()


@admin.message(Flow.edit_value)
async def edit_value(m: Message, state: FSMContext):
    data = await state.get_data()
    pid, field = data["pid"], data["field"]
    raw = (m.text or "").strip()
    if field in ("price", "cost"):
        v = parse_int(raw)
        if v is None or (field == "price" and v <= 0):
            await m.answer("عدد معتبر بفرست.")
            return
        await ex(f"UPDATE products SET {field}=? WHERE id=?", (v, pid))
    elif field == "name":
        if not raw:
            await m.answer("اسم معتبر بفرست.")
            return
        await ex("UPDATE products SET name=? WHERE id=?", (raw, pid))
    else:
        await ex("UPDATE products SET description=? WHERE id=?", ("" if raw == "-" else raw, pid))
    await state.clear()
    text, markup = await product_admin_view(pid)
    await m.answer("✅ تغییر کرد.\n\n" + text, reply_markup=markup)


@admin.callback_query(F.data.startswith("aprod_del:"))
async def aprod_del(c: CallbackQuery):
    pid = int(c.data.split(":")[1])
    await edit(c, "⚠️ محصول و موجودی فروش‌نرفته‌اش حذف می‌شود (سابقه‌ی سفارش‌ها می‌ماند). مطمئنی؟",
               kb([("🗑 بله، حذف شود", f"aprod_delok:{pid}"), ("❌ انصراف", f"aprod:{pid}")]))
    await c.answer()


@admin.callback_query(F.data.startswith("aprod_delok:"))
async def aprod_delok(c: CallbackQuery):
    pid = int(c.data.split(":")[1])
    p = await q1("SELECT category_id FROM products WHERE id=?", (pid,))
    await db.execute("DELETE FROM stock WHERE product_id=? AND sold=0", (pid,))
    await db.execute("DELETE FROM products WHERE id=?", (pid,))
    await db.commit()
    await c.answer("حذف شد.")
    if p:
        text, markup = await category_admin_view(p["category_id"])
        await edit(c, text, markup)


# ── موجودی (اکانت/کد) ──
@admin.callback_query(F.data.startswith("astock_add:"))
async def astock_add(c: CallbackQuery, state: FSMContext):
    pid = int(c.data.split(":")[1])
    await state.set_state(Flow.add_stock)
    await state.update_data(pid=pid)
    await edit(
        c,
        "موجودی را بفرست:\n\n"
        "• هر خط = یک عدد (یک اکانت یا یک کد)\n"
        "• اگر هر آیتم چند خط است، بین آیتم‌ها یک خط فقط با --- بگذار\n"
        "• برای تعداد زیاد می‌توانی یک فایل .txt بفرستی\n\n"
        "می‌توانی چند پیام پشت‌سرهم بفرستی. در آخر «پایان» را بزن.",
        kb([("✅ پایان", f"aprod:{pid}")]))
    await c.answer()


@admin.message(Flow.add_stock)
async def add_stock_msg(m: Message, state: FSMContext, bot: Bot):
    pid = (await state.get_data())["pid"]
    text = m.text
    if m.document:
        if (m.document.file_size or 0) > 5 * 1024 * 1024:
            await m.answer("فایل خیلی بزرگ است (حداکثر ۵ مگابایت).")
            return
        buf = io.BytesIO()
        await bot.download(m.document, destination=buf)
        text = buf.getvalue().decode("utf-8", errors="ignore")
    if not text:
        await m.answer("متن یا فایل .txt بفرست.")
        return
    items = split_items(text)
    if not items:
        await m.answer("چیزی برای افزودن پیدا نشد.")
        return
    await db.executemany("INSERT INTO stock(product_id, content) VALUES(?,?)", [(pid, i) for i in items])
    await db.commit()
    total = await stock_count(pid)
    await m.answer(f"✅ {len(items)} عدد اضافه شد. موجودی فعلی: {total}\n"
                   "می‌توانی ادامه بدهی یا «پایان» را بزنی.",
                   reply_markup=kb([("✅ پایان", f"aprod:{pid}")]))


@admin.callback_query(F.data.startswith("astock_clear:"))
async def astock_clear(c: CallbackQuery):
    pid = int(c.data.split(":")[1])
    n = await stock_count(pid)
    await edit(c, f"⚠️ {n} عدد موجودی فروش‌نرفته پاک می‌شود. مطمئنی؟",
               kb([("🧹 بله، پاک شود", f"astock_clearok:{pid}"), ("❌ انصراف", f"aprod:{pid}")]))
    await c.answer()


@admin.callback_query(F.data.startswith("astock_clearok:"))
async def astock_clearok(c: CallbackQuery):
    pid = int(c.data.split(":")[1])
    await ex("DELETE FROM stock WHERE product_id=? AND sold=0", (pid,))
    await c.answer("پاک شد.")
    text, markup = await product_admin_view(pid)
    if text:
        await edit(c, text, markup)


# ── آمار و شارژ دستی ──
@admin.callback_query(F.data == "adm_stats")
async def adm_stats(c: CallbackQuery):
    u = await q1("SELECT COUNT(*) n, COALESCE(SUM(balance),0) b FROM users")
    o = await q1("SELECT COUNT(*) n, COALESCE(SUM(price),0) s, COALESCE(SUM(price-cost),0) p FROM orders")
    st = await q1("SELECT COUNT(*) n FROM stock WHERE sold=0")
    await edit(
        c,
        f"👥 کاربران: {u['n']}\n💰 مجموع موجودی کیف پول کاربران: {fmt(u['b'])}\n\n"
        f"🧾 تعداد فروش: {o['n']}\n💵 فروش کل: {fmt(o['s'])}\n"
        f"📈 سود کل (طبق قیمت خریدهای واردشده): {fmt(o['p'])} {CURRENCY}\n"
        f"📦 کل موجودی فروش‌نرفته: {st['n']}",
        kb([("🔙 بازگشت", "admin")]))
    await c.answer()


@admin.callback_query(F.data == "adm_manual")
async def adm_manual(c: CallbackQuery, state: FSMContext):
    await state.set_state(Flow.manual)
    await edit(c, "فرمت: «آیدی‌کاربر مبلغ» مثل  123456 50000\n(مبلغ منفی = کسر)",
               kb([("🔙 بازگشت", "admin")]))
    await c.answer()


@admin.message(Flow.manual)
async def adm_manual_do(m: Message, state: FSMContext):
    try:
        uid, amount = (int(x) for x in (m.text or "").translate(_DIGITS).split())
    except ValueError:
        await m.answer("فرمت اشتباه است.")
        return
    await credit(uid, amount)
    await state.clear()
    await m.answer(f"✅ انجام شد. موجودی جدید: {fmt(await balance_of(uid))}",
                   reply_markup=main_menu(m.from_user.id))


@admin.callback_query(F.data.startswith(("ta:", "tr:")))
async def topup_decide(c: CallbackQuery, bot: Bot):
    action, tid = c.data.split(":")
    t = await q1("SELECT * FROM topups WHERE id=?", (int(tid),))
    if not t or t["status"] != "pending":
        await c.answer("این درخواست قبلاً بررسی شده.", show_alert=True)
        return
    if action == "ta":
        await credit(t["user_id"], t["amount"])
        await ex("UPDATE topups SET status='approved' WHERE id=?", (t["id"],))
        msg, note = f"✅ شارژ {fmt(t['amount'])} {CURRENCY} تأیید شد.", "تأیید شد ✅"
    else:
        await ex("UPDATE topups SET status='rejected' WHERE id=?", (t["id"],))
        msg, note = "❌ درخواست شارژ رد شد. با پشتیبانی تماس بگیر.", "رد شد ❌"
    try:
        await bot.send_message(t["user_id"], msg)
    except Exception:
        pass
    await c.message.edit_caption(caption=(c.message.caption or "") + f"\n\n{note}")
    await c.answer()


# ───────────────────────── آپدیت خودکار از گیت‌هاب ─────────────────────────
_last_update_error = ""


async def run_update(bot: Bot, manual: bool = False) -> tuple[str, str]:
    global _last_update_error
    status, msg = await asyncio.to_thread(updater.check_and_update)
    if status == "updated":
        await notify_admins(bot, f"✅ آپدیت جدید دریافت شد. در حال ریستارت...\n\n{msg[:800]}")
        for _ in range(60):  # صبر برای تمام‌شدن خریدهای در حال انجام
            if not any(lk.locked() for lk in user_locks.values()) and not purchase_lock.locked():
                break
            await asyncio.sleep(1)
        try:
            await db.close()
            await bot.session.close()
        except Exception:
            pass
        updater.restart()
    elif status == "error":
        if not manual and msg != _last_update_error:
            _last_update_error = msg
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


@admin.callback_query(F.data == "adm_update")
async def adm_update(c: CallbackQuery, bot: Bot):
    await c.answer("در حال بررسی آپدیت...")
    await _manual_update_reply(bot, c.message.answer)


@admin.message(Command("update"))
async def cmd_update(m: Message, bot: Bot):
    await m.answer("⏳ در حال بررسی آپدیت...")
    await _manual_update_reply(bot, m.answer)


# ───────────────────────── اجرا ─────────────────────────
async def main():
    await init_db()
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin)
    dp.include_router(router)
    bg = asyncio.create_task(auto_update_loop(bot))  # noqa: F841
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
