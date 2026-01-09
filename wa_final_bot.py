import sys
import asyncio
import os
import logging
import random
import psutil
import shutil
import aiosqlite
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

# 🚀 1. UVLOOP (Ускоритель ядра)
if sys.platform != 'win32':
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except ImportError: pass

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from faker import Faker

# --- SELENIUM ---
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains

# --- TTS (Для звонков) ---
try:
    from gtts import gTTS
    TTS_AVAILABLE = True
except ImportError:
    TTS_AVAILABLE = False

# ==========================================
# ⚙️ КОНФИГУРАЦИЯ v31.0 PLATINUM
# ==========================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
except ValueError:
    sys.exit("❌ FATAL: ADMIN_ID должен быть числом!")

if not BOT_TOKEN:
    sys.exit("❌ FATAL: Нет токена! Установи переменную BOT_TOKEN.")

DB_NAME = 'imperator_v31.db'
SESSIONS_DIR = os.path.abspath("./sessions")
TMP_BASE = os.path.abspath("./tmp")
AUDIO_DIR = os.path.abspath("./audio")

# Создаем директории
for d in [SESSIONS_DIR, TMP_BASE, AUDIO_DIR]:
    os.makedirs(d, exist_ok=True)

# Ограничения ресурсов
BROWSER_SEMAPHORE = asyncio.Semaphore(1)

# Настройки скоростей (в секундах)
SPEED_CONFIGS = {
    "TURBO": {
        "normal": (180, 300),    # 3-5 мин
        "ghost": 900,            # 15 мин
        "caller": 1800           # 30 мин
    },
    "MEDIUM": {
        "normal": (300, 600),    # 5-10 мин
        "ghost": 1800,           # 30 мин
        "caller": 3600           # 60 мин
    },
    "SLOW": {
        "normal": (600, 1500),   # 10-25 мин
        "ghost": 3600,           # 60 мин
        "caller": 7200           # 120 мин
    }
}

CURRENT_SPEED = "MEDIUM"

# Глобальные переменные
ACTIVE_DRIVERS = {}
CLEANUP_LOCK = asyncio.Lock()
LAST_WORKER_PING = time.time()

# Настройка логирования
logger = logging.getLogger("Imperator")
logger.setLevel(logging.INFO)

# Консоль
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('%(asctime)s | 31.0 | %(levelname)s | %(message)s'))
logger.addHandler(console_handler)

# Файл с ротацией
file_handler = RotatingFileHandler('imperator.log', maxBytes=10*1024*1024, backupCount=3)
file_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(message)s'))
logger.addHandler(file_handler)

fake = Faker('ru_RU')

DEVICES = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36", "res": "1920,1080", "plat": "Win32"},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "res": "1366,768", "plat": "Linux x86_64"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36", "res": "1440,900", "plat": "MacIntel"}
]

class BotStates(StatesGroup):
    waiting_phone_auto = State()
    waiting_phone_manual = State()

# ==========================================
# 🧠 AI & UTILS
# ==========================================
class DialogueAI:
    def __init__(self):
        self.msgs = [
            "Привет", "Ку", "Как дела?", "На связи", 
            "Окей", "Принял", "Скоро буду", "Работаю", 
            "Перезвоню потом", "Скинь инфу", "Понял тебя",
            "Хорошо", "Да", "Норм", "Договорились"
        ]
        self.emojis = [" 😄", " 👍", " 🔥", " 👌", " ✅", " 💪"]
    
    def generate(self):
        msg = random.choice(self.msgs)
        if random.random() < 0.3: msg += random.choice(self.emojis)
        if random.random() < 0.1: msg = self._add_typo(msg)
        return msg
    
    def _add_typo(self, text):
        typo_map = {'а': 'о', 'о': 'а', 'е': 'и', 'и': 'е', 'т': 'р', 'р': 'т'}
        text_list = list(text.lower())
        for i, char in enumerate(text_list):
            if char in typo_map and random.random() < 0.5:
                text_list[i] = typo_map[char]
                break
        return ''.join(text_list).capitalize()

