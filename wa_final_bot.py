import asyncio
import os
import logging
import sqlite3
import random
import re
import shutil
import psutil
import traceback
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
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN") 
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except:
    ADMIN_ID = 0

# Лимиты
BROWSER_SEMAPHORE = asyncio.Semaphore(3)
DB_NAME = 'bot_database.db'
SESSIONS_DIR = "./sessions"
LOG_DIR = "./logs"

ACTIVE_DRIVERS = {}
fake = Faker('ru_RU')

# Тайминги (сек)
FARM_DELAY_MIN = 120
FARM_DELAY_MAX = 300

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("WA_FARM_PRO")

# --- DATABASE ---
def init_db():
    with sqlite3.connect(DB_NAME, timeout=10) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
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

def db_get_acc(phone):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT * FROM accounts WHERE phone_number = ?", (phone,)).fetchone()

def db_get_active_phones():
    with sqlite3.connect(DB_NAME) as conn:
        return [row[0] for row in conn.execute("SELECT phone_number FROM accounts WHERE status = 'active'").fetchall()]

def db_update_status(phone, status, reason=None):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET status = ?, ban_reason = ?, last_active = ? WHERE phone_number = ?", 
                     (status, reason, datetime.now(), phone))

def db_inc_msg(phone):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET messages_sent = messages_sent + 1, last_active = ? WHERE phone_number = ?", 
                     (datetime.now(), phone))

# --- SYSTEM HEALTH ---
def is_memory_safe():
    try:
        mem = psutil.virtual_memory().available / (1024 * 1024)
        if mem < 200: return False
        return True
    except: return True

async def zombie_killer():
    while True:
        await asyncio.sleep(120)
        for proc in psutil.process_iter(['pid', 'name', 'create_time']):
            try:
                if 'chrome' in proc.info['name']:
                    if (datetime.now().timestamp() - proc.info['create_time']) > 1800:
                        proc.kill()
            except: pass

# --- NOTIFICATIONS (НОВОЕ) ---
async def alert_admin(text):
    """Шлет уведомление админу"""
    if ADMIN_ID == 0: return
    try: await bot.send_message(ADMIN_ID, text)
    except: pass

# --- BROWSER ---
def get_driver(phone):
    if not is_memory_safe(): return None
    path = os.path.join(SESSIONS_DIR, str(phone))
    if not os.path.exists(path): os.makedirs(path)

    acc = db_get_acc(phone)
    if acc and acc[5]:
        ua, res, plat = acc[5], acc[6], acc[7]
    else:
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        res, plat = "1920,1080", "Win32"
        with sqlite3.connect(DB_NAME) as conn:
            conn.execute("UPDATE accounts SET user_agent=?, resolution=?, platform=? WHERE phone_number=?", (ua, res, plat, phone))

    opt = Options()
    opt.add_argument("--headless=new")
    opt.add_argument("--no-sandbox")
    opt.add_argument("--disable-dev-shm-usage")
    opt.add_argument(f"--window-size={res}")
    opt.add_argument("--lang=ru-KZ")
    opt.add_argument(f"user-agent={ua}")
    opt.add_argument(f"--user-data-dir={path}")
    opt.page_load_strategy = 'eager'
    
    # Anti-detect
    opt.add_argument("--disable-blink-features=AutomationControlled")
    opt.add_experimental_option("excludeSwitches", ["enable-automation"])
    opt.add_experimental_option('useAutomationExtension', False)

    try:
        driver = webdriver.Chrome(options=opt)
        return driver
    except: return None

async def human_type(element, text):
    for char in text:
        element.send_keys(char)
        await asyncio.sleep(random.uniform(0.05, 0.2))

def get_screenshot(driver):
    try: return driver.get_screenshot_as_png()
    except: return None

# --- UI ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
class Form(StatesGroup): phone = State()

