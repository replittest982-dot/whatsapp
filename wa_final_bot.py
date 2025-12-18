import asyncio
import os
import logging
import sqlite3
import random
import re
import shutil
import psutil
from datetime import datetime, timedelta
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

# --- КОНФИГ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0)) 
except:
    ADMIN_ID = 0

# Ссылка на группу (Троянский конь)
GROUP_INVITE_LINK = "https://chat.whatsapp.com/KtKFYIMlAmSH8U0OKhWI8f?mode=hqrt2"

# Лимиты
BROWSER_SEMAPHORE = asyncio.Semaphore(3)
DB_NAME = 'bot_database.db'
SESSIONS_DIR = "./sessions"

ACTIVE_DRIVERS = {}
fake = Faker('ru_RU')

# Тайминги
FARM_DELAY_MIN = 120
FARM_DELAY_MAX = 300
GROUP_DELAY_MIN = 1500 # 25 мин
GROUP_DELAY_MAX = 2700 # 45 мин

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("WA_FARM_FIX")

# --- БАЗА ДАННЫХ ---
def init_db():
    with sqlite3.connect(DB_NAME, timeout=10) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute('''CREATE TABLE IF NOT EXISTS accounts 
                        (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                         user_id INTEGER, phone_number TEXT UNIQUE, 
                         status TEXT DEFAULT 'pending', 
                         messages_sent INTEGER DEFAULT 0,
                         user_agent TEXT, resolution TEXT, platform TEXT,
                         ban_reason TEXT, last_active TIMESTAMP,
                         last_group_msg TIMESTAMP)''')

def db_get_active_phones():
    with sqlite3.connect(DB_NAME) as conn:
        return [row[0] for row in conn.execute("SELECT phone_number FROM accounts WHERE status = 'active'").fetchall()]

def db_update_status(phone, status):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET status = ? WHERE phone_number = ?", (status, phone))

def db_record_activity(phone, is_group=False):
    with sqlite3.connect(DB_NAME) as conn:
        now = datetime.now()
        if is_group:
            conn.execute("UPDATE accounts SET messages_sent = messages_sent + 1, last_active = ?, last_group_msg = ? WHERE phone_number = ?", (now, now, phone))
        else:
            conn.execute("UPDATE accounts SET messages_sent = messages_sent + 1, last_active = ? WHERE phone_number = ?", (now, phone))

def db_check_group_cooldown(phone):
    with sqlite3.connect(DB_NAME) as conn:
        row = conn.execute("SELECT last_group_msg FROM accounts WHERE phone_number = ?", (phone,)).fetchone()
        if not row or not row[0]: return True
        last = datetime.fromisoformat(row[0])
        interval = random.randint(GROUP_DELAY_MIN, GROUP_DELAY_MAX)
        return (datetime.now() - last).total_seconds() > interval

# --- SYSTEM GUARD ---
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

# --- SELENIUM ---
def get_driver(phone):
    if not is_memory_safe(): return None
    path = os.path.join(SESSIONS_DIR, str(phone))
    if not os.path.exists(path): os.makedirs(path)

    acc = None
    with sqlite3.connect(DB_NAME) as conn:
        acc = conn.execute("SELECT * FROM accounts WHERE phone_number = ?", (phone,)).fetchone()
    
    if acc and acc[5]:
        ua, res, plat = acc[5], acc[6], acc[7]
    else:
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
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

    try:
        driver = webdriver.Chrome(options=opt)
        return driver
    except: return None

async def human_type(element, text):
    for char in text:
        element.send_keys(char)
        await asyncio.sleep(random.uniform(0.05, 0.2))

# --- BOT & UI ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
class Form(StatesGroup): phone = State()

def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить", callback_data="add"),
         InlineKeyboardButton(text="📂 Список", callback_data="list")]
    ])

# ИСПРАВЛЕННЫЕ КНОПКИ
def kb_auth_classic():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 ЧЕК", callback_data="check"),
         InlineKeyboardButton(text="✅ ГОТОВО", callback_data="done")],
        [InlineKeyboardButton(text="🔗 Вход по номеру (1)", callback_data="force_link")],
        [InlineKeyboardButton(text="⌨️ Ввести номер (2)", callback_data="force_type")],
        [InlineKeyboardButton(text="🔑 ПОЛУЧИТЬ КОД (3)", callback_data="force_code")] # НОВАЯ КНОПКА
    ])

