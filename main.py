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
#                    MA'LUMOTLAR BAZASI (ASYNC)
# ============================================================
async def init_db() -> None:
    global db
    db = await aiosqlite.connect(DB_NAME)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL;")

    await db.execute(
        "CREATE TABLE IF NOT EXISTS users ("
        "id INTEGER PRIMARY KEY, full_name TEXT, joined_at TEXT)"
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT UNIQUE,
            link TEXT
        )"""
    )
    await db.execute(
        "CREATE TABLE IF NOT EXISTS movies (code TEXT PRIMARY KEY, title TEXT, file_id TEXT, added_at TEXT)"
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS series (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            title TEXT NOT NULL,
            section TEXT,
            episode TEXT,
            file_id TEXT NOT NULL,
            added_at TEXT
        )"""
    )
    await db.execute("CREATE INDEX IF NOT EXISTS idx_series_code ON series(code)")
    await db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    await db.commit()

    defaults = {
        "start": "👋 Xush kelibsiz! Kino yoki serial kodini yuboring:",
        "help": (
            "💡 <b>Yordam</b>\n\n"
            "Botdan foydalanish uchun shunchaki qidirayotgan kino yoki "
            "serialingiz kodini yuboring."
        ),
        "instagram": "https://instagram.com/",
    }
    for key, value in defaults.items():
        cur = await db.execute("SELECT 1 FROM settings WHERE key=?", (key,))
        if not await cur.fetchone():
            await db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))
    await db.commit()
    logger.info("Baza tayyor: %s", DB_NAME)


async def get_setting(key: str) -> str:
    cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = await cur.fetchone()
    return row["value"] if row else ""


async def set_setting(key: str, value: str) -> None:
    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    await db.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ============================================================
#                    HOLATLAR (FSM)
# ============================================================
class AddMovie(StatesGroup):
    code = State()
    title = State()
    video = State()


class AddSeries(StatesGroup):
    code = State()
    title = State()
    section_choice = State()
    section = State()
    episode = State()
    video = State()


class AddChannel(StatesGroup):
    chat_id = State()
    link = State()


class DeleteMedia(StatesGroup):
    code = State()
    confirm = State()


class EditSettings(StatesGroup):
    start_text = State()
    help_text = State()
    instagram = State()


class Broadcast(StatesGroup):
    content = State()
    confirm = State()


# ============================================================
#                    FILTRLAR
# ============================================================
class IsAdmin(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery) -> bool:
        return event.from_user is not None and event.from_user.id in ADMIN_IDS


CANCEL_TEXT = "🚫 Bekor qilish"


# ============================================================
#                    KLAVIATURALAR
# ============================================================
def get_main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(text="🔍 Qidiruv (Kino/Serial)")],
        [KeyboardButton(text="🆘 Yordam")],
    ]
    if user_id in ADMIN_IDS:
        keyboard.append([KeyboardButton(text="⚙️ Admin panel")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def get_cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=CANCEL_TEXT)]], resize_keyboard=True
    )


def get_admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🎬 Kino qo'shish", callback_data="admin:add_movie"),
                InlineKeyboardButton(text="📺 Serial qo'shish", callback_data="admin:add_series"),
            ],
            [
                InlineKeyboardButton(text="🗂 Katalog (Baza)", callback_data="admin:catalog:0"),
                InlineKeyboardButton(text="🗑 O'chirish", callback_data="admin:delete"),
            ],
            [
                InlineKeyboardButton(text="📢 Kanal qo'shish", callback_data="admin:add_channel"),
                InlineKeyboardButton(text="➖ Kanal o'chirish", callback_data="admin:del_channel"),
            ],
            [
                InlineKeyboardButton(text="✍️ Start matnini o'zgartirish", callback_data="admin:edit_start"),
            ],
            [
                InlineKeyboardButton(text="✍️ Yordam matnini o'zgartirish", callback_data="admin:edit_help"),
            ],
            [
                InlineKeyboardButton(text="📸 Instagram havolasi", callback_data="admin:edit_insta"),
            ],
            [
                InlineKeyboardButton(text="📣 Habar yuborish (Broadcast)", callback_data="admin:broadcast"),
                InlineKeyboardButton(text="📊 Statistika", callback_data="admin:stats"),
            ],
        ]
    )


