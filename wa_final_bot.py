import asyncio
import os
import logging
import sqlite3
import random
import re
import string
import json
import psutil  # НОВАЯ БИБЛИОТЕКА ДЛЯ КОНТРОЛЯ ПАМЯТИ
from datetime import datetime
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
from selenium.webdriver.common.action_chains import ActionChains

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except:
    ADMIN_ID = 0

# 🔥 МОЩНОСТЬ: 4 БРАУЗЕРА ОДНОВРЕМЕННО
BROWSER_SEMAPHORE = asyncio.Semaphore(4)
DB_NAME = 'bot_database.db'
SESSIONS_DIR = "/app/sessions"

ACTIVE_DRIVERS = {} 
fake = Faker('ru_RU')

# Скорость фарма (быстрее, так как потоков больше)
FARM_DELAY_MIN = 40
FARM_DELAY_MAX = 120

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- БАЗА УСТРОЙСТВ (FINGERPRINTS) ---
DEVICES = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36", "res": "1920,1080", "platform": "Windows"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "res": "1440,900", "platform": "MacIntel"},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36", "res": "1366,768", "platform": "Linux x86_64"},
]

# --- БАЗА ДАННЫХ ---
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS accounts 
                        (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                         user_id INTEGER, 
                         phone_number TEXT UNIQUE, 
                         status TEXT DEFAULT 'pending', 
                         messages_sent INTEGER DEFAULT 0,
                         user_agent TEXT, resolution TEXT, platform TEXT,
                         ban_reason TEXT,
                         last_active TIMESTAMP)''')
        conn.commit()

def db_get_account(phone):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT * FROM accounts WHERE phone_number = ?", (phone,)).fetchone()

def db_get_active_phones():
    with sqlite3.connect(DB_NAME) as conn:
        return [row[0] for row in conn.execute("SELECT phone_number FROM accounts WHERE status = 'active'").fetchall()]

def db_update_status(phone, status, reason=None):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET status = ?, ban_reason = ? WHERE phone_number = ?", (status, reason, phone))

def db_inc_msg(phone):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET messages_sent = messages_sent + 1, last_active = ? WHERE phone_number = ?", (datetime.now(), phone))

# --- ЗАЩИТНИК ПАМЯТИ (AI FEATURE) ---
def is_memory_safe():
    """Проверяет, есть ли свободная память на сервере"""
    mem = psutil.virtual_memory()
    available_mb = mem.available / 1024 / 1024
    if available_mb < 300: # Если меньше 300МБ свободно
        logger.warning(f"⚠️ ОПАСНО МАЛО ПАМЯТИ: {int(available_mb)}MB. Ждем...")
        return False
    return True

# --- ДРАЙВЕР (KZ TIMEZONE + STEALTH) ---
def get_driver(phone):
    # Достаем или генерируем "Паспорт" устройства
    acc = db_get_account(phone)
    if acc and acc[5]:
        ua, res, platform = acc[5], acc[6], acc[7]
    else:
        dev = random.choice(DEVICES)
        ua, res, platform = dev['ua'], dev['res'], dev['platform']
        with sqlite3.connect(DB_NAME) as conn:
            conn.execute("UPDATE accounts SET user_agent=?, resolution=?, platform=? WHERE phone_number=?", 
                         (ua, res, platform, phone))
    
    opt = Options()
    opt.binary_location = "/usr/bin/google-chrome"
    opt.add_argument("--headless=new")
    opt.add_argument("--no-sandbox")
    opt.add_argument("--disable-dev-shm-usage")
    opt.add_argument(f"--window-size={res}")
    
    # STEALTH
    opt.add_argument("--disable-blink-features=AutomationControlled")
    opt.add_experimental_option("excludeSwitches", ["enable-automation"])
    opt.add_experimental_option('useAutomationExtension', False)
    
    # KZ LOCALE
    opt.add_argument("--lang=ru-KZ")
    opt.add_argument(f"user-agent={ua}")
    opt.add_argument(f"--user-data-dir={os.path.join(SESSIONS_DIR, str(phone))}")

    driver = webdriver.Chrome(service=Service("/usr/local/bin/chromedriver"), options=opt)
    
    # ИНЪЕКЦИЯ JS: Скрываем WebDriver + Меняем Timezone
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": f"""
        Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}});
        Object.defineProperty(navigator, 'platform', {{get: () => '{platform}'}});
        // Подмена часового пояса на Алматы
        const toLocaleStringOriginal = Date.prototype.toLocaleString;
        Date.prototype.toLocaleString = function(locale, options) {{
            return toLocaleStringOriginal.call(this, locale, {{ ...options, timeZone: "Asia/Almaty" }});
        }};
        """
    })
    
    # ГЕОЛОКАЦИЯ
    driver.execute_cdp_cmd("Emulation.setGeolocationOverride", {
        "latitude": 43.2389, "longitude": 76.8897, "accuracy": 100
    })
    
    return driver

# --- ЭМУЛЯЦИЯ ---
async def human_type(element, text):
    for char in text:
        if random.random() < 0.04:
            element.send_keys(random.choice(string.ascii_lowercase))
            await asyncio.sleep(0.1)
            element.send_keys(Keys.BACKSPACE)
        element.send_keys(char)
        await asyncio.sleep(random.uniform(0.04, 0.12))

async def check_ban_status(driver, phone):
    try:
        if "WhatsApp Web" in driver.title and len(driver.find_elements(By.XPATH, "//canvas")) > 0:
            logger.warning(f"QR DETECTED FOR {phone}")
            return True # Требует QR = Слет
        # Проверка на текст бана
        if driver.find_elements(By.XPATH, "//*[contains(text(), 'account is not allowed')]"):
            logger.error(f"BAN DETECTED FOR {phone}")
            db_update_status(phone, 'banned', 'Permanent Ban')
            return True
        return False
    except: return False

# --- КЛАВИАТУРЫ ---
def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить Аккаунт", callback_data="add")],
        [InlineKeyboardButton(text="📊 Статус Фермы", callback_data="list")]
    ])

def kb_auth():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 ЧЕК", callback_data="check"),
         InlineKeyboardButton(text="✅ ГОТОВО", callback_data="done")],
        [InlineKeyboardButton(text="🔗 Вход по номеру", callback_data="force_link")],
        [InlineKeyboardButton(text="⌨️ Ввести номер", callback_data="force_type")]
    ])

# --- БОТ ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
class Form(StatesGroup): phone = State()

@dp.message(Command("start"))
async def start(msg: types.Message):
    init_db()
    await msg.answer("💎 **WhatsApp Imperator v15.0**\nМощность: 4 потока.\nЗащита: KZ-Geo + Timezone + Memory Guard.", reply_markup=kb_main())

@dp.callback_query(F.data == "add")
async def add_flow(call: types.CallbackQuery, state: FSMContext):
    await call.message.edit_text("Введи номер (7XXXXXXXXXX):")
    await state.set_state(Form.phone)

@dp.message(Form.phone)
async def proc_phone(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR IGNORE INTO accounts (user_id, phone_number) VALUES (?, ?)", (msg.from_user.id, phone))
    await state.update_data(phone=phone)
    await msg.answer(f"🚀 Запускаю {phone}...", reply_markup=kb_auth())
    asyncio.create_task(bg_login(msg.from_user.id, phone))

async def bg_login(uid, phone):
    # Перед запуском проверяем память
    while not is_memory_safe(): await asyncio.sleep(10)
    
    async with BROWSER_SEMAPHORE:
        try:
            driver = await asyncio.to_thread(get_driver, phone)
            ACTIVE_DRIVERS[uid] = driver
            driver.get("https://web.whatsapp.com/")
            await asyncio.sleep(900)
        finally:
            if uid in ACTIVE_DRIVERS: ACTIVE_DRIVERS.pop(uid).quit()

# --- ВХОД И ПРОВЕРКИ ---
@dp.callback_query(F.data == "check")
async def check(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    phone = data.get("phone")
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    
    temp = False
    if not driver:
        if not phone: return await call.answer("Нет сессии")
        if not is_memory_safe(): return await call.answer("Мало памяти на сервере, жди...")
        await call.answer("Запуск браузера...")
        driver = await asyncio.to_thread(get_driver, phone)
        driver.get("https://web.whatsapp.com/")
        await asyncio.sleep(10)
        temp = True
    else:
        await call.answer("Скрин...")

    try:
        scr = await asyncio.to_thread(driver.get_screenshot_as_png)
        code = ""
        try: 
            el = driver.find_element(By.XPATH, "//div[@aria-details='link-device-phone-number-code']")
            code = f"\nКОД: {el.text}"
        except: pass
        await call.message.answer_photo(BufferedInputFile(scr, "s.png"), caption=f"Экран{code}")
    except: await call.answer("Ошибка")
    finally:
        if temp: driver.quit()

@dp.callback_query(F.data == "force_link")
async def f_link(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Закрыт")
    try:
        btn = driver.find_element(By.XPATH, "//*[contains(text(), 'Link with phone')] | //*[contains(text(), 'Связать с номером')]")
        driver.execute_script("arguments[0].click();", btn)
        await call.answer("Ок")
    except: await call.answer("Не нашел")

@dp.callback_query(F.data == "force_type")
async def f_type(call: types.CallbackQuery, state: FSMContext):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    data = await state.get_data()
    if not driver: return
    try:
        # Авто-клик по ссылке если надо
        try:
            l = driver.find_element(By.XPATH, "//*[contains(text(), 'Link with phone')] | //*[contains(text(), 'Связать с номером')]")
            driver.execute_script("arguments[0].click();", l)
            await asyncio.sleep(1)
        except: pass

        inp = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.TAG_NAME, "input")))
        driver.execute_script("arguments[0].value = '';", inp)
        inp.send_keys(Keys.CONTROL + "a" + Keys.BACKSPACE)
        for ch in f"+{data['phone']}":
            inp.send_keys(ch)
            await asyncio.sleep(0.05)
        inp.send_keys(Keys.ENTER)
        await call.answer("Ввел")
    except: await call.answer("Ошибка")

@dp.callback_query(F.data == "done")
async def done(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    phone = data.get("phone")
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET status = 'active' WHERE phone_number = ?", (phone,))
    if call.from_user.id in ACTIVE_DRIVERS:
        ACTIVE_DRIVERS.pop(call.from_user.id).quit()
    await call.message.answer(f"✅ {phone} готов! Начинаю прогрев.")
    asyncio.create_task(process_account(phone, solo_mode=True))

@dp.callback_query(F.data == "list")
async def list_a(call: types.CallbackQuery):
    with sqlite3.connect(DB_NAME) as conn:
        accs = conn.execute("SELECT phone_number, status, messages_sent FROM accounts").fetchall()
    
    txt = f"📈 **Ферма ({len(accs)}):**\n"
    for p, s, m in accs:
        icon = "🟢" if s=='active' else "🔴"
        if s == 'banned': icon = "🚫"
        txt += f"\n{icon} {p} | Смс: {m}"
    await call.message.answer(txt, reply_markup=kb_main())

# --- УНИВЕРСАЛЬНЫЙ ВОРКЕР (СОЛО + МАСС) ---
async def process_account(sender, solo_mode=False):
    # Ждем память, если сервер перегружен
    while not is_memory_safe(): await asyncio.sleep(10)

    async with BROWSER_SEMAPHORE:
        driver = None
        try:
            logger.info(f"Worker: {sender} (Solo: {solo_mode})")
            driver = await asyncio.to_thread(get_driver, sender)
            driver.get("https://web.whatsapp.com/")
            
            wait = WebDriverWait(driver, 60)
            try:
                wait.until(EC.presence_of_element_located((By.ID, "pane-side")))
            except:
                if await check_ban_status(driver, sender): return
                driver.refresh()
                await asyncio.sleep(15)

            if solo_mode:
                # === СОЛО (ИЗБРАННОЕ + СТАТУС) ===
                if random.random() < 0.3: # 30% шанс сменить статус
                    try:
                        driver.find_element(By.XPATH, "//header//img | //header//div[@role='button']").click()
                        await asyncio.sleep(2)
                        eds = driver.find_elements(By.XPATH, "//span[@data-icon='pencil']")
                        if len(eds) >= 2:
                            eds[1].click()
                            await asyncio.sleep(1)
                            act = driver.switch_to.active_element
                            act.send_keys(Keys.CONTROL + "a" + Keys.BACKSPACE)
                            await human_type(act, fake.catch_phrase())
                            act.send_keys(Keys.ENTER)
                            await asyncio.sleep(1)
                            driver.find_element(By.XPATH, "//span[@data-icon='back']").click()
                    except: pass
                
                # Пишем себе
                driver.get(f"https://web.whatsapp.com/send?phone={sender}")
                try:
                    inp = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@contenteditable='true'][@data-tab='10']")))
                    await human_type(inp, f"Заметка: {fake.word()}")
                    inp.send_keys(Keys.ENTER)
                    db_inc_msg(sender)
                except: pass

            else:
                # === МАСС (ДРУГ ДРУГУ) ===
                accs = db_get_active_phones()
                targets = [p for p in accs if p != sender]
                if targets:
                    target = random.choice(targets)
                    driver.get(f"https://web.whatsapp.com/send?phone={target}")
                    
                    inp = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@contenteditable='true'][@data-tab='10']")))
                    
                    await asyncio.sleep(random.randint(3, 8))
                    text = fake.sentence(nb_words=random.randint(4, 15))
                    await human_type(inp, text)
                    await asyncio.sleep(1)
                    inp.send_keys(Keys.ENTER)
                    db_inc_msg(sender)

            await asyncio.sleep(5)

        except Exception as e:
            logger.error(f"Err {sender}: {e}")
        finally:
            if driver: driver.quit()

async def farm_loop():
    logger.info("FARM STARTED")
    while True:
        accs = db_get_active_phones()
        if not accs:
            await asyncio.sleep(30)
            continue
            
        sender = random.choice(accs)
        # 40% Соло (безопасно), 60% Общение
        is_solo = random.random() < 0.4
        
        await process_account(sender, solo_mode=is_solo)
        
        delay = random.randint(FARM_DELAY_MIN, FARM_DELAY_MAX)
        logger.info(f"💤 Жду {delay} сек...")
        await asyncio.sleep(delay)

async def main():
    init_db()
    asyncio.create_task(farm_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
