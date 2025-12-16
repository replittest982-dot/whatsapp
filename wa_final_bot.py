import asyncio
import os
import logging
import sqlite3
import random
import re
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
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except:
    ADMIN_ID = 0

BROWSER_SEMAPHORE = asyncio.Semaphore(1)
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
        conn.execute("INSERT OR REPLACE INTO accounts (user_id, phone_number, status, start_time) VALUES (?, ?, 'pending', ?)", 
                     (user_id, phone, datetime.now()))

def db_get_active_accounts_full():
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT phone_number, user_id FROM accounts WHERE status = 'active'").fetchall()

def db_get_user_accounts(user_id):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT phone_number, status FROM accounts WHERE user_id = ?", (user_id,)).fetchall()

def db_get_stats():
    with sqlite3.connect(DB_NAME) as conn:
        total = conn.execute("SELECT count(*) FROM accounts").fetchone()[0]
        active = conn.execute("SELECT count(*) FROM accounts WHERE status = 'active'").fetchone()[0]
        dead = conn.execute("SELECT count(*) FROM accounts WHERE status = 'dead'").fetchone()[0]
        return total, active, dead

# --- ДРАЙВЕР ---
def get_driver(phone_number=None):
    options = Options()
    options.binary_location = "/usr/bin/google-chrome"
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage") 
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1366,768")
    options.add_argument("user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    
    if phone_number:
        profile_path = os.path.join(SESSIONS_DIR, phone_number)
        options.add_argument(f"--user-data-dir={profile_path}")

    service = Service(executable_path="/usr/local/bin/chromedriver")
    return webdriver.Chrome(service=service, options=options)

# --- ЛОГИКА ВВОДА НОМЕРА (ВЫНЕСЕНА ОТДЕЛЬНО) ---
async def perform_typing(driver, phone):
    """Ищет поле и вводит номер"""
    try:
        wait = WebDriverWait(driver, 10)
        # Ищем поле ввода (любое текстовое поле на этом этапе)
        inp = wait.until(EC.element_to_be_clickable((By.XPATH, "//input[@aria-label='Type your phone number.'] | //input[@type='text']")))
        
        # 1. Агрессивная очистка
        driver.execute_script("arguments[0].click();", inp)
        inp.send_keys(Keys.CONTROL + "a")
        inp.send_keys(Keys.DELETE)
        await asyncio.sleep(0.5)
        
        # 2. Если не очистилось, пробуем Backspace много раз
        inp.send_keys(Keys.BACKSPACE * 20)
        
        # 3. Ввод
        full_phone = f"+{phone}"
        logger.info(f"Typing: {full_phone}")
        for char in full_phone:
            inp.send_keys(char)
            await asyncio.sleep(random.uniform(0.05, 0.15))
        
        await asyncio.sleep(1)
        inp.send_keys(Keys.ENTER)
        return True
    except Exception as e:
        logger.error(f"Typing error: {e}")
        return False

# --- СМЕНА ПРОФИЛЯ ---
def change_profile_info(driver):
    try:
        wait = WebDriverWait(driver, 10)
        profile_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//header//div[@role='button']//img")))
        profile_btn.click()
        # Тут логика сложная из-за верстки WA, оставим пока переход
    except: pass

# --- КЛАВИАТУРЫ ---
def kb_main(uid):
    kb = [[InlineKeyboardButton(text="➕ Добавить Аккаунт", callback_data="add")],
          [InlineKeyboardButton(text="📂 Мои Аккаунты", callback_data="list")]]
    if uid == ADMIN_ID:
        kb.append([InlineKeyboardButton(text="👑 Админ Панель", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def kb_auth_process():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 ЧЕК (Экран)", callback_data="check_browser")],
        [InlineKeyboardButton(text="🔗 Жми 'Вход по номеру'", callback_data="force_link")],
        [InlineKeyboardButton(text="⌨️ Ввести номер (Если пусто)", callback_data="force_type")],
        [InlineKeyboardButton(text="✅ Я вошел (Проверить)", callback_data="check_scan")]
    ])

# --- БОТ ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
class Form(StatesGroup): wait_phone = State()

@dp.message(Command("start"))
async def start(msg: types.Message):
    await msg.answer("🤖 **WhatsApp Farm Ultimate**", reply_markup=kb_main(msg.from_user.id), parse_mode="Markdown")

@dp.callback_query(F.data == "add")
async def add_start(call: types.CallbackQuery, state: FSMContext):
    await call.message.edit_text("📞 Введите номер (7XXXXXXXXXX):")
    await state.set_state(Form.wait_phone)

@dp.message(Form.wait_phone)
async def process_phone(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    if len(phone) < 10:
        await msg.answer("❌ Номер кривой.")
        return
    
    db_add_pending(msg.from_user.id, phone)
    await state.update_data(phone=phone)
    
    await msg.answer(
        f"🚀 **Запуск {phone}...**\n\n"
        "1. Бот сам попробует всё нажать.\n"
        "2. Если завис на вводе номера — жми **'⌨️ Ввести номер'**.\n"
        "3. Если QR — жми **'🔗 Вход по номеру'**.", 
        reply_markup=kb_auth_process(), parse_mode="Markdown"
    )
    asyncio.create_task(bg_login_task(msg.from_user.id, phone))

async def bg_login_task(user_id, phone):
    async with BROWSER_SEMAPHORE:
        driver = None
        try:
            driver = await asyncio.to_thread(get_driver, phone)
            ACTIVE_DRIVERS[user_id] = driver
            driver.set_page_load_timeout(60)
            driver.get("https://web.whatsapp.com/")
            
            # Авто-попытка нажать Link и ввести номер
            await asyncio.sleep(8)
            try:
                # Клик по ссылке
                btn = driver.find_element(By.XPATH, "//span[contains(text(), 'Link with phone')] | //div[contains(text(), 'Link with phone')]")
                btn.click()
                await asyncio.sleep(2)
                
                # Ввод номера
                await perform_typing(driver, phone)
            except: pass 
            
            # Держим 10 минут
            await asyncio.sleep(600) 
        except Exception as e:
            logger.error(f"BG Error: {e}")
        finally:
            if user_id in ACTIVE_DRIVERS:
                d = ACTIVE_DRIVERS.pop(user_id)
                try: d.quit()
                except: pass

@dp.callback_query(F.data == "force_link")
async def force_link_click(call: types.CallbackQuery, state: FSMContext):
    await call.answer("Нажимаю...")
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return
    
    try:
        btn = driver.find_element(By.XPATH, "//span[contains(text(), 'Link with phone')] | //div[contains(text(), 'Link with phone')]")
        driver.execute_script("arguments[0].click();", btn)
        await call.message.answer("✅ Нажал ссылку! Пробую ввести номер...")
        
        # Сразу пробуем печатать после клика
        await asyncio.sleep(2)
        data = await state.get_data()
        phone = data.get("phone")
        if phone:
            await perform_typing(driver, phone)
            
    except Exception as e:
        await call.message.answer(f"❌ Кнопка не найдена (уже нажата?).")

@dp.callback_query(F.data == "force_type")
async def force_type_click(call: types.CallbackQuery, state: FSMContext):
    """Принудительный ввод номера, если бот тупит на форме ввода"""
    await call.answer("⌨️ Печатаю номер...")
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    data = await state.get_data()
    phone = data.get("phone")
    
    if not driver or not phone:
        await call.message.answer("Ошибка: нет браузера или номера.")
        return
        
    success = await perform_typing(driver, phone)
    if success:
        await call.message.answer("✅ Номер введен! Жми ЧЕК, ищи код.")
    else:
        await call.message.answer("❌ Не нашел поле ввода! Сделай ЧЕК, посмотри где мы.")

@dp.callback_query(F.data == "check_browser")
async def check_browser(call: types.CallbackQuery):
    await call.answer()
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver:
        await call.message.answer("⚠️ Браузер не активен.")
        return
    try:
        screen = await asyncio.to_thread(driver.get_screenshot_as_png)
        code_txt = ""
        try:
            el = driver.find_element(By.XPATH, "//div[@aria-details='link-device-phone-number-code']")
            code_txt = f"\n\n🔑 **КОД:** `{el.text}`"
        except: pass
        
        caption = "👀 **Экран**" + code_txt
        await call.message.answer_photo(BufferedInputFile(screen, "status.png"), caption=caption, parse_mode="Markdown")
    except: await call.answer("Ошибка скрина")

@dp.callback_query(F.data == "check_scan")
async def check_scan(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()
    phone = data.get("phone")
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    
    if not driver: return

    try:
        driver.find_element(By.XPATH, "//div[@id='pane-side'] | //span[@data-icon='chat']")
        db_update_status(phone, 'active')
        await call.message.answer(f"✅ **Аккаунт {phone} в базе!**\n🔥 Начинаю моментальный прогрев...")
        
        asyncio.create_task(single_warmup_action(phone))
        try: change_profile_info(driver)
        except: pass
        
        driver.quit()
        if call.from_user.id in ACTIVE_DRIVERS: del ACTIVE_DRIVERS[call.from_user.id]
        await state.clear()
    except:
        await call.message.answer("❌ Вход не выполнен.", show_alert=True)

@dp.callback_query(F.data == "list")
async def list_accs(call: types.CallbackQuery):
    await call.answer()
    with sqlite3.connect(DB_NAME) as conn:
        accs = conn.execute("SELECT phone_number, status FROM accounts WHERE user_id = ?", (call.from_user.id,)).fetchall()
    text = "📂 **Аккаунты:**\n" + ("\n".join([f"{'🟢' if s=='active' else '🔴'} `{p}`" for p,s in accs]) if accs else "Пусто")
    await call.message.answer(text, reply_markup=kb_main(call.from_user.id), parse_mode="Markdown")

@dp.callback_query(F.data == "admin")
async def admin_panel(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    t, a, d = db_get_stats()
    await call.message.edit_text(f"📊 Всего: {t} | Актив: {a} | Слет: {d}", reply_markup=kb_main(call.from_user.id))

# --- ПРОГРЕВ ---
async def single_warmup_action(sender_phone):
    await asyncio.sleep(10)
    accounts = db_get_active_accounts_full()
    if len(accounts) < 2: return
    receiver = random.choice(accounts)
    while receiver[0] == sender_phone: receiver = random.choice(accounts)
    logger.info(f"🚀 IMMEDIATE WARMUP: {sender_phone} -> {receiver[0]}")
    await perform_warmup(sender_phone, receiver[0])

async def perform_warmup(sender_phone, receiver_phone):
    async with BROWSER_SEMAPHORE:
        driver = None
        try:
            driver = await asyncio.to_thread(get_driver, sender_phone)
            driver.get(f"https://web.whatsapp.com/send?phone={receiver_phone}")
            wait = WebDriverWait(driver, 45)
            
            inp = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@contenteditable='true'][@data-tab='10']")))
            msg = fake.sentence(nb_words=random.randint(3, 10))
            driver.execute_script(f"document.execCommand('insertText', false, '{msg}');", inp)
            await asyncio.sleep(1)
            inp.send_keys(Keys.ENTER)
            
            logger.info(f"✅ Sent: {msg}")
            await asyncio.sleep(5)
            
        except Exception as e:
            logger.error(f"Warmup Fail: {e}")
            db_update_status(sender_phone, 'dead')
        finally:
            if driver: driver.quit()

async def farm_loop():
    while True:
        sleep_time = random.randint(120, 540) # 2-9 минут
        await asyncio.sleep(sleep_time)
        
        accounts = db_get_active_accounts_full()
        if len(accounts) < 2: continue
        
        sender = random.choice(accounts)
        receiver = random.choice(accounts)
        if sender[0] == receiver[0]: continue
        
        logger.info(f"🔄 CYCLE WARMUP: {sender[0]} -> {receiver[0]}")
        await perform_warmup(sender[0], receiver[0])

async def main():
    init_db()
    asyncio.create_task(farm_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