# ============================================================
#                MAJBURIY OBUNA TEKSHIRUVI
# ============================================================
async def check_subscription(user_id: int) -> InlineKeyboardMarkup | None:
    if user_id in ADMIN_IDS:
        return None

    cur = await db.execute("SELECT chat_id, link FROM channels")
    channels = await cur.fetchall()
    if not channels:
        return None

    unsubbed = []
    for row in channels:
        ch_id, ch_link = row["chat_id"], row["link"]
        try:
            target_chat = ch_id
            if not target_chat.startswith("-100") and not target_chat.startswith("@") and not target_chat.startswith("-"):
                if target_chat.isdigit():
                    target_chat = "-100" + target_chat
                else:
                    target_chat = "@" + target_chat

            member = await bot.get_chat_member(chat_id=target_chat, user_id=user_id)
            if member.status in ("left", "kicked", "restricted"):
                unsubbed.append(ch_link)
        except Exception as e:
            logger.error(f"Obuna tekshirishda xato ({ch_id}): {e}")
            unsubbed.append(ch_link)

    if unsubbed:
        btns = [[InlineKeyboardButton(text="📢 Kanalga obuna bo'lish", url=link)] for link in unsubbed]

        insta_link = await get_setting("instagram")
        if insta_link and insta_link.lower() != "none":
            btns.append([InlineKeyboardButton(text="📸 Instagram sahifamiz", url=insta_link)])

        btns.append([InlineKeyboardButton(text="✅ Obuna bo'ldim", callback_data="sub_check")])
        return InlineKeyboardMarkup(inline_keyboard=btns)
    return None


# ============================================================
#              FOYDALANUVCHI QISMI
# ============================================================
@router.message(CommandStart())
async def start_cmd(message: Message, command: CommandObject, state: FSMContext) -> None:
    await state.clear()
    await db.execute(
        "INSERT INTO users (id, full_name, joined_at) VALUES (?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET full_name=excluded.full_name",
        (message.from_user.id, message.from_user.full_name, now_iso()),
    )
    await db.commit()

    code = command.args.strip() if command.args else None
    if code:
        await state.update_data(pending_code=code)

    sub_kb = await check_subscription(message.from_user.id)

    if sub_kb:
        await message.answer("🍿 Kinoni ko'rish uchun avval kanallarimizga obuna bo'ling:", reply_markup=sub_kb)
        return

    if code:
        await process_search_code(message.chat.id, code)
    else:
        start_text = await get_setting("start")
        await message.answer(start_text, reply_markup=get_main_keyboard(message.from_user.id))


