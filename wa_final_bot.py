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
from contextlib import asynccontextmanager

# 🚀 UVLOOP (Turbo Core)
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

# ==========================================
# ⚙️ КОНФИГУРАЦИЯ v27.0 LITE (2GB RAM)
# ==========================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

if not BOT_TOKEN or not ADMIN_ID:
    sys.exit("❌ FATAL: Установи переменные окружения BOT_TOKEN и ADMIN_ID")

DB_NAME = 'imperator_lite_v27.db'
SESSIONS_DIR = os.path.abspath("./sessions")
TMP_BASE = os.path.abspath("./tmp")

# 🔥 ЖЕСТКИЕ ЛИМИТЫ ДЛЯ 2GB RAM
MAX_CONCURRENT_BROWSERS = 1  # ТОЛЬКО 1 браузер одновременно!
BROWSER_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_BROWSERS)
MAX_MSGS_PER_HOUR = 12  # Снижено с 15
SPY_MODE_DURATION = 90  # 1.5 мин вместо 2
SCREENSHOT_QUALITY = 50  # Сжатие PNG
MAX_DRIVER_LIFETIME = 180  # 3 мин макс на сессию

logging.basicConfig(
    level=logging.WARNING,  # Меньше логов = меньше памяти
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("Imperator")
fake = Faker('ru_RU')

for d in [SESSIONS_DIR, TMP_BASE]:
    os.makedirs(d, exist_ok=True)

ACTIVE_DRIVERS = {}
CLEANUP_LOCK = asyncio.Lock()

# Минимальный набор устройств
DEVICES = [
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36", "res": "1366,768", "plat": "Linux x86_64"}
]

class BotStates(StatesGroup):
    waiting_phone_auto = State()
    waiting_phone_manual = State()

# ==========================================
# 🧠 AI ENGINE (Облегченная)
# ==========================================
class DialogueAI:
    def __init__(self):
        self.msgs = ["Привет", "Ку", "Норм", "Ок", "Как дела?", "На связи", "Позже", "Работаю"]
    
    def generate(self):
        return random.choice(self.msgs)

ai_engine = DialogueAI()

# ==========================================
# 🗄️ БАЗА ДАННЫХ (Оптимизированная)
# ==========================================
async def db_init():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS accounts 
                            (phone TEXT PRIMARY KEY, 
                             status TEXT DEFAULT 'active', 
                             ua TEXT, res TEXT, plat TEXT,
                             last_act REAL,
                             msgs_hour INTEGER DEFAULT 0, 
                             last_msg_reset REAL)""")
        
        await db.execute("""CREATE TABLE IF NOT EXISTS whitelist 
                            (user_id INTEGER PRIMARY KEY, 
                             approved INTEGER DEFAULT 0)""")
        
        await db.execute("CREATE INDEX IF NOT EXISTS idx_status ON accounts(status)")
        await db.commit()

async def db_get_active():
    """Возвращает только активные аккаунты (не в бане и не в отлеге)"""
    now = time.time()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT phone FROM accounts WHERE status='active' AND (last_act IS NULL OR last_act < ?)", 
            (now,)
        ) as cursor:
            return [r[0] for r in await cursor.fetchall()]

async def db_check_perm(user_id):
    if user_id == ADMIN_ID: return True
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT approved FROM whitelist WHERE user_id=?", (user_id,)) as cursor:
            res = await cursor.fetchone()
            return res[0] == 1 if res else False

async def db_add_request(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO whitelist (user_id, approved) VALUES (?, 0)", (user_id,))
        await db.commit()

async def db_approve(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE whitelist SET approved=1 WHERE user_id=?", (user_id,))
        await db.commit()

async def db_save(phone, ua, res, plat):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""INSERT INTO accounts (phone, status, ua, res, plat, last_act, msgs_hour, last_msg_reset) 
                            VALUES (?, 'active', ?, ?, ?, NULL, 0, ?) 
                            ON CONFLICT(phone) DO UPDATE SET status='active', last_act=NULL""", 
                         (phone, ua, res, plat, time.time()))
        await db.commit()