def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить Аккаунт", callback_data="add")],
        [InlineKeyboardButton(text="📂 Список Аккаунтов", callback_data="list")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats")]
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
    if msg.from_user.id != ADMIN_ID: return # ЗАЩИТА АДМИНА
    init_db()
    if not os.path.exists(SESSIONS_DIR): os.makedirs(SESSIONS_DIR)
    await msg.answer("🔥 **WA Farm Pro: Mass Edition**\nУведомления включены.\nМассовая рассылка активна.", reply_markup=kb_main())

@dp.callback_query(F.data == "stats")
async def stats(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    mem = psutil.virtual_memory()
    phones = db_get_active_phones()
    
    # Считаем общее кол-во смс
    total_sms = 0
    with sqlite3.connect(DB_NAME) as conn:
        res = conn.execute("SELECT SUM(messages_sent) FROM accounts").fetchone()
        if res and res[0]: total_sms = res[0]

    txt = (f"🖥 **Server Stats:**\n"
           f"👥 Активных номеров: {len(phones)}\n"
           f"✉️ Отправлено SMS: {total_sms}\n"
           f"🧠 RAM Free: {mem.available // 1024 // 1024} MB\n"
           f"⚙️ Потоков: 3")
    await call.answer(txt, show_alert=True)

@dp.callback_query(F.data == "add")
async def add_start(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID: return
    
    uid = call.from_user.id
    if uid in ACTIVE_DRIVERS:
        try: ACTIVE_DRIVERS[uid].quit()
        except: pass
        del ACTIVE_DRIVERS[uid]
        
    await call.message.edit_text("Введи номер (7XXXXXXXXXX):")
    await state.set_state(Form.phone)

@dp.message(Form.phone)
async def add_phone(msg: types.Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID: return
    
    phone = re.sub(r'\D', '', msg.text)
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR IGNORE INTO accounts (user_id, phone_number) VALUES (?, ?)", (msg.from_user.id, phone))
    
    await state.update_data(phone=phone)
    await msg.answer(f"⏳ Запускаю браузер для **{phone}**...\nЖди 15-30 сек.", reply_markup=kb_auth())
    asyncio.create_task(bg_open_browser(msg.from_user.id, phone))

async def bg_open_browser(uid, phone):
    async with BROWSER_SEMAPHORE:
        try:
            driver = await asyncio.to_thread(get_driver, phone)
            ACTIVE_DRIVERS[uid] = driver
            driver.get("https://web.whatsapp.com/")
            await asyncio.sleep(600)
        except: pass
        finally:
            if uid in ACTIVE_DRIVERS:
                try: ACTIVE_DRIVERS[uid].quit()
                except: pass
                del ACTIVE_DRIVERS[uid]

@dp.callback_query(F.data == "check")
async def check_screen(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("❌ Браузер закрыт. Добавь заново.", show_alert=True)
    
    await call.answer("📸 Скрин...")
    scr = get_screenshot(driver)
    if scr: await call.message.answer_photo(BufferedInputFile(scr, "screen.png"), caption="Экран")
    else: await call.message.answer("Ошибка скрина.")

@dp.callback_query(F.data == "link_phone")
async def link_phone_pro(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID: return
    uid = call.from_user.id
    driver = ACTIVE_DRIVERS.get(uid)
    data = await state.get_data()
    phone = data.get('phone')
    if not driver: return await call.answer("❌ Браузер закрыт.", show_alert=True)
    
    await call.message.answer("🕵️‍♂️ Жму кнопки...")
    try:
        # 1. Жмем Link
        xpaths = ["//span[contains(text(), 'Link with phone')]", "//a[contains(@href, 'link-device')]", "//span[contains(text(), 'Связать с номером')]"]
        for xp in xpaths:
            try:
                driver.find_element(By.XPATH, xp).click()
                break
            except: continue
        await asyncio.sleep(2)
        
        # 2. Вводим номер
        inp = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.XPATH, "//input[@aria-label='Type your phone number.'] | //input[@type='text']")))
        driver.execute_script("arguments[0].value = '';", inp)
        inp.send_keys(Keys.CONTROL + "a" + Keys.BACKSPACE)
        for ch in phone: inp.send_keys(ch); await asyncio.sleep(0.05)
        inp.send_keys(Keys.ENTER)
        
        # 3. Код
        await asyncio.sleep(3)
        code_el = WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.XPATH, "//div[@aria-details='link-device-phone-number-code']")))
        await call.message.answer(f"🔑 **КОД:** `{code_el.text}`", parse_mode="Markdown")
        
    except Exception as e:
        await call.message.answer(f"❌ Ошибка (пробуй QR): {e}")

