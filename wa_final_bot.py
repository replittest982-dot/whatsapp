import asyncio
import logging
import sys
import os
import re
import csv
import io
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager

try:
    import aiosqlite
    from aiogram import Bot, Dispatcher, Router, F
    from aiogram.filters import Command, CommandStart, CommandObject
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.state import State, StatesGroup
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.types import (
        InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, 
        Message, ReactionTypeEmoji, BufferedInputFile
    )
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.exceptions import TelegramForbiddenError
except ImportError:
    sys.exit("❌ pip install aiogram aiosqlite")

# ==========================================
# КОНФИГУРАЦИЯ
# ==========================================
TOKEN = os.getenv("BOT_TOKEN", "YOUR_TOKEN_HERE")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DB_NAME = "bot_final.db"

SEP = "━━━━━━━━━━━━━━━━━━━━"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
router = Router()

# ==========================================
# БАЗА ДАННЫХ
# ==========================================
@asynccontextmanager
async def get_db():
    conn = await aiosqlite.connect(DB_NAME, timeout=30)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA journal_mode=WAL")
    try: 
        yield conn
    finally: 
        await conn.close()

async def init_db():
    async with get_db() as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                is_approved INTEGER DEFAULT 0,
                is_banned INTEGER DEFAULT 0,
                reg_date TEXT DEFAULT CURRENT_TIMESTAMP,
                last_afk_ping TEXT,
                afk_level INTEGER DEFAULT 0
            )
        """)
        
        await db.execute("""
            CREATE TABLE IF NOT EXISTS numbers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                phone TEXT,
                phone_hash TEXT,
                tariff_name TEXT,
                tariff_price TEXT,
                work_time TEXT,
                status TEXT DEFAULT 'queue',
                worker_id INTEGER DEFAULT 0,
                worker_chat_id INTEGER DEFAULT 0,
                worker_thread_id INTEGER DEFAULT 0,
                start_time TEXT,
                end_time TEXT,
                last_ping TEXT,
                afk_level INTEGER DEFAULT 0,
                wait_code_start TEXT,
                code_type TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        await db.execute("CREATE INDEX IF NOT EXISTS idx_active_numbers ON numbers(phone_hash, status) WHERE status IN('queue','work','active')")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_status_afk ON numbers(status, afk_level)")
        
        await db.execute("""CREATE TABLE IF NOT EXISTS tariffs (name TEXT PRIMARY KEY,price TEXT,work_time TEXT)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS groups (group_num INTEGER PRIMARY KEY,chat_id INTEGER,title TEXT)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY,value TEXT)""")
        
        await db.execute("INSERT OR IGNORE INTO tariffs VALUES('WhatsApp','50₽','10:00-22:00 МСК')")
        await db.execute("INSERT OR IGNORE INTO tariffs VALUES('MAX','10$','24/7')")
        await db.commit()
    logger.info("✅ Database initialized")

# ==========================================
# УТИЛИТЫ
# ==========================================
def clean_phone(phone):
    clean = re.sub(r'[^\d]', '', str(phone))
    if clean.startswith('77') and len(clean) == 11: return '+' + clean
    if clean.startswith('8') and len(clean) == 11: clean = '7' + clean[1:]
    elif len(clean) == 10: clean = '7' + clean
    return '+' + clean if re.match(r'^7\d{10}$', clean) else None

def get_phone_hash(phone):
    return re.sub(r'[^\d]', '', str(phone))

def mask_phone(phone, user_id):
    if user_id == ADMIN_ID: return phone
    try:
        if len(phone) < 9: return phone
        return f"{phone[:5]}***{phone[-4:]}"
    except: return phone

def get_now():
    return datetime.now(timezone.utc).isoformat()