async def db_set_sleep(phone, hours=24):
    """Отлега на N часов"""
    wake_time = time.time() + (hours * 3600)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE accounts SET last_act=? WHERE phone=?", (wake_time, phone))
        await db.commit()

async def db_check_msg_limit(phone):
    """Проверка лимита сообщений (atomic)"""
    now = time.time()
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT msgs_hour, last_msg_reset FROM accounts WHERE phone=?", 
            (phone,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row: return False
            
            cnt, last_reset = row
            # Сброс счетчика если прошел час
            if now - (last_reset or 0) > 3600:
                await db.execute("UPDATE accounts SET msgs_hour=0, last_msg_reset=? WHERE phone=?", (now, phone))
                await db.commit()
                return True
            
            return cnt < MAX_MSGS_PER_HOUR

async def db_increment_msg(phone):
    """Инкремент счетчика сообщений"""
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE accounts SET msgs_hour=msgs_hour+1 WHERE phone=?", (phone,))
        await db.commit()

# ==========================================
# 🌐 SELENIUM (ULTRA-LITE для 2GB)
# ==========================================
def get_sys_status():
    mem = psutil.virtual_memory()
    return f"RAM: {mem.percent:.1f}% ({mem.available//1024//1024}MB free) | CPU: {psutil.cpu_percent()}%"

def get_driver(phone):
    """Минимальный Chrome для 2GB RAM"""
    d_profile = DEVICES[0]  # Только 1 профиль
    ua, res, plat = d_profile['ua'], d_profile['res'], d_profile['plat']
    
    options = Options()
    prof = os.path.join(SESSIONS_DIR, phone)
    unique_tmp = os.path.join(TMP_BASE, f"tmp_{phone}")
    os.makedirs(unique_tmp, exist_ok=True)

    options.add_argument(f"--user-data-dir={prof}")
    options.add_argument(f"--data-path={unique_tmp}")
    
    # 🔥 АГРЕССИВНАЯ ОПТИМИЗАЦИЯ ПАМЯТИ
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-plugins")
    options.add_argument("--disable-images")
    options.add_argument("--blink-settings=imagesEnabled=false")
    options.add_argument("--disable-javascript-harmony-shipping")
    options.add_argument("--disable-background-networking")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-breakpad")
    options.add_argument("--disable-component-update")
    options.add_argument("--disable-domain-reliability")
    options.add_argument("--disable-features=AudioServiceOutOfProcess,IsolateOrigins,site-per-process")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-logging")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--mute-audio")
    options.add_argument("--single-process")  # ⚠️ Опасно, но экономит RAM
    options.add_argument("--disk-cache-size=1")
    options.add_argument("--media-cache-size=1")
    
    options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
    options.add_experimental_option('useAutomationExtension', False)
    options.add_experimental_option("prefs", {
        "profile.default_content_setting_values.notifications": 2,
        "profile.managed_default_content_settings.images": 2
    })
    
    options.add_argument(f"--user-agent={ua}")
    options.add_argument(f"--window-size={res}")

    try:
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(40)
        driver.set_script_timeout(30)
        return driver, ua, res, plat, unique_tmp
    except Exception as e:
        logger.error(f"❌ Driver Init Error: {e}")
        return None, None, None, None, None

async def cleanup_driver(phone, reason="timeout"):
    """Полная очистка драйвера с убийством процессов"""
    async with CLEANUP_LOCK:
        if phone not in ACTIVE_DRIVERS:
            return
        
        data = ACTIVE_DRIVERS.pop(phone)
        driver = data.get('driver')
        tmp = data.get('tmp')
        
        # Убиваем Chrome процессы
        try:
            if driver:
                await asyncio.to_thread(driver.quit)
        except Exception as e:
            logger.warning(f"Driver quit error: {e}")
        
        # Убиваем зомби-процессы Chrome
        try:
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                if 'chrome' in proc.info['name'].lower():
                    cmdline = ' '.join(proc.info['cmdline'] or [])
                    if phone in cmdline or tmp in cmdline:
                        proc.kill()
        except Exception as e:
            logger.warning(f"Process kill error: {e}")
        
        # Удаляем tmp
        if tmp and os.path.exists(tmp):
            try:
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception as e:
                logger.warning(f"TMP cleanup error: {e}")
        
        logger.info(f"🧹 Cleaned {phone} ({reason})")