@dp.message(Command("start"))
async def start(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return 
    init_db()
    await msg.answer("🔥 **WA Farm: Fix Edition**\nКнопки исправлены.\nЖми по порядку: 1 -> 2 -> 3.", reply_markup=kb_main())

@dp.callback_query(F.data == "add")
async def add_flow(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID: return
    if call.from_user.id in ACTIVE_DRIVERS:
        try: ACTIVE_DRIVERS.pop(call.from_user.id).quit()
        except: pass
        
    await call.message.edit_text("Введи номер (7XXXXXXXXXX):")
    await state.set_state(Form.phone)

@dp.message(Form.phone)
async def proc_phone(msg: types.Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID: return
    
    phone = re.sub(r'\D', '', msg.text)
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR IGNORE INTO accounts (user_id, phone_number) VALUES (?, ?)", (msg.from_user.id, phone))
    await state.update_data(phone=phone)
    
    await msg.answer(
        f"🚀 Запускаю {phone}...\nЖми кнопки строго по порядку!", 
        reply_markup=kb_auth_classic()
    )
    asyncio.create_task(bg_login(msg.from_user.id, phone))

async def bg_login(uid, phone):
    async with BROWSER_SEMAPHORE:
        driver = await asyncio.to_thread(get_driver, phone)
        if not driver: return
        ACTIVE_DRIVERS[uid] = driver
        try:
            driver.get("https://web.whatsapp.com/")
            await asyncio.sleep(600) 
        except: pass
        finally:
            if uid in ACTIVE_DRIVERS: ACTIVE_DRIVERS.pop(uid).quit()

# --- ЛОГИКА КНОПОК ---
@dp.callback_query(F.data == "check")
async def check(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Браузер закрыт", show_alert=True)
    try:
        scr = driver.get_screenshot_as_png()
        await call.message.answer_photo(BufferedInputFile(scr, "s.png"), caption="Экран")
    except: await call.answer("Ошибка скрина")

@dp.callback_query(F.data == "force_link")
async def f_link(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Браузер закрыт", show_alert=True)
    
    await call.message.answer("1. Нажимаю 'Вход по номеру'...")
    try:
        # Пытаемся нажать всеми способами
        xpaths = [
            "//span[contains(text(), 'Link with phone')]", 
            "//a[contains(@href, 'link-device')]", 
            "//span[contains(text(), 'Связать с номером')]",
            "//div[@role='button']//div[contains(text(), 'Link')]"
        ]
        clicked = False
        for xp in xpaths:
            try:
                el = driver.find_element(By.XPATH, xp)
                el.click()
                clicked = True
                break
            except: continue
        
        if clicked: await call.message.answer("✅ Нажал! Теперь жми 'Ввести номер'.")
        else: await call.message.answer("⚠️ Не нашел кнопку. Проверь ЧЕК, может уже нажато?")
    except: pass

@dp.callback_query(F.data == "force_type")
async def f_type(call: types.CallbackQuery, state: FSMContext):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    data = await state.get_data()
    phone = data.get("phone")
    if not driver: return await call.answer("Браузер закрыт", show_alert=True)
    
    await call.message.answer(f"2. Ввожу {phone} и жму Enter...")
    try:
        # Ищем поле
        inp = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.XPATH, "//input[@aria-label='Type your phone number.'] | //input[@type='text']")))
        
        # Чистим и вводим
        driver.execute_script("arguments[0].value = '';", inp)
        inp.send_keys(Keys.CONTROL + "a" + Keys.BACKSPACE)
        for ch in phone: inp.send_keys(ch); await asyncio.sleep(0.05)
        
        # ЖМЕМ ENTER
        inp.send_keys(Keys.ENTER)
        await asyncio.sleep(1)
        
        # АГРЕССИВНО ИЩЕМ И ЖМЕМ КНОПКУ "ДАЛЕЕ" (NEXT)
        # Иногда Enter не срабатывает, нужно кликнуть мышкой
        try:
            next_btns = driver.find_elements(By.XPATH, "//div[text()='Next'] | //div[text()='Далее'] | //button/div[contains(text(), 'Next')]")
            for btn in next_btns:
                try: 
                    btn.click()
                    await call.message.answer("🖱 Кликнул кнопку 'Далее'!")
                except: pass
        except: pass

        await call.message.answer("✅ Ввел! Подожди 2-3 сек и жми 'ПОЛУЧИТЬ КОД'.")
    except Exception as e:
        await call.message.answer(f"❌ Ошибка ввода: {e}")

@dp.callback_query(F.data == "force_code")
async def f_code(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Браузер закрыт", show_alert=True)
    
    try:
        # Ищем контейнер с кодом
        code_el = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.XPATH, "//div[@aria-details='link-device-phone-number-code']")))
        code_text = code_el.text
        
        # Делаем скрин
        scr = driver.get_screenshot_as_png()
        await call.message.answer_photo(BufferedInputFile(scr, "code.png"), caption=f"🔑 **КОД:** `{code_text}`", parse_mode="Markdown")
    except:
        # Если кода нет - шлем скрин ошибки
        scr = driver.get_screenshot_as_png()
        await call.message.answer_photo(BufferedInputFile(scr, "err.png"), caption="❌ Код пока не появился. Проверь скрин, может WhatsApp грузится?")

