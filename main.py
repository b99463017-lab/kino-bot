import asyncio
import html
import logging
import os
import re
import urllib.parse
from datetime import datetime, timezone

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    MessageOriginChannel,
    MessageOriginChat,
)
from dotenv import load_dotenv

# ============================================================
#                        SOZLAMALAR
# ============================================================
load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN topilmadi! .env faylida BOT_TOKEN qiymatini kiriting.")

_admin_raw = os.getenv("ADMIN_IDS") or os.getenv("ADMIN_ID", "0")
ADMIN_IDS = {int(x) for x in re.split(r"[,\s]+", _admin_raw.strip()) if x.isdigit()}

DB_NAME = os.getenv("DB_NAME", "kino_bot.db")
CATALOG_PAGE_SIZE = 12
EPISODES_PER_ROW = 4
BROADCAST_DELAY = 0.05

BTN_MAIN = "🏠 Bosh menyu"
BTN_ADMIN_BACK = "⚙️ Admin panelga qaytish"
CANCEL_TEXT = "🚫 Bekor qilish"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("kino_bot")

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

db: aiosqlite.Connection | None = None


# ============================================================
#                    MA'LUMOTLAR BAZASI
# ============================================================
async def init_db() -> None:
    global db
    db = await aiosqlite.connect(DB_NAME)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL;")

    await db.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, full_name TEXT, joined_at TEXT)")
    await db.execute("CREATE TABLE IF NOT EXISTS channels (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT UNIQUE, link TEXT)")
    await db.execute("CREATE TABLE IF NOT EXISTS movies (code TEXT PRIMARY KEY, title TEXT, file_id TEXT, added_at TEXT)")
    await db.execute("CREATE TABLE IF NOT EXISTS series (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, title TEXT NOT NULL, section TEXT, episode TEXT, file_id TEXT NOT NULL, added_at TEXT)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_series_code ON series(code)")
    await db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    await db.commit()

    defaults = {
        "start": "👋 Xush kelibsiz! Kino yoki serial kodini yuboring:",
        "help": "💡 <b>Yordam</b>\n\nBotdan foydalanish uchun qidirayotgan kino yoki serialingiz kodini yuboring.",
        "instagram": "https://instagram.com/",
    }
    for key, value in defaults.items():
        cur = await db.execute("SELECT 1 FROM settings WHERE key=?", (key,))
        if not await cur.fetchone():
            await db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))
    await db.commit()


async def get_setting(key: str) -> str:
    cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = await cur.fetchone()
    return row["value"] if row else ""

async def set_setting(key: str, value: str) -> None:
    await db.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    await db.commit()

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def chunk_list(lst, n):
    return [lst[i:i + n] for i in range(0, len(lst), n)]


# ============================================================
#                    HOLATLAR (FSM)
# ============================================================
class AddMovie(StatesGroup):
    code, title, video = State(), State(), State()

class AddSeries(StatesGroup):
    code, title, section_choice, section, episode, video = State(), State(), State(), State(), State(), State()

class AddChannel(StatesGroup):
    chat_id, link = State(), State()

class DeleteMedia(StatesGroup):
    code, confirm = State(), State()

class EditSettings(StatesGroup):
    start_text, help_text, instagram = State(), State(), State()

class Broadcast(StatesGroup):
    content, confirm = State(), State()

class AdminCatalog(StatesGroup):
    page = State()

class DelChannel(StatesGroup):
    ch_id = State()

class ViewSeries(StatesGroup):
    code, section, episode = State(), State(), State()


class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return event.from_user is not None and event.from_user.id in ADMIN_IDS


# ============================================================
#                    KLAVIATURALAR
# ============================================================
def get_main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    keyboard = [[KeyboardButton(text="🔍 Qidiruv")], [KeyboardButton(text="🆘 Yordam")]]
    if user_id in ADMIN_IDS:
        keyboard.append([KeyboardButton(text="⚙️ Admin panel")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

def get_admin_main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎬 Kino qo'shish"), KeyboardButton(text="📺 Serial qo'shish")],
            [KeyboardButton(text="🗂 Katalog (Baza)"), KeyboardButton(text="🗑 O'chirish")],
            [KeyboardButton(text="📢 Kanal qo'shish"), KeyboardButton(text="➖ Kanal o'chirish")],
            [KeyboardButton(text="✍️ Matnlarni o'zgartirish")],
            [KeyboardButton(text="📣 Habar yuborish"), KeyboardButton(text="📊 Statistika")],
            [KeyboardButton(text=BTN_MAIN)]
        ], resize_keyboard=True
    )

