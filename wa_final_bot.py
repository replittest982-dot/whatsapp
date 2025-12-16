import asyncio
import os
import logging
import sqlite3
import random
import re
import string
import shutil
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

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except:
    ADMIN_ID = 0

BROWSER_SEMAPHORE = asyncio.Semaphore(2)
DB_NAME = 'bot_database.db'
SESSIONS_DIR = "/app/sessions"

ACTIVE_DRIVERS = {} 
fake = Faker('ru_RU')

FARM_DELAY_MIN = 120
FARM_DELAY_MAX = 300

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- БАЗА ДАННЫХ ---
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS accounts 
                        (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                         user_id INTEGER, phone_number TEXT UNIQUE, 
                         status TEXT DEFAULT 'pending', 
                         messages_sent INTEGER DEFAULT 0)''')
        conn.commit()

def db_get_active():
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT phone_number FROM accounts WHERE status = 'active'").fetchall()

def db_inc_msg(phone):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET messages_sent = messages_sent + 1 WHERE phone_number = ?", (phone,))

# --- БРАУЗЕР ---
def get_driver(phone):
    opt = Options()
    opt.binary_location = "/usr/bin/google-chrome"
    opt.add_argument("--headless=new")
    opt.add_argument("--no-sandbox")
    opt.add_argument("--disable-dev-shm-usage")
    opt.add_argument("--window-size=1366,768")
    
    # Русский язык принудительно, чтобы кнопки были понятны
    opt.add_argument("--lang=ru")
    
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    opt.add_argument(f"user-agent={ua}")
    opt.add_argument("--log-level=3")
    opt.add_argument(f"--user-data-dir={os.path.join(SESSIONS_DIR, str(phone))}")
    
    return webdriver.Chrome(service=Service("/usr/local/bin/chromedriver"), options=opt)

# --- ИМИТАЦИЯ ---
async def human_type(element, text):
    for char in text:
        if random.random() < 0.05:
            element.send_keys(random.choice(string.ascii_lowercase))
            await asyncio.sleep(0.1)
            element.send_keys(Keys.BACKSPACE)
        element.send_keys(char)
        await asyncio.sleep(random.uniform(0.05, 0.2))

# --- КЛАВИАТУРЫ ---
def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить Аккаунт", callback_data="add")],
        [InlineKeyboardButton(text="📂 Статус Фермы", callback_data="list")],
        [InlineKeyboardButton(text="⚙️ Скорость", callback_data="settings")]
    ])

def kb_auth():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 ЧЕК", callback_data="check"),
         InlineKeyboardButton(text="✅ ГОТОВО", callback_data="done")],
        [InlineKeyboardButton(text="🔗 Вход по номеру (РУС/ENG)", callback_data="force_link")],
        [InlineKeyboardButton(text="⌨️ Ввести номер (Любое поле)", callback_data="force_type")]
    ])

def kb_settings():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 ТУРБО (1-3 мин)", callback_data="set_fast")],
        [InlineKeyboardButton(text="🚗 СРЕДНЕ (5-10 мин)", callback_data="set_mid")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu")]
    ])

# --- БОТ ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
class Form(StatesGroup): phone = State()

@dp.message(Command("start"))
async def start(msg: types.Message):
    init_db()
    await msg.answer("🤖 **WhatsApp Farm v6.0 (Ru-Fix)**\nТеперь понимаю русский интерфейс.", reply_markup=kb_main())

@dp.callback_query(F.data == "settings")
async def settings_menu(call: types.CallbackQuery):
    await call.message.edit_text("⚙️ Выбери скорость:", reply_markup=kb_settings())

@dp.callback_query(F.data.startswith("set_"))
async def set_speed(call: types.CallbackQuery):
    global FARM_DELAY_MIN, FARM_DELAY_MAX
    mode = call.data.split("_")[1]
    if mode == "fast": FARM_DELAY_MIN, FARM_DELAY_MAX = 60, 180
    else: FARM_DELAY_MIN, FARM_DELAY_MAX = 300, 600
    await call.message.edit_text("✅ Скорость изменена.", reply_markup=kb_main())

@dp.callback_query(F.data == "menu")
async def menu_back(call: types.CallbackQuery):
    await call.message.edit_text("Меню", reply_markup=kb_main())

@dp.callback_query(F.data == "add")
async def add_flow(call: types.CallbackQuery, state: FSMContext):
    await call.message.edit_text("📞 Введите номер (7XXXXXXXXXX):")
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
    async with BROWSER_SEMAPHORE:
        try:
            driver = await asyncio.to_thread(get_driver, phone)
            ACTIVE_DRIVERS[uid] = driver
            driver.get("https://web.whatsapp.com/")
            await asyncio.sleep(900)
        except: pass
        finally:
            if uid in ACTIVE_DRIVERS: ACTIVE_DRIVERS.pop(uid).quit()

# --- ЛОГИКА НАЖАТИЙ (ИСПРАВЛЕННАЯ) ---
@dp.callback_query(F.data == "check")
async def check(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Браузер закрыт.")
    try:
        scr = await asyncio.to_thread(driver.get_screenshot_as_png)
        code = ""
        try: 
            el = driver.find_element(By.XPATH, "//div[@aria-details='link-device-phone-number-code']")
            code = f"\nКОД: `{el.text}`"
        except: pass
        await call.message.answer_photo(BufferedInputFile(scr, "s.png"), caption=f"Экран{code}", parse_mode="Markdown")
    except: await call.answer("Ошибка фото")

@dp.callback_query(F.data == "force_link")
async def f_link(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return
    
    # Список всех возможных вариантов кнопки (Русский, Английский, Кнопка)
    xpaths = [
        "//span[contains(text(), 'Link with phone')]",
        "//span[contains(text(), 'Связать с номером')]",
        "//div[contains(text(), 'Link with phone')]",
        "//div[contains(text(), 'Связать с номером')]",
        "//span[@role='button']"
    ]
    
    found = False
    for xp in xpaths:
        try:
            btn = driver.find_element(By.XPATH, xp)
            driver.execute_script("arguments[0].click();", btn)
            found = True
            break
        except: continue
        
    if found: await call.answer("✅ Нажал!")
    else: await call.answer("❌ Не нашел кнопку. Проверь ЧЕК.")

@dp.callback_query(F.data == "force_type")
async def f_type(call: types.CallbackQuery, state: FSMContext):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    data = await state.get_data()
    if not driver or not data.get("phone"): return
    
    try:
        # Ищем ЛЮБОЕ поле input. Обычно на этой странице оно одно.
        inp = driver.find_element(By.TAG_NAME, "input")
        
        # Жесткая очистка через JS
        driver.execute_script("arguments[0].value = '';", inp)
        inp.send_keys(Keys.CONTROL + "a" + Keys.BACKSPACE)
        
        await call.answer("Ввожу...")
        for ch in f"+{data['phone']}":
            inp.send_keys(ch)
            await asyncio.sleep(0.05)
        inp.send_keys(Keys.ENTER)
        await call.message.answer("✅ Номер введен! Жми ЧЕК.")
    except Exception as e:
        await call.message.answer(f"❌ Не нашел поле ввода: {e}")

@dp.callback_query(F.data == "done")
async def done(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    phone = data.get("phone")
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET status = 'active' WHERE phone_number = ?", (phone,))
    
    if call.from_user.id in ACTIVE_DRIVERS:
        ACTIVE_DRIVERS.pop(call.from_user.id).quit()
    
    await call.message.answer(f"✅ {phone} готов! Прогрев запущен.")
    asyncio.create_task(single_warmup(phone))

@dp.callback_query(F.data == "list")
async def list_a(call: types.CallbackQuery):
    with sqlite3.connect(DB_NAME) as conn:
        accs = conn.execute("SELECT phone_number, status, messages_sent FROM accounts").fetchall()
    txt = "📊 **Ферма:**\n"
    for p, s, m in accs:
        txt += f"\n{'🟢' if s=='active' else '🔴'} `{p}` | Смс: {m}"
    await call.message.answer(txt, reply_markup=kb_main())

# --- ПРОГРЕВ ---
async def single_warmup(sender):
    await asyncio.sleep(5)
    accs = db_get_active()
    if len(accs) < 2: return
    targets = [a[0] for a in accs if a[0] != sender]
    if targets: await perform_msg(sender, random.choice(targets))

async def perform_msg(sender, receiver):
    async with BROWSER_SEMAPHORE:
        driver = None
        try:
            logger.info(f"MSG: {sender} -> {receiver}")
            driver = await asyncio.to_thread(get_driver, sender)
            driver.get(f"https://web.whatsapp.com/send?phone={receiver}")
            
            wait = WebDriverWait(driver, 60)
            inp = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@contenteditable='true'][@data-tab='10']")))
            
            text = fake.sentence(nb_words=random.randint(3, 10))
            await human_type(inp, text)
            await asyncio.sleep(1)
            inp.send_keys(Keys.ENTER)
            
            db_inc_msg(sender)
            await asyncio.sleep(5)
        except: pass
        finally:
            if driver: driver.quit()

async def farm_loop():
    while True:
        await asyncio.sleep(random.randint(FARM_DELAY_MIN, FARM_DELAY_MAX))
        accs = db_get_active()
        if len(accs) >= 2:
            s = random.choice(accs)[0]
            targets = [a[0] for a in accs if a[0] != s]
            if targets: await perform_msg(s, random.choice(targets))

async def main():
    init_db()
    asyncio.create_task(farm_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
