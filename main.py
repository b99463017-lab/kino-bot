import asyncio
import logging
import sqlite3
import html

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.states import StatesGroup, State

# --- SOZLAMALAR ---
BOT_TOKEN = "8989891347:AAHaN14lyx4PnbLFwYcx_jNlI26wHuv0sFQ"
ASOSIY_ADMIN_ID = 8488028783 # O'zingizning Telegram ID raqamingizni yozing

# --- BAZA BILAN ISHLASH (SQLite) ---
conn = sqlite3.connect('database.db', check_same_thread=False)
cursor = conn.cursor()

def init_db():
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, full_name TEXT, username TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS movies (code TEXT PRIMARY KEY, file_id TEXT, type TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS channels (id TEXT PRIMARY KEY, link TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS admins (id INTEGER PRIMARY KEY)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    
    # Standart sozlamalar
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('start_text', 'Salom! Kino kodini yuboring:')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('help_text_status', 'on')")
    cursor.execute("INSERT OR IGNORE INTO admins (id) VALUES (?)", (ASOSIY_ADMIN_ID,))
    conn.commit()

init_db()

def get_setting(key):
    cursor.execute("SELECT value FROM settings WHERE key=?", (key,))
    res = cursor.fetchone()
    return res[0] if res else None

def is_admin(user_id):
    cursor.execute("SELECT id FROM admins WHERE id=?", (user_id,))
    return True if cursor.fetchone() else False

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
    cursor.execute("SELECT id, link FROM channels")
    channels = cursor.fetchall()
    unsubbed = []
    for ch_id, ch_link in channels:
        try:
            member = await bot.get_chat_member(chat_id=ch_id, user_id=user_id)
            if member.status in ['left', 'kicked']:
                unsubbed.append(ch_link)
        except Exception:
            pass 
    return unsubbed

# --- BEKOR QILISH ---
@dp.message(F.text == "❌ Bekor qilish", StateFilter('*'))
async def cancel_process(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Jarayon bekor qilindi.", reply_markup=ReplyKeyboardRemove())
    if is_admin(message.from_user.id):
        await message.answer("Admin paneliga qaytdingiz:", reply_markup=admin_menu)

# --- START VA YORDAM ---
@dp.message(CommandStart())
async def start_cmd(message: Message):
    user = message.from_user
    cursor.execute("INSERT OR IGNORE INTO users (id, full_name, username) VALUES (?, ?, ?)", 
                   (user.id, user.full_name, user.username))
    conn.commit()

    unsubbed = await check_sub(user.id)
    if unsubbed:
        btns = [[InlineKeyboardButton(text=f"📢 Kanalga obuna bo'lish", url=link)] for link in unsubbed]
        btns.append([InlineKeyboardButton(text="✅ Obuna bo'ldim", callback_data="check_sub")])
        await message.answer("Kinolarni ko'rish uchun avval quyidagi kanallarga obuna bo'ling:", 
                             reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
        return

    start_text = get_setting('start_text')
    await message.answer(start_text, reply_markup=ReplyKeyboardRemove())

@dp.callback_query(F.data == "check_sub")
async def check_sub_callback(call: CallbackQuery):
    unsubbed = await check_sub(call.from_user.id)
    if unsubbed:
        await call.answer("Barcha kanallarga obuna bo'lmadingiz!", show_alert=True)
    else:
        await call.message.delete()
        start_text = get_setting('start_text')
        await call.message.answer("Ajoyib! " + start_text)

@dp.message(Command("help"))
async def help_cmd(message: Message):
    if get_setting('help_text_status') == 'on':
        await message.answer("Bu bot orqali kinolarni kod yuborish orqali qabul qilib olishingiz mumkin. Shunchaki kino kodini yuboring!")

# --- ADMIN PANEL ---
@dp.message(Command("admin"))
async def admin_cmd(message: Message):
    if is_admin(message.from_user.id):
        await message.answer("🔧 Admin panelga xush kelibsiz. Nima qilamiz?", reply_markup=admin_menu)

# --- KINO QO'SHISH ---
@dp.callback_query(F.data == "add_movie")
async def add_movie_start(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    await call.message.answer("Kino uchun kod kiriting (masalan: 101):", reply_markup=cancel_btn)
    await state.set_state(BotStates.add_movie_code)

@dp.message(BotStates.add_movie_code)
async def add_movie_code_catch(message: Message, state: FSMContext):
    code = message.text.strip()
    cursor.execute("SELECT code FROM movies WHERE code=?", (code,))
    if cursor.fetchone():
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
    
    cursor.execute("INSERT INTO movies (code, file_id, type) VALUES (?, ?, ?)", (code, file_id, file_type))
    conn.commit()
    await state.clear()
    await message.answer(f"✅ Kino muvaffaqiyatli qo'shildi!\nKodi: <b>{code}</b>", parse_mode="HTML", reply_markup=ReplyKeyboardRemove())
    await message.answer("Admin panel:", reply_markup=admin_menu)

# --- QOLGAN ADMIN AMALLARI ---
@dp.callback_query(F.data.in_(["del_movie", "add_chan", "del_chan", "add_admin", "del_admin", "change_start", "broadcast"]))
async def handle_admin_callbacks(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id): return
    d = call.data
    
    if d == "del_movie":
        message_or_edit = call.message
        await message_or_edit.answer("O'chirmoqchi bo'lgan kino kodini yuboring:", reply_markup=cancel_btn)
        await state.set_state(BotStates.del_movie)
    elif d == "add_chan":
        await call.message.answer("Kanalni qo'shish uchun uning havolasini yuboring (Masalan: `https://t.me/kanal_nomi` yoki `@kanal_nomi`):", reply_markup=cancel_btn, parse_mode="Markdown")
        await state.set_state(BotStates.add_channel)
    elif d == "del_chan":
        cursor.execute("SELECT id, link FROM channels")
        chans = cursor.fetchall()
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
    cursor.execute("DELETE FROM movies WHERE code=?", (message.text.strip(),))
    conn.commit()
    await state.clear()
    await message.answer("✅ Kino bazadan o'chirildi (agar mavjud bo'lgan bo'lsa).", reply_markup=ReplyKeyboardRemove())

@dp.message(BotStates.add_channel)
async def add_channel(message: Message, state: FSMContext):
    text = message.text.strip()
    try:
        chat = await bot.get_chat(text)
        ch_id = str(chat.id)
        ch_link = chat.invite_link or (f"https://t.me/{chat.username}" if chat.username else text)
        
        cursor.execute("INSERT OR REPLACE INTO channels (id, link) VALUES (?, ?)", (ch_id, ch_link))
        conn.commit()
        await state.clear()
        await message.answer(f"✅ Kanal muvaffaqiyatli qo'shildi!\nID: {ch_id}", reply_markup=ReplyKeyboardRemove())
        if is_admin(message.from_user.id):
            await message.answer("Admin panel:", reply_markup=admin_menu)
    except Exception as e:
        await message.answer(f"❌ Kanalni topib bo'lmadi yoki bot o'sha kanalda admin emas!\n\nXatolik: {e}\n\nQaytadan to'g'ri havola yoki username yuboring:", reply_markup=cancel_btn)

@dp.message(BotStates.del_channel)
async def del_channel(message: Message, state: FSMContext):
    cursor.execute("DELETE FROM channels WHERE id=?", (message.text.strip(),))
    conn.commit()
    await state.clear()
    await message.answer("✅ Kanal o'chirildi.", reply_markup=ReplyKeyboardRemove())

@dp.message(BotStates.add_admin)
async def add_admin(message: Message, state: FSMContext):
    try:
        new_admin_id = int(message.text.strip())
        if new_admin_id == ASOSIY_ADMIN_ID:
            await message.answer("Bu asosiy admin!")
        else:
            cursor.execute("INSERT OR IGNORE INTO admins (id) VALUES (?)", (new_admin_id,))
            conn.commit()
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
            cursor.execute("DELETE FROM admins WHERE id=?", (del_id,))
            conn.commit()
            await message.answer("✅ Admin olib tashlandi.", reply_markup=ReplyKeyboardRemove())
    except ValueError:
        await message.answer("Faqat raqam (ID) yuboring!")
        return
    await state.clear()

@dp.message(BotStates.set_start_text)
async def set_start(message: Message, state: FSMContext):
    cursor.execute("UPDATE settings SET value=? WHERE key='start_text'", (message.text,))
    conn.commit()
    await state.clear()
    await message.answer("✅ Start matni yangilandi.", reply_markup=ReplyKeyboardRemove())

@dp.message(BotStates.broadcast)
async def broadcast_msg(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("📢 Xabar tarqatish boshlandi...", reply_markup=ReplyKeyboardRemove())
    cursor.execute("SELECT id FROM users")
    users = cursor.fetchall()
    count = 0
    for u in users:
        try:
            await message.send_copy(chat_id=u[0])
            count += 1
            await asyncio.sleep(0.04)
        except Exception:
            pass
    await message.answer(f"✅ Xabar jami {count} ta foydalanuvchiga yetkazildi.")

# --- YORDAM YOQISH/O'CHIRISH ---
@dp.callback_query(F.data == "toggle_help")
async def toggle_help(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    current = get_setting('help_text_status')
    new_stat = 'off' if current == 'on' else 'on'
    cursor.execute("UPDATE settings SET value=? WHERE key='help_text_status'", (new_stat,))
    conn.commit()
    await call.answer(f"Yordam xabari {'O‘CHIRILDI' if new_stat=='off' else 'YOQILDI'}", show_alert=True)

# --- STATISTIKA ---
@dp.callback_query(F.data == "stats")
async def show_stats(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    cursor.execute("SELECT COUNT(*) FROM users")
    u_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM movies")
    m_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT id, full_name, username FROM users ORDER BY id DESC LIMIT 15")
    last_users = cursor.fetchall()
    
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
    cursor.execute("SELECT file_id, type FROM movies WHERE code=?", (code,))
    movie = cursor.fetchone()
    
    if movie:
        file_id, m_type = movie
        if m_type == "video":
            await message.answer_video(video=file_id, caption=f"🎬 Kino kodi: {code}")
        else:
            await message.answer_document(document=file_id, caption=f"🎬 Kino kodi: {code}")
    else:
        if not is_admin(message.from_user.id):
            await message.answer("❌ Bunday kod bilan kino topilmadi.")

# --- ISHGA TUSHIRISH ---
async def main():
    print("Bot muvaffaqiyatli ishga tushdi...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