def format_time(iso_str):
    try:
        dt = datetime.fromisoformat(iso_str)
        return (dt + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M МСК")
    except: return "-"

def calc_duration(start_iso, end_iso):
    try:
        if not start_iso or not end_iso: return "0 мин"
        s = datetime.fromisoformat(start_iso)
        e = datetime.fromisoformat(end_iso)
        mins = int((e - s).total_seconds() / 60)
        return f"{mins} мин"
    except: return "0 мин"

# ==========================================
# FSM
# ==========================================
class UserState(StatesGroup):
    waiting_numbers = State()
    waiting_help = State()

class AdminState(StatesGroup):
    waiting_broadcast = State()
    edit_price = State()
    edit_time = State()
    help_reply = State()
    report_hours = State()

# ==========================================
# КЛАВИАТУРЫ
# ==========================================
def main_kb(user_id):
    kb = InlineKeyboardBuilder()
    kb.button(text="📥 Сдать номер", callback_data="sel_tariff")
    kb.button(text="👤 Профиль", callback_data="profile")
    kb.button(text="ℹ️ Помощь", callback_data="guide")
    kb.button(text="🆘 Поддержка", callback_data="ask_help")
    if user_id == ADMIN_ID: kb.button(text="⚡ Админ панель", callback_data="admin_main")
    kb.adjust(1, 2, 1, 1)
    return kb.as_markup()

def worker_kb_whatsapp(nid):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Встал", callback_data=f"w_act_{nid}")
    kb.button(text="❌ Ошибка", callback_data=f"w_err_{nid}")
    return kb.as_markup()

def worker_kb_max(nid):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Встал", callback_data=f"w_act_{nid}")
    kb.button(text="⏭ Пропуск", callback_data=f"w_skip_{nid}")
    return kb.as_markup()

def worker_active_kb(nid):
    return InlineKeyboardBuilder().button(text="📉 Слет", callback_data=f"w_drop_{nid}").as_markup()

# ==========================================
# КОМАНДЫ
# ==========================================
@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    async with get_db() as db:
        res = await (await db.execute("SELECT * FROM users WHERE user_id=?", (uid,))).fetchone()
        if not res:
            await db.execute("INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?)", (uid, m.from_user.username, m.from_user.first_name))
            await db.commit()
            if ADMIN_ID:
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Принять", callback_data=f"acc_ok_{uid}"), InlineKeyboardButton(text="🚫 Бан", callback_data=f"acc_no_{uid}")]])
                try: await m.bot.send_message(ADMIN_ID, f"👤 Запрос доступа: {uid} (@{m.from_user.username})", reply_markup=kb)
                except: pass
            return await m.answer("🔒 Доступ ограничен.\nОжидайте одобрения.")
        if res['is_banned']: return await m.answer("🚫 Вы заблокированы.")
        if res['is_approved']: await m.answer(f"👋 Привет, {m.from_user.first_name}!\n{SEP}", reply_markup=main_kb(uid))
        else: await m.answer("⏳ Заявка на рассмотрении.")

@router.message(Command("bindgroup"))
async def cmd_bindgroup(m: Message, command: CommandObject):
    if m.from_user.id != ADMIN_ID: return
    if not command.args: return await m.reply("❌ Использование: /bindgroup 1")
    try:
        group_num = int(command.args.strip())
        if group_num not in [1, 2, 3]: raise ValueError
    except: return await m.reply("❌ Номер группы: 1, 2 или 3")
    async with get_db() as db:
        await db.execute("INSERT OR REPLACE INTO groups (group_num, chat_id, title) VALUES (?, ?, ?)", (group_num, m.chat.id, m.chat.title or f"Chat {m.chat.id}"))
        await db.commit()
    await m.answer(f"✅ Чат привязан!\n\n👨‍💻 Гайд:\n\n1️⃣ /num → Получить номер\n2️⃣ Вбить в WhatsApp Web\n3️⃣ QR: Фото + /sms +77... Сканируй\n4️⃣ Код: Фото + /sms +77... Вводи код\n5️⃣ Встал → ✅ Встал\n6️⃣ Слетел → 📉 Слет")

@router.message(Command("startwork"))
async def cmd_startwork(m: Message):
    if m.from_user.id != ADMIN_ID: return
    async with get_db() as db:
        tariffs = await (await db.execute("SELECT name FROM tariffs")).fetchall()
    kb = InlineKeyboardBuilder()
    for t in tariffs: kb.button(text=t['name'], callback_data=f"bind_{t['name']}")
    kb.adjust(1)
    await m.answer("⚙️ Настройка воркера\n{SEP}\nВыберите тариф:", reply_markup=kb.as_markup())

@router.message(Command("stopwork"))
async def cmd_stopwork(m: Message):
    if m.from_user.id != ADMIN_ID: return
    async with get_db() as db:
        await db.execute("DELETE FROM config WHERE key=?", (f"topic_{m.chat.id}_{m.message_thread_id if m.is_topic_message else 0}",))
        await db.commit()
    await m.reply("🛑 Топик отключен.")

@router.message(Command("num"))
async def cmd_num(m: Message, bot: Bot):
    tid = m.message_thread_id if m.is_topic_message else 0
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            conf = await (await db.execute("SELECT value FROM config WHERE key=?", (f"topic_{m.chat.id}_{tid}",))).fetchone()
            if not conf: 
                await db.rollback()
                return await m.reply("❌ Топик не настроен")
            
            tariff_name = conf['value']
            row = await (await db.execute("SELECT * FROM numbers WHERE status='queue' AND tariff_name=? ORDER BY id ASC LIMIT 1", (tariff_name,))).fetchone()
            
            if not row: 
                await db.commit()
                return await m.reply("📭 Очередь пуста")
            
            await db.execute("UPDATE numbers SET status='work', worker_id=?, worker_chat_id=?, worker_thread_id=?, start_time=? WHERE id=?", (m.from_user.id, m.chat.id, tid, get_now(), row['id']))
            await db.commit()
            
        except Exception as e:
            await db.rollback()
            logger.error(f"Error in /num: {e}")
            return await m.reply("❌ Ошибка при получении номера")

    if "MAX" in tariff_name.upper():
        msg = f"🚀 Вы взяли номер\n{SEP}\n📱 {row['phone']}\n\nКод: /code {row['phone']}"
        kb = worker_kb_max(row['id'])
    else:
        msg = f"🚀 Вы взяли номер\n{SEP}\n📱 {row['phone']}\n\nКод: /sms {row['phone']} текст"
        kb = worker_kb_whatsapp(row['id'])
    
    await m.answer(msg, reply_markup=kb)
    try: await bot.send_message(row['user_id'], f"⚡ Ваш номер взяли\n{SEP}\n📱 {mask_phone(row['phone'], row['user_id'])}\nОжидайте код")
    except: pass

@router.message(Command("sms"))
async def cmd_sms(m: Message, command: CommandObject, bot: Bot):
    if not command.args: return await m.reply("⚠️ Формат: /sms +7999... текст")
    parts = command.args.split(maxsplit=1)
    if len(parts) < 2: return await m.reply("⚠️ Укажите текст после номера")
    ph = clean_phone(parts[0])
    async with get_db() as db:
        row = await (await db.execute("SELECT * FROM numbers WHERE phone=? AND status IN ('work','active')", (ph,))).fetchone()
    if not row or row['worker_id'] != m.from_user.id: return await m.reply("❌ Не ваш номер")
    try:
        await bot.send_message(row['user_id'], f"📩 {parts[1]}\n{SEP}\n📱 {mask_phone(row['phone'], row['user_id'])}")
        await m.reply("✅ Отправлено")
    except: await m.reply("❌ Ошибка")

@router.message(Command("code"))
async def cmd_code(m: Message, command: CommandObject, bot: Bot):
    if not command.args: return await m.reply("⚠️ Пример: /code +7999...")
    ph = clean_phone(command.args.split()[0])
    async with get_db() as db:
        row = await (await db.execute("SELECT * FROM numbers WHERE phone=? AND status IN ('work','active')", (ph,))).fetchone()
    if not row or row['worker_id'] != m.from_user.id: return await m.reply("❌ Не ваш номер")
    async with get_db() as db:
        await db.execute("UPDATE numbers SET wait_code_start=? WHERE id=?", (get_now(), row['id']))
        await db.commit()
    try:
        await bot.send_message(row['user_id'], f"🔔 Запрос кода\n{SEP}\n📱 {mask_phone(row['phone'], row['user_id'])}\n\nОтветьте сообщением")
        await m.reply("✅ Запрос отправлен") 
    except: pass

# ==========================================
# CALLBACK ХЭНДЛЕРЫ
# ==========================================

@router.callback_query(F.data == "guide")
async def cb_guide(c: CallbackQuery):
    await c.message.edit_text(f"📲 Что делает бот\nБот принимает номера WhatsApp/MAX, ставит их в очередь и выплачивает средства.\n\n📦 Требования\n✔️ Активный номер\n✔️ Доступ к SMS\n❌ Виртуальные не принимаются\n\n⚠️ ОДИН НОМЕР = ОДНА ОТПРАВКА\n\nПоддержка: кнопка SOS", reply_markup=main_kb(c.from_user.id))

@router.callback_query(F.data == "profile")
async def cb_profile(c: CallbackQuery):
    uid = c.from_user.id
    async with get_db() as db:
        total = (await (await db.execute("SELECT COUNT(*) FROM numbers WHERE user_id=?", (uid,))).fetchone())[0]
        active = (await (await db.execute("SELECT COUNT(*) FROM numbers WHERE user_id=? AND status='active'", (uid,))).fetchone())[0]
        my_first = await (await db.execute("SELECT id FROM numbers WHERE user_id=? AND status='queue' ORDER BY id ASC LIMIT 1", (uid,))).fetchone()
        q_pos = 0
        if my_first: q_pos = (await (await db.execute("SELECT COUNT(*) FROM numbers WHERE status='queue' AND id < ?", (my_first[0],))).fetchone())[0] + 1
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 История", callback_data="my_nums")
    kb.button(text="🔙 Меню", callback_data="back_main")
    kb.adjust(1)
    await c.message.edit_text(f"👤 Личный кабинет\n{SEP}\n🆔 ID: {uid}\n📦 Всего: {total}\n🔥 В работе: {active}\n🕒 Позиция: {q_pos if q_pos else '-'}", reply_markup=kb.as_markup())

@router.callback_query(F.data == "my_nums")
async def cb_my_nums(c: CallbackQuery):
    uid = c.from_user.id
    async with get_db() as db:
        rows = await (await db.execute("SELECT id, phone, status, tariff_price FROM numbers WHERE user_id=? ORDER BY id DESC LIMIT 10", (uid,))).fetchall()
    kb = InlineKeyboardBuilder()
    txt = f"📝 История\n{SEP}\n"
    if not rows: txt += "📭 Пусто"
    else:
        for r in rows:
            icon = "🟡" if r['status'] == 'queue' else "🟢" if r['status'] == 'active' else "✅" if r['status'] == 'finished' else "❌"
            txt += f"{icon} {mask_phone(r['phone'], uid)} | {r['tariff_price']}\n"
            if r['status'] == 'queue': kb.button(text=f"🗑 {mask_phone(r['phone'], uid)}", callback_data=f"del_{r['id']}")
    kb.button(text="🔙 Назад", callback_data="profile")
    kb.adjust(1)
    await c.message.edit_text(txt, reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("del_"))
async def cb_del(c: CallbackQuery):
    nid = c.data.split("_")[1]
    async with get_db() as db:
        row = await (await db.execute("SELECT status FROM numbers WHERE id=? AND user_id=?", (nid, c.from_user.id))).fetchone()
        if row and row['status'] == 'queue':
            await db.execute("DELETE FROM numbers WHERE id=?", (nid,))
            await db.commit()
            await c.answer("✅ Удалено")
            await cb_my_nums(c)
        else: await c.answer("❌ Уже в работе!", show_alert=True)

@router.callback_query(F.data == "sel_tariff")
async def cb_sel_tariff(c: CallbackQuery):
    async with get_db() as db:
        tariffs = await (await db.execute("SELECT * FROM tariffs")).fetchall()
    if not tariffs: return await c.message.edit_text("❌ Тарифы не настроены!", reply_markup=main_kb(c.from_user.id))
    kb = InlineKeyboardBuilder()
    for t in tariffs: kb.button(text=f"{t['name']} | {t['price']}", callback_data=f"pick_{t['name']}")
    kb.button(text="🔙 Меню", callback_data="back_main")
    kb.adjust(1)
    await c.message.edit_text(f"📂 Выберите тариф\n{SEP}", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("pick_"))
async def cb_pick(c: CallbackQuery, state: FSMContext):
    tn = c.data.split("_")[1]
    async with get_db() as db:
        t = await (await db.execute("SELECT * FROM tariffs WHERE name=?", (tn,))).fetchone()
    await state.update_data(tariff=tn, price=t['price'], work_time=t['work_time'])
    await state.set_state(UserState.waiting_numbers)
    kb = InlineKeyboardBuilder().button(text="🔙 Отмена", callback_data="back_main")
    await c.message.edit_text(f"💎 Тариф: {tn}\n{SEP}\n💰 {t['price']}\n⏰ {t['work_time']}\n\n📱 Отправьте номера (списком или по одному)", reply_markup=kb.as_markup())

@router.callback_query(F.data == "ask_help")
async def cb_ask_help(c: CallbackQuery, state: FSMContext):
    await state.set_state(UserState.waiting_help)
    kb = InlineKeyboardBuilder().button(text="🔙 Отмена", callback_data="back_main")
    await c.message.edit_text("🆘 Поддержка\n{SEP}\nНапишите запрос:", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("bind_"))
async def cb_bind(c: CallbackQuery):
    tn = c.data.split("_")[1]
    tid = c.message.message_thread_id if c.message.is_topic_message else 0
    async with get_db() as db:
        await db.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (f"topic_{c.message.chat.id}_{tid}", tn))
        await db.commit()
    await c.message.edit_text(f"✅ Топик привязан! Тариф: {tn}\n/num - взять номер")