@router.message(Command("cancel"), StateFilter("*"))
@router.message(F.text == CANCEL_TEXT, StateFilter("*"))
async def cancel_handler(message: Message, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state is None:
        await message.answer("Bekor qilinadigan hech narsa yo'q.", reply_markup=get_main_keyboard(message.from_user.id))
        return
    await state.clear()
    await message.answer("❌ Amal bekor qilindi.", reply_markup=get_main_keyboard(message.from_user.id))


@router.message(F.text == "🆘 Yordam", StateFilter(None))
@router.message(Command("help"), StateFilter(None))
async def help_cmd(message: Message) -> None:
    help_text = await get_setting("help")
    await message.answer(help_text)


@router.message(F.text == "🔍 Qidiruv (Kino/Serial)", StateFilter(None))
async def search_prompt(message: Message) -> None:
    await message.answer("🔢 Qidirmoqchi bo'lgan kino yoki serial kodini yuboring:")


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
        await process_search_code(call.message.chat.id, code)
    else:
        start_text = await get_setting("start")
        await call.message.answer(
            f"✅ Rahmat! Obuna tasdiqlandi.\n\n{start_text}",
            reply_markup=get_main_keyboard(call.message.chat.id),
        )


@router.message(F.text.regexp(r"^[A-Za-z0-9_-]+$"), StateFilter(None))
async def search_handler(message: Message, state: FSMContext) -> None:
    code = message.text.strip()
    sub_kb = await check_subscription(message.from_user.id)
    if sub_kb:
        await state.update_data(pending_code=code)
        await message.answer("Avval obuna bo'ling:", reply_markup=sub_kb)
        return
state_data = await state.get_data()

async def process_search_code(chat_id: int, code: str) -> None:
    cur = await db.execute("SELECT title, file_id FROM movies WHERE code=?", (code,))
    movie = await cur.fetchone()
    if movie:
        await send_video_with_share(chat_id, code, movie["title"], movie["file_id"], is_series=False)
        return

    cur = await db.execute(
        "SELECT DISTINCT section FROM series WHERE code=? AND section IS NOT NULL ORDER BY section",
        (code,),
    )
    sections = await cur.fetchall()
    if sections:
        cur = await db.execute("SELECT title FROM series WHERE code=? LIMIT 1", (code,))
        title_row = await cur.fetchone()
        title = title_row["title"] if title_row else code

        btns = [
            [InlineKeyboardButton(text=row["section"], callback_data=f"sec:{code}:{row['section']}")]
            for row in sections
        ]
        kb = InlineKeyboardMarkup(inline_keyboard=btns)
        await bot.send_message(
            chat_id,
            f"📺 <b>{html.escape(title)}</b>\n\nKerakli bo'limni tanlang:",
            reply_markup=kb,
        )
        return

    await bot.send_message(chat_id, f"❌ <b>{html.escape(code)}</b> kodli kino yoki serial topilmadi.")


@router.callback_query(F.data.startswith("sec:"))
async def show_episodes(call: CallbackQuery) -> None:
    _, code, section_name = call.data.split(":", 2)
    cur = await db.execute(
        "SELECT id, episode FROM series WHERE code=? AND section=? ORDER BY id", (code, section_name)
    )
    episodes = await cur.fetchall()

    btns, row = [], []
    for ep in episodes:
        row.append(InlineKeyboardButton(text=str(ep["episode"]), callback_data=f"ep:{ep['id']}"))
        if len(row) == EPISODES_PER_ROW:
            btns.append(row)
            row = []
    if row:
        btns.append(row)
    btns.append([InlineKeyboardButton(text="⬅️ Orqaga", callback_data=f"backsec:{code}")])

    kb = InlineKeyboardMarkup(inline_keyboard=btns)
    await call.message.edit_text(
        f"📁 Bo'lim: <b>{html.escape(section_name)}</b>\nQismni tanlang:", reply_markup=kb
    )


@router.callback_query(F.data.startswith("backsec:"))
async def back_to_sections(call: CallbackQuery) -> None:
    code = call.data.split(":", 1)[1]
    cur = await db.execute(
        "SELECT DISTINCT section FROM series WHERE code=? AND section IS NOT NULL ORDER BY section", (code,)
    )
    sections = await cur.fetchall()
    cur = await db.execute("SELECT title FROM series WHERE code=? LIMIT 1", (code,))
    title_row = await cur.fetchone()
    title = title_row["title"] if title_row else code

    btns = [
        [InlineKeyboardButton(text=row["section"], callback_data=f"sec:{code}:{row['section']}")]
        for row in sections
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=btns)
    await call.message.edit_text(
        f"📺 <b>{html.escape(title)}</b>\n\nKerakli bo'limni tanlang:", reply_markup=kb
    )


@router.callback_query(F.data.startswith("ep:"))
async def send_episode(call: CallbackQuery) -> None:
    ep_id = call.data.split(":", 1)[1]
    cur = await db.execute(
        "SELECT code, title, episode, file_id FROM series WHERE id=?", (ep_id,)
    )
    series_data = await cur.fetchone()
    if not series_data:
        await call.answer("❌ Bu qism topilmadi (ehtimol o'chirilgan).", show_alert=True)
        return

    await call.message.delete()
    await send_video_with_share(
        call.message.chat.id,
        series_data["code"],
        f"{series_data['title']} | {series_data['episode']}",
        series_data["file_id"],
        is_series=True,
    )


async def send_video_with_share(chat_id: int, code: str, title: str, file_id: str, is_series: bool = False) -> None:
    bot_info = await bot.get_me()
    safe_title = urllib.parse.quote(title)
    share_url = (
        f"https://t.me/share/url?url=https://t.me/{bot_info.username}?start={code}"
        f"&text=%F0%9F%8D%BF%20{safe_title}%20ko'ring!"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🚀 Do'stlarga ulashish", url=share_url)]]
    )
    type_str = "Serial" if is_series else "Kino"
    caption = (
        f"🎬 <b>{type_str}:</b> {html.escape(title)}\n"
        f"🔢 <b>Kod:</b> {html.escape(code)}\n\n"
        f"🤖 @{bot_info.username}"
    )

    try:
        await bot.send_video(chat_id=chat_id, video=file_id, caption=caption, reply_markup=kb)
    except TelegramBadRequest as e:
        logger.error("Video yuborilmadi (chat=%s, code=%s): %s", chat_id, code, e)
        await bot.send_message(
            chat_id,
            "⚠️ Kechirasiz, videoni yuborishda xatolik yuz berdi. Admin bilan bog'laning.",
        )


# ============================================================
#                 ADMIN PANEL
# ============================================================
@router.message(F.text == "⚙️ Admin panel", IsAdmin(), StateFilter(None))
async def admin_panel(message: Message) -> None:
    await message.answer("🔧 Admin panelga xush kelibsiz. Nima qilamiz?", reply_markup=get_admin_keyboard())


@router.callback_query(F.data == "admin:stats", IsAdmin())
async def admin_stats(call: CallbackQuery) -> None:
    users_cnt = (await (await db.execute("SELECT COUNT(*) c FROM users")).fetchone())["c"]
    movies_cnt = (await (await db.execute("SELECT COUNT(*) c FROM movies")).fetchone())["c"]
    series_titles_cnt = (
        await (await db.execute("SELECT COUNT(DISTINCT code) c FROM series")).fetchone()
    )["c"]
    episodes_cnt = (await (await db.execute("SELECT COUNT(*) c FROM series")).fetchone())["c"]
    channels_cnt = (await (await db.execute("SELECT COUNT(*) c FROM channels")).fetchone())["c"]

    text = (
        "📊 <b>Bot statistikasi</b>\n\n"
        f"👤 Foydalanuvchilar: <b>{users_cnt}</b>\n"
        f"🎬 Kinolar: <b>{movies_cnt}</b>\n"
        f"📺 Seriallar (nomi bo'yicha): <b>{series_titles_cnt}</b>\n"
        f"🎞 Jami serial qismlari: <b>{episodes_cnt}</b>\n"
        f"📢 Majburiy obuna kanallari: <b>{channels_cnt}</b>"
    )
    await call.message.answer(text)
    await call.answer()


@router.callback_query(F.data == "admin:edit_start", IsAdmin())
async def edit_start_prompt(call: CallbackQuery, state: FSMContext) -> None:
    await call.message.answer(
        "📝 <b>Yangi Start matnini yuboring:</b>\n\n<i>(HTML taglaridan foydalanishingiz mumkin)</i>",
        reply_markup=get_cancel_keyboard(),
    )
    await state.set_state(EditSettings.start_text)


@router.message(EditSettings.start_text, IsAdmin())
async def save_start_text(message: Message, state: FSMContext) -> None:
    new_text = message.text
    await set_setting("start", new_text)
    await message.answer("✅ Start matni muvaffaqiyatli o'zgartirildi!", reply_markup=get_main_keyboard(message.from_user.id))
    await state.clear()


@router.callback_query(F.data == "admin:edit_help", IsAdmin())
async def edit_help_prompt(call: CallbackQuery, state: FSMContext) -> None:
    await call.message.answer("📝 <b>Yangi Yordam matnini yuboring:</b>", reply_markup=get_cancel_keyboard())
    await state.set_state(EditSettings.help_text)


@router.message(EditSettings.help_text, IsAdmin())
async def save_help_text(message: Message, state: FSMContext) -> None:
    new_text = message.text
    await set_setting("help", new_text)
    await message.answer("✅ Yordam matni muvaffaqiyatli o'zgartirildi!", reply_markup=get_main_keyboard(message.from_user.id))
    await state.clear()


@router.callback_query(F.data == "admin:edit_insta", IsAdmin())
async def edit_insta_prompt(call: CallbackQuery, state: FSMContext) -> None:
    await call.message.answer(
        "📸 <b>Yangi Instagram havolangizni yuboring</b> (https://instagram.com/...).\n\n"
        "<i>Agar Instagram tugmasini olib tashlamoqchi bo'lsangiz, shunchaki <b>none</b> deb yozing.</i>",
        reply_markup=get_cancel_keyboard(),
    )
    await state.set_state(EditSettings.instagram)


@router.message(EditSettings.instagram, IsAdmin())
async def save_insta_text(message: Message, state: FSMContext) -> None:
    new_link = message.text.strip()
    if new_link.lower() != "none" and not re.match(r"^https?://", new_link):
        await message.answer("⚠️ Havola https:// bilan boshlanishi kerak (yoki 'none' deb yozing). Qayta urinib ko'ring:")
        return
    await set_setting("instagram", new_link)
    await message.answer(f"✅ Instagram havolasi saqlandi:\n{html.escape(new_link)}", reply_markup=get_main_keyboard(message.from_user.id))
    await state.clear()


def _catalog_keyboard(page: int, has_next: bool) -> InlineKeyboardMarkup:
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Oldingi", callback_data=f"admin:catalog:{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton(text="Keyingi ➡️", callback_data=f"admin:catalog:{page + 1}"))
    rows = [nav] if nav else []
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("admin:catalog:"), IsAdmin())
async def show_catalog(call: CallbackQuery) -> None:
    page = int(call.data.split(":")[2])
    offset = page * CATALOG_PAGE_SIZE

    cur = await db.execute(
        "SELECT code, title FROM movies ORDER BY code LIMIT ? OFFSET ?",
        (CATALOG_PAGE_SIZE + 1, offset),
    )
    movies = await cur.fetchall()

    text = "🗂 <b>BAZA (KATALOG)</b>\n\n<b>🎬 KINOLAR:</b>\n"
    shown_movies = movies[:CATALOG_PAGE_SIZE]
    if shown_movies:
        for m in shown_movies:
            text += f"• <code>{html.escape(m['code'])}</code> — {html.escape(m['title'])}\n"
    else:
        text += "— (yo'q)\n"

    cur = await db.execute("SELECT DISTINCT code, title FROM series ORDER BY code LIMIT ? OFFSET ?", (CATALOG_PAGE_SIZE + 1, offset))
    series_rows = await cur.fetchall()
    text += "\n<b>📺 SERIALLAR:</b>\n"
    shown_series = series_rows[:CATALOG_PAGE_SIZE]
    if shown_series:
        for s in shown_series:
            text += f"• <code>{html.escape(s['code'])}</code> — {html.escape(s['title'])}\n"
    else:
        text += "— (yo'q)\n"

    has_next = len(movies) > CATALOG_PAGE_SIZE or len(series_rows) > CATALOG_PAGE_SIZE
    kb = _catalog_keyboard(page, has_next)

    try:
        await call.message.edit_text(text[:4000], reply_markup=kb)
    except TelegramBadRequest:
        await call.message.answer(text[:4000], reply_markup=kb)
    await call.answer()


