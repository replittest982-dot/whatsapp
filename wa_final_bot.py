import asyncio
import os
import logging
import sqlite3
import random
import re
import string
import shutil
import psutil
import traceback
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
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
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN") # Убедись, что токен в ENV
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))

# Лимиты BotHost (PRO)
# Semaphore 3 = Максимум 3 одновременных окна браузера. 
# Если поставить больше, контейнер OOM Kill (Out Of Memory).
BROWSER_SEMAPHORE = asyncio.Semaphore(3)
DB_NAME = 'bot_database.db'
SESSIONS_DIR = "./sessions"
LOG_DIR = "./logs"

# Глобальное хранилище драйверов {user_id: driver_instance}
ACTIVE_DRIVERS = {}
fake = Faker('ru_RU')

# Настройки таймингов фарма (сек)
FARM_DELAY_MIN = 120
FARM_DELAY_MAX = 300

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("farm.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("ARCHITECT")

# --- DATABASE ---
def init_db():
    with sqlite3.connect(DB_NAME, timeout=10) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS accounts 
                        (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                         user_id INTEGER, 
                         phone_number TEXT UNIQUE, 
                         status TEXT DEFAULT 'pending', 
                         messages_sent INTEGER DEFAULT 0,
                         user_agent TEXT, resolution TEXT, platform TEXT,
                         ban_reason TEXT, 
                         last_active TIMESTAMP,
                         created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        conn.commit()

def db_get_acc(phone):
    with sqlite3.connect(DB_NAME, timeout=10) as conn:
        return conn.execute("SELECT * FROM accounts WHERE phone_number = ?", (phone,)).fetchone()

def db_get_active_phones():
    with sqlite3.connect(DB_NAME, timeout=10) as conn:
        return [row[0] for row in conn.execute("SELECT phone_number FROM accounts WHERE status = 'active'").fetchall()]

def db_update_status(phone, status, reason=None):
    with sqlite3.connect(DB_NAME, timeout=10) as conn:
        conn.execute("UPDATE accounts SET status = ?, ban_reason = ?, last_active = ? WHERE phone_number = ?", 
                     (status, reason, datetime.now(), phone))

def db_inc_msg(phone):
    with sqlite3.connect(DB_NAME, timeout=10) as conn:
        conn.execute("UPDATE accounts SET messages_sent = messages_sent + 1, last_active = ? WHERE phone_number = ?", 
                     (datetime.now(), phone))

def db_delete_acc(phone):
    with sqlite3.connect(DB_NAME, timeout=10) as conn:
        conn.execute("DELETE FROM accounts WHERE phone_number = ?", (phone,))
    path = os.path.join(SESSIONS_DIR, str(phone))
    if os.path.exists(path):
        try: shutil.rmtree(path)
        except: pass

# --- SYSTEM HEALTH (MEMORY GUARD) ---
def is_memory_safe():
    """Возвращает False, если памяти меньше 200MB"""
    mem = psutil.virtual_memory()
    free_mb = mem.available / (1024 * 1024)
    if free_mb < 200:
        logger.warning(f"⚠️ LOW MEMORY: {free_mb:.1f}MB. Pause operations.")
        return False
    return True

async def zombie_killer():
    """Убийца зомби. Удаляет процессы Chrome, которые висят дольше 20 минут."""
    logger.info("🧟 Zombie Killer activated")
    while True:
        await asyncio.sleep(120)
        killed_count = 0
        current_time = datetime.now().timestamp()
        
        for proc in psutil.process_iter(['pid', 'name', 'create_time']):
            try:
                if 'chrome' in proc.info['name'] or 'chromedriver' in proc.info['name']:
                    # Если процесс живет > 20 минут (1200 сек)
                    if (current_time - proc.info['create_time']) > 1200:
                        proc.kill()
                        killed_count += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        
        if killed_count > 0:
            logger.warning(f"⚔️ Killed {killed_count} zombie processes.")

# --- BROWSER CORE ---
def get_driver(phone, force_new=False):
    # 1. Проверка памяти
    if not is_memory_safe():
        raise Exception("Server overload (Low RAM)")

    # 2. Подготовка папки
    path = os.path.join(SESSIONS_DIR, str(phone))
    if force_new and os.path.exists(path):
        shutil.rmtree(path, ignore_errors=True)
        logger.info(f"♻️ Session reset for {phone}")

    if not os.path.exists(path):
        os.makedirs(path)

    # 3. Получение/Генерация фингерпринта
    acc = db_get_acc(phone)
    if acc and acc[5]:
        ua, res, plat = acc[5], acc[6], acc[7]
    else:
        # Стабильные юзер-агенты
        dev_list = [
            {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", "res": "1920,1080", "plat": "Windows"},
            {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36", "res": "1440,900", "plat": "MacIntel"}
        ]
        dev = random.choice(dev_list)
        ua, res, plat = dev['ua'], dev['res'], dev['plat']
        with sqlite3.connect(DB_NAME) as conn:
            conn.execute("UPDATE accounts SET user_agent=?, resolution=?, platform=? WHERE phone_number=?", (ua, res, plat, phone))

    # 4. Опции Chrome (Оптимизация для BotHost)
    opt = Options()
    opt.add_argument("--headless=new") # Новый headless режим
    opt.add_argument("--no-sandbox")
    opt.add_argument("--disable-dev-shm-usage") # Важно для Docker/LXC
    opt.add_argument("--disable-gpu")
    opt.add_argument("--disable-software-rasterizer")
    opt.add_argument(f"--window-size={res}")
    opt.add_argument("--lang=ru-KZ")
    opt.add_argument(f"user-agent={ua}")
    opt.add_argument(f"--user-data-dir={path}")
    
    # Eager - не ждем полной загрузки картинок
    opt.page_load_strategy = 'eager' 
    
    # Скрытие автоматизации
    opt.add_argument("--disable-blink-features=AutomationControlled")
    opt.add_experimental_option("excludeSwitches", ["enable-automation"])
    opt.add_experimental_option('useAutomationExtension', False)

    try:
        driver = webdriver.Chrome(options=opt)
        
        # JS INJECTION (STEALTH + TIMEZONE)
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": f"""
                Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}});
                Object.defineProperty(navigator, 'platform', {{get: () => '{plat}'}});
                
                // Fake Timezone Asia/Almaty
                const toLocaleStringOriginal = Date.prototype.toLocaleString;
                Date.prototype.toLocaleString = function(locale, options) {{
                    return toLocaleStringOriginal.call(this, locale, {{ ...options, timeZone: "Asia/Almaty" }});
                }};
                
                // WebGL Noise (Minimal)
                const getParameter = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(parameter) {{
                    if (parameter === 37445) return 'Google Inc. (NVIDIA)';
                    if (parameter === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)';
                    return getParameter(parameter);
                }};
            """
        })
        
        # GEO INJECTION
        driver.execute_cdp_cmd("Emulation.setGeolocationOverride", {
            "latitude": 43.2389, "longitude": 76.8897, "accuracy": 50
        })

        return driver
    except Exception as e:
        logger.error(f"Failed to create driver: {e}")
        raise e

# --- HUMAN HELPERS ---
async def human_type(element, text, speed=0.1):
    """Имитация ввода с опечатками (редко)"""
    for char in text:
        element.send_keys(char)
        await asyncio.sleep(random.uniform(0.02, speed))

def get_screenshot(driver):
    try:
        return driver.get_screenshot_as_png()
    except:
        return None

# --- BOT INTERFACE ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
class Form(StatesGroup): phone = State()

def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить Аккаунт", callback_data="add")],
        [InlineKeyboardButton(text="📂 Список Аккаунтов", callback_data="list")],
        [InlineKeyboardButton(text="📊 Статистика Сервера", callback_data="stats")]
    ])

def kb_auth():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 ПОКАЗАТЬ QR/КОД", callback_data="check")],
        [InlineKeyboardButton(text="🔗 ВХОД ПО НОМЕРУ (FIX)", callback_data="link_phone")],
        [InlineKeyboardButton(text="✅ Я ВОШЕЛ", callback_data="done")],
        [InlineKeyboardButton(text="♻️ СБРОС (HARD RESET)", callback_data="hard_reset")]
    ])

# --- HANDLERS ---
@dp.message(Command("start"))
async def start(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    init_db()
    if not os.path.exists(SESSIONS_DIR): os.makedirs(SESSIONS_DIR)
    await msg.answer("🏛 **WA Farm Architect Pro 2.0**\nСистема в норме.", reply_markup=kb_main())

@dp.callback_query(F.data == "stats")
async def stats(call: types.CallbackQuery):
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    
    active_workers = len(ACTIVE_DRIVERS)
    
    txt = (f"🖥 **Server Status:**\n"
           f"🧠 RAM Free: {mem.available // 1024 // 1024} MB\n"
           f"💿 Disk Free: {disk.free // 1024 // 1024} MB\n"
           f"🤖 Active Drivers: {active_workers}/3\n"
           f"🏗 CPU Load: {psutil.cpu_percent()}%")
    
    await call.answer(txt, show_alert=True)

@dp.callback_query(F.data == "add")
async def add_start(call: types.CallbackQuery, state: FSMContext):
    # Очистка старого драйвера пользователя
    uid = call.from_user.id
    if uid in ACTIVE_DRIVERS:
        try: ACTIVE_DRIVERS[uid].quit()
        except: pass
        del ACTIVE_DRIVERS[uid]
        
    await call.message.edit_text("Введите номер телефона (только цифры, например 79991234567):")
    await state.set_state(Form.phone)

@dp.message(Form.phone)
async def add_phone(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    if len(phone) < 10:
        return await msg.answer("❌ Неверный формат номера.")
    
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR IGNORE INTO accounts (user_id, phone_number) VALUES (?, ?)", (msg.from_user.id, phone))
    
    await state.update_data(phone=phone)
    await msg.answer(f"⏳ Запускаю браузер для **{phone}**...\nЖди 15-30 сек.", reply_markup=kb_auth())
    
    # Запуск фоновой задачи для открытия браузера
    asyncio.create_task(bg_open_browser(msg.from_user.id, phone))

async def bg_open_browser(uid, phone, force_new=False):
    async with BROWSER_SEMAPHORE:
        try:
            driver = await asyncio.to_thread(get_driver, phone, force_new)
            ACTIVE_DRIVERS[uid] = driver
            
            logger.info(f"Navigating to WA for {phone}")
            driver.get("https://web.whatsapp.com/")
            
            # Ждем либо QR, либо загрузки чатов (мало ли уже залогинен)
            try:
                WebDriverWait(driver, 60).until(
                    EC.any_of(
                        EC.presence_of_element_located((By.TAG_NAME, "canvas")),
                        EC.presence_of_element_located((By.ID, "pane-side")),
                        EC.presence_of_element_located((By.XPATH, "//*[contains(text(), 'Link with phone')]"))
                    )
                )
            except:
                logger.warning(f"Timeout waiting for initial load {phone}")
                
            # Держим сессию активной 5 минут для настройки
            await asyncio.sleep(300)
            
        except Exception as e:
            logger.error(f"Browser Init Error: {e}")
            if uid in ACTIVE_DRIVERS:
                try: ACTIVE_DRIVERS[uid].quit()
                except: pass
                del ACTIVE_DRIVERS[uid]

@dp.callback_query(F.data == "check")
async def check_screen(call: types.CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    driver = ACTIVE_DRIVERS.get(uid)
    if not driver:
        return await call.answer("❌ Браузер закрыт. Начни заново.", show_alert=True)
    
    await call.answer("📸 Делаю скрин...")
    scr = get_screenshot(driver)
    if scr:
        await call.message.answer_photo(BufferedInputFile(scr, "screen.png"), caption="Текущий экран")
    else:
        await call.message.answer("❌ Не удалось сделать скрин.")

# --- УЛУЧШЕННЫЙ ВХОД ПО НОМЕРУ ---
@dp.callback_query(F.data == "link_phone")
async def link_phone_pro(call: types.CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    driver = ACTIVE_DRIVERS.get(uid)
    data = await state.get_data()
    phone = data.get('phone')
    
    if not driver:
        return await call.answer("❌ Браузер закрыт.", show_alert=True)
    
    await call.message.answer("🕵️‍♂️ Ищу кнопку 'Вход по номеру'...")
    
    try:
        # 1. Поиск и клик по кнопке "Link with phone number"
        # Используем несколько вариантов XPath, так как WA меняет их
        link_xpath_list = [
            "//span[contains(text(), 'Link with phone number')]",
            "//span[contains(text(), 'Связать с номером телефона')]",
            "//div[@role='button']//div[contains(text(), 'Link with phone')]",
            "//a[contains(@href, 'link-device-phone-number')]"
        ]
        
        btn = None
        for xp in link_xpath_list:
            try:
                btn = WebDriverWait(driver, 2).until(EC.element_to_be_clickable((By.XPATH, xp)))
                if btn: break
            except: continue
            
        if btn:
            btn.click()
            await asyncio.sleep(1)
        else:
            # Если не нашли кнопку, пробуем проверить, может мы уже на экране ввода?
            pass

        # 2. Ждем поле ввода
        await call.message.answer("⌨️ Ищу поле ввода...")
        try:
            inp_box = WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.XPATH, "//input[@aria-label='Type your phone number.'] | //input[@type='text']"))
            )
            
            # Очистка и ввод (Javascript надежнее)
            driver.execute_script("arguments[0].value = '';", inp_box)
            inp_box.send_keys(Keys.CONTROL + "a")
            inp_box.send_keys(Keys.DELETE)
            
            # Вводим номер
            for ch in phone:
                inp_box.send_keys(ch)
                await asyncio.sleep(0.05)
            
            # Жмем ENTER или кнопку Next
            await asyncio.sleep(0.5)
            inp_box.send_keys(Keys.ENTER)
            
            try:
                next_btn = driver.find_element(By.XPATH, "//div[text()='Next'] | //div[text()='Далее']")
                next_btn.click()
            except: pass
            
        except Exception as e:
            return await call.message.answer(f"❌ Не нашел поле ввода: {e}")

        # 3. Ждем КОД
        await call.message.answer("⏳ Жду код (это может занять 10-15 сек)...")
        try:
            code_el = WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.XPATH, "//div[@aria-details='link-device-phone-number-code']"))
            )
            code_text = code_el.text
            await call.message.answer(f"🔑 **ТВОЙ КОД:** `{code_text}`\n\nВводи его в телефоне!", parse_mode="Markdown")
            
            # Сразу шлем скрин для надежности
            scr = get_screenshot(driver)
            if scr: await call.message.answer_photo(BufferedInputFile(scr, "code.png"))
            
        except TimeoutException:
            scr = get_screenshot(driver)
            await call.message.answer_photo(BufferedInputFile(scr, "err.png"), caption="❌ Не увидел код. Проверь скрин, может он там?")
            
    except Exception as e:
        logger.error(traceback.format_exc())
        await call.message.answer("❌ Ошибка процесса. См. логи.")

@dp.callback_query(F.data == "hard_reset")
async def hard_reset(call: types.CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    if uid in ACTIVE_DRIVERS:
        try: ACTIVE_DRIVERS[uid].quit()
        except: pass
        del ACTIVE_DRIVERS[uid]
    
    data = await state.get_data()
    phone = data.get('phone')
    if phone:
        path = os.path.join(SESSIONS_DIR, str(phone))
        if os.path.exists(path): shutil.rmtree(path)
        await call.answer("🗑 Сессия удалена. Начинаем с нуля.", show_alert=True)
        # Перезапуск
        await add_start(call, state)

@dp.callback_query(F.data == "done")
async def auth_done(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    phone = data.get('phone')
    
    # Закрываем браузер настройки
    uid = call.from_user.id
    if uid in ACTIVE_DRIVERS:
        ACTIVE_DRIVERS[uid].quit()
        del ACTIVE_DRIVERS[uid]
        
    db_update_status(phone, 'active')
    await call.message.edit_text(f"✅ Аккаунт {phone} добавлен в ферму!\nОн начнет работу в следующем цикле.")

@dp.callback_query(F.data == "list")
async def list_accs(call: types.CallbackQuery):
    with sqlite3.connect(DB_NAME) as conn:
        accs = conn.execute("SELECT phone_number, status, messages_sent FROM accounts").fetchall()
    
    text = "📂 **Список аккаунтов:**\n\n"
    if not accs: text += "Пусто."
    
    for p, s, m in accs:
        status_icon = "🟢" if s == 'active' else "🔴"
        text += f"{status_icon} `{p}` | SMS: {m}\n"
        
    await call.message.edit_text(text, reply_markup=kb_main(), parse_mode="Markdown")

# --- FARM LOGIC (AUTO) ---
async def farm_worker(phone):
    """Один цикл работы аккаунта"""
    if not is_memory_safe(): return
    
    logger.info(f"🚜 Farming: {phone}")
    driver = None
    try:
        # Используем to_thread для тяжелых операций блокировки
        driver = await asyncio.to_thread(get_driver, phone)
        
        try:
            driver.get("https://web.whatsapp.com/")
        except TimeoutException:
            logger.warning(f"Load timeout {phone}")
            driver.quit()
            return

        wait = WebDriverWait(driver, 30)
        
        # Ждем загрузку (или бан)
        try:
            wait.until(EC.presence_of_element_located((By.ID, "pane-side")))
        except TimeoutException:
            # Проверка на бан
            src = driver.page_source
            if "account is not allowed" in src or "Need to download" in src:
                db_update_status(phone, 'banned', 'PermBan')
                logger.error(f"🚫 BAN DETECTED: {phone}")
            driver.quit()
            return

        # LOGIC: SOLO (Нарцисс)
        # Пишем сами себе в "Saved Messages" (свой номер)
        driver.get(f"https://web.whatsapp.com/send?phone={phone}")
        
        # Ждем поле ввода
        inp_xpath = "//div[@contenteditable='true'][@data-tab='10']"
        try:
            inp = wait.until(EC.presence_of_element_located((By.XPATH, inp_xpath)))
            
            # Эмуляция печати
            phrase = fake.sentence()
            await human_type(inp, phrase)
            await asyncio.sleep(1)
            inp.send_keys(Keys.ENTER)
            
            db_inc_msg(phone)
            logger.info(f"✅ {phone} sent solo msg.")
            
        except TimeoutException:
            logger.warning(f"Could not find input for {phone}")

        await asyncio.sleep(5)
        
    except Exception as e:
        logger.error(f"Farm Error {phone}: {e}")
    finally:
        if driver:
            try: driver.quit()
            except: pass

async def farm_loop():
    """Бесконечный цикл фермы"""
    logger.info("🚜 Farm Loop Started")
    # Запускаем киллера зомби
    asyncio.create_task(zombie_killer())
    
    while True:
        phones = db_get_active_phones()
        if not phones:
            await asyncio.sleep(30)
            continue
            
        # Выбираем случайного, кто давно не работал
        # (в PRO версии тут была бы очередь, но пока рандом)
        target = random.choice(phones)
        
        # Запускаем воркера (Semaphore ограничит количество)
        asyncio.create_task(farm_worker(target))
        
        # Случайная задержка между запусками разных аккаунтов
        delay = random.randint(FARM_DELAY_MIN, FARM_DELAY_MAX)
        logger.info(f"💤 Sleeping {delay}s before next launch")
        await asyncio.sleep(delay)

# --- MAIN ---
async def main():
    if not BOT_TOKEN:
        print("❌ ERROR: BOT_TOKEN is missing!")
        return
        
    init_db()
    
    # Запуск фермы в фоне
    asyncio.create_task(farm_loop())
    
    print("🚀 Bot Started!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped.")