@dp.callback_query(F.data == "hard_reset")
async def hard_reset(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID: return
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
        await call.answer("🗑 Удалено.", show_alert=True)
        await add_start(call, state)

@dp.callback_query(F.data == "done")
async def auth_done(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID: return
    data = await state.get_data()
    phone = data.get('phone')
    
    uid = call.from_user.id
    if uid in ACTIVE_DRIVERS:
        ACTIVE_DRIVERS[uid].quit()
        del ACTIVE_DRIVERS[uid]
        
    db_update_status(phone, 'active')
    await call.message.edit_text(f"✅ {phone} добавлен в базу!")

@dp.callback_query(F.data == "list")
async def list_accs(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    with sqlite3.connect(DB_NAME) as conn:
        accs = conn.execute("SELECT phone_number, status, messages_sent FROM accounts").fetchall()
    
    text = "📂 **Аккаунты:**\n\n"
    if not accs: text += "Пусто."
    
    for p, s, m in accs:
        icon = "🟢" if s == 'active' else "🔴"
        text += f"{icon} `{p}` | SMS: {m}\n"
    await call.message.edit_text(text, reply_markup=kb_main(), parse_mode="Markdown")

# --- УЛУЧШЕННЫЙ ФАРМЕР (MASS DM + NOTIFY) ---
async def farm_worker(sender_phone):
    """Логика: Если есть другие акки - пишем им. Если нет - себе."""
    if not is_memory_safe(): return
    
    logger.info(f"🚜 Worker: {sender_phone}")
    driver = None
    try:
        driver = await asyncio.to_thread(get_driver, sender_phone)
        if not driver: return # Не удалось открыть
        
        try:
            driver.get("https://web.whatsapp.com/")
        except TimeoutException:
            driver.quit(); return

        wait = WebDriverWait(driver, 40)
        
        # ПРОВЕРКА НА БАН/СЛЕТ
        try:
            wait.until(EC.presence_of_element_located((By.ID, "pane-side")))
        except:
            src = driver.page_source.lower()
            if "account is not allowed" in src or "spam" in src:
                db_update_status(sender_phone, 'banned', 'PermBan')
                await alert_admin(f"🚫 **BAN ALERT:** Аккаунт {sender_phone} забанен!")
            elif "link with phone" in src or "qr code" in src:
                db_update_status(sender_phone, 'pending')
                await alert_admin(f"⚠️ **LOGOUT:** Аккаунт {sender_phone} слетел. Нужен перевход.")
            driver.quit()
            return

        # ВЫБОР ЦЕЛИ (MASS DM)
        active_phones = db_get_active_phones()
        target_phone = sender_phone # По дефолту пишем себе
        
        # Если есть другие аккаунты, с шансом 80% пишем им
        if len(active_phones) > 1 and random.random() < 0.8:
            candidates = [p for p in active_phones if p != sender_phone]
            if candidates:
                target_phone = random.choice(candidates)

        # ОТПРАВКА
        driver.get(f"https://web.whatsapp.com/send?phone={target_phone}")
        
        inp_xpath = "//div[@contenteditable='true'][@data-tab='10'] | //footer//div[@role='textbox']"
        try:
            inp = wait.until(EC.presence_of_element_located((By.XPATH, inp_xpath)))
            
            phrase = fake.sentence()
            await human_type(inp, phrase)
            await asyncio.sleep(1)
            inp.send_keys(Keys.ENTER)
            
            db_inc_msg(sender_phone)
            logger.info(f"✅ {sender_phone} -> {target_phone}: {phrase}")
            
        except:
            logger.warning(f"Не нашел поле ввода для {sender_phone}")

        await asyncio.sleep(5)
        
    except Exception as e:
        logger.error(f"Farm Error {sender_phone}: {e}")
    finally:
        if driver:
            try: driver.quit()
            except: pass

async def farm_loop():
    logger.info("🚜 Farm Loop Started")
    asyncio.create_task(zombie_killer())
    
    while True:
        phones = db_get_active_phones()
        if not phones:
            await asyncio.sleep(60)
            continue
            
        target = random.choice(phones)
        asyncio.create_task(farm_worker(target))
        
        delay = random.randint(FARM_DELAY_MIN, FARM_DELAY_MAX)
        await asyncio.sleep(delay)

async def main():
    if not BOT_TOKEN:
        print("❌ ERROR: BOT_TOKEN is missing!")
        return
    if ADMIN_ID == 0:
        print("⚠️ WARNING: ADMIN_ID not set! Notifications won't work.")

    init_db()
    asyncio.create_task(farm_loop())
    print("🚀 Bot Started!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped.")