@dp.callback_query(F.data == "done")
async def done(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    phone = data.get("phone")
    if call.from_user.id in ACTIVE_DRIVERS:
        ACTIVE_DRIVERS.pop(call.from_user.id).quit()
    db_update_status(phone, 'active')
    await call.message.answer(f"✅ {phone} сохранен в базу!")

@dp.callback_query(F.data == "list")
async def list_a(call: types.CallbackQuery):
    accs = db_get_active_phones()
    txt = f"Активных: {len(accs)}\n" + "\n".join([f"🟢 {a}" for a in accs])
    if not accs: txt = "Пусто"
    await call.message.edit_text(txt, reply_markup=kb_main())

# --- ФАРМ (ГРУППА) ---
async def farm_worker(phone):
    if not is_memory_safe(): return
    
    async with BROWSER_SEMAPHORE:
        driver = await asyncio.to_thread(get_driver, phone)
        if not driver: return
        try:
            driver.get("https://web.whatsapp.com/")
            wait = WebDriverWait(driver, 60)
            try: wait.until(EC.presence_of_element_located((By.ID, "pane-side")))
            except: return 

            if db_check_group_cooldown(phone):
                try:
                    code = GROUP_INVITE_LINK.split("whatsapp.com/")[1].split("?")[0]
                    driver.get(f"https://web.whatsapp.com/accept?code={code}")
                    try:
                        join = WebDriverWait(driver, 8).until(EC.element_to_be_clickable((By.XPATH, "//div[contains(text(), 'Вступить') or contains(text(), 'Join')]")))
                        join.click()
                        await asyncio.sleep(5)
                    except: pass
                    
                    inp = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@contenteditable='true'][@data-tab='10'] | //footer//div[@role='textbox']")))
                    await human_type(inp, fake.sentence())
                    inp.send_keys(Keys.ENTER)
                    db_record_activity(phone, is_group=True)
                except: pass
            else:
                driver.get(f"https://web.whatsapp.com/send?phone={phone}")
                try:
                    inp = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@contenteditable='true'][@data-tab='10'] | //footer//div[@role='textbox']")))
                    await human_type(inp, f"Status: {fake.word()}")
                    inp.send_keys(Keys.ENTER)
                    db_record_activity(phone, is_group=False)
                except: pass

            await asyncio.sleep(5)
        except: pass
        finally: driver.quit()

async def farm_loop():
    asyncio.create_task(zombie_killer())
    while True:
        phones = db_get_active_phones()
        if phones:
            p = random.choice(phones)
            asyncio.create_task(farm_worker(p))
        await asyncio.sleep(random.randint(FARM_DELAY_MIN, FARM_DELAY_MAX))

async def main():
    init_db()
    asyncio.create_task(farm_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