def get_admin_texts_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="✍️ Start matni")],
            [KeyboardButton(text="✍️ Yordam matni")],
            [KeyboardButton(text="📸 Instagram havolasi")],
            [KeyboardButton(text=BTN_ADMIN_BACK)]
        ], resize_keyboard=True
    )

def get_cancel_kb(admin_back: bool = False) -> ReplyKeyboardMarkup:
    btn = BTN_ADMIN_BACK if admin_back else CANCEL_TEXT
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=btn)]], resize_keyboard=True)


# ============================================================
#                MAJBURIY OBUNA (INLINE BO'LISHI SHART)
# ============================================================
async def check_subscription(user_id: int) -> InlineKeyboardMarkup | None:
    if user_id in ADMIN_IDS: return None
    cur = await db.execute("SELECT chat_id, link FROM channels")
    channels = await cur.fetchall()
    if not channels: return None

    unsubbed = []
    for row in channels:
        ch_id, ch_link = str(row["chat_id"]), row["link"]
        try:
            member = await bot.get_chat_member(chat_id=ch_id, user_id=user_id)
            if member.status in ("left", "kicked"):
                unsubbed.append(ch_link)
        except Exception as e:
            logger.error(f"Obuna tekshirish xatosi ({ch_id}): {e}")
            unsubbed.append(ch_link)

    if unsubbed:
        btns = [[InlineKeyboardButton(text=f"📢 {i}-Kanalga obuna bo'lish", url=link)] for i, link in enumerate(unsubbed, start=1)]
        insta = await get_setting("instagram")
        if insta and insta.lower() != "none":
            btns.append([InlineKeyboardButton(text="📸 Instagram sahifamiz", url=insta)])
        btns.append([InlineKeyboardButton(text="✅ Obuna bo'ldim", callback_data="sub_check")])
        return InlineKeyboardMarkup(inline_keyboard=btns)
    return None


# ============================================================
#              GLOBAL TUGMALAR VA BEKOR QILISH
# ============================================================
@router.message(F.text == BTN_MAIN, StateFilter("*"))
async def global_main_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🏠 Bosh menyudasiz.", reply_markup=get_main_keyboard(message.from_user.id))