ai_engine = DialogueAI()

TTS_PHRASES = [
    "Алло, привет!", "Да, слушаю", "Сейчас занят, перезвоню",
    "Позвони позже пожалуйста", "Не могу говорить", "Перезвони через час"
]

def get_sys_status():
    mem = psutil.virtual_memory()
    return f"🖥 RAM: {mem.percent}% | CPU: {psutil.cpu_percent()}%"

# ==========================================
# 🗄️ БАЗА ДАННЫХ
# ==========================================
async def db_init():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS accounts 
                            (phone TEXT PRIMARY KEY, 
                             status TEXT DEFAULT 'active', 
                             mode TEXT DEFAULT 'normal',
                             ua TEXT, res TEXT, plat TEXT,
                             last_act REAL,
                             total_sent INTEGER DEFAULT 0,
                             total_calls INTEGER DEFAULT 0,
                             ban_date REAL)""")
        
        await db.execute("""CREATE TABLE IF NOT EXISTS whitelist 
                            (user_id INTEGER PRIMARY KEY, approved INTEGER DEFAULT 0, username TEXT, request_time REAL)""")
        
        await db.execute("""CREATE TABLE IF NOT EXISTS message_logs 
                            (id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT, target TEXT, text TEXT, timestamp REAL, success INTEGER DEFAULT 1)""")
        
        await db.execute("""CREATE TABLE IF NOT EXISTS call_logs 
                            (id INTEGER PRIMARY KEY AUTOINCREMENT, caller TEXT, target TEXT, duration INTEGER, timestamp REAL, success INTEGER DEFAULT 1)""")
        await db.commit()

async def db_get_all_phones():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT phone FROM accounts WHERE status='active'") as cursor:
            return [r[0] for r in await cursor.fetchall()]

async def db_get_ready_accounts():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT phone, mode, last_act FROM accounts WHERE status='active'") as cursor:
            return await cursor.fetchall()

async def db_save(phone, ua, res, plat):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""INSERT INTO accounts (phone, status, mode, ua, res, plat, last_act) 
                            VALUES (?, 'active', 'normal', ?, ?, ?, ?) 
                            ON CONFLICT(phone) DO UPDATE SET status='active'""", 
                         (phone, ua, res, plat, time.time()))
        await db.commit()

async def db_update_mode(phone, mode):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE accounts SET mode=? WHERE phone=?", (mode, phone))
        await db.commit()

async def db_update_last_act(phone):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE accounts SET last_act=? WHERE phone=?", (time.time(), phone))
        await db.commit()

async def db_log_message(sender, target, text, success=True):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO message_logs (sender, target, text, timestamp, success) VALUES (?, ?, ?, ?, ?)", (sender, target, text, time.time(), int(success)))
        await db.execute("UPDATE accounts SET total_sent = total_sent + 1 WHERE phone=?", (sender,))
        await db.commit()

async def db_log_call(caller, target, duration, success=True):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO call_logs (caller, target, duration, timestamp, success) VALUES (?, ?, ?, ?, ?)", (caller, target, duration, time.time(), int(success)))
        await db.execute("UPDATE accounts SET total_calls = total_calls + 1 WHERE phone=?", (caller,))
        await db.commit()

async def db_check_perm(user_id):
    if user_id == ADMIN_ID: return True
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT approved FROM whitelist WHERE user_id=?", (user_id,)) as cur:
            res = await cur.fetchone()
            return res[0] == 1 if res else False

async def db_add_request(user_id, username):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO whitelist (user_id, approved, username, request_time) VALUES (?, 0, ?, ?)", (user_id, username, time.time()))
        await db.commit()