@router.callback_query(F.data == "admin:delete", IsAdmin())
async def ask_delete_code(call: CallbackQuery, state: FSMContext) -> None:
    await call.message.answer("🗑 O'chirish uchun Kino yoki Serial kodini yuboring:", reply_markup=get_cancel_keyboard())
    await state.set_state(DeleteMedia.code)


@router.message(DeleteMedia.code, IsAdmin())
async def confirm_delete(message: Message, state: FSMContext) -> None:
    code = message.text.strip()
    cur = await db.execute("SELECT title FROM movies WHERE code=?", (code,))
    movie = await cur.fetchone()
    cur = await db.execute("SELECT title FROM series WHERE code=? LIMIT 1", (code,))
    series = await cur.fetchone()

    if not movie and not series:
        await message.answer(f"❌ <code>{html.escape(code)}</code> kodli hech narsa topilmadi.")
        await state.clear()
        return

    title = movie["title"] if movie else series["title"]
    await state.update_data(code=code)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Ha, o'chirish", callback_data="del_confirm:yes"),
                InlineKeyboardButton(text="❌ Bekor qilish", callback_data="del_confirm:no"),
            ]
        ]
    )
    await message.answer(
        f"⚠️ <b>{html.escape(title)}</b> (kod: <code>{html.escape(code)}</code>) "
        f"ni bazadan butunlay o'chirishni tasdiqlaysizmi?",
        reply_markup=kb,
    )
    await state.set_state(DeleteMedia.confirm)