@router.message(F.text == BTN_ADMIN_BACK, IsAdmin(), StateFilter("*"))
async def global_admin_back(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🔧 Admin panel:", reply_markup=get_admin_main_kb())

@router.message(F.text == CANCEL_TEXT, StateFilter("*"))
@router.message(Command("cancel"), StateFilter("*"))
async def cancel_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Amal bekor qilindi.", reply_markup=get_main_keyboard(message.from_user.id))


# ============================================================
#              FOYDALANUVCHI QISMI & QIDIRUV
# ============================================================
@router.message(CommandStart())
async def start_cmd(message: Message, command: CommandObject, state: FSMContext) -> None:
    await state.clear()
    await db.execute(
        "INSERT INTO users (id, full_name, joined_at) VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET full_name=excluded.full_name",
        (message.from_user.id, message.from_user.full_name, now_iso()),
    )
    await db.commit()

    code = command.args.strip() if command.args else None
    sub_kb = await check_subscription(message.from_user.id)

    if sub_kb:
        if code: await state.update_data(pending_code=code)
        await message.answer("🍿 Kinoni ko'rish uchun avval kanallarimizga obuna bo'ling:", reply_markup=sub_kb)
        return

    if code:
        await process_search_code(message.chat.id, message.from_user.id, state, code)
    else:
        start_text = await get_setting("start")
        await message.answer(start_text, reply_markup=get_main_keyboard(message.from_user.id))


@router.message(F.text.in_({"🔍 Qidiruv", "🆘 Yordam"}), StateFilter(None))
async def standard_menu_actions(message: Message):
    if message.text == "🔍 Qidiruv":
        await message.answer("🔢 Qidirmoqchi bo'lgan kino yoki serial kodini yuboring:")
    else:
        help_text = await get_setting("help")
        await message.answer(help_text)


@router.callback_query(F.data == "sub_check")
async def sub_check_callback(call: CallbackQuery, state: FSMContext) -> None:
    sub_kb = await check_subscription(call.from_user.id)
    if sub_kb:
        await call.answer("❌ Hali barcha kanallarga obuna bo'lmadingiz!", show_alert=True)
        return

    await call.message.delete()
    data = await state.get_data()
    code = data.get("pending_code")
    await state.clear()

    if code:
        await process_search_code(call.message.chat.id, call.from_user.id, state, code)
    else:
        start_text = await get_setting("start")
        await bot.send_message(call.message.chat.id, f"✅ Rahmat! Obuna tasdiqlandi.\n\n{start_text}", reply_markup=get_main_keyboard(call.message.chat.id))


@router.message(F.text.regexp(r"^[A-Za-z0-9_-]+$"), StateFilter(None))
async def search_handler(message: Message, state: FSMContext) -> None:
    code = message.text.strip()
    sub_kb = await check_subscription(message.from_user.id)
    if sub_kb:
        await state.update_data(pending_code=code)
        await message.answer("🍿 Kinoni ko'rish uchun avval kanallarimizga obuna bo'ling:", reply_markup=sub_kb)
        return
    await process_search_code(message.chat.id, message.from_user.id, state, code)


async def process_search_code(chat_id: int, user_id: int, state: FSMContext, code: str) -> None:
    cur = await db.execute("SELECT title, file_id FROM movies WHERE code=?", (code,))
    movie = await cur.fetchone()
    if movie:
        await send_video_with_share(chat_id, code, movie["title"], movie["file_id"], False)
        return

    cur = await db.execute("SELECT DISTINCT section FROM series WHERE code=? AND section IS NOT NULL ORDER BY section", (code,))
    sections = await cur.fetchall()
    if sections:
        cur = await db.execute("SELECT title FROM series WHERE code=? LIMIT 1", (code,))
        title_row = await cur.fetchone()
        title = title_row["title"] if title_row else code

        btns = [KeyboardButton(text=row["section"]) for row in sections]
        rows = chunk_list(btns, 2)
        rows.append([KeyboardButton(text=BTN_MAIN)])
        
        await state.set_state(ViewSeries.code)
        await state.update_data(code=code)
        await bot.send_message(chat_id, f"📺 <b>{html.escape(title)}</b>\n\nKerakli bo'limni tanlang:", reply_markup=ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True))
        return

    await bot.send_message(chat_id, f"❌ <b>{html.escape(code)}</b> kodli kino yoki serial topilmadi.")


# --- SERIAL KO'RISH (REPLY TUGMALAR ORQALI) ---
@router.message(ViewSeries.code)
async def series_section_chosen(message: Message, state: FSMContext):
    section_name = message.text.strip()
    data = await state.get_data()
    code = data['code']
    
    cur = await db.execute("SELECT id, episode FROM series WHERE code=? AND section=? ORDER BY id", (code, section_name))
    episodes = await cur.fetchall()
    
    if not episodes:
        cur2 = await db.execute("SELECT code FROM movies WHERE code=? UNION SELECT code FROM series WHERE code=?", (section_name, section_name))
        if await cur2.fetchone():
            await state.clear()
            return await process_search_code(message.chat.id, message.from_user.id, state, section_name)
        await message.answer("⚠️ Noma'lum bo'lim. Iltimos, pastdagi tugmalardan foydalaning yoki to'g'ri kod yuboring.")
        return

    await state.update_data(section=section_name)
    btns = [KeyboardButton(text=str(ep['episode'])) for ep in episodes]
    rows = chunk_list(btns, 4)
    rows.append([KeyboardButton(text="⬅️ Bo'limlarga qaytish")])
    rows.append([KeyboardButton(text=BTN_MAIN)])

    await message.answer(f"📁 Bo'lim: <b>{html.escape(section_name)}</b>\nQismni tanlang:", reply_markup=ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True))
    await state.set_state(ViewSeries.episode)


@router.message(ViewSeries.episode, F.text == "⬅️ Bo'limlarga qaytish")
async def back_to_sections_view(message: Message, state: FSMContext):
    data = await state.get_data()
    await process_search_code(message.chat.id, message.from_user.id, state, data['code'])