@router.callback_query(F.data.startswith("w_act_"))
async def cb_w_act(c: CallbackQuery, bot: Bot):
    nid = c.data.split("_")[2]
    async with get_db() as db:
        row = await (await db.execute("SELECT * FROM numbers WHERE id=?", (nid,))).fetchone()
        if not row or row['worker_id'] != c.from_user.id: return await c.answer("🚫 Не твой номер!", show_alert=True)
        await db.execute("UPDATE numbers SET status='active', last_ping=? WHERE id=?", (get_now(), nid))
        await db.commit()
    await c.message.edit_text("✅ Номер встал", reply_markup=worker_active_kb(nid))
    try: await bot.send_message(row['user_id'], "✅ Номер встал")
    except: pass

@router.callback_query(F.data.startswith("w_skip_"))
async def cb_w_skip(c: CallbackQuery, bot: Bot):
    nid = c.data.split("_")[2]
    async with get_db() as db:
        row = await (await db.execute("SELECT * FROM numbers WHERE id=?", (nid,))).fetchone()
        if not row or row['worker_id'] != c.from_user.id: return await c.answer("🚫 Не твой!", show_alert=True)
        await db.execute("UPDATE numbers SET status='queue', worker_id=0, worker_chat_id=0 WHERE id=?", (nid,))
        await db.commit()
    await c.message.edit_text("⏭ Пропуск")
    try: await bot.send_message(row['user_id'], "⏭ Номер вернули в очередь")
    except: pass