@router.callback_query(F.data.startswith("del_confirm:"), DeleteMedia.confirm, IsAdmin())
async def process_delete(call: CallbackQuery, state: FSMContext) -> None:
    answer = call.data.split(":", 1)[1]
    data = await state.get_data()
    code = data.get("code")

    if answer == "yes" and code:
        await db.execute("DELETE FROM movies WHERE code=?", (code,))
        await db.execute("DELETE FROM series WHERE code=?", (code,))
        await db.commit()
        await call.message.edit_text(f"✅ <code>{html.escape(code)}</code> kodli barcha ma'lumotlar bazadan tozalandi.")
    else:
        await call.message.edit_text("❌ O'chirish bekor qilindi.")

    await state.clear()
    await call.answer()


# --- KANAL QO'SHISH / O'CHIRISH ---
@router.callback_query(F.data == "admin:add_channel", IsAdmin())
async def ask_channel_id(call: CallbackQuery, state: FSMContext) -> None:
    await call.message.answer(
        "📢 <b>Kanalni qanday qo'shamiz?</b>\n\n"
        "1. Kanal ID raqamini yozing (masalan: <code>-10012345678</code>)\n"
        "2. Yoki ommaviy username'ni yozing (masalan: <code>@kanal_nomi</code>)\n"
        "3. <b>YOKI ENG OSONI:</b> o'sha kanaldan ixtiyoriy bitta xabarni menga "
        "<b>Forward</b> (uzatish) qilib yuboring.",
        reply_markup=get_cancel_keyboard(),
    )
    await state.set_state(AddChannel.chat_id)