async def db_approve(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE whitelist SET approved=1 WHERE user_id=?", (user_id,))
        await db.commit()

# ==========================================
# 🌐 SELENIUM
# ==========================================
def get_driver(phone):
    d_profile = random.choice(DEVICES)
    ua, res, plat = d_profile['ua'], d_profile['res'], d_profile['plat']
    
    options = Options()
    prof = os.path.join(SESSIONS_DIR, phone)
    unique_tmp = os.path.join(TMP_BASE, f"tmp_{phone}_{int(time.time())}")
    os.makedirs(unique_tmp, exist_ok=True)

    options.add_argument(f"--user-data-dir={prof}")
    options.add_argument(f"--data-path={unique_tmp}")
    
    # ANTI-CRASH
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-notifications")
    options.add_argument("--mute-audio")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(f"--user-agent={ua}")
    options.add_argument(f"--window-size={res}")

    try:
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(60)
        return driver, ua, res, plat, unique_tmp
    except Exception as e:
        logger.error(f"❌ Driver Init Error: {e}")
        return None, None, None, None, None

async def safe_click(driver, selector_type, selector_val, timeout=10):
    try:
        wait = WebDriverWait(driver, timeout)
        elem = wait.until(EC.element_to_be_clickable((selector_type, selector_val)))
        elem.click()
        return True
    except:
        try:
            elem = driver.find_element(selector_type, selector_val)
            driver.execute_script("arguments[0].click();", elem)
            return True
        except:
            return False

async def cleanup_driver(phone):
    async with CLEANUP_LOCK:
        if phone not in ACTIVE_DRIVERS: return
        data = ACTIVE_DRIVERS.pop(phone)
        try: await asyncio.to_thread(data['driver'].quit)
        except: pass
        if data['tmp'] and os.path.exists(data['tmp']):
            shutil.rmtree(data['tmp'], ignore_errors=True)

# ==========================================
# 📤 ОТПРАВКА & ЗВОНКИ
# ==========================================
async def perform_send(sender, target, text):
    driver = None
    try:
        async with BROWSER_SEMAPHORE:
            driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, sender)
            if not driver: return False
            
            ACTIVE_DRIVERS[sender] = {'driver': driver, 'tmp': tmp}
            
            try:
                driver.set_page_load_timeout(40)
                await asyncio.to_thread(driver.get, f"https://web.whatsapp.com/send?phone={target}")
            except: driver.execute_script("window.stop();")
            
            wait = WebDriverWait(driver, 50)
            
            # Поле ввода
            inp = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "footer div[contenteditable='true']")))
            
            for c in text: 
                inp.send_keys(c)
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.5)
            inp.send_keys(Keys.ENTER)
            
            await db_update_last_act(sender)
            logger.info(f"✅ Sent: {sender} -> {target}")
            return True
            
    except Exception as e:
        logger.error(f"Send Error {sender}: {e}")
        return False
    finally:
        await cleanup_driver(sender)

async def make_call(sender_phone, target_phone, duration=15):
    driver = None
    try:
        async with BROWSER_SEMAPHORE:
            driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, sender_phone)
            if not driver: return False
            
            ACTIVE_DRIVERS[sender_phone] = {'driver': driver, 'tmp': tmp}
            
            await asyncio.to_thread(driver.get, f"https://web.whatsapp.com/send?phone={target_phone}")
            wait = WebDriverWait(driver, 60)
            
            # Ждем загрузки
            try: wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "footer")))
            except: return False
            
            # Ищем кнопку звонка
            if await safe_click(driver, By.CSS_SELECTOR, "[aria-label*='Voice call']"):
                logger.info(f"📞 CALLING: {sender_phone} -> {target_phone}")
                await asyncio.sleep(duration)
                
                # Сброс
                await safe_click(driver, By.CSS_SELECTOR, "[aria-label*='End call']")
                
                await db_log_call(sender_phone, target_phone, duration)
                return True
            return False
            
    except Exception as e:
        logger.error(f"Call Error {sender_phone}: {e}")
        return False
    finally:
        await cleanup_driver(sender_phone)

async def run_caller_mode(phone):
    targs = await db_get_all_phones()
    if len(targs) < 2: return
    target = random.choice([t for t in targs if t != phone])
    await make_call(phone, target)