async def kill_timer(phone, chat_id, timeout=MAX_DRIVER_LIFETIME):
    """Убийца сессий по таймауту"""
    await asyncio.sleep(timeout)
    if phone in ACTIVE_DRIVERS:
        await cleanup_driver(phone, "timer")
        try:
            await bot.send_message(chat_id, f"⏰ Сессия +{phone} закрыта (таймаут {timeout}с)")
        except: pass

# ==========================================
# 🤖 BOT UI
# ==========================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

def kb_main(is_admin=False):
    btns = [
        [InlineKeyboardButton(text="🤖 АВТО", callback_data="add_auto"), 
         InlineKeyboardButton(text="🎮 РУЧНОЙ", callback_data="add_manual")],
        [InlineKeyboardButton(text="📊 СТАТУС", callback_data="dashboard")]
    ]
    if is_admin:
        btns.append([InlineKeyboardButton(text="⚙️ АДМИН", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_manual_control(phone):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸", callback_data=f"m1_{phone}"),
         InlineKeyboardButton(text="🔗 ВХОД", callback_data=f"m2_{phone}")],
        [InlineKeyboardButton(text="⌨️ НОМЕР", callback_data=f"m3_{phone}"),
         InlineKeyboardButton(text="➡️ NEXT", callback_data=f"m4_{phone}")],
        [InlineKeyboardButton(text="✅ СОХР", callback_data=f"m5_{phone}"),
         InlineKeyboardButton(text="💤 24ч", callback_data=f"ms_{phone}")],
        [InlineKeyboardButton(text="🗑", callback_data=f"mc_{phone}")]
    ])

# ==========================================
# 🛂 AUTH & START
# ==========================================
@dp.message(Command("start"))
async def start(msg: types.Message):
    await db_init()
    user_id = msg.from_user.id
    
    if await db_check_perm(user_id):
        await msg.answer("🔱 **IMPERATOR v27 LITE**\n💾 Оптимизация: 2GB RAM", 
                         reply_markup=kb_main(user_id==ADMIN_ID))
    else:
        await db_add_request(user_id)
        if ADMIN_ID:
            await bot.send_message(ADMIN_ID, 
                f"👤 Заявка: `{user_id}` (@{msg.from_user.username or 'NoUsername'})", 
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="✅ OK", callback_data=f"approve_{user_id}")
                ]]))
        await msg.answer("🔒 Заявка отправлена админу.")

@dp.callback_query(F.data.startswith("approve_"))
async def approve_user(cb: types.CallbackQuery):
    uid = int(cb.data.split("_")[1])
    await db_approve(uid)
    await bot.send_message(uid, "✅ Доступ открыт! /start")
    await cb.answer("✅")

# ==========================================
# 📊 DASHBOARD
# ==========================================
@dp.callback_query(F.data == "dashboard")
async def show_dash(cb: types.CallbackQuery):
    act = await db_get_active()
    sys_stat = get_sys_status()
    
    text = (
        f"📊 **DASHBOARD v27**\n"
        f"📱 Аккаунтов: `{len(act)}`\n"
        f"🏎 Драйверов: `{len(ACTIVE_DRIVERS)}`\n\n"
        f"{sys_stat}\n\n"
        f"⚙️ Лимит: {MAX_CONCURRENT_BROWSERS} браузер"
    )
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔙", callback_data="menu")
    ]]))