@router.message(AddChannel.chat_id, IsAdmin())
async def ask_channel_link(message: Message, state: FSMContext) -> None:
    chat_id = None
    if message.forward_from_chat and message.forward_from_chat.type in ("channel", "supergroup"):
        chat_id = str(message.forward_from_chat.id)
    else:
        text = (message.text or "").strip()
        if not text:
            await message.answer("⚠️ Iltimos, kanal ID/username yuboring yoki xabarni forward qiling.")
            return
        
        if "t.me/" in text:
            clean_link = text.split("?")[0].rstrip("/")
            username = clean_link.split("/")[-1]
            if username and "+" not in username and "joinchat" not in username:
                chat_id = "@" + username
            else:
                chat_id = text
        elif text.startswith("@"):
            chat_id = text
        elif text.startswith("-100") or (text.startswith("-") and text[1:].isdigit()):
            chat_id = text
        elif text.isdigit():
            chat_id = "-100" + text
        else:
            chat_id = "@" + text

    if not chat_id:
        await message.answer("⚠️ Havola yoki kanal formati noto'g'ri. Qaytadan urinib ko'ring:")
        return

    await state.update_data(chat_id=chat_id)
    await message.answer(
        f"✅ Kanal qabul qilindi: <b>{html.escape(chat_id)}</b>\n\n"
        "🔗 Endi obuna tugmasi ishlashi uchun kanal havolasining o'zini yuboring:"
    )
    await state.set_state(AddChannel.link)


@router.message(AddChannel.link, IsAdmin())
async def save_channel(message: Message, state: FSMContext) -> None:
    link = message.text.strip()
    if not re.match(r"^https?://", link):
        await message.answer("⚠️ Havola https:// bilan boshlanishi kerak. Qayta yuboring:")
        return
    data = await state.get_data()
    await db.execute(
        "INSERT INTO channels (chat_id, link) VALUES (?, ?) "
        "ON CONFLICT(chat_id) DO UPDATE SET link=excluded.link",
        (data["chat_id"], link),
    )
    await db.commit()
    await message.answer("✅ Kanal majburiy obunaga qo'shildi!", reply_markup=get_main_keyboard(message.from_user.id))
    await state.clear()


