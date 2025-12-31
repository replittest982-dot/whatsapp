import sys
import asyncio
import os
import logging
import sqlite3
import random
import psutil
import shutil
from datetime import datetime, timedelta

# --- AIOGRAM ---
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
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import WebDriverException, TimeoutException

# --- WEBDRIVER MANAGER ---
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.core.os_manager import ChromeType

# ==========================================
# ⚙️ КОНФИГУРАЦИЯ
# ==========================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
INSTANCE_ID = int(os.getenv("INSTANCE_ID", 1))
TOTAL_INSTANCES = int(os.getenv("TOTAL_INSTANCES", 1))

# Настройки для BotHost
BROWSER_SEMAPHORE = asyncio.Semaphore(1) 
DB_NAME = 'imperator_titanium.db'
SESSIONS_DIR = os.path.abspath("./sessions")
TMP_BASE = os.path.abspath("./tmp_chrome_data")

HEAT_MODES = {
    "TURBO": (15, 30),
    "MEDIUM": (60, 180),
    "SLOW": (300, 600)
}
CURRENT_MODE = "MEDIUM"

# 🔥 FIX LOGGING ERROR: Вставляем ID прямо в строку формата
logging.basicConfig(
    level=logging.INFO, 
    format=f'%(asctime)s | INST-{INSTANCE_ID} | %(levelname)s | %(message)s'
)
logger = logging.getLogger("Imperator")
fake = Faker('ru_RU')

# Создаем папки
for d in [SESSIONS_DIR, TMP_BASE]:
    os.makedirs(d, exist_ok=True)

ACTIVE_DRIVERS = {}

# ==========================================
# 🛠 УТИЛИТЫ
# ==========================================

def find_browser_binary():
    """Ищет браузер. Сначала Chromium, потом Chrome."""
    paths = [
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable"
    ]
    chk = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
    if chk: return chk
    
    for p in paths:
        if os.path.exists(p): return p
    return None

def cleanup_zombie():
    """Чистка памяти перед стартом"""
    killed = 0
    for p in psutil.process_iter(['name']):
        if p.info['name'] in ['chromium', 'chromedriver', 'chrome']:
            try: 
                p.kill()
                killed += 1
            except: pass
    
    if os.path.exists(TMP_BASE):
        try: shutil.rmtree(TMP_BASE, ignore_errors=True)
        except: pass
        os.makedirs(TMP_BASE)
    
    if killed > 0:
        logger.info(f"🧹 Zombie Cleanup: {killed} procs killed")

# ==========================================
# 🗄️ БАЗА ДАННЫХ
# ==========================================
def db_init():
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS accounts (phone TEXT PRIMARY KEY, status TEXT, ua TEXT, res TEXT, plat TEXT, last_act DATETIME, created_at DATETIME)")
        conn.execute("CREATE TABLE IF NOT EXISTS whitelist (user_id INTEGER PRIMARY KEY, approved INTEGER DEFAULT 0)")

def db_check_perm(user_id):
    if user_id == ADMIN_ID: return True
    with sqlite3.connect(DB_NAME) as conn:
        res = conn.execute("SELECT approved FROM whitelist WHERE user_id=?", (user_id,)).fetchone()
    return res and res[0] == 1

def db_save_acc(phone, ua, res, plat):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT INTO accounts VALUES (?, 'active', ?, ?, ?, ?, ?) ON CONFLICT(phone) DO UPDATE SET status='active', last_act=excluded.last_act", 
                     (phone, ua, res, plat, datetime.now(), datetime.now()))

def db_get_targets():
    with sqlite3.connect(DB_NAME) as conn:
        res = conn.execute(f"SELECT phone FROM accounts WHERE status='active' AND (rowid % {TOTAL_INSTANCES}) = ({INSTANCE_ID}-1)").fetchall()
    return [r[0] for r in res]