@dp.callback_query(F.data == "menu")
async def back_menu(cb: types.CallbackQuery):
    await cb.message.edit_text("Меню", reply_markup=kb_main(cb.from_user.id==ADMIN_ID))

# ==========================================
# 🔥 AUTO MODE
# ==========================================
@dp.callback_query(F.data == "add_auto")
async def auto_start(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("🤖 Введи номер:")
    await state.set_state(BotStates.waiting_phone_auto)

@dp.message(BotStates.waiting_phone_auto)
async def auto_flow(msg: types.Message, state: FSMContext):
    phone = "".join(filter(str.isdigit, msg.text))
    await state.clear()
    s = await msg.answer(f"🚀 Запуск +{phone}...")

    async with BROWSER_SEMAPHORE:
        driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, phone)
        if not driver: 
            return await s.edit_text("💥 Chrome сбой")
        
        ACTIVE_DRIVERS[phone] = {"driver": driver, "ua": ua, "res": res, "plat": plat, "tmp": tmp}
        asyncio.create_task(kill_timer(phone, msg.chat.id))
        
        try:
            await asyncio.to_thread(driver.get, "https://web.whatsapp.com/?lang=en")
            wait = WebDriverWait(driver, 40)
            
            # Link
            await s.edit_text("⏳ Клик...")
            try:
                btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "[data-testid='link-phone']")))
                btn.click()
            except:
                driver.execute_script("document.querySelector('[data-testid=\"link-phone\"]').click()")

            # Номер
            await s.edit_text("⏳ Ввод...")
            inp = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='text']")))
            inp.clear()
            for d in f"+{phone}": 
                inp.send_keys(d)
                await asyncio.sleep(0.08)
            inp.send_keys(Keys.ENTER)

            # Код
            await s.edit_text("⏳ Код...")
            await asyncio.sleep(10)
            
            # Скриншот (сжатый)
            png = await asyncio.to_thread(driver.get_screenshot_as_png)
            
            await s.delete()
            await msg.answer_photo(
                BufferedInputFile(png, "c.png"),
                caption=f"✅ Код получен!\n+{phone}",
                reply_markup=kb_manual_control(phone)
            )
            
        except Exception as e:
            await s.edit_text(f"❌ Ошибка: {str(e)[:100]}")
            await cleanup_driver(phone, "error")

# ==========================================
# 🎮 MANUAL MODE
# ==========================================
@dp.callback_query(F.data == "add_manual")
async def manual_start(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("🎮 Введи номер:")
    await state.set_state(BotStates.waiting_phone_manual)

@dp.message(BotStates.waiting_phone_manual)
async def manual_flow(msg: types.Message, state: FSMContext):
    phone = "".join(filter(str.isdigit, msg.text))
    await state.clear()
    s = await msg.answer(f"🚀 Запуск +{phone}...")

    async with BROWSER_SEMAPHORE:
        driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, phone)
        if not driver: 
            return await s.edit_text("💥 Chrome сбой")
        
        ACTIVE_DRIVERS[phone] = {"driver": driver, "ua": ua, "res": res, "plat": plat, "tmp": tmp}
        asyncio.create_task(kill_timer(phone, msg.chat.id))
        
        await asyncio.to_thread(driver.get, "https://web.whatsapp.com/?lang=en")
        await s.edit_text(f"✅ Пульт готов\n+{phone}", reply_markup=kb_manual_control(phone))