@router.callback_query(F.data == "admin:del_channel", IsAdmin())
async def del_channels_menu(call: CallbackQuery) -> None:
    cur = await db.execute("SELECT id, chat_id, link FROM channels")
    channels = await cur.fetchall()
    if not channels:
        await call.answer("Bazada kanallar yo'q!", show_alert=True)
        return

    btns = [
        [InlineKeyboardButton(text=f"❌ O'chirish: {row['chat_id']}", callback_data=f"del_ch:{row['id']}")]
        for row in channels
    ]
    await call.message.answer("O'chirmoqchi bo'lgan kanalni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    await call.answer()


@router.callback_query(F.data.startswith("del_ch:"), IsAdmin())
async def process_del_channel(call: CallbackQuery) -> None:
    ch_id = call.data.split(":", 1)[1]
    await db.execute("DELETE FROM channels WHERE id=?", (ch_id,))
    await db.commit()
    await call.message.delete()
    await call.answer("Kanal o'chirildi!", show_alert=True)


# --- 1. KINO QO'SHISH ---
@router.callback_query(F.data == "admin:add_movie", IsAdmin())
async def start_add_movie(call: CallbackQuery, state: FSMContext) -> None:
    await call.message.answer("🎬 Yakkalik kino kodini kiriting (Masalan: 101):", reply_markup=get_cancel_keyboard())
    await state.set_state(AddMovie.code)


@router.message(AddMovie.code, IsAdmin())
async def process_movie_code_add(message: Message, state: FSMContext) -> None:
    code = message.text.strip()
    if not code:
        await message.answer("⚠️ Kod bo'sh bo'lishi mumkin emas. Qayta kiriting:")
        return
    existing = await (await db.execute("SELECT 1 FROM movies WHERE code=?", (code,))).fetchone()
    await state.update_data(code=code, overwrite=bool(existing))
    if existing:
        await message.answer(
            f"⚠️ <code>{html.escape(code)}</code> kodi bazada allaqachon mavjud. "
            "Davom etsangiz, u qayta yoziladi.\n\nKino nomini yozing:"
        )
    else:
        await message.answer("Kino nomini yozing:")
    await state.set_state(AddMovie.title)


@router.message(AddMovie.title, IsAdmin())
async def process_movie_title(message: Message, state: FSMContext) -> None:
    await state.update_data(title=message.text.strip())
    await message.answer("Endi kino videosini (faylini) yuboring:")
    await state.set_state(AddMovie.video)


@router.message(AddMovie.video, IsAdmin())
async def process_movie_video(message: Message, state: FSMContext) -> None:
    if not message.video and not message.document:
        await message.answer("⚠️ Iltimos, video fayl yuboring!")
        return

    file_id = message.video.file_id if message.video else message.document.file_id
    data = await state.get_data()
    await db.execute(
        "INSERT INTO movies (code, title, file_id, added_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(code) DO UPDATE SET title=excluded.title, file_id=excluded.file_id",
        (data["code"], data["title"], file_id, now_iso()),
    )
    await db.commit()
    await message.answer(
        f"✅ Kino saqlandi! Kod: <code>{html.escape(data['code'])}</code>",
        reply_markup=get_main_keyboard(message.from_user.id),
    )
    await state.clear()


# --- 2. SERIAL QO'SHISH ---
@router.callback_query(F.data == "admin:add_series", IsAdmin())
async def start_add_series(call: CallbackQuery, state: FSMContext) -> None:
    await call.message.answer("📺 Serial kodini kiriting (Masalan: 200):", reply_markup=get_cancel_keyboard())
    await state.set_state(AddSeries.code)


@router.message(AddSeries.code, IsAdmin())
async def process_series_code_add(message: Message, state: FSMContext) -> None:
    code = message.text.strip()
    if not code:
        await message.answer("⚠️ Kod bo'sh bo'lishi mumkin emas. Qayta kiriting:")
        return
    await state.update_data(code=code)

    existing = await (await db.execute("SELECT title FROM series WHERE code=? LIMIT 1", (code,))).fetchone()
    if existing:
        title = existing["title"]
        await state.update_data(title=title)
        cur = await db.execute(
            "SELECT DISTINCT section FROM series WHERE code=? AND section IS NOT NULL", (code,)
        )
        sections = await cur.fetchall()

        btns = [[KeyboardButton(text=row["section"])] for row in sections]
        btns.append([KeyboardButton(text="➕ Yangi bo'lim qo'shish")])
        btns.append([KeyboardButton(text=CANCEL_TEXT)])

        kb = ReplyKeyboardMarkup(keyboard=btns, resize_keyboard=True)
        await message.answer(
            f"Bu kod bazada mavjud: <b>{html.escape(title)}</b>\n"
            "Qaysi bo'limga qism qo'shasiz yoki yangi bo'lim ochasizmi?",
            reply_markup=kb,
        )
        await state.set_state(AddSeries.section_choice)
    else:
        await message.answer(
            "Yangi serial kodi! Umumiy nomini yozing (Masalan: Merlin):",
            reply_markup=get_cancel_keyboard(),
        )
        await state.set_state(AddSeries.title)


@router.message(AddSeries.title, IsAdmin())
async def process_series_title_add(message: Message, state: FSMContext) -> None:
    await state.update_data(title=message.text.strip())
    await message.answer("Bo'lim nomini yozing (Masalan: 1-10 qismlar):", reply_markup=get_cancel_keyboard())
    await state.set_state(AddSeries.section)


@router.message(AddSeries.section_choice, IsAdmin())
async def process_series_section_choice(message: Message, state: FSMContext) -> None:
    if message.text == "➕ Yangi bo'lim qo'shish":
        await message.answer("Yangi bo'lim nomini yozing:", reply_markup=get_cancel_keyboard())
        await state.set_state(AddSeries.section)
    else:
        await state.update_data(section=message.text.strip())
        await message.answer(
            f"Bo'lim tanlandi: {html.escape(message.text.strip())}\n\n"
            "Qism raqami yoki nomini yozing (Masalan: 1-qism):",
            reply_markup=get_cancel_keyboard(),
        )
        await state.set_state(AddSeries.episode)


@router.message(AddSeries.section, IsAdmin())
async def process_series_section(message: Message, state: FSMContext) -> None:
    await state.update_data(section=message.text.strip())
    await message.answer("Qism raqami yoki nomini yozing (Masalan: 1-qism):")
    await state.set_state(AddSeries.episode)


@router.message(AddSeries.episode, IsAdmin())
async def process_series_episode(message: Message, state: FSMContext) -> None:
    await state.update_data(episode=message.text.strip())
    await message.answer("Endi videoni (qismni) yuboring:")
    await state.set_state(AddSeries.video)


@router.message(AddSeries.video, IsAdmin())
async def process_series_video(message: Message, state: FSMContext) -> None:
    if not message.video and not message.document:
        await message.answer("⚠️ Iltimos, video yuboring!")
        return

    file_id = message.video.file_id if message.video else message.document.file_id
    data = await state.get_data()
    await db.execute(
        "INSERT INTO series (code, title, section, episode, file_id, added_at) VALUES (?, ?, ?, ?, ?, ?)",
        (data["code"], data["title"], data["section"], data["episode"], file_id, now_iso()),
    )
    await db.commit()

    await message.answer(
        f"✅ Serial qismi muvaffaqiyatli saqlandi!\n"
        f"Kod: <code>{html.escape(data['code'])}</code> | Bo'lim: {html.escape(data['section'])} | "
        f"{html.escape(data['episode'])}",
        reply_markup=get_main_keyboard(message.from_user.id),
    )
    await state.clear()


# --- 3. BROADCAST (BARCHA FOYDALANUVCHILARGA XABAR) ---
@router.callback_query(F.data == "admin:broadcast", IsAdmin())
async def start_broadcast(call: CallbackQuery, state: FSMContext) -> None:
    await call.message.answer(
        "📣 Barcha foydalanuvchilarga yubormoqchi bo'lgan xabaringizni yuboring "
        "(matn, rasm, video — caption bilan ham bo'lishi mumkin):",
        reply_markup=get_cancel_keyboard(),
    )
    await state.set_state(Broadcast.content)


@router.message(Broadcast.content, IsAdmin())
async def preview_broadcast(message: Message, state: FSMContext) -> None:
    await state.update_data(chat_id=message.chat.id, message_id=message.message_id)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Yuborish", callback_data="bcast_confirm:yes"),
                InlineKeyboardButton(text="❌ Bekor qilish", callback_data="bcast_confirm:no"),
            ]
        ]
    )
    await message.answer("⬆️ Ushbu xabar barcha foydalanuvchilarga yuborilsinmi?", reply_markup=kb)
    await state.set_state(Broadcast.confirm)


