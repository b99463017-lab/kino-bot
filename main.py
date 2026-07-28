import asyncio
import logging
import os
import sys
from typing import Union

import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router, html
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

# 1. Muhit o'zgaruvchilarini yuklaymiz
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS_ENV = os.getenv("ADMIN_IDS", "")

if not BOT_TOKEN:
    print("❌ XATOLIK: .env faylida BOT_TOKEN topilmadi!")
    sys.exit(1)

# Ma'lumotlar bazasi faylining nomi
DB_PATH = "data.db"


# =========================================================================
# 2. MA'LUMOTLAR BAZASI BILAN ISHLASH (SQLITE)
# =========================================================================

async def init_db():
    """Bot ishga tushganda bazani va jadvallarni yaratadi"""
    async with aiosqlite.connect(DB_PATH) as db:
        # Foydalanuvchilar jadvali
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                joined_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Kanallar va guruhlar jadvali
        await db.execute("""
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT UNIQUE,
                link TEXT,
                title TEXT
            )
        """)
        # Adminlar jadvali
        await db.execute("""
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY
            )
        """)
        
        # .env faylidan kelgan adminlarni bazaga qo'shish
        if ADMIN_IDS_ENV:
            for admin_str in ADMIN_IDS_ENV.split(","):
                admin_str = admin_str.strip()
                if admin_str.isdigit():
                    await db.execute(
                        "INSERT OR IGNORE INTO admins (user_id) VALUES (?)",
                        (int(admin_str),)
                    )
        await db.commit()

async def add_user(user_id: int):
    """Yangi foydalanuvchini bazaga qo'shish"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        await db.commit()

async def get_users_count() -> int:
    """Jami foydalanuvchilar sonini olish"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            res = await cursor.fetchone()
            return res[0] if res else 0

async def get_all_users() -> list[int]:
    """Barcha foydalanuvchilar ID raqamlarini olish (Rassilka uchun)"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            rows = await cursor.fetchall()
            return [r[0] for r in rows]

async def get_admins() -> list[int]:
    """Barcha adminlar ro'yxatini olish"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM admins") as cursor:
            rows = await cursor.fetchall()
            return [r[0] for r in rows]

async def add_admin(user_id: int):
    """Yangi admin qo'shish"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))
        await db.commit()

async def remove_admin(user_id: int):
    """Adminni o'chirish"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        await db.commit()

async def get_channels():
    """Ulangan barcha kanal va guruhlarni olish"""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT chat_id, link, title FROM channels") as cursor:
            return await cursor.fetchall()

async def add_channel(chat_id: str, link: str, title: str):
    """Kanal yoki guruhni bazaga saqlash"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO channels (chat_id, link, title) VALUES (?, ?, ?)",
            (chat_id, link, title)
        )
        await db.commit()

async def delete_channel(chat_id: str):
    """Kanal yoki guruhni bazadan o'chirish"""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM channels WHERE chat_id = ?", (chat_id,))
        await db.commit()


# =========================================================================
# 3. FILTRLAR VA FSM (HOLATLAR)
# =========================================================================

class IsAdmin(BaseFilter):
    """Foydalanuvchi admin ekanligini tekshiruvchi maxsus filtr"""
    async def __call__(self, message: Message) -> bool:
        admins = await get_admins()
        return message.from_user.id in admins

class AddChannelState(StatesGroup):
    chat_id = State()
    link = State()

class AddAdminState(StatesGroup):
    user_id = State()

class BroadcastState(StatesGroup):
    message = State()


# =========================================================================
# 4. TUGMALAR (KEYBOARDS)
# =========================================================================

def get_admin_main_kb() -> ReplyKeyboardMarkup:
    """Admin panelning asosiy menyusi"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Statistika"), KeyboardButton(text="📢 Kanal/Guruh qo'shish")],
            [KeyboardButton(text="🗑 Kanal/Guruh o'chirish"), KeyboardButton(text="👥 Adminlarni boshqarish")],
            [KeyboardButton(text="✉️ Xabar yuborish (Rassilka)")]
        ],
        resize_keyboard=True
    )

def get_cancel_kb() -> ReplyKeyboardMarkup:
    """Amalni bekor qilish tugmasi"""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Bekor qilish")]],
        resize_keyboard=True
    )


# =========================================================================
# 5. MAJBURIY OBUNANI TEKSHIRISH
# =========================================================================

async def check_subscriptions(bot: Bot, user_id: int) -> list:
    """Foydalanuvchi barcha kanallarga obuna bo'lganligini tekshiradi"""
    channels = await get_channels()
    unsubscribed = []
    
    for chat_id, link, title in channels:
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if member.status in ["left", "kicked"]:
                unsubscribed.append((link, title or "Kanal/Guruh"))
        except Exception:
            unsubscribed.append((link, title or "Kanal/Guruh"))
            
    return unsubscribed