# ==========================================
# 🚜 WORKER LOOP
# ==========================================
async def worker_logic():
    global LAST_WORKER_PING
    while True:
        try:
            LAST_WORKER_PING = time.time()
            rows = await db_get_ready_accounts()
            
            if not rows:
                await asyncio.sleep(60)
                continue
            
            speed_cfg = SPEED_CONFIGS[CURRENT_SPEED]
            
            for row in rows:
                phone, mode, last_act = row
                now = time.time()
                last = last_act if last_act else 0
                
                # Определение задержки
                if mode == 'caller': req = speed_cfg['caller']
                elif mode == 'ghost': req = speed_cfg['ghost']
                else: req = random.randint(*speed_cfg['normal'])
                
                if now - last < req: continue
                
                # Выполнение
                if mode == 'normal':
                    targs = await db_get_all_phones()
                    if len(targs) > 1:
                        tgt = random.choice([t for t in targs if t != phone])
                        txt = ai_engine.generate()
                        if await perform_send(phone, tgt, txt):
                            await db_log_message(phone, tgt, txt)
                
                elif mode == 'solo':
                    txt = ai_engine.generate()
                    if await perform_send(phone, phone, txt):
                        await db_log_message(phone, phone, txt)
                        
                elif mode == 'ghost':
                    await db_update_last_act(phone)
                
                elif mode == 'caller':
                    await run_caller_mode(phone)
                
                await asyncio.sleep(random.randint(10, 20))
            
            await asyncio.sleep(30)
            
        except Exception as e:
            logger.error(f"Worker Loop Error: {e}")
            await asyncio.sleep(60)

# ==========================================
# 🤖 BOT UI & HANDLERS
# ==========================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

def kb_main(is_admin=False):
    btns = [
        [InlineKeyboardButton(text="📱 МОИ НОМЕРА", callback_data="my_numbers")],
        [InlineKeyboardButton(text="⚙️ КОНФИГ", callback_data="config_speed")],
        [InlineKeyboardButton(text="➕ ДОБАВИТЬ", callback_data="add_menu"),
         InlineKeyboardButton(text="📊 СТАТУС", callback_data="dashboard")]
    ]
    if is_admin: btns.append([InlineKeyboardButton(text="🔒 АДМИН", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_add_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 АВТО ВХОД", callback_data="add_auto")],
        [InlineKeyboardButton(text="🎮 РУЧНОЙ ВХОД", callback_data="add_manual")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu")]
    ])

def kb_manual_control(phone):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸", callback_data=f"m1_{phone}"),
         InlineKeyboardButton(text="🔗 ВХОД", callback_data=f"m2_{phone}")],
        [InlineKeyboardButton(text="⌨️ НОМЕР", callback_data=f"m3_{phone}"),
         InlineKeyboardButton(text="➡️ NEXT", callback_data=f"m4_{phone}")],
        [InlineKeyboardButton(text="✅ СОХР", callback_data=f"m5_{phone}"),
         InlineKeyboardButton(text="🗑 ОТМЕНА", callback_data=f"mc_{phone}")]
    ])

@dp.message(Command("start"))
async def start(msg: types.Message):
    await db_init()
    if await db_check_perm(msg.from_user.id):
        await msg.answer("🔱 **IMPERATOR v31**", reply_markup=kb_main(msg.from_user.id==ADMIN_ID))
    else:
        await db_add_request(msg.from_user.id, msg.from_user.username)
        if ADMIN_ID: await bot.send_message(ADMIN_ID, f"Заявка: {msg.from_user.id}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅", callback_data=f"approve_{msg.from_user.id}")]]))
        await msg.answer("🔒 Заявка отправлена")

@dp.callback_query(F.data.startswith("approve_"))
async def approve(cb: types.CallbackQuery):
    await db_approve(int(cb.data.split("_")[1]))
    await cb.answer("✅")

# --- MENUS ---
@dp.callback_query(F.data == "my_numbers")
async def show_numbers(cb: types.CallbackQuery):
    phones = await db_get_all_phones()
    if not phones: return await cb.message.edit_text("📭 Пусто.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙", callback_data="menu")]]))
    kb = [[InlineKeyboardButton(text=f"📱 +{p}", callback_data=f"manage_{p}")] for p in phones]
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="menu")])
    await cb.message.edit_text("📂 **Твои номера:**", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data == "menu")
