import asyncio
import os
import logging
import sqlite3
import random
import shutil
import re
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from faker import Faker

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except:
    ADMIN_ID = 0

BROWSER_SEMAPHORE = asyncio.Semaphore(1) # Только 1 браузер за раз для RAM
DB_NAME = 'bot_database.db'
SESSIONS_DIR = "/app/sessions"

ACTIVE_DRIVERS = {} 
fake = Faker('ru_RU')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- БАЗА ДАННЫХ ---
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS accounts 
                        (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                         user_id INTEGER, 
                         phone_number TEXT UNIQUE, 
                         status TEXT DEFAULT 'pending', 
                         start_time TIMESTAMP,
                         last_activity TIMESTAMP)''')
        conn.commit()

def db_update_status(phone, status):
    with sqlite3.connect(DB_NAME) as conn:
        now = datetime.now()
        if status == 'active':
            conn.execute("UPDATE accounts SET status = ?, last_activity = ?, start_time = COALESCE(start_time, ?) WHERE phone_number = ?", 
                         (status, now, now, phone))
        else:
            conn.execute("UPDATE accounts SET status = ? WHERE phone_number = ?", (status, phone))

def db_add_pending(user_id, phone):
    with sqlite3.connect(DB_NAME) as conn:
        try:
            conn.execute("INSERT INTO accounts (user_id, phone_number, status, start_time) VALUES (?, ?, 'pending', ?)", 
                         (user_id, phone, datetime.now()))
        except sqlite3.IntegrityError:
            conn.execute("UPDATE accounts SET status = 'pending', start_time = ? WHERE phone_number = ?", 
                         (datetime.now(), phone))

def db_get_active():
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT phone_number, user_id, start_time FROM accounts WHERE status = 'active'").fetchall()

def db_get_stats():
    with sqlite3.connect(DB_NAME) as conn:
        total = conn.execute("SELECT count(*) FROM accounts").fetchone()[0]
        active = conn.execute("SELECT count(*) FROM accounts WHERE status = 'active'").fetchone()[0]
        dead = conn.execute("SELECT count(*) FROM accounts WHERE status = 'dead'").fetchone()[0]
        return total, active, dead

def db_get_user_accounts(user_id):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT phone_number, status FROM accounts WHERE user_id = ?", (user_id,)).fetchall()

# --- ЛОГИКА БРАУЗЕРА ---
def get_driver(phone_number=None):
    options = Options()
    CHROME_BINARIES = ["/usr/bin/google-chrome", "/opt/google/chrome/chrome"]
    found_path = next((p for p in CHROME_BINARIES if os.path.exists(p)), "/usr/bin/google-chrome")
    options.binary_location = found_path

    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage") 
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-setuid-sandbox")
    options.add_argument("--window-size=1366,768")
    
    EDGE_UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0'
    options.add_argument(f"user-agent={EDGE_UA}")
    options.add_argument("accept-language=en-US,en;q=0.9") 

    if phone_number:
        profile_path = os.path.join(SESSIONS_DIR, phone_number)
        options.add_argument(f"--user-data-dir={profile_path}")

    service = Service(executable_path="/usr/local/bin/chromedriver")
    return webdriver.Chrome(service=service, options=options)

# --- КЛАВИАТУРЫ ---
def kb_main(uid):
    kb = [[InlineKeyboardButton(text="➕ Добавить Аккаунт", callback_data="add")],
          [InlineKeyboardButton(text="📂 Мои Аккаунты", callback_data="list")]]
    if uid == ADMIN_ID:
        kb.append([InlineKeyboardButton(text="👑 Админ Панель", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def kb_auth_process():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 ЧЕК (Скриншот)", callback_data="check_browser")],
        [InlineKeyboardButton(text="✅ Проверить вход", callback_data="check_scan")]
    ])

# --- ХЕНДЛЕРЫ БОТА ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
class Form(StatesGroup): 
    wait_phone = State()

@dp.message(Command("start"))
async def start(msg: types.Message):
    await msg.answer(f"🤖 **WhatsApp Farm NL**", reply_markup=kb_main(msg.from_user.id), parse_mode="Markdown")

@dp.callback_query(F.data == "add")
async def add_start(call: types.CallbackQuery, state: FSMContext):
    await call.message.edit_text("📞 Введите номер (7XXXXXXXXXX):")
    await state.set_state(Form.wait_phone)

@dp.message(Form.wait_phone)
async def process_phone(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    if len(phone) < 10:
        await msg.answer("❌ Ошибка в номере")
        return
    
    db_add_pending(msg.from_user.id, phone)
    await state.update_data(phone=phone)
    
    await msg.answer(f"🚀 **Запуск браузера для {phone}...**\n\nЖми ЧЕК через 10 секунд для получения QR.", 
                     reply_markup=kb_auth_process(), parse_mode="Markdown")
    
    asyncio.create_task(bg_login_task(msg.from_user.id, phone))

async def bg_login_task(user_id, phone):
    async with BROWSER_SEMAPHORE:
        try:
            driver = await asyncio.to_thread(get_driver, phone)
            ACTIVE_DRIVERS[user_id] = driver
            driver.get("https://web.whatsapp.com/")
            await asyncio.sleep(8)
            # Пытаемся прожать вход по номеру для генерации QR в этом режиме
            try:
                btn = driver.find_element(By.XPATH, "//span[contains(text(), 'Link with phone')] | //div[contains(text(), 'Link with phone')]")
                driver.execute_script("arguments[0].click();", btn)
                await asyncio.sleep(2)
                inp = driver.find_element(By.XPATH, "//input[@type='text']")
                driver.execute_script(f"arguments[0].value = '+{phone}';", inp)
                driver.execute_script("arguments[0].dispatchEvent(new Event('input', { bubbles: true }));", inp)
                await asyncio.sleep(1)
                nxt = driver.find_element(By.XPATH, "//div[text()='Next']")
                driver.execute_script("arguments[0].click();", nxt)
            except: pass
        except Exception as e:
            logger.error(f"Ошибка фона: {e}")

@dp.callback_query(F.data == "check_browser")
async def check_browser(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver:
        await call.answer("⚠️ Браузер еще грузится...", show_alert=True)
        return
    try:
        screen = await asyncio.to_thread(driver.get_screenshot_as_png)
        await call.message.answer_photo(BufferedInputFile(screen, "view.png"), caption="👀 Сканируй QR!")
    except: await call.answer("Ошибка скриншота")

@dp.callback_query(F.data == "check_scan")
async def check_scan(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    phone = data.get("phone")
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver or not phone:
        await call.answer("Ошибка: сессия не найдена")
        return
    try:
        driver.find_element(By.XPATH, "//div[@id='pane-side']")
        db_update_status(phone, 'active')
        await call.message.answer(f"✅ **Аккаунт {phone} активен!**")
        driver.quit()
        del ACTIVE_DRIVERS[call.from_user.id]
        await state.clear()
    except:
        await call.answer("❌ QR еще не отсканирован!", show_alert=True)

@dp.callback_query(F.data == "list")
async def list_accs(call: types.CallbackQuery):
    accs = db_get_user_accounts(call.from_user.id)
    text = "📂 **Ваши аккаунты:**\n"
    if not accs: text += "Пусто."
    for p, s in accs:
        text += f"\n{'🟢' if s=='active' else '🔴'} `{p}`"
    try: await call.message.edit_text(text, reply_markup=kb_main(call.from_user.id), parse_mode="Markdown")
    except: await call.answer()

@dp.callback_query(F.data == "admin")
async def admin_stats(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    t, a, d = db_get_stats()
    text = f"📊 **СТАТИСТИКА**\n\nВсего: {t}\n🟢 Актив: {a}\n🔴 Слет: {d}"
    try: await call.message.edit_text(text, reply_markup=kb_main(call.from_user.id), parse_mode="Markdown")
    except: await call.answer()

# --- ЦИКЛ ПРОГРЕВА ---
async def farm_loop():
    while True:
        await asyncio.sleep(random.randint(300, 600)) # Раз в 5-10 минут
        accounts = db_get_active()
        if len(accounts) < 2: continue
        
        sender = random.choice(accounts)
        receiver = random.choice(accounts)
        if sender[0] == receiver[0]: continue
        
        async with BROWSER_SEMAPHORE:
            driver = None
            try:
                driver = await asyncio.to_thread(get_driver, sender[0])
                driver.get(f"https://web.whatsapp.com/send?phone={receiver[0]}")
                wait = WebDriverWait(driver, 40)
                inp = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@contenteditable='true'][@data-tab='10']")))
                msg = fake.sentence(nb_words=5)
                driver.execute_script(f"document.execCommand('insertText', false, '{msg}');", inp)
                await asyncio.sleep(1)
                driver.execute_script("arguments[0].dispatchEvent(new KeyboardEvent('keydown', {bubbles: true, keyCode: 13}));", inp)
                await asyncio.sleep(5)
                logger.info(f"Farm: {sender[0]} -> {receiver[0]}")
            except Exception as e:
                logger.error(f"Слет аккаунта {sender[0]}")
                db_update_status(sender[0], 'dead')
                try: await bot.send_message(sender[1], f"⚠️ Аккаунт {sender[0]} слетел!")
                except: pass
            finally:
                if driver: driver.quit()

async def main():
    init_db()
    asyncio.create_task(farm_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