# =========================================================================
# 6. HANDLERLAR (BOT BUYRUQLari VA XABARLARI)
# =========================================================================

router = Router()

@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot, state: FSMContext):
    await state.clear()
    await add_user(message.from_user.id)
    
    unsubscribed = await check_subscriptions(bot, message.from_user.id)
    
    if unsubscribed:
        builder = []
        for idx, (link, title) in enumerate(unsubscribed, 1):
            builder.append([InlineKeyboardButton(text=f"➕ {idx}-kanal/guruhga a'zo bo'lish", url=link)])
        builder.append([InlineKeyboardButton(text="✅ Obunani tekshirish", callback_data="check_sub")])
        
        kb = InlineKeyboardMarkup(inline_keyboard=builder)
        await message.answer(
            "👋 <b>Hush kelibsiz!</b>\n\n"
            "Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling va <b>'✅ Obunani tekshirish'</b> tugmasini bosing:",
            reply_markup=kb
        )
    else:
        admins = await get_admins()
        if message.from_user.id in admins:
            await message.answer("👋 Xush kelibsiz, Admin!", reply_markup=get_admin_main_kb())
        else:
            await message.answer("🎉 Xush kelibsiz! Botdan bemalol foydalanishingiz mumkin.")

@router.callback_query(F.data == "check_sub")
async def callback_check_sub(query: CallbackQuery, bot: Bot):
    unsubscribed = await check_subscriptions(bot, query.from_user.id)
    
    if unsubscribed:
        await query.answer("⚠️ Hali barcha kanallarga obuna bo'lmadingiz!", show_alert=True)
    else:
        await query.message.delete()
        await query.message.answer("✅ Rahmat! Obuna tasdiqlandi. Botdan foydalanishingiz mumkin.")

@router.message(F.text == "❌ Bekor qilish")
async def cancel_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🛑 Amal bekor qilindi.", reply_markup=get_admin_main_kb())


# --- ADMIN PANEL BO'LIMI ---

@router.message(Command("admin"), IsAdmin())
async def admin_panel(message: Message):
    await message.answer("🔑 <b>Admin Panelga xush kelibsiz!</b>", reply_markup=get_admin_main_kb())

@router.message(F.text == "📊 Statistika", IsAdmin())
async def show_stats(message: Message):
    users_count = await get_users_count()
    channels = await get_channels()
    admins = await get_admins()
    
    await message.answer(
        f"📊 <b>Bot Statistikasi:</b>\n\n"
        f"👤 Obunachilar soni: <b>{users_count} ta</b>\n"
        f"📢 Ulandigan kanallar: <b>{len(channels)} ta</b>\n"
        f"👑 Adminlar soni: <b>{len(admins)} ta</b>"
    )

# Kanal yoki guruh qo'shish jarayoni
@router.message(F.text == "📢 Kanal/Guruh qo'shish", IsAdmin())
async def start_add_channel(message: Message, state: FSMContext):
    await state.set_state(AddChannelState.chat_id)
    await message.answer(
        "📝 Kanal yoki guruhning <b>@username</b> manzilini yoki <b>-100...</b> bilan boshlanuvchi ID raqamini yuboring:\n\n"
        "<i>Eslatma: Bot o'sha kanalda/guruhda <b>Admin</b> bo'lishi shart!</i>",
        reply_markup=get_cancel_kb()
    )