# ==========================================
# 🌐 SELENIUM ENGINE
# ==========================================
def get_driver(phone):
    # 1. Проверка RAM
    mem = psutil.virtual_memory()
    if mem.available / (1024*1024) < 200:
        logger.error("🛑 LOW RAM. Skipping launch.")
        return None, None, None, None, None

    # 2. Поиск бинарника
    binary_path = find_browser_binary()
    if not binary_path:
        logger.critical("❌ CRITICAL: Browser binary NOT found!")
        return None, None, None, None, None

    # 3. Конфиг
    ua_data = random.choice([
        {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "plat": "Win32", "res": "1920,1080"},
        {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "plat": "Linux x86_64", "res": "1366,768"}
    ])

    options = Options()
    options.binary_location = binary_path 
    
    prof_dir = os.path.join(SESSIONS_DIR, phone)
    tmp_dir = os.path.join(TMP_BASE, f"tmp_{phone}_{random.randint(1000,9999)}")
    os.makedirs(tmp_dir, exist_ok=True)

    options.add_argument(f"--user-data-dir={prof_dir}")
    options.add_argument(f"--data-path={tmp_dir}")
    options.add_argument(f"--disk-cache-dir={tmp_dir}")
    options.add_argument(f"--user-agent={ua_data['ua']}")
    options.add_argument(f"--window-size={ua_data['res']}")
    
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.page_load_strategy = 'eager'

    try:
        # Авто-установка драйвера
        driver_path = ChromeDriverManager(chrome_type=ChromeType.CHROMIUM).install()
        service = Service(executable_path=driver_path)
        driver = webdriver.Chrome(options=options, service=service)
        
        # Стелс-инъекция
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": f"""
                Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}});
                Object.defineProperty(navigator, 'platform', {{get: () => '{ua_data['plat']}'}});
            """
        })
        return driver, ua_data['ua'], ua_data['res'], ua_data['plat'], tmp_dir
    except Exception as e:
        logger.error(f"❌ Driver Init Error: {e}")
        return None, None, None, None, None

# ==========================================
# 🤖 BOT LOGIC
# ==========================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class BotStates(StatesGroup):
    wait_phone = State()

@dp.message(Command("start"))
async def start_handler(msg: types.Message):
    if not db_check_perm(msg.from_user.id):
        with sqlite3.connect(DB_NAME) as conn:
            conn.execute("INSERT OR IGNORE INTO whitelist (user_id) VALUES (?)", (msg.from_user.id,))
        return await msg.answer("🔒 Заявка отправлена.")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="add_acc")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats")]
    ])
    await msg.answer(f"👑 **Imperator Titanium v19.2**\nИнстанс: {INSTANCE_ID}", reply_markup=kb)

@dp.callback_query(F.data == "stats")
async def stats_handler(cb: types.CallbackQuery):
    targets = db_get_targets()
    mem = psutil.virtual_memory().percent
    await cb.message.answer(f"📱 Аккаунтов в работе: {len(targets)}\n🧠 RAM Load: {mem}%")
    await cb.answer()

@dp.callback_query(F.data == "add_acc")
async def add_acc_start(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("Введите номер телефона (только цифры):")
    await state.set_state(BotStates.wait_phone)

@dp.message(BotStates.wait_phone)
async def process_phone_add(msg: types.Message, state: FSMContext):
    phone = "".join(filter(str.isdigit, msg.text))
    await state.clear()
    
    status_msg = await msg.answer(f"🚀 Запуск +{phone}...\n(Ждем 20-30 сек)")
    
    async with BROWSER_SEMAPHORE:
        driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, phone)
        
        if not driver:
            return await status_msg.edit_text("❌ Ошибка запуска (см. логи/RAM).")
        
        ACTIVE_DRIVERS[phone] = {"driver": driver, "tmp": tmp}
        
        try:
            await asyncio.to_thread(driver.get, "https://web.whatsapp.com")
            wait = WebDriverWait(driver, 45)
            
            # Поиск Link with phone
            try:
                link_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//span[@role='button'][contains(., 'Link')]")))
                driver.execute_script("arguments[0].click();", link_btn)
            except:
                driver.execute_script("""
                    const spans = document.querySelectorAll('span[role="button"]');
                    for (const s of spans) {
                        if (s.innerText.includes('Link') || s.innerText.includes('Связать')) {
                            s.click(); break;
                        }
                    }
                """)
            
            # Ввод номера JS
            inp_xpath = "//input[@aria-label='Type your phone number.']"
            try:
                inp_elem = wait.until(EC.presence_of_element_located((By.XPATH, inp_xpath)))
                driver.execute_script(f"""
                    var el = arguments[0];
                    el.value = '{phone}';
                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                """, inp_elem)
                await asyncio.sleep(1)
                inp_elem.send_keys(Keys.ENTER)
            except:
                await status_msg.edit_text("❌ Не удалось найти поле ввода номера.")
                return

            await asyncio.sleep(6)
            
            # Скриншот
            png = await asyncio.to_thread(driver.get_screenshot_as_png)
            await status_msg.delete()
            
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Я ВВЕЛ КОД", callback_data=f"finish_{phone}")]
            ])
            await msg.answer_photo(BufferedInputFile(png, "code.png"), caption=f"🔑 Код для +{phone}", reply_markup=kb)
            db_save_acc(phone, ua, res, plat)

        except Exception as e:
            logger.error(f"Add Error: {e}")
            await status_msg.edit_text(f"❌ Ошибка: {str(e)[:50]}")
            if phone in ACTIVE_DRIVERS:
                d = ACTIVE_DRIVERS.pop(phone)
                d['driver'].quit()

@dp.callback_query(F.data.startswith("finish_"))
async def finish_setup(cb: types.CallbackQuery):
    phone = cb.data.split("_")[1]
    if phone in ACTIVE_DRIVERS:
        d = ACTIVE_DRIVERS.pop(phone)
        d['driver'].quit()
        if d['tmp']: shutil.rmtree(d['tmp'], ignore_errors=True)
    
    await cb.message.edit_text(f"✅ Аккаунт +{phone} сохранен!")

# ==========================================
# 🐝 HIVE MIND
# ==========================================
async def hive_worker(phone):
    async with BROWSER_SEMAPHORE:
        driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, phone)
        if not driver: return
        
        try:
            await asyncio.to_thread(driver.get, "https://web.whatsapp.com")
            wait = WebDriverWait(driver, 60)
            
            try:
                wait.until(EC.presence_of_element_located((By.ID, "pane-side")))
            except:
                logger.warning(f"⚠️ {phone}: Не прогрузился.")
                return

            # Self-chat
            await asyncio.to_thread(driver.get, f"https://web.whatsapp.com/send?phone={phone}")
            inp = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@contenteditable='true'][@data-tab='10']")))
            
            text = fake.sentence()
            for char in text:
                inp.send_keys(char)
                await asyncio.sleep(random.uniform(0.05, 0.2))
            inp.send_keys(Keys.ENTER)
            
            logger.info(f"✅ {phone}: Warming done.")
            await asyncio.sleep(5)
            
        except Exception as e:
            logger.error(f"Hive Error {phone}: {e}")
        finally:
            if driver: driver.quit()
            if tmp and os.path.exists(tmp): shutil.rmtree(tmp, ignore_errors=True)

async def hive_loop():
    logger.info("🐝 HIVE MIND STARTED")
    while True:
        try:
            targets = db_get_targets()
            if not targets:
                await asyncio.sleep(60)
                continue
            
            random.shuffle(targets)
            for phone in targets:
                if phone not in ACTIVE_DRIVERS:
                    await hive_worker(phone)
                    await asyncio.sleep(random.randint(30, 90))
            
            delay = random.randint(*HEAT_MODES[CURRENT_MODE])
            logger.info(f"💤 Сон {delay}с...")
            await asyncio.sleep(delay)
            
        except Exception as e:
            logger.error(f"Loop Critical: {e}")
            await asyncio.sleep(30)

# ==========================================
# 🚀 MAIN
# ==========================================
async def main():
    cleanup_zombie()
    db_init()
    asyncio.create_task(hive_loop())
    logger.info(f"🚀 IMPERATOR STARTED (Inst-{INSTANCE_ID})")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
