import sys
import asyncio
import os
import logging
import random
import psutil
import shutil
import aiosqlite
import time
import json
import zipfile
from io import BytesIO
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

# 🚀 1. UVLOOP (Ускорение для Linux)
if sys.platform != 'win32':
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except ImportError: pass

# --- AIOGRAM & UTILS ---
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from faker import Faker

# --- MATPLOTLIB (ГРАФИКИ) ---
import matplotlib
matplotlib.use('Agg') # Режим без GUI для серверов
import matplotlib.pyplot as plt

# --- SELENIUM ---
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains

# ==========================================
# ⚙️ КОНФИГУРАЦИЯ v36.1 PLATINUM (FIXED)
# ==========================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID_STR = os.environ.get("ADMIN_ID", "0")
CHANNEL_ID = "@WhatsAppstatpro"

try:
    ADMIN_ID = int(ADMIN_ID_STR)
except ValueError:
    sys.exit("❌ FATAL: ADMIN_ID должен быть числом!")

if not BOT_TOKEN or len(BOT_TOKEN) < 20:
    sys.exit("❌ FATAL: Нет токена! Установи переменную BOT_TOKEN.")

DB_NAME = 'imperator_v36.db'
SESSIONS_DIR = os.path.abspath("./sessions")
TMP_BASE = os.path.abspath("./tmp")
BACKUP_DIR = os.path.abspath("./backups")

# Создаем директории
for d in [SESSIONS_DIR, TMP_BASE, BACKUP_DIR]:
    os.makedirs(d, exist_ok=True)

# 🚦 SEMAPHORE: Ограничиваем количество одновременных браузеров
BROWSER_SEMAPHORE = asyncio.Semaphore(1)

# Настройки скоростей (паузы в секундах)
SPEED_CONFIGS = {
    "TURBO": { "normal": (180, 300), "ghost": 900, "caller": 1800 },
    "MEDIUM": { "normal": (300, 600), "ghost": 1800, "caller": 3600 },
    "SLOW": { "normal": (600, 1500), "ghost": 3600, "caller": 7200 },
    "BLITZ": { "normal": (60, 180), "ghost": 600, "caller": 1200 },
    "BUSINESS": { "normal": (900, 1800), "ghost": 5400, "caller": 7200 }, # Только 9:00-18:00
}

CURRENT_SPEED = "MEDIUM"
STEALTH_MODE = False

# Глобальные переменные
ACTIVE_DRIVERS = {}
CLEANUP_LOCK = asyncio.Lock()
START_TIME = time.time()

# Логирование
logger = logging.getLogger("Imperator")
logger.setLevel(logging.INFO)
log_formatter = logging.Formatter('%(asctime)s | v36.1 | %(levelname)s | %(message)s')

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
logger.addHandler(console_handler)

file_handler = RotatingFileHandler('imperator.log', maxBytes=10*1024*1024, backupCount=5)
file_handler.setFormatter(log_formatter)
logger.addHandler(file_handler)

fake = Faker('ru_RU')

# Профили устройств
DEVICES = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36", "res": "1280,720", "plat": "Win32"},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "res": "1366,768", "plat": "Linux x86_64"}
]

# Состояния FSM
class BotStates(StatesGroup):
    waiting_phone_manual = State()
    
class BroadcastStates(StatesGroup):
    waiting_text = State()
    confirm_send = State()

# ==========================================
# 🧠 AI & UTILS
# ==========================================

class SmartAI:
    def generate(self):
        hour = datetime.now().hour
        pool = []
        if 6 <= hour < 12:
            pool = ["Доброе утро!", "Привет! Как спалось?", "Начинаем работу?", "Утро доброе"]
        elif 18 <= hour < 23:
            pool = ["Привет! Как день прошел?", "Устал уже?", "Вечер добрый", "Скоро домой"]
        else:
            pool = ["Как дела?", "На связи", "Тут?", "Работаем", "Есть вопрос", "Привет", "Как сам?"]
            
        msg = random.choice(pool)
        if random.random() < 0.3:
            msg += random.choice([" :)", " 👍", " ✅", " 👋", " 😉"])
        return msg

ai_engine = SmartAI()

