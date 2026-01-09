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

# 🚀 1. UVLOOP (Ускоритель ядра для Linux)
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

# --- TTS ---
try:
    from gtts import gTTS
    TTS_AVAILABLE = True
except ImportError:
    TTS_AVAILABLE = False

# ==========================================
# ⚙️ КОНФИГУРАЦИЯ v33.0
# ==========================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
except ValueError:
    sys.exit("❌ FATAL: ADMIN_ID должен быть числом!")

if not BOT_TOKEN:
    sys.exit("❌ FATAL: Нет токена! Установи переменную BOT_TOKEN.")

DB_NAME = 'imperator_v33.db'
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

console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter('%(asctime)s | 33.0 | %(levelname)s | %(message)s'))
logger.addHandler(console_handler)

file_handler = RotatingFileHandler('imperator.log', maxBytes=10*1024*1024, backupCount=3)
file_handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(message)s'))
logger.addHandler(file_handler)

fake = Faker('ru_RU')

DEVICES = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36", "res": "1920,1080", "plat": "Win32"},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "res": "1366,768", "plat": "Linux x86_64"}
]

class BotStates(StatesGroup):
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
        # Простые эмодзи, чтобы не ломать старые драйверы
        self.emojis = [" :)", " ;)", " !", " +", " ok"]
    
    def generate(self):
        msg = random.choice(self.msgs)
        if random.random() < 0.3: msg += random.choice(self.emojis)
        return msg

ai_engine = DialogueAI()

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
    
    # ПРИНУДИТЕЛЬНЫЙ АНГЛИЙСКИЙ (чтобы селекторы работали)
    options.add_argument("--lang=en-US")
    
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
    """
    🔥 JS-KILLER: Пробивает защиту интерфейса
    """
    try:
        # 1. Сначала ждем элемент
        wait = WebDriverWait(driver, timeout)
        elem = wait.until(EC.presence_of_element_located((selector_type, selector_val)))
        
        # 2. Пробуем JavaScript (самый надежный)
        driver.execute_script("arguments[0].click();", elem)
        return True
    except:
        try:
            # 3. Если JS не сработал, пробуем ActionChains
            elem = driver.find_element(selector_type, selector_val)
            ActionChains(driver).move_to_element(elem).click().perform()
            return True
        except:
            # 4. Последний шанс - обычный клик
            try:
                elem.click()
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
# 📤 ОТПРАВКА (BMP FIX + JS INJECTION)
# ==========================================
async def perform_send(sender, target, text):
    driver = None
    try:
        async with BROWSER_SEMAPHORE:
            driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, sender)
            if not driver: return False
            
            ACTIVE_DRIVERS[sender] = {'driver': driver, 'tmp': tmp}
            
            # 1. Открываем чат
            try:
                driver.set_page_load_timeout(40)
                await asyncio.to_thread(driver.get, f"https://web.whatsapp.com/send?phone={target}")
            except: driver.execute_script("window.stop();")
            
            wait = WebDriverWait(driver, 50)
            
            # 2. Ждем поле ввода
            inp = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "footer div[contenteditable='true']")))
            await asyncio.sleep(5) # Даем время на прогрузку
            
            # 3. 🔥 ВСТАВКА ТЕКСТА ЧЕРЕЗ JS (ИСПРАВЛЕНИЕ BMP ERROR)
            try:
                # Вставляем текст напрямую в DOM
                driver.execute_script("arguments[0].innerText = arguments[1];", inp, text)
                # Триггерим событие ввода (пробел + бэкспейс)
                inp.send_keys(Keys.SPACE)
                await asyncio.sleep(0.5)
                inp.send_keys(Keys.BACK_SPACE)
            except Exception as e:
                logger.error(f"JS Insert Failed, trying legacy: {e}")
                for c in text: 
                    try: inp.send_keys(c)
                    except: pass
            
            await asyncio.sleep(1)
            
            # 4. 🔥 ЖЕСТКАЯ ОТПРАВКА
            inp.send_keys(Keys.ENTER)
            try:
                # Ищем кнопку самолетика и жмем JS-ом
                send_btn = driver.find_element(By.CSS_SELECTOR, "span[data-icon='send']")
                driver.execute_script("arguments[0].click();", send_btn)
            except: pass
            
            await asyncio.sleep(3) # Ждем галочку
            
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
            
            try: wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "footer")))
            except: return False
            
            # Ищем кнопку звонка по всем возможным атрибутам
            selectors = [
                 (By.CSS_SELECTOR, "[aria-label='Voice call']"),
                 (By.CSS_SELECTOR, "[title='Voice call']"),
                 (By.XPATH, "//*[@data-icon='voice-call']")
            ]
            
            clicked = False
            for s_type, s_val in selectors:
                if await safe_click(driver, s_type, s_val):
                    clicked = True
                    break
            
            if clicked:
                logger.info(f"📞 CALLING: {sender_phone} -> {target_phone}")
                await asyncio.sleep(duration)
                await safe_click(driver, By.CSS_SELECTOR, "[aria-label*='End']")
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
                
                if mode == 'caller': req = speed_cfg['caller']
                elif mode == 'ghost': req = speed_cfg['ghost']
                else: req = random.randint(*speed_cfg['normal'])
                
                if now - last < req: continue
                
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
        [InlineKeyboardButton(text="➕ ДОБАВИТЬ (РУЧНОЙ)", callback_data="add_manual"),
         InlineKeyboardButton(text="📊 СТАТУС", callback_data="dashboard")]
    ]
    if is_admin: btns.append([InlineKeyboardButton(text="🔒 АДМИН", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_manual_control(phone):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 ЧЕК", callback_data=f"m1_{phone}"),
         InlineKeyboardButton(text="🔗 ВХОД", callback_data=f"m2_{phone}")],
        [InlineKeyboardButton(text="⌨️ НОМЕР", callback_data=f"m3_{phone}"),
         InlineKeyboardButton(text="➡️ NEXT", callback_data=f"m4_{phone}")],
        [InlineKeyboardButton(text="✅ СОХРАНИТЬ", callback_data=f"m5_{phone}"),
         InlineKeyboardButton(text="🗑 ОТМЕНА", callback_data=f"mc_{phone}")]
    ])

