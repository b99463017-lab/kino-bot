import asyncio
import logging
import sqlite3
import os
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart, CommandObject
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from dotenv import load_dotenv

# --- SOZLAMALAR ---
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
INSTAGRAM_LINK = "https://instagram.com/sizning_sahifangiz"  # O'zingiznikiga o'zgartiring
DB_NAME = "kino_bot.db"

bot = Bot(token=TOKEN)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

# --- BAZA BILAN ISHLASH ---
def db_query(query, args=(), fetchone=False, fetchall=False, commit=False):
    with sqlite3.connect(DB_NAME) as conn:
        cur = conn.cursor()
        cur.execute(query, args)
        if commit:
            conn.commit()
        if fetchone:
            return cur.fetchone()
        if fetchall:
            return cur.fetchall()

def init_db():
    db_query("""CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, full_name TEXT)""", commit=True)
    db_query("""CREATE TABLE IF NOT EXISTS channels (id INTEGER PRIMARY KEY, link TEXT)""", commit=True)
    db_query("""CREATE TABLE IF NOT EXISTS movies (code TEXT PRIMARY KEY, title TEXT, file_id TEXT)""", commit=True)
    db_query("""
        CREATE TABLE IF NOT EXISTS series (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT, title TEXT, section TEXT, episode TEXT, file_id TEXT
        )
    """, commit=True)

init_db()

# --- HOLATLAR (FSM) ---
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

# --- ASOSIY MENYU ---
    if user_id == ADMIN_ID:
        keyboard.append([KeyboardButton(text="⚙️ Admin panel")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

def get_admin_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Kino qo'shish", callback_data="admin:add_movie"),
         InlineKeyboardButton(text="📺 Serial qo'shish", callback_data="admin:add_series")],
        [InlineKeyboardButton(text="🗂 Katalog (Baza)", callback_data="admin:catalog"),
         InlineKeyboardButton(text="📢 Kanal qo'shish", callback_data="admin:add_channel")]
    ])

# --- MAJBURIY OBUNA TEKSHIRUVI ---
async def check_subscription(user_id, target_code=None):
    channels = db_query("SELECT id, link FROM channels", fetchall=True)
    unsubbed = []
    
    for ch_id, ch_link in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch_id, user_id=user_id)
            if member.status in ['left', 'kicked']:
                unsubbed.append((ch_link, "📢 Kanalga obuna bo'lish"))
        except:
            unsubbed.append((ch_link, "📢 Kanalga obuna bo'lish"))

    if unsubbed:
        btns = [[InlineKeyboardButton(text=title, url=link)] for link, title in unsubbed]
        btns.append([InlineKeyboardButton(text="📸 Instagram sahifamiz", url=INSTAGRAM_LINK)])
        cb_data = f"sub_check:{target_code}" if target_code else "sub_check:none"
        btns.append([InlineKeyboardButton(text="✅ Obuna bo'ldim", callback_data=cb_data)])
        return InlineKeyboardMarkup(inline_keyboard=btns)
    return None

# --- FOYDALANUVCHI QISMI ---
@dp.message(CommandStart())
async def start_cmd(message: types.Message, command: CommandObject):
    db_query("INSERT OR IGNORE INTO users (id, full_name) VALUES (?, ?)", (message.from_user.id, message.from_user.full_name), commit=True)
    
    code = command.args.strip() if command.args else None
    sub_kb = await check_subscription(message.from_user.id, target_code=code)
    
    if sub_kb:
        await message.answer("🍿 Kinoni ko'rish uchun avval kanallarimizga va Instagramga obuna bo'ling:", reply_markup=sub_kb)
        return

    if code:
        await process_search_code(message.chat.id, code)
    else:
        await message.answer("👋 Xush kelibsiz! Kino yoki serial kodini yuboring:", reply_markup=get_main_keyboard(message.from_user.id))

@dp.callback_query(F.data.startswith("sub_check:"))
async def sub_check_callback(call: CallbackQuery):
    code = call.data.split(":")[1]
    code = None if code == "none" else code
    
    sub_kb = await check_subscription(call.from_user.id, target_code=code)
    if sub_kb:
        await call.answer("❌ Hali barcha kanallarga obuna bo'lmadingiz!", show_alert=True)
    else:
        await call.message.delete()
        if code:
            await process_search_code(call.message.chat.id, code)
        else:
            await call.message.answer("✅ Rahmat! Obuna tasdiqlandi. Kino kodini yuborishingiz mumkin.", reply_markup=get_main_keyboard(call.from_user.id))