def get_sys_status():
    mem = psutil.virtual_memory()
    uptime_sec = time.time() - START_TIME
    uptime = str(timedelta(seconds=int(uptime_sec)))
    return (f"🖥 RAM: {mem.percent}% ({mem.available // (1024*1024)}MB free)\n"
            f"💻 CPU: {psutil.cpu_percent()}%\n"
            f"⏱ Uptime: {uptime}")

def check_memory_critical():
    """Блокирует запуск, если RAM < 250MB"""
    mem = psutil.virtual_memory()
    available_mb = mem.available / (1024 * 1024)
    if available_mb < 250:
        logger.warning(f"🚨 CRITICAL MEMORY: {int(available_mb)}MB. Pausing tasks.")
        return True
    return False

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
                             last_act REAL DEFAULT 0,
                             total_sent INTEGER DEFAULT 0,
                             total_calls INTEGER DEFAULT 0,
                             ban_date REAL,
                             created_at REAL DEFAULT 0)""")
        
        await db.execute("""CREATE TABLE IF NOT EXISTS whitelist 
                            (user_id INTEGER PRIMARY KEY, 
                             approved INTEGER DEFAULT 0, 
                             username TEXT, 
                             request_time REAL)""")
        
        await db.execute("""CREATE TABLE IF NOT EXISTS message_logs 
                            (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                             sender TEXT, target TEXT, text TEXT, 
                             timestamp REAL, success INTEGER DEFAULT 1)""")
        
        await db.execute("""CREATE TABLE IF NOT EXISTS call_logs 
                            (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                             caller TEXT, target TEXT, duration INTEGER, 
                             timestamp REAL, success INTEGER DEFAULT 1)""")
        await db.commit()

async def db_get_all_phones():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT phone FROM accounts WHERE status='active'") as cur:
            return [r[0] for r in await cur.fetchall()]

async def db_save_account(phone, ua, res, plat):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""INSERT INTO accounts (phone, status, mode, ua, res, plat, last_act, created_at) 
                            VALUES (?, 'active', 'normal', ?, ?, ?, 0, ?) 
                            ON CONFLICT(phone) DO UPDATE SET status='active', last_act=0""", 
                         (phone, ua, res, plat, time.time()))
        await db.commit()

async def db_get_stats_24h():
    cutoff = time.time() - 86400
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM message_logs WHERE timestamp > ? AND success=1", (cutoff,)) as cur:
            msgs = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM call_logs WHERE timestamp > ? AND success=1", (cutoff,)) as cur:
            calls = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM accounts WHERE status='active'") as cur:
            active = (await cur.fetchone())[0]
    return {"msgs": msgs, "calls": calls, "active": active}

# ==========================================
# 📊 ГРАФИКИ
# ==========================================
async def generate_graph():
    cutoff = time.time() - (24 * 3600)
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT timestamp FROM message_logs WHERE timestamp > ?", (cutoff,)) as cur:
            rows = await cur.fetchall()
            
    hours = [0] * 24
    for r in rows:
        h = datetime.fromtimestamp(r[0]).hour
        hours[h] += 1
        
    plt.figure(figsize=(8, 4))
    plt.bar(range(24), hours, color='#4CAF50', alpha=0.7)
    plt.xlabel('Час (0-23)')
    plt.ylabel('Сообщений')
    plt.title('Активность за 24 часа')
    plt.grid(axis='y', linestyle='--', alpha=0.3)
    
    buf = BytesIO()
    plt.savefig(buf, format='png', dpi=80)
    buf.seek(0)
    plt.close()
    return buf