@dp.message(Command("start"))
async def start(msg: types.Message):
    await db_init()
    if await db_check_perm(msg.from_user.id):
        await msg.answer("🔱 **IMPERATOR v33 (PLATINUM)**", reply_markup=kb_main(msg.from_user.id==ADMIN_ID))
    else:
        await db_add_request(msg.from_user.id, msg.from_user.username)
        if ADMIN_ID: await bot.send_message(ADMIN_ID, f"Заявка: {msg.from_user.id}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅", callback_data=f"approve_{msg.from_user.id}")]]))
        await msg.answer("🔒 Заявка отправлена")

@dp.callback_query(F.data.startswith("approve_"))
async def approve(cb: types.CallbackQuery):
    await db_approve(int(cb.data.split("_")[1]))
    await cb.answer("✅")

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
    
    # Получаем текущий режим для галочек
    mode = "normal"
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT mode FROM accounts WHERE phone=?", (phone,)) as cur:
            res = await cur.fetchone()
            if res: mode = res[0]
            
    # Динамическая клавиатура с галочками
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{'✅ ' if mode=='normal' else ''}🔥 NORMAL", callback_data=f"setmode_normal_{phone}")],
        [InlineKeyboardButton(text=f"{'✅ ' if mode=='solo' else ''}👤 SOLO", callback_data=f"setmode_solo_{phone}")],
        [InlineKeyboardButton(text=f"{'✅ ' if mode=='ghost' else ''}👻 GHOST", callback_data=f"setmode_ghost_{phone}")],
        [InlineKeyboardButton(text=f"{'✅ ' if mode=='caller' else ''}📞 CALLER", callback_data=f"setmode_caller_{phone}")],
        [InlineKeyboardButton(text="🗑 УДАЛИТЬ", callback_data=f"delacc_{phone}"),
         InlineKeyboardButton(text="🔙", callback_data="my_numbers")]
    ])
    await cb.message.edit_text(f"⚙️ Настройка **+{phone}**\nТекущий режим: {mode.upper()}", reply_markup=kb)

@dp.callback_query(F.data.startswith("setmode_"))
async def set_mode(cb: types.CallbackQuery):
    _, mode, phone = cb.data.split("_")
    await db_update_mode(phone, mode)
    await cb.answer(f"✅ Режим {mode} установлен!")
    await manage_num(cb) # Обновляем меню чтобы появилась галочка

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
    # Галочка для скорости
    kb_btns = []
    for spd in ["TURBO", "MEDIUM", "SLOW"]:
        prefix = "✅ " if CURRENT_SPEED == spd else ""
        kb_btns.append([InlineKeyboardButton(text=f"{prefix}{spd}", callback_data=f"setspeed_{spd}")])
    kb_btns.append([InlineKeyboardButton(text="🔙", callback_data="menu")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=kb_btns)
    try: await cb.message.edit_text(f"Скорость: **{CURRENT_SPEED}**", reply_markup=kb)
    except: await cb.answer("Меню открыто")

@dp.callback_query(F.data.startswith("setspeed_"))
async def set_spd(cb: types.CallbackQuery):
    global CURRENT_SPEED
    new_speed = cb.data.split("_")[1]
    if CURRENT_SPEED == new_speed: return await cb.answer("Уже выбрано")
    CURRENT_SPEED = new_speed
    await cb.answer(f"Скорость: {CURRENT_SPEED}")
    try: await conf_speed(cb)
    except: pass

@dp.callback_query(F.data == "dashboard")
async def dash(cb: types.CallbackQuery):
    act = await db_get_all_phones()
    await cb.message.edit_text(f"📊 **STATUS v33**\nActive: {len(act)}\n{get_sys_status()}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙", callback_data="menu")]]))