@router.callback_query(F.data.startswith(("w_drop_", "w_err_")))
async def cb_w_finish(c: CallbackQuery, bot: Bot):
    nid = c.data.split("_")[2]
    is_drop = "drop" in c.data
    async with get_db() as db:
        row = await (await db.execute("SELECT * FROM numbers WHERE id=?", (nid,))).fetchone()
        if not row or row['worker_id'] != c.from_user.id: return await c.answer("🚫 Не твой!", show_alert=True)
        status = "finished" if is_drop else "dead"
        end_time = get_now()
        duration = calc_duration(row['start_time'], end_time)
        await db.execute("UPDATE numbers SET status=?, end_time=? WHERE id=?", (status, end_time, nid))
        await db.commit()
    msg = "📉 Слет" if is_drop else "❌ Ошибка"
    user_msg = f"📉 Слет\n{SEP}\n⏱ {duration}" if is_drop else "❌ Ошибка"
    await c.message.edit_text(msg)
    try: await bot.send_message(row['user_id'], user_msg)
    except: pass

@router.callback_query(F.data == "back_main")
async def cb_back(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text(f"👋 Главное меню\n{SEP}", reply_markup=main_kb(c.from_user.id))

@router.callback_query(F.data.startswith("acc_"))
async def cb_acc(c: CallbackQuery, bot: Bot):
    if c.from_user.id != ADMIN_ID: return
    action, uid = c.data.split("_")[1], int(c.data.split("_")[2])
    async with get_db() as db:
        if action == "ok":
            await db.execute("UPDATE users SET is_approved=1 WHERE user_id=?", (uid,))
            await db.commit()
            await c.message.edit_text(f"✅ Принят {uid}")
            try: await bot.send_message(uid, "✅ Доступ открыт!\n/start")
            except: pass
        else:
            await db.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (uid,))
            await db.commit()
            await c.message.edit_text(f"🚫 Забанен {uid}")