@router.message(ViewSeries.episode)
async def series_episode_chosen(message: Message, state: FSMContext):
    episode_name = message.text.strip()
    data = await state.get_data()
    code, section = data['code'], data['section']

    cur = await db.execute("SELECT title, file_id FROM series WHERE code=? AND section=? AND episode=?", (code, section, episode_name))
    series_data = await cur.fetchone()
    
    if not series_data:
        cur2 = await db.execute("SELECT code FROM movies WHERE code=? UNION SELECT code FROM series WHERE code=?", (episode_name, episode_name))
        if await cur2.fetchone():
            await state.clear()
            return await process_search_code(message.chat.id, message.from_user.id, state, episode_name)
        await message.answer("⚠️ Noma'lum qism.")
        return

    await send_video_with_share(message.chat.id, code, f"{series_data['title']} | {episode_name}", series_data['file_id'], True)
    # State qoladi, shunda foydalanuvchi keyingi qism tugmasini bosib ko'rishda davom etishi mumkin!


async def send_video_with_share(chat_id: int, code: str, title: str, file_id: str, is_series: bool = False) -> None:
    bot_info = await bot.get_me()
    safe_title = urllib.parse.quote(title)
    share_url = f"https://t.me/share/url?url=https://t.me/{bot_info.username}?start={code}&text=%F0%9F%8D%BF%20{safe_title}%20ko'ring!"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚀 Do'stlarga ulashish", url=share_url)]])
    
    type_str = "Serial" if is_series else "Kino"
    caption = f"🎬 <b>{type_str}:</b> {html.escape(title)}\n🔢 <b>Kod:</b> {html.escape(code)}\n\n🤖 @{bot_info.username}"
    
    try:
        await bot.send_video(chat_id=chat_id, video=file_id, caption=caption, reply_markup=kb)
    except TelegramBadRequest as e:
        logger.error("Video yuborilmadi: %s", e)
        await bot.send_message(chat_id, "⚠️ Kechirasiz, videoni yuborishda xatolik yuz berdi.")


# ============================================================
#                 ADMIN PANEL (TUGMALAR ORQALI)
# ============================================================
@router.message(F.text == "⚙️ Admin panel", IsAdmin(), StateFilter(None))
async def admin_panel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("🔧 Admin panelga xush kelibsiz.", reply_markup=get_admin_main_kb())

@router.message(F.text == "✍️ Matnlarni o'zgartirish", IsAdmin())
async def admin_texts_menu(message: Message):
    await message.answer("Qaysi matnni o'zgartiramiz?", reply_markup=get_admin_texts_kb())

@router.message(F.text == "📊 Statistika", IsAdmin())
async def admin_stats(message: Message) -> None:
    users = (await (await db.execute("SELECT COUNT(*) c FROM users")).fetchone())["c"]
    movies = (await (await db.execute("SELECT COUNT(*) c FROM movies")).fetchone())["c"]
    s_titles = (await (await db.execute("SELECT COUNT(DISTINCT code) c FROM series")).fetchone())["c"]
    episodes = (await (await db.execute("SELECT COUNT(*) c FROM series")).fetchone())["c"]
    channels = (await (await db.execute("SELECT COUNT(*) c FROM channels")).fetchone())["c"]

    text = f"📊 <b>Bot statistikasi</b>\n\n👤 Foydalanuvchilar: <b>{users}</b>\n🎬 Kinolar: <b>{movies}</b>\n📺 Seriallar: <b>{s_titles}</b>\n🎞 Jami serial qismlari: <b>{episodes}</b>\n📢 Majburiy obunalar: <b>{channels}</b>"
    await message.answer(text)

# --- MATNLARNI O'ZGARTIRISH ---
@router.message(F.text == "✍️ Start matni", IsAdmin())
async def edit_start_prompt(message: Message, state: FSMContext) -> None:
    await message.answer("📝 <b>Yangi Start matnini yuboring:</b>", reply_markup=get_cancel_kb(True))
    await state.set_state(EditSettings.start_text)

@router.message(EditSettings.start_text, IsAdmin())
async def save_start_text(message: Message, state: FSMContext) -> None:
    await set_setting("start", message.text)
    await message.answer("✅ Start matni o'zgartirildi!", reply_markup=get_admin_main_kb())
    await state.clear()