# --- MANUAL ADD ONLY ---
@dp.callback_query(F.data == "add_manual")
async def add_manual_start(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("🎮 **РУЧНОЙ РЕЖИМ**\nВведи номер (только цифры):")
    await state.set_state(BotStates.waiting_phone_manual)

@dp.message(BotStates.waiting_phone_manual)
async def add_manual_flow(msg: types.Message, state: FSMContext):
    phone = "".join(filter(str.isdigit, msg.text))
    await state.clear()
    s = await msg.answer(f"🚀 Запуск +{phone}...")
    
    async with BROWSER_SEMAPHORE:
        driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, phone)
        if not driver: return await s.edit_text("💥 Chrome Crash")
        
        ACTIVE_DRIVERS[phone] = {'driver': driver, 'ua': ua, 'res': res, 'plat': plat, 'tmp': tmp}
        await asyncio.to_thread(driver.get, "https://web.whatsapp.com/?lang=en")
        
        await s.edit_text(f"✅ Пульт готов: +{phone}", reply_markup=kb_manual_control(phone))

# --- FIXED MANUAL CONTROLS ---
@dp.callback_query(lambda c: c.data and c.data.startswith("m"))
async def manual_control_handler(cb: types.CallbackQuery):
    parts = cb.data[1:].split("_")
    action, phone = parts[0], parts[1]
    
    if phone not in ACTIVE_DRIVERS: 
        try: await cb.answer("❌ Сессия закрыта", show_alert=True)
        except: pass
        return
        
    d = ACTIVE_DRIVERS[phone]; drv = d['driver']
    
    async def safe_reply(text, alert=False):
        try: await cb.answer(text, show_alert=alert)
        except: await cb.message.answer(text)

    try:
        if action == "1": # Screen
            png = await asyncio.to_thread(drv.get_screenshot_as_png)
            await cb.message.answer_photo(BufferedInputFile(png, "s.png"))
            await safe_reply("📸 Готово")
            
        elif action == "2": # Login Link (FIXED SELECTORS)
            found = False
            # Ищем кнопку "Link with phone"
            if await safe_click(drv, By.XPATH, "//span[contains(text(), 'Link with phone')]"): found = True
            elif await safe_click(drv, By.XPATH, "//div[contains(text(), 'Link with phone')]"): found = True
            elif await safe_click(drv, By.CSS_SELECTOR, "[data-testid='link-phone']"): found = True
            
            if found: await safe_reply("✅ Нажал Link!")
            else: await safe_reply("❌ Кнопка 'Link' не найдена (проверь ЧЕК)", alert=True)
            
        elif action == "3": # Number Input
            try:
                inp = drv.find_element(By.CSS_SELECTOR, "input[type='text']")
                inp.clear()
                for x in f"+{phone}": inp.send_keys(x); await asyncio.sleep(0.05)
                await safe_reply("✅ Номер введен")
            except:
                await safe_reply("❌ Поле ввода не найдено", alert=True)
                
        elif action == "4": # Next Button
            if await safe_click(drv, By.XPATH, "//div[text()='Next']"):
                await safe_reply("✅ Нажал Next")
            else:
                await safe_reply("❌ Кнопка Next не найдена", alert=True)
                
        elif action == "5": # Save
            await db_save(phone, d['ua'], d['res'], d['plat'])
            await cleanup_driver(phone)
            await cb.message.edit_text(f"🎉 +{phone} Сохранен!")
            
        elif action == "c": # Cancel
            await cleanup_driver(phone)
            await cb.message.edit_text("Отменено")
            
    except Exception as e:
        await safe_reply(f"Err: {str(e)[:50]}", alert=True)

async def main():
    await db_init()
    asyncio.create_task(worker_logic())
    logger.info("🚀 IMPERATOR v33 STARTED")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__": asyncio.run(main())