@dp.message(lambda m: m.text and m.text.isdigit())
async def search_handler(message: types.Message):
    code = message.text.strip()
    sub_kb = await check_subscription(message.from_user.id, target_code=code)
    if sub_kb:
        await message.answer("Avval obuna bo'ling:", reply_markup=sub_kb)
        return
    await process_search_code(message.chat.id, code)

async def process_search_code(chat_id, code):
    movie = db_query("SELECT title, file_id FROM movies WHERE code=?", (code,), fetchone=True)
    if movie:
        title, file_id = movie
        await send_video_with_share(chat_id, code, title, file_id, is_series=False)
        return
    
    sections = db_query("SELECT DISTINCT section FROM series WHERE code=? ORDER BY section", (code,), fetchall=True)
    if sections:
        title = db_query("SELECT title FROM series WHERE code=? LIMIT 1", (code,), fetchone=True)[0]
        btns = []
        for sec in sections:
            btns.append([InlineKeyboardButton(text=sec[0], callback_data=f"sec:{code}:{sec[0]}")])
        kb = InlineKeyboardMarkup(inline_keyboard=btns)
        await bot.send_message(chat_id, f"📺 <b>{title}</b>\n\nKerakli bo'limni tanlang:", reply_markup=kb, parse_mode="HTML")
        return
        
    await bot.send_message(chat_id, f"❌ <b>{code}</b> kodli kino yoki serial topilmadi.", parse_mode="HTML")

@dp.callback_query(F.data.startswith("sec:"))
async def show_episodes(call: CallbackQuery):
    _, code, section_name = call.data.split(":")
    episodes = db_query("SELECT id, episode FROM series WHERE code=? AND section=?", (code, section_name), fetchall=True)
    
    btns = []
    row = []
    for ep_id, ep_num in episodes:
        row.append(InlineKeyboardButton(text=f"{ep_num}", callback_data=f"ep:{ep_id}"))
        if len(row) == 4:
            btns.append(row)
            row = []
    if row: btns.append(row)
    
    kb = InlineKeyboardMarkup(inline_keyboard=btns)
    await call.message.edit_text(f"📁 Bo'lim: <b>{section_name}</b>\nQismni tanlang:", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("ep:"))
async def send_episode(call: CallbackQuery):
    ep_id = call.data.split(":")[1]
    series_data = db_query("SELECT code, title, episode, file_id FROM series WHERE id=?", (ep_id,), fetchone=True)
    if series_data:
        code, title, episode, file_id = series_data
        await call.message.delete()
        await send_video_with_share(call.message.chat.id, code, f"{title} | {episode}", file_id, is_series=True)

async def send_video_with_share(chat_id, code, title, file_id, is_series=False):
    bot_info = await bot.get_me()
    share_url = f"https://t.me/share/url?url=https://t.me/{bot_info.username}?start={code}&text=🍿%20{title.replace(' ', '%20')}%20ko'ring!"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚀 Do'stlarga ulashish", url=share_url)]])
    
    type_str = "Serial" if is_series else "Kino"
    caption = f"🎬 <b>{type_str}:</b> {title}\n🔢 <b>Kod:</b> {code}\n\n🤖 @{bot_info.username}"
    
    await bot.send_video(chat_id=chat_id, video=file_id, caption=caption, parse_mode="HTML", reply_markup=kb)