@router.message(F.text == "✍️ Yordam matni", IsAdmin())
async def edit_help_prompt(message: Message, state: FSMContext) -> None:
    await message.answer("📝 <b>Yangi Yordam matnini yuboring:</b>", reply_markup=get_cancel_kb(True))
    await state.set_state(EditSettings.help_text)

@router.message(EditSettings.help_text, IsAdmin())
async def save_help_text(message: Message, state: FSMContext) -> None:
    await set_setting("help", message.text)
    await message.answer("✅ Yordam matni o'zgartirildi!", reply_markup=get_admin_main_kb())
    await state.clear()

@router.message(F.text == "📸 Instagram havolasi", IsAdmin())
async def edit_insta_prompt(message: Message, state: FSMContext) -> None:
    await message.answer("📸 <b>Yangi Instagram havolasini yuboring:</b>\n<i>O'chirish uchun 'none' deb yozing.</i>", reply_markup=get_cancel_kb(True))
    await state.set_state(EditSettings.instagram)

@router.message(EditSettings.instagram, IsAdmin())
async def save_insta_text(message: Message, state: FSMContext) -> None:
    new_link = message.text.strip()
    if new_link.lower() != "none" and not re.match(r"^https?://", new_link):
        return await message.answer("⚠️ Havola https:// bilan boshlanishi kerak (yoki 'none').")
    await set_setting("instagram", new_link)
    await message.answer("✅ Instagram havolasi saqlandi!", reply_markup=get_admin_main_kb())
    await state.clear()


# --- KATALOG ---
@router.message(F.text == "🗂 Katalog (Baza)", IsAdmin())
async def show_catalog_first(message: Message, state: FSMContext):
    await state.set_state(AdminCatalog.page)
    await state.update_data(page=0)
    await send_catalog_page(message, 0)

@router.message(F.text == "⬅️ Oldingi", AdminCatalog.page, IsAdmin())
async def catalog_prev(message: Message, state: FSMContext):
    data = await state.get_data()
    page = max(0, data.get("page", 0) - 1)
    await state.update_data(page=page)
    await send_catalog_page(message, page)

@router.message(F.text == "Keyingi ➡️", AdminCatalog.page, IsAdmin())
async def catalog_next(message: Message, state: FSMContext):
    data = await state.get_data()
    page = data.get("page", 0) + 1
    await state.update_data(page=page)
    await send_catalog_page(message, page)

async def send_catalog_page(message: Message, page: int):
    offset = page * CATALOG_PAGE_SIZE
    cur = await db.execute("SELECT code, title FROM movies ORDER BY code LIMIT ? OFFSET ?", (CATALOG_PAGE_SIZE + 1, offset))
    movies = await cur.fetchall()
    
    text = "🗂 <b>BAZA (KATALOG)</b>\n\n<b>🎬 KINOLAR:</b>\n"
    for m in movies[:CATALOG_PAGE_SIZE]: text += f"• <code>{html.escape(m['code'])}</code> — {html.escape(m['title'])}\n"
    if not movies[:CATALOG_PAGE_SIZE]: text += "— (yo'q)\n"

    cur = await db.execute("SELECT DISTINCT code, title FROM series ORDER BY code LIMIT ? OFFSET ?", (CATALOG_PAGE_SIZE + 1, offset))
    series = await cur.fetchall()
    text += "\n<b>📺 SERIALLAR:</b>\n"
    for s in series[:CATALOG_PAGE_SIZE]: text += f"• <code>{html.escape(s['code'])}</code> — {html.escape(s['title'])}\n"
    if not series[:CATALOG_PAGE_SIZE]: text += "— (yo'q)\n"

    nav = []
    if page > 0: nav.append(KeyboardButton(text="⬅️ Oldingi"))
    if len(movies) > CATALOG_PAGE_SIZE or len(series) > CATALOG_PAGE_SIZE: nav.append(KeyboardButton(text="Keyingi ➡️"))
    
    rows = [nav] if nav else []
    rows.append([KeyboardButton(text=BTN_ADMIN_BACK)])
    await message.answer(text[:4000], reply_markup=ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True))