# ==========================================
# 🌐 SELENIUM ENGINE
# ==========================================
def get_driver(phone):
    if check_memory_critical():
        return None, None, None, None, None

    d_profile = random.choice(DEVICES)
    ua, res, plat = d_profile['ua'], d_profile['res'], d_profile['plat']
    
    options = Options()
    prof = os.path.join(SESSIONS_DIR, phone)
    unique_tmp = os.path.join(TMP_BASE, f"tmp_{phone}_{int(time.time())}")
    os.makedirs(unique_tmp, exist_ok=True)

    options.add_argument(f"--user-data-dir={prof}")
    options.add_argument(f"--data-path={unique_tmp}")
    
    # Оптимизация памяти (Отключение картинок и CSS)
    options.add_argument("--blink-settings=imagesEnabled=false")
    options.add_experimental_option("prefs", {
        "profile.managed_default_content_settings.images": 2,
        "profile.managed_default_content_settings.stylesheets": 2,
    })
    
    options.add_argument("--lang=en-US")
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--mute-audio")
    options.add_argument(f"--user-agent={ua}")
    options.add_argument(f"--window-size={res}")

    try:
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(45)
        
        # JS Injections: Stealth
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.navigator.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
            """
        })
        
        return driver, ua, res, plat, unique_tmp
    except Exception as e:
        logger.error(f"❌ Driver Init Error: {e}")
        return None, None, None, None, None

async def safe_click(driver, by, value, timeout=5):
    try:
        wait = WebDriverWait(driver, timeout)
        elem = wait.until(EC.element_to_be_clickable((by, value)))
        driver.execute_script("arguments[0].click();", elem)
        return True
    except:
        return False

async def human_type(element, text):
    """Имитация ввода человеком"""
    for char in text:
        element.send_keys(char)
        await asyncio.sleep(random.uniform(0.05, 0.2))

async def cleanup_driver(phone):
    async with CLEANUP_LOCK:
        if phone not in ACTIVE_DRIVERS: return
        data = ACTIVE_DRIVERS.pop(phone)
        try: await asyncio.to_thread(data['driver'].quit)
        except: pass
        if data['tmp'] and os.path.exists(data['tmp']):
            shutil.rmtree(data['tmp'], ignore_errors=True)

# ==========================================
# 🚜 FARM WORKER (SEQUENTIAL)
# ==========================================
async def process_one_account(phone, mode, speed_cfg):
    """Обработка одного аккаунта за раз"""
    driver = None
    try:
        if CURRENT_SPEED == "BUSINESS" and not (9 <= datetime.now().hour <= 18):
            return

        async with BROWSER_SEMAPHORE:
            driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, phone)
            if not driver: return
            
            ACTIVE_DRIVERS[phone] = {'driver': driver, 'tmp': tmp}
            
            if STEALTH_MODE:
                await asyncio.sleep(random.uniform(2, 5))

            # --- MESSAGE MODE ---
            if mode in ['normal', 'solo']:
                targets = await db_get_all_phones()
                if mode == 'solo': target = phone
                else: 
                    target = random.choice([t for t in targets if t != phone]) if len(targets) > 1 else phone

                await asyncio.to_thread(driver.get, f"https://web.whatsapp.com/send?phone={target}")
                
                wait = WebDriverWait(driver, 40)
                inp = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "footer div[contenteditable='true']")))
                
                text = ai_engine.generate()
                await human_type(inp, text)
                await asyncio.sleep(1)
                inp.send_keys(Keys.ENTER)
                await asyncio.sleep(3)
                
                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute("INSERT INTO message_logs (sender, target, text, timestamp) VALUES (?,?,?,?)",
                                     (phone, target, text, time.time()))
                    await db.execute("UPDATE accounts SET total_sent = total_sent + 1, last_act=? WHERE phone=?",
                                     (time.time(), phone))
                    await db.commit()
                logger.info(f"✅ {phone} -> {target}")

            # --- CALLER MODE ---
            elif mode == 'caller':
                targets = await db_get_all_phones()
                if len(targets) > 1:
                    target = random.choice([t for t in targets if t != phone])
                    await asyncio.to_thread(driver.get, f"https://web.whatsapp.com/send?phone={target}")
                    
                    wait = WebDriverWait(driver, 30)
                    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "footer")))
                    
                    # Клик на кнопку звонка
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
                        logger.info(f"📞 CALLING: {phone} -> {target}")
                        await asyncio.sleep(15) # Длительность звонка
                        await safe_click(driver, By.CSS_SELECTOR, "[aria-label*='End']")
                        
                        async with aiosqlite.connect(DB_NAME) as db:
                            await db.execute("INSERT INTO call_logs (caller, target, duration, timestamp) VALUES (?,?,?,?)",
                                             (phone, target, 15, time.time()))
                            await db.execute("UPDATE accounts SET total_calls = total_calls + 1, last_act=? WHERE phone=?",
                                             (time.time(), phone))
                            await db.commit()
                    
    except Exception as e:
        logger.error(f"Worker Error {phone}: {e}")
    finally:
        await cleanup_driver(phone)

async def farm_loop():
    logger.info("🚜 FARM LOOP STARTED")
    while True:
        try:
            if check_memory_critical():
                await asyncio.sleep(60)
                continue
            
            phones = await db_get_all_phones()
            random.shuffle(phones)
            
            for phone in phones:
                async with aiosqlite.connect(DB_NAME) as db:
                    async with db.execute("SELECT mode, last_act FROM accounts WHERE phone=?", (phone,)) as cur:
                        res = await cur.fetchone()
                        if not res: continue
                        mode, last_act = res
                
                speed_cfg = SPEED_CONFIGS[CURRENT_SPEED]
                delay = speed_cfg['normal'][0] if mode == 'normal' else speed_cfg['ghost']
                
                # ИСПРАВЛЕНИЕ: Если last_act > 0, проверяем задержку. Если 0 - запускаем сразу.
                if last_act > 0 and (time.time() - last_act < delay):
                    continue
                
                # Запуск обработки
                await process_one_account(phone, mode, speed_cfg)
                await asyncio.sleep(5) # Пауза для очистки RAM
                
            await asyncio.sleep(10)
        except Exception as e:
            logger.error(f"Loop Err: {e}")
            await asyncio.sleep(30)

async def auto_backup():
    while True:
        await asyncio.sleep(86400) # 24ч
        try:
            name = f"backup_{datetime.now().strftime('%Y%m%d')}.zip"
            path = os.path.join(BACKUP_DIR, name)
            with zipfile.ZipFile(path, 'w') as zf:
                zf.write(DB_NAME)
            
            if ADMIN_ID:
                await bot.send_document(ADMIN_ID, FSInputFile(path), caption="📦 Auto-Backup")
            os.remove(path)
        except Exception as e:
            logger.error(f"Backup Err: {e}")

# ==========================================
# 🤖 BOT UI & HANDLERS
# ==========================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- KEYBOARDS ---
def kb_main(is_admin=False):
    btns = [
        [InlineKeyboardButton(text="📱 МОИ НОМЕРА", callback_data="my_numbers"),
         InlineKeyboardButton(text="⚙️ КОНФИГ", callback_data="config_speed")],
        [InlineKeyboardButton(text="➕ ДОБАВИТЬ", callback_data="add_manual"),
         InlineKeyboardButton(text="📊 СТАТУС", callback_data="dashboard")],
        [InlineKeyboardButton(text="📤 РАССЫЛКА", callback_data="broadcast"),
         InlineKeyboardButton(text="💾 БЭКАП", callback_data="backup_menu")]
    ]
    if is_admin: btns.append([InlineKeyboardButton(text="🔒 АДМИН", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_manual(phone):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 ЧЕК", callback_data=f"m1_{phone}"),
         InlineKeyboardButton(text="🔗 ВХОД", callback_data=f"m2_{phone}")],
        [InlineKeyboardButton(text="⌨️ НОМЕР", callback_data=f"m3_{phone}"),
         InlineKeyboardButton(text="➡️ NEXT", callback_data=f"m4_{phone}")],
        [InlineKeyboardButton(text="✅ СОХРАНИТЬ", callback_data=f"m5_{phone}"),
         InlineKeyboardButton(text="🗑 ОТМЕНА", callback_data=f"mc_{phone}")]
    ])

# --- AUTH ---
async def check_perm(user_id):
    if user_id == ADMIN_ID: return True
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT approved FROM whitelist WHERE user_id=?", (user_id,)) as cur:
            res = await cur.fetchone()
            return res and res[0] == 1

async def check_sub(user_id):
    try:
        m = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return m.status in ['member', 'administrator', 'creator']
    except: return False

@dp.message(Command("start"))
async def start(msg: types.Message):
    await db_init()
    
    if not await check_sub(msg.from_user.id):
        return await msg.answer(f"🔒 Подпишись: {CHANNEL_ID}", 
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ ГОТОВО", callback_data="check_sub")]]))
    
    if await check_perm(msg.from_user.id):
        await msg.answer("🔱 **IMPERATOR v36.1 PLATINUM**", reply_markup=kb_main(msg.from_user.id == ADMIN_ID))
    else:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("INSERT OR IGNORE INTO whitelist (user_id, username, request_time) VALUES (?,?,?)",
                             (msg.from_user.id, msg.from_user.username, time.time()))
            await db.commit()
        await msg.answer("⏳ Заявка отправлена.")
        if ADMIN_ID:
            await bot.send_message(ADMIN_ID, f"👤 Заявка: {msg.from_user.id}", 
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅", callback_data=f"app_{msg.from_user.id}"),
                     InlineKeyboardButton(text="❌", callback_data=f"rej_{msg.from_user.id}")]
                ]))

@dp.callback_query(F.data == "check_sub")
async def sub_check(cb: types.CallbackQuery):
    if await check_sub(cb.from_user.id):
        await cb.message.delete()
        await start(cb.message)
    else: await cb.answer("❌ Нет подписки", show_alert=True)

@dp.callback_query(F.data.startswith("app_"))
async def approve_user(cb: types.CallbackQuery):
    uid = int(cb.data.split("_")[1])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE whitelist SET approved=1 WHERE user_id=?", (uid,))
        await db.commit()
    await bot.send_message(uid, "✅ Доступ получен!")
    await cb.message.edit_text(f"✅ Одобрен: {uid}")

@dp.callback_query(F.data.startswith("rej_"))
async def reject_user(cb: types.CallbackQuery):
    uid = int(cb.data.split("_")[1])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM whitelist WHERE user_id=?", (uid,))
        await db.commit()
    await cb.message.edit_text(f"❌ Отклонен: {uid}")

# --- ADMIN PANEL ---
@dp.callback_query(F.data == "admin_panel")
async def admin_panel(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM whitelist WHERE approved=0") as cur: reqs = (await cur.fetchone())[0]
        async with db.execute("SELECT COUNT(*) FROM whitelist WHERE approved=1") as cur: usrs = (await cur.fetchone())[0]
    await cb.message.edit_text(f"🔒 **ADMIN**\nReqs: {reqs} | Users: {usrs}", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙", callback_data="menu")]]))

# --- MANUAL ADD ---
@dp.callback_query(F.data == "add_manual")
async def add_manual(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.edit_text("📱 Введи номер (без +):")
    await state.set_state(BotStates.waiting_phone_manual)

@dp.message(BotStates.waiting_phone_manual)
async def process_phone(msg: types.Message, state: FSMContext):
    phone = "".join(filter(str.isdigit, msg.text))
    if not (7 <= len(phone) <= 15): return await msg.answer("❌ 7-15 цифр!")
    await state.clear()
    
    s = await msg.answer("🚀 Запуск...")
    async with BROWSER_SEMAPHORE:
        driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, phone)
        if not driver: return await s.edit_text("💥 Crash / Memory Limit")
        
        ACTIVE_DRIVERS[phone] = {'driver': driver, 'ua': ua, 'res': res, 'plat': plat, 'tmp': tmp}
        await asyncio.to_thread(driver.get, "https://web.whatsapp.com")
        await s.edit_text(f"✅ Пульт +{phone}", reply_markup=kb_manual(phone))

@dp.callback_query(lambda c: c.data and c.data.startswith("m") and "_" in c.data)
async def manual_handler(cb: types.CallbackQuery):
    action, phone = cb.data[1:].split("_")
    if phone not in ACTIVE_DRIVERS: return await cb.answer("❌ Сессия закрыта", show_alert=True)
    drv = ACTIVE_DRIVERS[phone]['driver']
    
    try:
        if action == "1": # Screen
            png = await asyncio.to_thread(drv.get_screenshot_as_png)
            await cb.message.answer_photo(BufferedInputFile(png, "s.png"))
            
        elif action == "2": # Link (4 Strategies)
            found = False
            for txt in ["Link with phone", "Link with phone number", "Привязать", "Войти по номеру", "S'associer"]:
                xp = f"//div[contains(text(), '{txt}')] | //span[contains(text(), '{txt}')]"
                if await safe_click(drv, By.XPATH, xp): found = True; break
            
            if not found: found = await safe_click(drv, By.CSS_SELECTOR, "[aria-label*='phone']")
            if not found:
                btns = drv.find_elements(By.TAG_NAME, "button")
                if len(btns)>=2: drv.execute_script("arguments[0].click()", btns[-1]); found=True
            if not found:
                drv.execute_script("const t=[...document.querySelectorAll('div,span,button')].find(e=>e.innerText&&(e.innerText.includes('phone')||e.innerText.includes('номер')));if(t)t.click()")
                found=True
            
            if found: await cb.answer("✅ Clicked")
            else:
                png = await asyncio.to_thread(drv.get_screenshot_as_png) 
                await cb.message.answer_photo(BufferedInputFile(png, "err.png"), caption="❌ Not Found")

        elif action == "3": # Input
            t = None
            for i in drv.find_elements(By.CSS_SELECTOR, "input"):
                if i.is_displayed(): t = i; break
            
            if t:
                t.clear()
                # HUMAN TYPING
                for c in f"+{phone}":
                    t.send_keys(c)
                    await asyncio.sleep(random.uniform(0.05, 0.15))
                await cb.answer("✅ Введено")
            else: await cb.answer("❌ Input not found", show_alert=True)

        elif action == "4": # Next
            found = False
            for txt in ["Next", "Далее", "Siguiente", "下一步"]:
                if await safe_click(drv, By.XPATH, f"//div[text()='{txt}'] | //button[text()='{txt}']"):
                    found = True; break
            
            if not found:
                btns = drv.find_elements(By.TAG_NAME, "button")
                for btn in btns:
                    if btn.is_displayed() and btn.is_enabled():
                        drv.execute_script("arguments[0].click()", btn)
                        found = True; break
            
            if found: await cb.answer("➡️ Next")
            else: await cb.answer("❌ Next not found", show_alert=True)

        elif action == "5": # Save
            d = ACTIVE_DRIVERS[phone]
            await db_save_account(phone, d['ua'], d['res'], d['plat'])
            await cleanup_driver(phone)
            await cb.message.edit_text("✅ Сохранено! В работе.")

        elif action == "c":
            await cleanup_driver(phone)
            await cb.message.edit_text("Отмена")
            
    except Exception as e: await cb.answer(f"Err: {e}", show_alert=True)

# --- DASHBOARD ---
@dp.callback_query(F.data == "dashboard")
async def dashboard(cb: types.CallbackQuery):
    stats = await db_get_stats_24h()
    graph = await generate_graph()
    txt = (f"📊 **STATUS**\n"
           f"SMS: {stats['msgs']} | Calls: {stats['calls']}\n"
           f"Active: {stats['active']}\n"
           f"{get_sys_status()}")
    await cb.message.answer_photo(BufferedInputFile(graph.read(), "g.png"), caption=txt,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙", callback_data="menu")]]))

# --- BROADCAST (REAL) ---
@dp.callback_query(F.data == "broadcast")
async def bc_start(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.edit_text("📝 Введите текст:")
    await state.set_state(BroadcastStates.waiting_text)

@dp.message(BroadcastStates.waiting_text)
async def bc_text(msg: types.Message, state: FSMContext):
    text = msg.text
    phones = await db_get_all_phones()
    
    if not phones:
        return await msg.answer("❌ Нет активных номеров")
    
    await msg.answer(f"🚀 Запуск рассылки на {len(phones)} номеров...")
    success = 0
    
    for sender in phones:
        # Шлем самому себе для теста (или рандом)
        targets = [p for p in phones if p != sender]
        if not targets: target = sender
        else: target = random.choice(targets)
        
        driver = None
        try:
            async with BROWSER_SEMAPHORE:
                driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, sender)
                if not driver: continue
                ACTIVE_DRIVERS[sender] = {'driver': driver, 'tmp': tmp}
                
                await asyncio.to_thread(driver.get, f"https://web.whatsapp.com/send?phone={target}")
                
                wait = WebDriverWait(driver, 40)
                inp = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "footer div[contenteditable='true']")))
                
                await human_type(inp, text)
                await asyncio.sleep(1)
                inp.send_keys(Keys.ENTER)
                await asyncio.sleep(2)
                
                success += 1
                logger.info(f"📤 BC: {sender} -> {target}")
                
        except Exception as e: logger.error(f"BC Err {sender}: {e}")
        finally: await cleanup_driver(sender)
        
    await msg.answer(f"✅ Рассылка завершена: {success}/{len(phones)}")
    await state.clear()

# --- BACKUP ---
@dp.callback_query(F.data == "backup_menu")
async def backup_menu(cb: types.CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM accounts") as cur: rows = await cur.fetchall()
    data = [{"phone": r[0]} for r in rows]
    b = json.dumps(data, indent=2).encode()
    await cb.message.answer_document(BufferedInputFile(b, "accounts.json"), caption="📥 Backup")

# --- MANAGE NUMBERS ---
@dp.callback_query(F.data == "menu")
async def menu(cb: types.CallbackQuery):
    await cb.message.edit_text("Меню", reply_markup=kb_main(cb.from_user.id == ADMIN_ID))

@dp.callback_query(F.data == "my_numbers")
async def my_numbers(cb: types.CallbackQuery):
    phones = await db_get_all_phones()
    kb = [[InlineKeyboardButton(text=f"+{p}", callback_data=f"nop_{p}")] for p in phones]
    kb.append([InlineKeyboardButton(text="🔙", callback_data="menu")])
    await cb.message.edit_text("📂 Выбери номер для настройки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("nop_"))
async def manage_number(cb: types.CallbackQuery):
    phone = cb.data.split("_")[1]
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT mode FROM accounts WHERE phone=?", (phone,)) as cur:
            res = await cur.fetchone()
            mode = res[0] if res else "normal"
            
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{'✅ ' if mode=='normal' else ''}🔥 NORMAL", callback_data=f"mode_normal_{phone}")],
        [InlineKeyboardButton(text=f"{'✅ ' if mode=='solo' else ''}👤 SOLO", callback_data=f"mode_solo_{phone}")],
        [InlineKeyboardButton(text=f"{'✅ ' if mode=='ghost' else ''}👻 GHOST", callback_data=f"mode_ghost_{phone}")],
        [InlineKeyboardButton(text=f"{'✅ ' if mode=='caller' else ''}📞 CALLER", callback_data=f"mode_caller_{phone}")],
        [InlineKeyboardButton(text="🗑 УДАЛИТЬ", callback_data=f"del_{phone}"),
         InlineKeyboardButton(text="🔙", callback_data="my_numbers")]
    ])
    await cb.message.edit_text(f"⚙️ Настройка +{phone}\nРежим: {mode.upper()}", reply_markup=kb)

@dp.callback_query(F.data.startswith("mode_"))
async def set_mode(cb: types.CallbackQuery):
    _, mode, phone = cb.data.split("_")
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE accounts SET mode=? WHERE phone=?", (mode, phone))
        await db.commit()
    await cb.answer(f"✅ {mode.upper()}")
    await manage_number(cb)

@dp.callback_query(F.data.startswith("del_"))
async def del_number(cb: types.CallbackQuery):
    phone = cb.data.split("_")[1]
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM accounts WHERE phone=?", (phone,))
        await db.commit()
    shutil.rmtree(os.path.join(SESSIONS_DIR, phone), ignore_errors=True)
    await cb.answer("🗑 Удалено")
    await my_numbers(cb)

@dp.callback_query(F.data == "config_speed")
async def config(cb: types.CallbackQuery):
    kb = [[InlineKeyboardButton(text=f"{'✅ ' if k==CURRENT_SPEED else ''}{k}", callback_data=f"spd_{k}")] for k in SPEED_CONFIGS]
    kb.append([InlineKeyboardButton(text="🔙", callback_data="menu")])
    await cb.message.edit_text(f"⚙️ Скорость: {CURRENT_SPEED}", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("spd_"))
async def set_spd(cb: types.CallbackQuery):
    global CURRENT_SPEED
    CURRENT_SPEED = cb.data.split("_")[1]
    await config(cb)

# ==========================================
# 🚀 ЗАПУСК
# ==========================================
async def main():
    await db_init()
    asyncio.create_task(farm_loop())
    asyncio.create_task(auto_backup())
    
    logger.info("🚀 IMPERATOR v36.1 PLATINUM STARTED")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 Stopped")