@router.message(AddChannelState.chat_id, IsAdmin())
async def process_channel_id(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    chat_id = None
    
    if text.startswith("@"):
        chat_id = text
    elif text.startswith("-100") and text[4:].isdigit():
        chat_id = text
    elif text.isdigit():
        chat_id = "-100" + text
    elif "t.me/" in text:
        clean = text.split("?")[0].rstrip("/").split("/")[-1]
        if clean and not clean.startswith("+"):
            chat_id = "@" + clean

    if not chat_id:
        return await message.answer("⚠️ Noto'g'ri format! Iltimos, <code>@username</code> yoki <code>-100...</code> kabi to'g'ri ID kiriting:")

    await state.update_data(chat_id=chat_id)
    await state.set_state(AddChannelState.link)
    await message.answer(
        f"✅ ID qabul qilindi: <b>{html.escape(chat_id)}</b>\n\n"
        "🔗 Endi ushbu kanal/guruh uchun <b>a'zo bo'lish havolasini (linkini)</b> yuboring:",
        reply_markup=get_cancel_kb()
    )

@router.message(AddChannelState.link, IsAdmin())
async def process_channel_link(message: Message, state: FSMContext, bot: Bot):
    link = (message.text or "").strip()
    if not link.startswith("http"):
        return await message.answer("⚠️ Noto'g'ri havola! Havola <code>http://</code> yoki <code>https://</code> bilan boshlanishi kerak.")
    
    data = await state.get_data()
    chat_id = data["chat_id"]
    
    title = chat_id
    try:
        chat = await bot.get_chat(chat_id)
        title = chat.title
    except Exception:
        pass

    await add_channel(chat_id, link, title)
    await state.clear()
    await message.answer(f"🎉 <b>{html.escape(title)}</b> muvaffaqiyatli saqlandi!", reply_markup=get_admin_main_kb())

# Kanal yoki guruhni o'chirish
@router.message(F.text == "🗑 Kanal/Guruh o'chirish", IsAdmin())
async def list_channels_delete(message: Message):
    channels = await get_channels()
    if not channels:
        return await message.answer("📂 Hozircha hech qanday kanal yoki guruh qo'shilmagan.")
    
    builder = []
    for chat_id, link, title in channels:
        builder.append([InlineKeyboardButton(text=f"❌ {title or chat_id}", callback_data=f"del_{chat_id}")])
    
    await message.answer("🗑 O'chirmoqchi bo'lganingizni tanlang:", reply_markup=InlineKeyboardMarkup(inline_keyboard=builder))

@router.callback_query(F.data.startswith("del_"), IsAdmin())
async def delete_channel_callback(query: CallbackQuery):
    chat_id = query.data.replace("del_", "")
    await delete_channel(chat_id)
    await query.answer("Muvaffaqiyatli o'chirildi!", show_alert=True)
    await query.message.delete()

# Adminlarni boshqarish
@router.message(F.text == "👥 Adminlarni boshqarish", IsAdmin())
async def manage_admins(message: Message):
    admins = await get_admins()
    msg = "<b>👑 Amaldagi Adminlar:</b>\n\n"
    
    builder = []
    for admin_id in admins:
        msg += f"• <code>{admin_id}</code>\n"
        if admin_id != message.from_user.id:
            builder.append([InlineKeyboardButton(text=f"❌ O'chirish: {admin_id}", callback_data=f"remadmin_{admin_id}")])
            
    builder.append([InlineKeyboardButton(text="➕ Yangi admin qo'shish", callback_data="add_new_admin")])
    
    await message.answer(msg, reply_markup=InlineKeyboardMarkup(inline_keyboard=builder))

@router.callback_query(F.data == "add_new_admin", IsAdmin())
async def start_add_admin(query: CallbackQuery, state: FSMContext):
    await state.set_state(AddAdminState.user_id)
    await query.message.answer(
        "👤 Yangi admin bo'ladigan foydalanuvchining <b>Telegram ID</b> raqamini yuboring:\n\n<i>ID raqamni @GetIDsBot orqali olishingiz mumkin.</i>",
        reply_markup=get_cancel_kb()
    )
    await query.answer()

@router.message(AddAdminState.user_id, IsAdmin())
async def process_add_admin(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text.isdigit():
        return await message.answer("⚠️ ID faqat raqamlardan iborat bo'lishi kerak. Qaytadan kiriting:")
    
    new_admin_id = int(text)
    await add_admin(new_admin_id)
    await state.clear()
    await message.answer(f"✅ <code>{new_admin_id}</code> admin qilib tayinlandi!", reply_markup=get_admin_main_kb())

@router.callback_query(F.data.startswith("remadmin_"), IsAdmin())
async def remove_admin_callback(query: CallbackQuery):
    admin_id = int(query.data.replace("remadmin_", ""))
    await remove_admin(admin_id)
    await query.answer("Admin o'chirildi!", show_alert=True)
    await query.message.delete()

# Rassilka (Xabar yuborish)
@router.message(F.text == "✉️ Xabar yuborish (Rassilka)", IsAdmin())
async def start_broadcast(message: Message, state: FSMContext):
    await state.set_state(BroadcastState.message)
    await message.answer(
        "📢 Barcha foydalanuvchilarga yubormoqchi bo'lgan xabaringizni yuboring (Matn, rasm, video va h.k.):",
        reply_markup=get_cancel_kb()
    )

@router.message(BroadcastState.message, IsAdmin())
async def process_broadcast(message: Message, state: FSMContext, bot: Bot):
    users = await get_all_users()
    await state.clear()
    
    await message.answer(f"🚀 Xabar yuborish boshlandi. Jami foydalanuvchilar: {len(users)} ta...", reply_markup=get_admin_main_kb())
    
    success, blocked = 0, 0
    for user_id in users:
        try:
            await message.copy_to(chat_id=user_id)
            success += 1
            await asyncio.sleep(0.05)
        except (TelegramForbiddenError, TelegramBadRequest):
            blocked += 1
        except Exception:
            blocked += 1

    await message.answer(
        f"✅ <b>Xabar yuborish yakunlandi!</b>\n\n"
        f"🟢 Yuborildi: <b>{success} ta</b>\n"
        f"🔴 Yetib bormadi (bloklagan/o'chirilgan): <b>{blocked} ta</b>"
    )


# =========================================================================
# 7. BOTNI ISHGA TUSHIRISH (MAIN)
# =========================================================================

async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    
    print("🤖 Bot muvaffaqiyatli ishga tushdi va ishga tayyor!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