# --- O'CHIRISH ---
@router.message(F.text == "🗑 O'chirish", IsAdmin())
async def ask_delete_code(message: Message, state: FSMContext) -> None:
    await message.answer("🗑 O'chirish uchun Kino/Serial kodini yuboring:", reply_markup=get_cancel_kb(True))
    await state.set_state(DeleteMedia.code)

@router.message(DeleteMedia.code, IsAdmin())
async def confirm_delete(message: Message, state: FSMContext) -> None:
    code = message.text.strip()
    cur = await db.execute("SELECT title FROM movies WHERE code=?", (code,))
    movie = await cur.fetchone()
    cur = await db.execute("SELECT title FROM series WHERE code=? LIMIT 1", (code,))
    series = await cur.fetchone()

    if not movie and not series:
        return await message.answer(f"❌ <code>{html.escape(code)}</code> kodi topilmadi.")

    title = movie["title"] if movie else series["title"]
    await state.update_data(code=code)
    
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="✅ Ha, o'chirish"), KeyboardButton(text="❌ Yo'q, bekor qilish")]], resize_keyboard=True)
    await message.answer(f"⚠️ <b>{html.escape(title)}</b> (kod: <code>{html.escape(code)}</code>) ni o'chirasizmi?", reply_markup=kb)
    await state.set_state(DeleteMedia.confirm)

@router.message(DeleteMedia.confirm, IsAdmin())
async def process_delete(message: Message, state: FSMContext) -> None:
    if message.text == "✅ Ha, o'chirish":
        data = await state.get_data()
        await db.execute("DELETE FROM movies WHERE code=?", (data["code"],))
        await db.execute("DELETE FROM series WHERE code=?", (data["code"],))
        await db.commit()
        await message.answer(f"✅ Ma'lumot tozalandi.", reply_markup=get_admin_main_kb())
    else:
        await message.answer("❌ Bekor qilindi.", reply_markup=get_admin_main_kb())
    await state.clear()


# --- KANAL QO'SHISH / O'CHIRISH ---
@router.message(F.text == "📢 Kanal qo'shish", IsAdmin())
async def ask_channel_id(message: Message, state: FSMContext) -> None:
    await message.answer("📢 Kanal ID/Username ni yozing yoki kanaldan xabarni forward qiling:", reply_markup=get_cancel_kb(True))
    await state.set_state(AddChannel.chat_id)

@router.message(AddChannel.chat_id, IsAdmin())
async def ask_channel_link(message: Message, state: FSMContext) -> None:
    chat_id = None
    if message.forward_origin:
        if isinstance(message.forward_origin, MessageOriginChannel): chat_id = str(message.forward_origin.chat.id)
        elif isinstance(message.forward_origin, MessageOriginChat): chat_id = str(message.forward_origin.sender_chat.id)
    elif message.forward_from_chat and message.forward_from_chat.type in ("channel", "supergroup"):
        chat_id = str(message.forward_from_chat.id)

    if not chat_id:
        text = (message.text or "").strip()
        if "t.me/" in text:
            username = text.split("?")[0].rstrip("/").split("/")[-1]
            if username and "+" not in username and "joinchat" not in username: chat_id = "@" + username
            else: chat_id = text
        elif text.startswith("@"): chat_id = text
        elif text.startswith("-100") and text[4:].isdigit(): chat_id = text
        elif text.startswith("-") and text[1:].isdigit(): chat_id = "-100" + text[1:]
        elif text.isdigit(): chat_id = "-100" + text
        else:
            return await message.answer("⚠️ Iltimos, faqat ID raqam yoki @username yuboring. Yoki forward qiling:")

    await state.update_data(chat_id=chat_id)
    await message.answer(f"✅ Kanal: <b>{html.escape(chat_id)}</b>\n🔗 Obuna havolasini yuboring:", reply_markup=get_cancel_kb(True))
    await state.set_state(AddChannel.link)

@router.message(AddChannel.link, IsAdmin())
async def save_channel(message: Message, state: FSMContext) -> None:
    link = message.text.strip()
    if not re.match(r"^https?://", link): return await message.answer("⚠️ Havola https:// bilan boshlanishi kerak.")
    data = await state.get_data()
    await db.execute("INSERT INTO channels (chat_id, link) VALUES (?, ?) ON CONFLICT(chat_id) DO UPDATE SET link=excluded.link", (data["chat_id"], link))
    await db.commit()
    await message.answer("✅ Kanal qo'shildi!", reply_markup=get_admin_main_kb())
    await state.clear()