# --- ADMIN PANEL ---
@dp.message(lambda m: m.text == "⚙️ Admin panel")
async def admin_panel(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("🔧 Admin panelga xush kelibsiz. Nima qilamiz?", reply_markup=get_admin_keyboard())

@dp.callback_query(F.data == "admin:catalog")
async def show_catalog(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    
    movies = db_query("SELECT code, title FROM movies", fetchall=True)
    series_codes = db_query("SELECT DISTINCT code, title FROM series", fetchall=True)
    
    text = "🗂 <b>BAZA (KATALOG)</b>\n\n<b>🎬 KINOLAR:</b>\n"
    for c, t in movies: text += f"• {c} - {t}\n"
    
    text += "\n<b>📺 SERIALLAR:</b>\n"
    for c, t in series_codes: text += f"• {c} - {t}\n"
        
    await call.message.answer(text, parse_mode="HTML")
    await call.answer()

@dp.callback_query(F.data == "admin:add_movie")
async def start_add_movie(call: CallbackQuery, state: FSMContext):
    await call.message.answer("🎬 Yakkalik kino kodini kiriting (Masalan: 101):")
    await state.set_state(AddMovie.code)

@dp.message(AddMovie.code)
async def process_movie_code(message: types.Message, state: FSMContext):
    await state.update_data(code=message.text)
    await message.answer("Kino nomini yozing:")
    await state.set_state(AddMovie.title)

@dp.message(AddMovie.title)
async def process_movie_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text)
    await message.answer("Endi kino videosini (faylini) yuboring:")
    await state.set_state(AddMovie.video)

@dp.message(AddMovie.video)
async def process_movie_video(message: types.Message, state: FSMContext):
    if not message.video and not message.document:
        await message.answer("Iltimos, video fayl yuboring!")
        return
        
    file_id = message.video.file_id if message.video else message.document.file_id
    data = await state.get_data()
    
    db_query("INSERT OR REPLACE INTO movies (code, title, file_id) VALUES (?, ?, ?)", 
             (data['code'], data['title'], file_id), commit=True)
             
    await message.answer(f"✅ Kino muvaffaqiyatli saqlandi!\nKod: {data['code']}")
    await state.clear()

@dp.callback_query(F.data == "admin:add_series")
async def start_add_series(call: CallbackQuery, state: FSMContext):
    await call.message.answer("📺 Serial kodini kiriting (Masalan: 200):")
    await state.set_state(AddSeries.code)

@dp.message(AddSeries.code)
async def process_series_code(message: types.Message, state: FSMContext):
    code = message.text.strip()
    await state.update_data(code=code)
    
    existing = db_query("SELECT title FROM series WHERE code=? LIMIT 1", (code,), fetchone=True)
    
    if existing:
        title = existing[0]
        await state.update_data(title=title)
        
        sections = db_query("SELECT DISTINCT section FROM series WHERE code=?", (code,), fetchall=True)
        btns = [[KeyboardButton(text=sec[0])] for sec in sections]
        btns.append([KeyboardButton(text="➕ Yangi bo'lim qo'shish")])
        
        kb = ReplyKeyboardMarkup(keyboard=btns, resize_keyboard=True)
        await message.answer(f"Bu kod bazada mavjud: <b>{title}</b>\nQaysi bo'limga qism qo'shasiz yoki yangi bo'lim ochasizmi?", reply_markup=kb, parse_mode="HTML")
        await state.set_state(AddSeries.section_choice)
    else:
        await message.answer("Yangi serial kodi! Serialning UMUMIY nomini yozing (Masalan: Merlin):")
        await state.set_state(AddSeries.title)

@dp.message(AddSeries.title)
async def process_series_title(message: types.Message, state: FSMContext):
    await state.update_data(title=message.text)
    await message.answer("Serial uchun bo'lim nomini yozing (Masalan: 1-10 qismlar yoki 1-mavsum):", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(AddSeries.section)

@dp.message(AddSeries.section_choice)
async def process_series_section_choice(message: types.Message, state: FSMContext):
    if message.text == "➕ Yangi bo'lim qo'shish":
        await message.answer("Yangi bo'lim nomini yozing:", reply_markup=types.ReplyKeyboardRemove())
        await state.set_state(AddSeries.section)
    else:
        await state.update_data(section=message.text)
        await message.answer(f"Bo'lim tanlandi: {message.text}\n\nQism raqami yoki nomini yozing (Masalan: 1-qism):", reply_markup=types.ReplyKeyboardRemove())
        await state.set_state(AddSeries.episode)

@dp.message(AddSeries.section)
async def process_series_section(message: types.Message, state: FSMContext):
    await state.update_data(section=message.text)
    await message.answer("Qism raqami yoki nomini yozing (Masalan: 1-qism):")
    await state.set_state(AddSeries.episode)

@dp.message(AddSeries.episode)
async def process_series_episode(message: types.Message, state: FSMContext):
    await state.update_data(episode=message.text)
    await message.answer("Endi videoni (qismni) yuboring:")
    await state.set_state(AddSeries.video)

@dp.message(AddSeries.video)
async def process_series_video(message: types.Message, state: FSMContext):
    if not message.video and not message.document:
        return await message.answer("Iltimos, video yuboring!")
        
    file_id = message.video.file_id if message.video else message.document.file_id
    data = await state.get_data()
    
    db_query("INSERT INTO series (code, title, section, episode, file_id) VALUES (?, ?, ?, ?, ?)", 
             (data['code'], data['title'], data['section'], data['episode'], file_id), commit=True)
             
    await message.answer(f"✅ Serial qismi muvaffaqiyatli saqlandi!\nKod: {data['code']} | Bo'lim: {data['section']} | {data['episode']}", 
                         reply_markup=get_main_keyboard(message.from_user.id))
    await state.clear()

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