@router.callback_query(F.data.startswith("bcast_confirm:"), Broadcast.confirm, IsAdmin())
async def run_broadcast(call: CallbackQuery, state: FSMContext) -> None:
    answer = call.data.split(":", 1)[1]
    data = await state.get_data()
    await state.clear()

    if answer != "yes":
        await call.message.edit_text("❌ Yuborish bekor qilindi.")
        return

    await call.message.edit_text("⏳ Yuborilmoqda... Bu biroz vaqt olishi mumkin.")

    cur = await db.execute("SELECT id FROM users")
    user_ids = [row["id"] for row in await cur.fetchall()]

    sent, failed = 0, 0
    for uid in user_ids:
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=data["chat_id"], message_id=data["message_id"])
            sent += 1
        except TelegramForbiddenError:
            failed += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await bot.copy_message(chat_id=uid, from_chat_id=data["chat_id"], message_id=data["message_id"])
                sent += 1
            except Exception:
                failed += 1
        except Exception as e:
            logger.warning("Broadcast xatosi (user=%s): %s", uid, e)
            failed += 1
        await asyncio.sleep(BROADCAST_DELAY)

    await call.message.answer(f"✅ Yuborildi: {sent} ta\n❌ Yetkazilmadi: {failed} ta")


# ============================================================
#                 XATOLARNI USHLASH
# ============================================================
@dp.error()
async def global_error_handler(event: ErrorEvent) -> bool:
    logger.exception("Kutilmagan xatolik: %s", event.exception)
    return True


# ============================================================
#                      ISHGA TUSHIRISH
# ============================================================
async def set_bot_commands() -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Botni ishga tushirish"),
            BotCommand(command="help", description="Yordam"),
            BotCommand(command="cancel", description="Joriy amalni bekor qilish"),
        ]
    )


async def main() -> None:
    await init_db()
    await set_bot_commands()
    try:
        await dp.start_polling(bot)
    finally:
        if db is not None:
            await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