@router.message(F.text == "➖ Kanal o'chirish", IsAdmin())
async def del_channels_menu(message: Message, state: FSMContext) -> None:
    cur = await db.execute("SELECT id, chat_id FROM channels")
    channels = await cur.fetchall()
    if not channels: return await message.answer("Bazada kanallar yo'q!", reply_markup=get_admin_main_kb())

    text = "<b>Kanallar:</b>\n"
    for r in channels: text += f"ID: <code>{r['id']}</code> | {r['chat_id']}\n"
    text += "\nO'chirmoqchi bo'lgan kanal ID raqamini (Boshidagi ID'ni) yuboring:"
    
    await message.answer(text, reply_markup=get_cancel_kb(True))
    await state.set_state(DelChannel.ch_id)

@router.message(DelChannel.ch_id, IsAdmin())
async def process_del_channel(message: Message, state: FSMContext) -> None:
    if not message.text.isdigit(): return await message.answer("Faqat ID raqamini yuboring:")
    await db.execute("DELETE FROM channels WHERE id=?", (int(message.text),))
    await db.commit()
    await message.answer("✅ Kanal o'chirildi!", reply_markup=get_admin_main_kb())
    await state.clear()


# --- KINO QO'SHISH ---
@router.message(F.text == "🎬 Kino qo'shish", IsAdmin())
async def start_add_movie(message: Message, state: FSMContext) -> None:
    await message.answer("🎬 Yakkalik kino kodini kiriting:", reply_markup=get_cancel_kb(True))
    await state.set_state(AddMovie.code)

@router.message(AddMovie.code, IsAdmin())
async def process_movie_code_add(message: Message, state: FSMContext) -> None:
    await state.update_data(code=message.text.strip())
    await message.answer("Kino nomini yozing:")
    await state.set_state(AddMovie.title)

@router.message(AddMovie.title, IsAdmin())
async def process_movie_title(message: Message, state: FSMContext) -> None:
    await state.update_data(title=message.text.strip())
    await message.answer("Endi videoni yuboring:")
    await state.set_state(AddMovie.video)

@router.message(AddMovie.video, IsAdmin())
async def process_movie_video(message: Message, state: FSMContext) -> None:
    if not message.video and not message.document: return await message.answer("⚠️ Iltimos, video fayl yuboring!")
    file_id = message.video.file_id if message.video else message.document.file_id
    data = await state.get_data()
    await db.execute("INSERT INTO movies (code, title, file_id, added_at) VALUES (?, ?, ?, ?) ON CONFLICT(code) DO UPDATE SET title=excluded.title, file_id=excluded.file_id", (data["code"], data["title"], file_id, now_iso()))
    await db.commit()
    await message.answer(f"✅ Kino saqlandi! Kod: <code>{html.escape(data['code'])}</code>", reply_markup=get_admin_main_kb())
    await state.clear()


# --- SERIAL QO'SHISH ---
@router.message(F.text == "📺 Serial qo'shish", IsAdmin())
async def start_add_series(message: Message, state: FSMContext) -> None:
    await message.answer("📺 Serial kodini kiriting:", reply_markup=get_cancel_kb(True))
    await state.set_state(AddSeries.code)

@router.message(AddSeries.code, IsAdmin())
async def process_series_code_add(message: Message, state: FSMContext) -> None:
    code = message.text.strip()
    await state.update_data(code=code)
    existing = await (await db.execute("SELECT title FROM series WHERE code=? LIMIT 1", (code,))).fetchone()
    if existing:
        await state.update_data(title=existing["title"])
        sections = await (await db.execute("SELECT DISTINCT section FROM series WHERE code=? AND section IS NOT NULL", (code,))).fetchall()
        btns = [[KeyboardButton(text=row["section"])] for row in sections]
        btns.append([KeyboardButton(text="➕ Yangi bo'lim qo'shish")])
        btns.append([KeyboardButton(text=BTN_ADMIN_BACK)])
        await message.answer("Qaysi bo'limga qism qo'shasiz?", reply_markup=ReplyKeyboardMarkup(keyboard=btns, resize_keyboard=True))
        await state.set_state(AddSeries.section_choice)
    else:
        await message.answer("Umumiy nomini yozing:")
        await state.set_state(AddSeries.title)

