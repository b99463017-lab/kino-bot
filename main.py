import asyncio
import logging
import html
import os
import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, 
    InlineKeyboardButton, ReplyKeyboardMarkup, 
    KeyboardButton, ReplyKeyboardRemove
)
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.states import StatesGroup, State

# --- SOZLAMALAR ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ASOSIY_ADMIN_ID = int(os.getenv("ASOSIY_ADMIN_ID", 0))

# --- BAZA BILAN ISHLASH (aiosqlite) ---
DB_NAME = 'database.db'
db: aiosqlite.Connection = None

async def init_db():
    await db.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, full_name TEXT, username TEXT)''')
    await db.execute('''CREATE TABLE IF NOT EXISTS movies (code TEXT PRIMARY KEY, file_id TEXT, type TEXT)''')
    await db.execute('''CREATE TABLE IF NOT EXISTS channels (id TEXT PRIMARY KEY, link TEXT)''')
    await db.execute('''CREATE TABLE IF NOT EXISTS admins (id INTEGER PRIMARY KEY)''')
    await db.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    
    await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('start_text', 'Salom! Kino kodini yuboring:')")
    await db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('help_text_status', 'on')")
    await db.execute("INSERT OR IGNORE INTO admins (id) VALUES (?)", (ASOSIY_ADMIN_ID,))
    await db.commit()

async def get_setting(key):
    async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as cursor:
        res = await cursor.fetchone()
        return res[0] if res else None

async def is_admin(user_id):
    async with db.execute("SELECT id FROM admins WHERE id=?", (user_id,)) as cursor:
        res = await cursor.fetchone()
        return bool(res)

# --- FSM HOLATLAR ---
class BotStates(StatesGroup):
    add_movie_code = State()
    add_movie_file = State()
    del_movie = State()
    add_channel = State()
    del_channel = State()
    add_admin = State()
    del_admin = State()
    broadcast = State()
    set_start_text = State()

# --- KEYBOARDLAR ---
cancel_btn = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Bekor qilish")]], resize_keyboard=True)

admin_menu = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="🎬 Kino qo'shish", callback_data="add_movie"), InlineKeyboardButton(text="🗑 Kino o'chirish", callback_data="del_movie")],
    [InlineKeyboardButton(text="📢 Kanal qo'shish", callback_data="add_chan"), InlineKeyboardButton(text="❌ Kanal o'chirish", callback_data="del_chan")],
    [InlineKeyboardButton(text="👮‍♂️ Admin qo'shish", callback_data="add_admin"), InlineKeyboardButton(text="⛔️ Admin o'chirish", callback_data="del_admin")],
    [InlineKeyboardButton(text="✍️ Start matnini o'zgartirish", callback_data="change_start")],
    [InlineKeyboardButton(text="ℹ️ Yordam xabarini yoqish/o'chirish", callback_data="toggle_help")],
    [InlineKeyboardButton(text="✉️ Xabar tarqatish", callback_data="broadcast"), InlineKeyboardButton(text="📊 Statistika", callback_data="stats")]
])

# --- BOT VA DISPATCHER ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- MAJBURIY OBUNA TEKSHIRUVI ---
async def check_sub(user_id):
    async with db.execute("SELECT id, link FROM channels") as cursor:
        channels = await cursor.fetchall()
        
    unsubbed = []
    for ch_id, ch_link in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch_id, user_id=user_id)
            if member.status in ['left', 'kicked']:
                unsubbed.append(ch_link)
        except Exception as e:
            logging.error(f"Kanal tekshirishda xatolik ({ch_id}): {e}")
    return unsubbed

# --- BEKOR QILISH ---
@dp.message(F.text == "❌ Bekor qilish", StateFilter('*'))
async def cancel_process(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Jarayon bekor qilindi.", reply_markup=ReplyKeyboardRemove())
    if await is_admin(message.from_user.id):
        await message.answer("Admin paneliga qaytdingiz:", reply_markup=admin_menu)

# --- START VA YORDAM ---
@dp.message(CommandStart())
async def start_cmd(message: Message):
    user = message.from_user
    await db.execute("INSERT OR IGNORE INTO users (id, full_name, username) VALUES (?, ?, ?)", 
                   (user.id, user.full_name, user.username))
    await db.commit()

    unsubbed = await check_sub(user.id)
    if unsubbed:
        btns = [[InlineKeyboardButton(text="📢 Kanalga obuna bo'lish", url=link)] for link in unsubbed]
        btns.append([InlineKeyboardButton(text="✅ Obuna bo'ldim", callback_data="check_sub")])
        await message.answer("Kinolarni ko'rish uchun avval quyidagi kanallarga obuna bo'ling:", 
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
        return

    start_text = await get_setting('start_text')
    await message.answer(start_text, reply_markup=ReplyKeyboardRemove())

@dp.callback_query(F.data == "check_sub")
async def check_sub_callback(call: CallbackQuery):
    unsubbed = await check_sub(call.from_user.id)
    if unsubbed:
        await call.answer("Barcha kanallarga obuna bo'lmadingiz!", show_alert=True)
    else:
        await call.message.delete()
        start_text = await get_setting('start_text')
        await call.message.answer("Ajoyib! " + start_text)

@dp.message(Command("help"))
async def help_cmd(message: Message):
    if await get_setting('help_text_status') == 'on':
        await message.answer("Bu bot orqali kinolarni kod yuborish orqali topishingiz mumkin. Shunchaki kino kodini yuboring!")

# --- ADMIN PANEL ---
@dp.message(Command("admin"))
async def admin_cmd(message: Message):
    if await is_admin(message.from_user.id):
        await message.answer("🔧 Admin panelga xush kelibsiz. Nima qilamiz?", reply_markup=admin_menu)

# --- KINO QO'SHISH ---
@dp.callback_query(F.data == "add_movie")
async def add_movie_start(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id): return
    await call.message.answer("Kino uchun kod kiriting (masalan: 101):", reply_markup=cancel_btn)
    await state.set_state(BotStates.add_movie_code)

@dp.message(BotStates.add_movie_code)
async def add_movie_code_catch(message: Message, state: FSMContext):
    code = message.text.strip()
    async with db.execute("SELECT code FROM movies WHERE code=?", (code,)) as cursor:
        if await cursor.fetchone():
            await message.answer("Bu kod band! Boshqa kod kiriting yoki ❌ Bekor qiling:")
            return
            
    await state.update_data(code=code)
    await message.answer("Endi istalgan kanaldan kino videofaylini yoki hujjatini ushbu botga **forward** qiling (yo'llang):", reply_markup=cancel_btn)
    await state.set_state(BotStates.add_movie_file)

@dp.message(BotStates.add_movie_file, F.video | F.document)
async def add_movie_file_catch(message: Message, state: FSMContext):
    data = await state.get_data()
    code = data['code']
    file_id = message.video.file_id if message.video else message.document.file_id
    file_type = "video" if message.video else "document"
    
    await db.execute("INSERT INTO movies (code, file_id, type) VALUES (?, ?, ?)", (code, file_id, file_type))
    await db.commit()
    await state.clear()
    await message.answer(f"✅ Kino muvaffaqiyatli qo'shildi!\nKodi: <b>{code}</b>", parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
    await message.answer("Admin panel:", reply_markup=admin_menu)

# --- QOLGAN ADMIN AMALLARI ---
@dp.callback_query(F.data.in_(["del_movie", "add_chan", "del_chan", "add_admin", "del_admin", "change_start", "broadcast"]))
async def handle_admin_callbacks(call: CallbackQuery, state: FSMContext):
    if not await is_admin(call.from_user.id): return
    d = call.data
    
    if d == "del_movie":
        await call.message.answer("O'chirmoqchi bo'lgan kino kodini yuboring:", reply_markup=cancel_btn)
        await state.set_state(BotStates.del_movie)
    elif d == "add_chan":
        await call.message.answer("Kanalni qo'shish uchun uning havolasini yuboring (Masalan: `https://t.me/kanal_nomi` yoki `@kanal_nomi`):", reply_markup=cancel_btn, parse_mode="Markdown")
        await state.set_state(BotStates.add_channel)
    elif d == "del_chan":
        async with db.execute("SELECT id, link FROM channels") as cursor:
            chans = await cursor.fetchall()
            
        if not chans:
            await call.answer("Hozircha kanallar yo'q!", show_alert=True)
            return
        msg = "Qaysi kanalni o'chiramiz? Kanal ID sini yuboring:\n\n" + "\n".join([f"ID: <code>{c[0]}</code> — {c[1]}" for c in chans])
        await call.message.answer(msg, parse_mode="HTML", reply_markup=cancel_btn)
        await state.set_state(BotStates.del_channel)
    elif d == "add_admin":
        await call.message.answer("Yangi adminning Telegram ID raqamini yuboring:", reply_markup=cancel_btn)
        await state.set_state(BotStates.add_admin)
    elif d == "del_admin":
        await call.message.answer("O'chiriladigan adminning ID raqamini yuboring:", reply_markup=cancel_btn)
        await state.set_state(BotStates.del_admin)
    elif d == "change_start":
        await call.message.answer("Yangi Start xabar matnini yuboring:", reply_markup=cancel_btn)
        await state.set_state(BotStates.set_start_text)
    elif d == "broadcast":
        await call.message.answer("Barcha foydalanuvchilarga tarqatish uchun xabar yuboring (Rasm, video yoki tekst):", reply_markup=cancel_btn)
        await state.set_state(BotStates.broadcast)

# --- FSM QABUL QILUVCHILAR ---
@dp.message(BotStates.del_movie)
async def delete_movie(message: Message, state: FSMContext):
    await db.execute("DELETE FROM movies WHERE code=?", (message.text.strip(),))
    await db.commit()
    await state.clear()
    await message.answer("✅ Kino bazadan o'chirildi (agar mavjud bo'lgan bo'lsa).", reply_markup=ReplyKeyboardRemove())

@dp.message(BotStates.add_channel)
async def add_channel(message: Message, state: FSMContext):
    text = message.text.strip()
    try:
        chat = await bot.get_chat(text)
        ch_id = str(chat.id)
        ch_link = chat.invite_link or (f"https://t.me/{chat.username}" if chat.username else text)
        
        await db.execute("INSERT OR REPLACE INTO channels (id, link) VALUES (?, ?)", (ch_id, ch_link))
        await db.commit()
        await state.clear()
        await message.answer(f"✅ Kanal muvaffaqiyatli qo'shildi!\nID: {ch_id}", reply_markup=ReplyKeyboardRemove())
        if await is_admin(message.from_user.id):
            await message.answer("Admin panel:", reply_markup=admin_menu)
    except Exception as e:
        logging.error(f"Kanal qo'shishda xato: {e}")
        await message.answer(f"❌ Kanalni topib bo'lmadi yoki bot o'sha kanalda admin emas!\n\nXatolik: {e}\n\nQaytadan to'g'ri havola yoki username yuboring:", reply_markup=cancel_btn)

@dp.message(BotStates.del_channel)
async def del_channel(message: Message, state: FSMContext):
    await db.execute("DELETE FROM channels WHERE id=?", (message.text.strip(),))
    await db.commit()
    await state.clear()
    await message.answer("✅ Kanal o'chirildi.", reply_markup=ReplyKeyboardRemove())

@dp.message(BotStates.add_admin)
async def add_admin(message: Message, state: FSMContext):
    try:
        new_admin_id = int(message.text.strip())
        if new_admin_id == ASOSIY_ADMIN_ID:
            await message.answer("Bu asosiy admin!")
        else:
            await db.execute("INSERT OR IGNORE INTO admins (id) VALUES (?)", (new_admin_id,))
            await db.commit()
            await message.answer("✅ Yangi admin qo'shildi.", reply_markup=ReplyKeyboardRemove())
    except ValueError:
        await message.answer("Faqat raqam (ID) yuboring!")
        return
    await state.clear()

@dp.message(BotStates.del_admin)
async def del_admin(message: Message, state: FSMContext):
    try:
        del_id = int(message.text.strip())
        if del_id == ASOSIY_ADMIN_ID:
            await message.answer("Asosiy adminni o'chirib bo'lmaydi!")
        else:
            await db.execute("DELETE FROM admins WHERE id=?", (del_id,))
            await db.commit()
            await message.answer("✅ Admin olib tashlandi.", reply_markup=ReplyKeyboardRemove())
    except ValueError:
        await message.answer("Faqat raqam (ID) yuboring!")
        return
    await state.clear()

@dp.message(BotStates.set_start_text)
async def set_start(message: Message, state: FSMContext):
    await db.execute("UPDATE settings SET value=? WHERE key='start_text'", (message.text,))
    await db.commit()
    await state.clear()
    await message.answer("✅ Start matni yangilandi.", reply_markup=ReplyKeyboardRemove())

@dp.message(BotStates.broadcast)
async def broadcast_msg(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("📢 Xabar tarqatish boshlandi...", reply_markup=ReplyKeyboardRemove())
    
    async with db.execute("SELECT id FROM users") as cursor:
        users = await cursor.fetchall()
        
    count = 0
    for u in users:
        try:
            await message.send_copy(chat_id=u[0])
            count += 1
            await asyncio.sleep(0.04)
        except Exception as e:
            logging.warning(f"Foydalanuvchiga yuborib bo'lmadi ({u[0]}): {e}")
            
    await message.answer(f"✅ Xabar jami {count} ta foydalanuvchiga yetkazildi.")

# --- YORDAM YOQISH/O'CHIRISH ---
@dp.callback_query(F.data == "toggle_help")
async def toggle_help(call: CallbackQuery):
    if not await is_admin(call.from_user.id): return
    current = await get_setting('help_text_status')
    new_stat = 'off' if current == 'on' else 'on'
    await db.execute("UPDATE settings SET value=? WHERE key='help_text_status'", (new_stat,))
    await db.commit()
    await call.answer(f"Yordam xabari {'O‘CHIRILDI' if new_stat=='off' else 'YOQILDI'}", show_alert=True)

# --- STATISTIKA ---
@dp.callback_query(F.data == "stats")
async def show_stats(call: CallbackQuery):
    if not await is_admin(call.from_user.id): return
    
    async with db.execute("SELECT COUNT(*) FROM users") as cursor:
        u_count = (await cursor.fetchone())[0]
        
    async with db.execute("SELECT COUNT(*) FROM movies") as cursor:
        m_count = (await cursor.fetchone())[0]
    
    async with db.execute("SELECT id, full_name, username FROM users ORDER BY id DESC LIMIT 15") as cursor:
        last_users = await cursor.fetchall()
    
    users_list = ""
    for u in last_users:
        name = html.escape(str(u[1]))
        uname = f"@{u[2]}" if u[2] else "username yo'q"
        users_list += f"• {name} | <code>{u[0]}</code> | {uname}\n"
    
    stat_text = (
        f"📊 <b>Bot Statistikasi:</b>\n\n"
        f"👥 Jami foydalanuvchilar: <b>{u_count}</b> ta\n"
        f"🎬 Jami kinolar: <b>{m_count}</b> ta\n\n"
        f"👤 <b>Oxirgi qo'shilganlar (15 ta):</b>\n{users_list if users_list else 'Hozircha foydalanuvchilar yoq'}"
    )
    await call.message.answer(stat_text, parse_mode="HTML")
    await call.answer()

# --- KOD ORQALI KINO QIDIRISH ---
@dp.message()
async def search_movie(message: Message):
    unsubbed = await check_sub(message.from_user.id)
    if unsubbed:
        await message.answer("Iltimos, avval kanallarga obuna bo'ling! /start ni bosing.")
        return

    code = message.text.strip()
    async with db.execute("SELECT file_id, type FROM movies WHERE code=?", (code,)) as cursor:
        movie = await cursor.fetchone()
    
    if movie:
        file_id, m_type = movie
        if m_type == "video":
            await message.answer_video(video=file_id, caption=f"🎬 Kino kodi: {code}")
        else:
            await message.answer_document(document=file_id, caption=f"🎬 Kino kodi: {code}")
    else:
        if not await is_admin(message.from_user.id):
            await message.answer("❌ Bunday kod bilan kino topilmadi.")

# --- STARTUP VA SHUTDOWN FUNKSIYALARI ---
async def on_startup():
    global db
    db = await aiosqlite.connect(DB_NAME)
    await init_db()
    logging.info("Bot ma'lumotlar bazasiga ulandi va ishga tushdi...")

async def on_shutdown():
    if db:
        await db.close()
    logging.info("Bot to'xtatildi va baza yopildi.")

# --- ISHGA TUSHIRISH ---
async def main():
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    asyncio.run(main())