async def main_menu(cb: types.CallbackQuery):
    await cb.message.edit_text("Меню", reply_markup=kb_main(cb.from_user.id==ADMIN_ID))

@dp.callback_query(F.data.startswith("manage_"))
async def manage_num(cb: types.CallbackQuery):
    phone = cb.data.split("_")[1]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 NORMAL", callback_data=f"setmode_normal_{phone}")],
        [InlineKeyboardButton(text="👤 SOLO", callback_data=f"setmode_solo_{phone}")],
        [InlineKeyboardButton(text="👻 GHOST", callback_data=f"setmode_ghost_{phone}")],
        [InlineKeyboardButton(text="📞 CALLER", callback_data=f"setmode_caller_{phone}")],
        [InlineKeyboardButton(text="🗑 УДАЛИТЬ", callback_data=f"delacc_{phone}"),
         InlineKeyboardButton(text="🔙", callback_data="my_numbers")]
    ])
    await cb.message.edit_text(f"⚙️ Настройка **+{phone}**", reply_markup=kb)

@dp.callback_query(F.data.startswith("setmode_"))
async def set_mode(cb: types.CallbackQuery):
    _, mode, phone = cb.data.split("_")
    await db_update_mode(phone, mode)
    await cb.answer(f"✅ Режим: {mode}")

@dp.callback_query(F.data.startswith("delacc_"))
async def del_acc(cb: types.CallbackQuery):
    phone = cb.data.split("_")[1]
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM accounts WHERE phone=?", (phone,))
        await db.commit()
    shutil.rmtree(os.path.join(SESSIONS_DIR, phone), ignore_errors=True)
    await cb.answer("🗑 Удалено")
    await show_numbers(cb)

@dp.callback_query(F.data == "config_speed")
async def conf_speed(cb: types.CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 TURBO", callback_data="setspeed_TURBO")],
        [InlineKeyboardButton(text="🚶 MEDIUM", callback_data="setspeed_MEDIUM")],
        [InlineKeyboardButton(text="🐢 SLOW", callback_data="setspeed_SLOW")],
        [InlineKeyboardButton(text="🔙", callback_data="menu")]
    ])
    await cb.message.edit_text(f"Скорость: **{CURRENT_SPEED}**", reply_markup=kb)

@dp.callback_query(F.data.startswith("setspeed_"))
async def set_spd(cb: types.CallbackQuery):
    global CURRENT_SPEED
    CURRENT_SPEED = cb.data.split("_")[1]
    await cb.answer(f"Скорость: {CURRENT_SPEED}")
    await conf_speed(cb)

@dp.callback_query(F.data == "dashboard")
async def dash(cb: types.CallbackQuery):
    act = await db_get_all_phones()
    await cb.message.edit_text(f"📊 **STATUS v31**\nActive: {len(act)}\n{get_sys_status()}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙", callback_data="menu")]]))

@dp.callback_query(F.data == "add_menu")
async def add_m(cb: types.CallbackQuery):
    await cb.message.edit_text("Метод добавления:", reply_markup=kb_add_menu())