@router.message(AddSeries.title, IsAdmin())
async def process_series_title_add(message: Message, state: FSMContext) -> None:
    await state.update_data(title=message.text.strip())
    await message.answer("Bo'lim nomini yozing (Masalan: 1-10 qismlar):")
    await state.set_state(AddSeries.section)

@router.message(AddSeries.section_choice, IsAdmin())
async def process_series_section_choice(message: Message, state: FSMContext) -> None:
    if message.text == "➕ Yangi bo'lim qo'shish":
        await message.answer("Yangi bo'lim nomini yozing:", reply_markup=get_cancel_kb(True))
        await state.set_state(AddSeries.section)
    else:
        await state.update_data(section=message.text.strip())
        await message.answer("Qism raqamini yozing:", reply_markup=get_cancel_kb(True))
        await state.set_state(AddSeries.episode)

@router.message(AddSeries.section, IsAdmin())
async def process_series_section(message: Message, state: FSMContext) -> None:
    await state.update_data(section=message.text.strip())
    await message.answer("Qism raqamini yozing:")
    await state.set_state(AddSeries.episode)

@router.message(AddSeries.episode, IsAdmin())
async def process_series_episode(message: Message, state: FSMContext) -> None:
    await state.update_data(episode=message.text.strip())
    await message.answer("Endi videoni yuboring:")
    await state.set_state(AddSeries.video)

@router.message(AddSeries.video, IsAdmin())
async def process_series_video(message: Message, state: FSMContext) -> None:
    if not message.video and not message.document: return await message.answer("⚠️ Video yuboring!")
    file_id = message.video.file_id if message.video else message.document.file_id
    data = await state.get_data()
    await db.execute("INSERT INTO series (code, title, section, episode, file_id, added_at) VALUES (?, ?, ?, ?, ?, ?)", (data["code"], data["title"], data["section"], data["episode"], file_id, now_iso()))
    await db.commit()
    await message.answer(f"✅ Serial qismi saqlandi!", reply_markup=get_admin_main_kb())
    await state.clear()


# --- BROADCAST ---
@router.message(F.text == "📣 Habar yuborish", IsAdmin())
async def start_broadcast(message: Message, state: FSMContext) -> None:
    await message.answer("📣 Barchaga yuboriladigan xabarni yuboring:", reply_markup=get_cancel_kb(True))
    await state.set_state(Broadcast.content)

@router.message(Broadcast.content, IsAdmin())
async def preview_broadcast(message: Message, state: FSMContext) -> None:
    await state.update_data(chat_id=message.chat.id, message_id=message.message_id)
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="✅ Yuborish"), KeyboardButton(text="❌ Bekor qilish")]], resize_keyboard=True)
    await message.answer("⬆️ Xabar barchaga yuborilsinmi?", reply_markup=kb)
    await state.set_state(Broadcast.confirm)

@router.message(Broadcast.confirm, IsAdmin())
async def run_broadcast(message: Message, state: FSMContext) -> None:
    if message.text != "✅ Yuborish":
        await state.clear()
        return await message.answer("❌ Bekor qilindi.", reply_markup=get_admin_main_kb())

    data = await state.get_data()
    await state.clear()
    await message.answer("⏳ Yuborilmoqda...", reply_markup=get_admin_main_kb())

    user_ids = [row["id"] for row in await (await db.execute("SELECT id FROM users")).fetchall()]
    sent, failed = 0, 0
    for uid in user_ids:
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=data["chat_id"], message_id=data["message_id"])
            sent += 1
        except Exception: failed += 1
        await asyncio.sleep(BROADCAST_DELAY)

    await message.answer(f"✅ Yuborildi: {sent} ta\n❌ Xato: {failed} ta")


# ============================================================
#                 XATOLARNI USHLASH VA START
# ============================================================
@dp.error()
async def global_error_handler(event: ErrorEvent) -> bool:
    logger.exception("Kutilmagan xatolik: %s", event.exception)
    return True

async def main() -> None:
    await init_db()
    await bot.set_my_commands([BotCommand(command="start", description="Ishga tushirish"), BotCommand(command="cancel", description="Bekor qilish")])
    try:
        await dp.start_polling(bot)
    finally:
        if db: await db.close()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