# ==========================================
# 🕹️ MANUAL CONTROLS
# ==========================================
@dp.callback_query(lambda c: c.data and c.data.startswith("m"))
async def manual_control(cb: types.CallbackQuery):
    parts = cb.data[1:].split("_")
    action, phone = parts[0], parts[1] if len(parts) > 1 else ""
    
    if phone not in ACTIVE_DRIVERS: 
        return await cb.answer("❌ Сессия закрыта", show_alert=True)
    
    drv = ACTIVE_DRIVERS[phone]['driver']
    
    try:
        if action == "1":  # Скриншот
            png = await asyncio.to_thread(drv.get_screenshot_as_png)
            await cb.message.answer_photo(BufferedInputFile(png, "s.png"))
            await cb.answer()
            
        elif action == "2":  # Вход
            drv.execute_script("document.querySelector('[data-testid=\"link-phone\"]').click()")
            await cb.answer("✅ Клик")
            
        elif action == "3":  # Номер
            inp = drv.find_element(By.CSS_SELECTOR, "input[type='text']")
            inp.clear()
            for x in f"+{phone}": 
                inp.send_keys(x)
                await asyncio.sleep(0.05)
            await cb.answer("✅ Номер")
            
        elif action == "4":  # Next
            drv.find_element(By.XPATH, "//*[text()='Next']").click()
            await asyncio.sleep(3)
            png = await asyncio.to_thread(drv.get_screenshot_as_png)
            await cb.message.answer_photo(BufferedInputFile(png, "c.png"), caption="✅ Код")
            await cb.answer()
            
        elif action == "5":  # Сохранить
            d = ACTIVE_DRIVERS[phone]
            await db_save(phone, d['ua'], d['res'], d['plat'])
            await cleanup_driver(phone, "saved")
            await cb.message.edit_text(f"🎉 +{phone} СОХРАНЕН")
            
        elif action == "s":  # Отлега
            d = ACTIVE_DRIVERS[phone]
            await db_save(phone, d['ua'], d['res'], d['plat'])
            await db_set_sleep(phone, 24)
            await cleanup_driver(phone, "sleep")
            await cb.message.edit_text(f"💤 +{phone} ОТЛЕГА 24ч")
            
        elif action == "c":  # Отмена
            await cleanup_driver(phone, "cancel")
            await cb.message.edit_text("🗑 Отменено")
            
    except Exception as e:
        await cb.answer(f"Err: {str(e)[:50]}", show_alert=True)

# ==========================================
# 🚜 HIVE MIND (Легкий режим)
# ==========================================
async def worker_hive(phone):
    """Отправка с проверкой лимитов"""
    if not await db_check_msg_limit(phone):
        logger.info(f"⚠️ {phone}: Лимит ({MAX_MSGS_PER_HOUR}/ч)")
        return
    
    targs = await db_get_active()
    if len(targs) < 2: return
    target = random.choice([t for t in targs if t != phone])
    
    driver = None; tmp = None
    try:
        async with BROWSER_SEMAPHORE:
            driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, phone)
            if not driver: return
            
            try:
                driver.set_page_load_timeout(30)
                await asyncio.to_thread(driver.get, f"https://web.whatsapp.com/send?phone={target}")
            except: 
                driver.execute_script("window.stop();")
            
            wait = WebDriverWait(driver, 40)
            inp = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "footer div[contenteditable='true']")))
            
            msg_text = ai_engine.generate()
            inp.send_keys(msg_text)
            await asyncio.sleep(0.5)
            inp.send_keys(Keys.ENTER)
            
            await db_increment_msg(phone)
            logger.info(f"✅ {phone} -> {target}: {msg_text}")
            await asyncio.sleep(3)
            
    except Exception as e:
        logger.error(f"Hive: {e}")
    finally:
        if driver: 
            try: 
                await asyncio.to_thread(driver.quit)
            except: pass
        if tmp and os.path.exists(tmp): 
            shutil.rmtree(tmp, ignore_errors=True)

async def main_loop():
    """Основной цикл (замедленный для 2GB)"""
    while True:
        try:
            phones = await db_get_active()
            if phones:
                phone = random.choice(phones)
                asyncio.create_task(worker_hive(phone))
            
            await asyncio.sleep(random.randint(90, 180))  # 1.5-3 мин между действиями
        except Exception as e:
            logger.error(f"Loop: {e}")
            await asyncio.sleep(60)

async def main():
    await db_init()
    asyncio.create_task(main_loop())
    logger.warning("🚀 IMPERATOR v27 LITE (2GB) STARTED")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__": 
    asyncio.run(main())