# --- ADD ACCOUNTS (FSM) ---
@dp.callback_query(F.data == "add_auto")
async def add_auto_start(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("🤖 Введи номер (только цифры):")
    await state.set_state(BotStates.waiting_phone_auto)

@dp.message(BotStates.waiting_phone_auto)
async def add_auto_flow(msg: types.Message, state: FSMContext):
    phone = "".join(filter(str.isdigit, msg.text))
    await state.clear()
    s = await msg.answer(f"🚀 Запуск +{phone}...")
    
    async with BROWSER_SEMAPHORE:
        driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, phone)
        if not driver: return await s.edit_text("💥 Chrome Crash")
        
        ACTIVE_DRIVERS[phone] = {'driver': driver, 'ua': ua, 'res': res, 'plat': plat, 'tmp': tmp}
        
        try:
            await asyncio.to_thread(driver.get, "https://web.whatsapp.com/?lang=en")
            wait = WebDriverWait(driver, 40)
            
            await s.edit_text("⏳ Ищу кнопку входа...")
            if not await safe_click(driver, By.XPATH, "//span[contains(text(),'Link with phone')]"):
                await safe_click(driver, By.CSS_SELECTOR, "[data-testid='link-phone']")
            
            await s.edit_text("⏳ Ввожу номер...")
            inp = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='text']")))
            inp.clear()
            for d in f"+{phone}": inp.send_keys(d); await asyncio.sleep(0.05)
            inp.send_keys(Keys.ENTER)
            
            await s.edit_text("⏳ Жду код...")
            await asyncio.sleep(8)
            
            png = await asyncio.to_thread(driver.get_screenshot_as_png)
            await s.delete()
            await msg.answer_photo(BufferedInputFile(png, "code.png"), caption=f"✅ Код для +{phone}", reply_markup=kb_manual_control(phone))
            
        except Exception as e:
            await s.edit_text(f"Ошибка: {e}")
            await cleanup_driver(phone)

@dp.callback_query(F.data == "add_manual")
async def add_manual_start(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("🎮 Введи номер:")
    await state.set_state(BotStates.waiting_phone_manual)

@dp.message(BotStates.waiting_phone_manual)
async def add_manual_flow(msg: types.Message, state: FSMContext):
    phone = "".join(filter(str.isdigit, msg.text))
    await state.clear()
    s = await msg.answer(f"🚀 Ручной режим +{phone}...")
    
    async with BROWSER_SEMAPHORE:
        driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, phone)
        if not driver: return await s.edit_text("💥 Crash")
        
        ACTIVE_DRIVERS[phone] = {'driver': driver, 'ua': ua, 'res': res, 'plat': plat, 'tmp': tmp}
        await asyncio.to_thread(driver.get, "https://web.whatsapp.com/?lang=en")
        
        await s.edit_text(f"✅ Пульт готов: +{phone}", reply_markup=kb_manual_control(phone))

# --- MANUAL CONTROL ---
@dp.callback_query(lambda c: c.data and c.data.startswith("m"))
async def manual_control_handler(cb: types.CallbackQuery):
    parts = cb.data[1:].split("_")
    action, phone = parts[0], parts[1]
    
    if phone not in ACTIVE_DRIVERS: return await cb.answer("❌ Сессия закрыта", show_alert=True)
    d = ACTIVE_DRIVERS[phone]; drv = d['driver']
    
    try:
        if action == "1": # Screen
            png = await asyncio.to_thread(drv.get_screenshot_as_png)
            await cb.message.answer_photo(BufferedInputFile(png, "s.png"))
            await cb.answer()
        elif action == "2": # Login
            if not await safe_click(drv, By.XPATH, "//span[contains(text(),'Link with phone')]"):
                await safe_click(drv, By.CSS_SELECTOR, "[data-testid='link-phone']")
            await cb.answer("Click!")
        elif action == "3": # Number
            inp = drv.find_element(By.CSS_SELECTOR, "input[type='text']")
            inp.clear()
            for x in f"+{phone}": inp.send_keys(x); await asyncio.sleep(0.05)
            await cb.answer("Typed")
        elif action == "4": # Next
            await safe_click(drv, By.XPATH, "//*[text()='Next']")
            await asyncio.sleep(3)
            png = await asyncio.to_thread(drv.get_screenshot_as_png)
            await cb.message.answer_photo(BufferedInputFile(png, "c.png"))
        elif action == "5": # Save
            await db_save(phone, d['ua'], d['res'], d['plat'])
            await cleanup_driver(phone)
            await cb.message.edit_text(f"🎉 +{phone} Saved!")
        elif action == "c": # Cancel
            await cleanup_driver(phone)
            await cb.message.edit_text("Canceled")
            
    except Exception as e:
        await cb.answer(f"Err: {e}", show_alert=True)

async def main():
    await db_init()
    asyncio.create_task(worker_logic())
    logger.info("🚀 IMPERATOR v31 STARTED")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__": asyncio.run(main())