# ==========================================
# ИСПРАВЛЕННАЯ AFK СИСТЕМА
# ==========================================
@router.callback_query(F.data.startswith("afk_ok_"))
async def cb_afk(c: CallbackQuery):
    """Юзер нажал 'Я тут!' - сбрасываем его AFK статус в таблице users"""
    uid = c.from_user.id
    async with get_db() as db:
        # Обновляем запись юзера (не номеров!)
        await db.execute(
            "UPDATE users SET last_afk_ping=?, afk_level=0 WHERE user_id=?", 
            (get_now(), uid)
        )
        await db.commit()
    
    try:
        await c.message.delete()
    except:
        pass
    await c.answer("✅ Вы в очереди!")

@router.callback_query(F.data == "admin_main")
async def cb_adm(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID: return
    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Тарифы", callback_data="adm_tariffs")
    kb.button(text="📊 Отчеты", callback_data="adm_reports")
    kb.button(text="📢 Рассылка", callback_data="adm_cast")
    kb.button(text="🏢 Группы", callback_data="manage_groups")
    kb.button(text="📋 Очередь (ВСЯ)", callback_data="all_queue")
    kb.button(text="🔙 Меню", callback_data="back_main")
    kb.adjust(2)
    await c.message.edit_text("⚡ Админ панель\n{SEP}", reply_markup=kb.as_markup())

@router.callback_query(F.data == "all_queue")
async def cb_all_queue(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID: return
    async with get_db() as db:
        queue = await (await db.execute("SELECT id, phone, tariff_name FROM numbers WHERE status='queue' ORDER BY id ASC LIMIT 50")).fetchall()
        active = await (await db.execute("SELECT id, phone, tariff_name, worker_id FROM numbers WHERE status IN ('work', 'active') ORDER BY id ASC LIMIT 50")).fetchall()
    
    txt = f"📋 ОЧЕРЕДЬ (Топ 50)\n{SEP}\n\n🟡 В ОЧЕРЕДИ ({len(queue)}):\n"
    if queue:
        for i, r in enumerate(queue, 1): txt += f"{i}. {r['phone']} | {r['tariff_name']}\n"
    else: txt += "Пусто\n"
    
    txt += f"\n🟢 В РАБОТЕ ({len(active)}):\n"
    if active:
        for r in active: txt += f"📱 {r['phone']} | {r['tariff_name']} | W:{r['worker_id']}\n"
    else: txt += "Пусто\n"
    
    if len(txt) > 4000: txt = txt[:4000] + "\n...обрезано..."
    kb = InlineKeyboardBuilder().button(text="🔙 Назад", callback_data="admin_main")
    await c.message.edit_text(txt, reply_markup=kb.as_markup())

@router.callback_query(F.data == "manage_groups")
async def cb_mgr(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID: return
    async with get_db() as db:
        groups = await (await db.execute("SELECT * FROM groups ORDER BY group_num")).fetchall()
    kb = InlineKeyboardBuilder()
    for i in range(1, 4):
        g_name = "Не привязана"
        for g in groups:
            if g['group_num'] == i: g_name = g['title']; break
        kb.button(text=f"🛑 {g_name}", callback_data=f"stop_group_{i}")
    kb.button(text="📊 Статус", callback_data="groups_status")
    kb.button(text="🔙 Назад", callback_data="admin_main")
    kb.adjust(1)
    await c.message.edit_text("🏢 Группы\n{SEP}", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("stop_group_"))
async def cb_stop_g(c: CallbackQuery, bot: Bot):
    if c.from_user.id != ADMIN_ID: return
    gn = int(c.data.split("_")[-1])
    stop_time = get_now()
    async with get_db() as db:
        g = await (await db.execute("SELECT * FROM groups WHERE group_num=?", (gn,))).fetchone()
        if not g: return await c.answer(f"❌ Группа {gn} не привязана!", show_alert=True)
        cid, title = g['chat_id'], g['title']
        nums = await (await db.execute("SELECT id, user_id, phone, start_time FROM numbers WHERE status IN ('work','active') AND worker_chat_id=?", (cid,))).fetchall()
        stopped = 0
        for num in nums:
            await db.execute("UPDATE numbers SET status=?, end_time=? WHERE id=?", (f"finished_group_{gn}", stop_time, num['id']))
            stopped += 1
            duration = calc_duration(num['start_time'], stop_time)
            try: await bot.send_message(num['user_id'], f"🛑 {title} остановлен\n{SEP}\n📱 {mask_phone(num['phone'], num['user_id'])}\n⏱ {duration}")
            except: pass
        await db.commit()
    await c.message.edit_text(f"🛑 Группа {gn} остановлена\n{SEP}\n🏢 {title}\n⏰ {format_time(stop_time)}\n📦 {stopped}")

@router.callback_query(F.data == "groups_status")
async def cb_g_stat(c: CallbackQuery):
    async with get_db() as db:
        stats = {}
        for i in range(1, 4): stats[f"Группа {i}"] = (await (await db.execute("SELECT COUNT(*) FROM numbers WHERE status=?", (f"finished_group_{i}",))).fetchone())[0]
        active = (await (await db.execute("SELECT COUNT(*) FROM numbers WHERE status IN ('work','active')")).fetchone())[0]
        queue = (await (await db.execute("SELECT COUNT(*) FROM numbers WHERE status='queue'")).fetchone())[0]
    txt = f"📊 СТАТУС\n{SEP}\n"
    for g, cnt in stats.items(): txt += f"🏁 {g}: {cnt}\n"
    txt += f"\n🔥 Активно: {active}\n🟡 Очередь: {queue}"
    kb = InlineKeyboardBuilder().button(text="🔙 Назад", callback_data="manage_groups")
    await c.message.edit_text(txt, reply_markup=kb.as_markup())

@router.callback_query(F.data == "adm_tariffs")
async def cb_adm_t(c: CallbackQuery):
    if c.from_user.id != ADMIN_ID: return
    async with get_db() as db:
        ts = await (await db.execute("SELECT * FROM tariffs")).fetchall()
    kb = InlineKeyboardBuilder()
    for t in ts: kb.button(text=f"✏️ {t['name']}", callback_data=f"ed_{t['name']}")
    kb.button(text="🔙 Назад", callback_data="admin_main")
    kb.adjust(1)
    await c.message.edit_text("🛠 Тариф:", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("ed_"))
async def cb_ed_t(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID: return
    target = c.data.split("_")[1]
    await state.update_data(target=target)
    await state.set_state(AdminState.edit_price)
    await c.message.edit_text(f"1️⃣ ЦЕНА для {target}\nПример: 50₽, 10$")

@router.callback_query(F.data == "adm_reports")
async def cb_adm_r(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID: return
    await state.set_state(AdminState.report_hours)
    await c.message.edit_text("📊 Отчет за сколько часов?\n(до 120 часов)")

@router.callback_query(F.data == "adm_cast")
async def cb_cast(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID: return
    await state.set_state(AdminState.waiting_broadcast)
    await c.message.edit_text("📢 Пришлите пост:")

@router.callback_query(F.data.startswith("helpreply_"))
async def cb_helpreply(c: CallbackQuery, state: FSMContext):
    if c.from_user.id != ADMIN_ID: return
    uid = c.data.split("_")[1]
    await state.update_data(help_uid=uid)
    await state.set_state(AdminState.help_reply)
    await c.message.answer(f"✍️ Ответ для {uid}:")
    await c.answer()

# ==========================================
# FSM ХЭНДЛЕРЫ
# ==========================================

@router.message(UserState.waiting_numbers)
async def fsm_nums(m: Message, state: FSMContext):
    data = await state.get_data()
    raw = re.split(r'[;,\n]', m.text)
    valid = [clean_phone(x.strip()) for x in raw if clean_phone(x.strip())]
    if not valid: return await m.reply("❌ Номера не найдены")
    added = 0
    duplicates = []
    
    # Инициализируем AFK статус для юзера если его нет
    async with get_db() as db:
        user_check = await (await db.execute("SELECT last_afk_ping FROM users WHERE user_id=?", (m.from_user.id,))).fetchone()
        if not user_check or not user_check['last_afk_ping']:
            await db.execute("UPDATE users SET last_afk_ping=?, afk_level=0 WHERE user_id=?", (get_now(), m.from_user.id))
            await db.commit()
    
    async with get_db() as db:
        for ph in valid:
            ph_hash = get_phone_hash(ph)
            exists = await (await db.execute("SELECT id FROM numbers WHERE phone_hash=? AND status IN ('queue', 'work', 'active')", (ph_hash,))).fetchone()
            if exists:
                duplicates.append(ph)
                continue
            # Номера больше не хранят afk_level - он теперь в users
            await db.execute("INSERT INTO numbers (user_id, phone, phone_hash, tariff_name, tariff_price, work_time) VALUES (?, ?, ?, ?, ?, ?)", (m.from_user.id, ph, ph_hash, data['tariff'], data['price'], data['work_time']))
            added += 1
        await db.commit()
    msg = f"✅ Добавлено: {added}\n"
    if duplicates:
        msg += f"\n❌ Уже в очереди/работе: {len(duplicates)}\n"
        for dup in duplicates[:5]: msg += f"• {mask_phone(dup, m.from_user.id)}\n"
    await state.clear()
    await m.answer(msg, reply_markup=main_kb(m.from_user.id))

@router.message(UserState.waiting_help)
async def fsm_help(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    kb = InlineKeyboardBuilder().button(text="💬 Ответить", callback_data=f"helpreply_{m.from_user.id}")
    try:
        await bot.send_message(ADMIN_ID, f"🆘 Запрос\n{SEP}\nОт: {m.from_user.id} (@{m.from_user.username})\n\n{m.text}", reply_markup=kb.as_markup())
        await m.answer("✅ Запрос отправлен", reply_markup=main_kb(m.from_user.id))
    except Exception as e:
        logger.error(f"Help error: {e}")
        await m.answer("❌ Ошибка")

@router.message(AdminState.help_reply)
async def fsm_helpreply(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    await state.clear()
    try:
        await bot.send_message(data['help_uid'], f"👨‍💻 Ответ:\n{SEP}\n{m.text}")
        await m.answer("✅ Отправлено")
    except: await m.answer("❌ Не доставлено")

@router.message(AdminState.waiting_broadcast)
async def fsm_cast(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    msg = await m.answer("⏳ Рассылка...")
    async with get_db() as db:
        users = await (await db.execute("SELECT user_id FROM users WHERE is_approved=1")).fetchall()
    success, fail = 0, 0
    for u in users:
        try:
            await m.copy_to(u['user_id'])
            success += 1
            await asyncio.sleep(0.05)
        except TelegramForbiddenError: fail += 1
        except: fail += 1
    await msg.edit_text(f"📢 Завершено\n{SEP}\n✅ {success}\n❌ {fail}\n📊 {len(users)}")

@router.message(AdminState.edit_price)
async def fsm_ep(m: Message, state: FSMContext):
    await state.update_data(price=m.text)
    await state.set_state(AdminState.edit_time)
    await m.answer("2️⃣ ВРЕМЯ РАБОТЫ\nПример: 10:00-22:00 МСК, 24/7")

@router.message(AdminState.edit_time)
async def fsm_et(m: Message, state: FSMContext):
    data = await state.get_data()
    async with get_db() as db:
        await db.execute("UPDATE tariffs SET price=?, work_time=? WHERE name=?", (data['price'], m.text, data['target']))
        await db.commit()
    await state.clear()
    await m.answer(f"✅ Обновлено!\n{SEP}\n💰 {data['price']}\n⏰ {m.text}")

@router.message(AdminState.report_hours)
async def fsm_rep(m: Message, state: FSMContext):
    await state.clear()
    try:
        hours = int(m.text)
        if hours < 1 or hours > 120: return await m.answer("❌ От 1 до 120")
    except: return await m.answer("❌ Число!")
    cut_time = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    async with get_db() as db:
        rows = await (await db.execute("SELECT n.*, g.title as group_name FROM numbers n LEFT JOIN groups g ON n.worker_chat_id = g.chat_id WHERE n.created_at >= ? ORDER BY n.id DESC", (cut_time,))).fetchall()
    if not rows: return await m.answer("📂 Пусто")
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(['ID', 'UserID', 'Phone', 'Status', 'Group', 'Tariff', 'Created', 'Start', 'End', 'Duration'])
    for r in rows:
        duration = calc_duration(r['start_time'], r['end_time'])
        gn = r['group_name'] if r['group_name'] else "-"
        w.writerow([r['id'], r['user_id'], r['phone'], r['status'], gn, r['tariff_name'], format_time(r['created_at']), format_time(r['start_time']), format_time(r['end_time']), duration])
    out.seek(0)
    doc = BufferedInputFile(out.getvalue().encode(), filename=f"report_{hours}h.csv")
    await m.answer_document(doc, caption=f"📊 Отчет за {hours}ч")

# ==========================================
# ПРОЧИЕ ХЭНДЛЕРЫ
# ==========================================

@router.message(F.photo & F.caption)
async def handle_photo(m: Message, bot: Bot):
    if "/sms" not in m.caption.lower(): return
    ph = clean_phone(m.caption.split()[1]) if len(m.caption.split()) > 1 else None
    if not ph: return await m.reply("⚠️ Формат: /sms +7...")
    async with get_db() as db:
        row = await (await db.execute("SELECT * FROM numbers WHERE phone=? AND status IN ('work','active')", (ph,))).fetchone()
    if not row or row['worker_id'] != m.from_user.id: return await m.reply("❌ Не ваш номер")
    try:
        await bot.send_photo(row['user_id'], m.photo[-1].file_id, caption=f"🔔 ОТ ОФИСА\n{SEP}")
        await m.react([ReactionTypeEmoji(emoji="🔥")])
    except: await m.reply("❌ Ошибка")

@router.message(F.chat.type == "private")
async def handle_msg(m: Message, bot: Bot, state: FSMContext):
    if m.text and m.text.startswith('/'): return
    if m.from_user.id == ADMIN_ID: return
    cs = await state.get_state()
    if cs: return
    async with get_db() as db:
        row = await (await db.execute("SELECT * FROM numbers WHERE user_id=? AND status IN ('work','active')", (m.from_user.id,))).fetchone()
    if row and row['worker_chat_id']:
        async with get_db() as db:
            await db.execute("UPDATE numbers SET wait_code_start=NULL WHERE id=?", (row['id'],))
            await db.commit()
        try:
            tc = row['worker_chat_id']
            tt = row['worker_thread_id'] if row['worker_thread_id'] else None
            hdr = f"📩 ОТВЕТ\n📱 {row['phone']}\n{SEP}\n"
            if m.text: await bot.send_message(tc, message_thread_id=tt, text=f"{hdr}💬 {m.text}")
            elif m.photo: await bot.send_photo(tc, message_thread_id=tt, photo=m.photo[-1].file_id, caption=f"{hdr}📸")
            await m.answer("✅ Отправлено")
        except: await m.answer("❌ Ошибка")

# ==========================================
# ИСПРАВЛЕННЫЙ МОНИТОР (1 УВЕДОМЛЕНИЕ НА ЮЗЕРА)
# ==========================================
async def monitor(bot: Bot):
    logger.info("🔍 Monitor started")
    while True:
        try:
            await asyncio.sleep(30)
            now = datetime.now(timezone.utc)
            
            async with get_db() as db:
                # ===== 1. КОД ТАЙМАУТ (5 мин) =====
                waiters = await (await db.execute(
                    "SELECT id, user_id, phone, worker_chat_id, worker_thread_id, wait_code_start "
                    "FROM numbers WHERE status='active' AND wait_code_start IS NOT NULL"
                )).fetchall()
                
                for w in waiters:
                    st = datetime.fromisoformat(w['wait_code_start'])
                    if (now - st).total_seconds() / 60 >= 5:
                        await db.execute(
                            "UPDATE numbers SET status='dead', end_time=?, wait_code_start=NULL WHERE id=?", 
                            (get_now(), w['id'])
                        )
                        try:
                            await bot.send_message(w['user_id'], f"⏰ Время ожидания кода вышло\n{w['phone']} отменен")
                            if w['worker_chat_id']: 
                                await bot.send_message(
                                    chat_id=w['worker_chat_id'], 
                                    message_thread_id=w['worker_thread_id'] if w['worker_thread_id'] else None, 
                                    text="⚠️ Таймаут кода (5 мин)!"
                                )
                        except Exception as e:
                            logger.error(f"Timeout notify failed: {e}")
                
                await db.commit()

                # ===== 2. AFK СИСТЕМА (ОДИН ЗАПРОС НА ЮЗЕРА) =====
                # Берем только тех юзеров, у которых ЕСТЬ номера в очереди
                sql = """
                    SELECT DISTINCT u.user_id, u.last_afk_ping, u.afk_level
                    FROM users u
                    INNER JOIN numbers n ON u.user_id = n.user_id
                    WHERE n.status = 'queue'
                """
                users_with_queue = await (await db.execute(sql)).fetchall()
                
                updates_to_apply = []
                notifications_to_send = []
                
                for user in users_with_queue:
                    uid = user['user_id']
                    last_ping = user['last_afk_ping']
                    current_level = user['afk_level'] or 0
                    
                    # Если нет пинга - ставим текущее время
                    if not last_ping:
                        await db.execute(
                            "UPDATE users SET last_afk_ping=?, afk_level=0 WHERE user_id=?", 
                            (get_now(), uid)
                        )
                        continue
                    
                    try:
                        last_time = datetime.fromisoformat(last_ping)
                    except:
                        logger.warning(f"Invalid timestamp for user {uid}: {last_ping}")
                        continue
                    
                    diff_min = (now - last_time).total_seconds() / 60
                    new_level = current_level
                    notify_text = None
                    kb = None
                    kick = False
                    
                    # ===== ЛОГИКА 7-3-1 =====
                    if current_level == 0 and diff_min > 7:
                        new_level = 1
                        notify_text = "⏳ Вы тут? Осталось 3 минуты! Нажмите кнопку ниже."
                        kb = InlineKeyboardBuilder().button(
                            text="👋 Я тут!", 
                            callback_data=f"afk_ok_{uid}"
                        ).as_markup()
                    
                    elif current_level == 1 and diff_min > 10:  # 7 + 3
                        new_level = 2
                        notify_text = "⚠️ ПОСЛЕДНЕЕ ПРЕДУПРЕЖДЕНИЕ: Осталась 1 минута!"
                        
                    elif current_level == 2 and diff_min > 11:  # 10 + 1
                        new_level = 3
                        kick = True
                        notify_text = "❌ Вы были удалены из очереди за неактивность (AFK)"

                    # Применяем только если уровень изменился
                    if new_level > current_level:
                        updates_to_apply.append((new_level, uid, kick))
                        if notify_text:
                            notifications_to_send.append((uid, notify_text, kb))
                
                # ===== ПРИМЕНЯЕМ ВСЕ ОБНОВЛЕНИЯ РАЗОМ =====
                for new_level, uid, kick in updates_to_apply:
                    if kick:
                        logger.info(f"❌ Kicking AFK user {uid}")
                        # Удаляем ВСЕ номера этого юзера из очереди
                        await db.execute("DELETE FROM numbers WHERE user_id=? AND status='queue'", (uid,))
                        # Сбрасываем его AFK статус
                        await db.execute("UPDATE users SET afk_level=0, last_afk_ping=NULL WHERE user_id=?", (uid,))
                    else:
                        # Обновляем уровень AFK в users
                        await db.execute(
                            "UPDATE users SET afk_level=?, last_afk_ping=? WHERE user_id=?", 
                            (new_level, get_now(), uid)
                        )
                
                # ✅ КРИТИЧНО: Коммитим ДО отправки уведомлений!
                await db.commit()
                
                # ===== ОТПРАВЛЯЕМ УВЕДОМЛЕНИЯ =====
                for uid, text, kb in notifications_to_send:
                    try:
                        await bot.send_message(uid, text, reply_markup=kb)
                        logger.info(f"✉️ AFK notification sent to {uid}")
                    except Exception as e:
                        logger.warning(f"⚠️ Failed to notify {uid}: {e}")
                
        except Exception as e:
            logger.exception(f"💥 Monitor loop error: {e}")
            await asyncio.sleep(5)

async def main():
    await init_db()
    if not TOKEN: sys.exit("FATAL: No BOT_TOKEN")
    bot = Bot(token=TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(monitor(bot))
    logger.info("🚀 BOT STARTED")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
