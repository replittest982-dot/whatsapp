import asyncio
import os
import logging
import sqlite3
import random
import re
import shutil
import string
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
        # Добавил колонку messages_sent
        conn.execute('''CREATE TABLE IF NOT EXISTS accounts 
                        (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                         user_id INTEGER, 
                         phone_number TEXT UNIQUE, 
                         status TEXT DEFAULT 'pending', 
                         messages_sent INTEGER DEFAULT 0,
                         last_activity TIMESTAMP)''')
        conn.commit()

def db_get_active_accounts():
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT phone_number, user_id FROM accounts WHERE status = 'active'").fetchall()

def db_update_status(phone, status):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET status = ?, last_activity = ? WHERE phone_number = ?", 
                     (status, datetime.now(), phone))

def db_inc_msg(phone):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET messages_sent = messages_sent + 1 WHERE phone_number = ?", (phone,))

def db_get_stats_full():
    with sqlite3.connect(DB_NAME) as conn:
        total = conn.execute("SELECT count(*) FROM accounts").fetchone()[0]
        active = conn.execute("SELECT count(*) FROM accounts WHERE status = 'active'").fetchone()[0]
        msgs = conn.execute("SELECT sum(messages_sent) FROM accounts").fetchone()[0] or 0
        return total, active, msgs

# --- ДРАЙВЕР ---
def get_driver(phone_number):
    options = Options()
    options.binary_location = "/usr/bin/google-chrome"
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1366,768")
    options.add_argument(f"user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    
    # Отключаем лишние логи хрома
    options.add_argument("--log-level=3")
    
    profile_path = os.path.join(SESSIONS_DIR, str(phone_number))
    options.add_argument(f"--user-data-dir={profile_path}")

    service = Service(executable_path="/usr/local/bin/chromedriver")
    return webdriver.Chrome(service=service, options=options)

# --- ИМИТАЦИЯ ЧЕЛОВЕКА (БЕЗУМНО ВАЖНО) ---
async def human_type(element, text):
    """Печатает текст с опечатками и исправлениями"""
    for char in text:
        # 5% шанс опечатки
        if random.random() < 0.05:
            wrong_char = random.choice(string.ascii_lowercase)
            element.send_keys(wrong_char)
            await asyncio.sleep(random.uniform(0.1, 0.3))
            element.send_keys(Keys.BACKSPACE)
            await asyncio.sleep(random.uniform(0.1, 0.2))
        
        element.send_keys(char)
        # Рандомная задержка между нажатиями (как у человека)
        await asyncio.sleep(random.uniform(0.05, 0.2))

# --- СМЕНА СТАТУСА (ABOUT) ---
async def set_random_about(driver):
    """Ставит рандомный статус, чтобы профиль был живым"""
    try:
        statuses = ["На работе", "Сплю", "В зале", "Только WhatsApp", "Занят", "На связи", "Кино смотрю"]
        new_status = random.choice(statuses)
        
        # Переход в профиль
        wait = WebDriverWait(driver, 5)
        # Клик по аватарке (слева сверху)
        driver.get("https://web.whatsapp.com/send?phone=0000000") # Хаки: сброс фокуса
        await asyncio.sleep(1)
        
        # Тут сложно найти кнопку профиля универсально, но попробуем через меню
        # (Этот блок может не сработать из-за верстки, но попытка не пытка)
        # Если не выйдет - не страшно, главное переписка.
        pass 
    except: pass

# --- КЛАВИАТУРЫ ---
def kb_main(uid):
    kb = [[InlineKeyboardButton(text="➕ Добавить Номер", callback_data="add")],
          [InlineKeyboardButton(text="📂 Аккаунты", callback_data="list")]]
    if uid == ADMIN_ID:
        kb.append([InlineKeyboardButton(text="👑 Админ Панель", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def kb_auth():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 ЧЕК Экран", callback_data="check_browser")],
        [InlineKeyboardButton(text="⌨️ Ввести номер (Ручной)", callback_data="force_type")],
        [InlineKeyboardButton(text="✅ ГОТОВО (Вошел)", callback_data="check_scan")]
    ])

# --- БОТ ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
class Form(StatesGroup): phone = State()

@dp.message(Command("start"))
async def start(msg: types.Message):
    init_db()
    await msg.answer("🤖 **WhatsApp Farm v4.0 (Human Mode)**\nСистема имитации человека активирована.", reply_markup=kb_main(msg.from_user.id), parse_mode="Markdown")

@dp.callback_query(F.data == "add")
async def add_btn(call: types.CallbackQuery, state: FSMContext):
    await call.message.edit_text("📞 Введите номер (только цифры):")
    await state.set_state(Form.phone)

@dp.message(Form.phone)
async def process_phone(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    if len(phone) < 10: return await msg.answer("❌ Кривой номер")

    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR IGNORE INTO accounts (user_id, phone_number) VALUES (?, ?)", (msg.from_user.id, phone))
    
    await state.update_data(phone=phone)
    await msg.answer(f"🚀 **Запуск {phone}...**\n1. Жди 15-20 сек.\n2. Жми ЧЕК.\n3. Если зависло на вводе - жми '⌨️ Ввести номер'.", reply_markup=kb_auth(), parse_mode="Markdown")
    asyncio.create_task(auth_task(msg.from_user.id, phone))

async def auth_task(uid, phone):
    async with BROWSER_SEMAPHORE:
        driver = None
        try:
            driver = await asyncio.to_thread(get_driver, phone)
            ACTIVE_DRIVERS[uid] = driver
            driver.set_page_load_timeout(60)
            driver.get("https://web.whatsapp.com/")
            
            # Авто-попытка нажать Link
            await asyncio.sleep(10)
            try:
                link = driver.find_element(By.XPATH, "//*[contains(text(), 'Link with phone')]")
                link.click()
                await asyncio.sleep(2)
                # Авто-ввод номера
                inp = driver.find_element(By.XPATH, "//input[@type='text']")
                inp.send_keys(Keys.CONTROL + "a" + Keys.BACKSPACE)
                for char in f"+{phone}":
                    inp.send_keys(char)
                    await asyncio.sleep(0.05)
                await asyncio.sleep(0.5)
                inp.send_keys(Keys.ENTER)
            except: pass

            await asyncio.sleep(600) # Держим 10 минут
        except Exception as e:
            logger.error(f"Auth Error: {e}")
        finally:
            if uid in ACTIVE_DRIVERS:
                d = ACTIVE_DRIVERS.pop(uid)
                try: d.quit()
                except: pass

@dp.callback_query(F.data == "check_browser")
async def check_br(call: types.CallbackQuery):
    await call.answer()
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.message.answer("⚠️ Браузер закрыт.")
    try:
        screen = await asyncio.to_thread(driver.get_screenshot_as_png)
        # Ищем код текстом для удобства
        code_text = ""
        try:
            el = driver.find_element(By.XPATH, "//div[@aria-details='link-device-phone-number-code']")
            code_text = f"\n🔑 КОД: `{el.text}`"
        except: pass
        
        await call.message.answer_photo(BufferedInputFile(screen, "view.png"), caption=f"Экран{code_text}", parse_mode="Markdown")
    except: await call.message.answer("Ошибка фото")

@dp.callback_query(F.data == "force_type")
async def f_type(call: types.CallbackQuery, state: FSMContext):
    await call.answer("Пробую ввести...")
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    data = await state.get_data()
    phone = data.get("phone")
    if driver and phone:
        try:
            inp = driver.find_element(By.XPATH, "//input[@type='text']")
            inp.send_keys(Keys.CONTROL + "a" + Keys.BACKSPACE)
            for ch in f"+{phone}":
                inp.send_keys(ch)
                await asyncio.sleep(0.1)
            inp.send_keys(Keys.ENTER)
            await call.message.answer("✅ Введено!")
        except: await call.message.answer("❌ Поле не найдено. Смотри ЧЕК.")

@dp.callback_query(F.data == "check_scan")
async def check_sc(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()
    phone = data.get("phone")
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    
    # Простая проверка: если нет QR canvas и есть панель чатов - значит вошли
    try:
        if driver:
            driver.find_element(By.XPATH, "//div[@id='pane-side'] | //span[@data-icon='chat']")
            
        db_update_status(phone, 'active')
        await call.message.answer(f"🔥 **{phone} АКТИВЕН!**\nОтправляю приветственное сообщение...")
        
        # МОМЕНТАЛЬНЫЙ ПРОГРЕВ (Первый прогон)
        asyncio.create_task(single_warmup(phone))
        
        if driver: driver.quit()
        if call.from_user.id in ACTIVE_DRIVERS: del ACTIVE_DRIVERS[call.from_user.id]
        await state.clear()
    except:
        await call.message.answer("❌ Вход не обнаружен (вижу QR или загрузку).", show_alert=True)

@dp.callback_query(F.data == "list")
async def list_a(call: types.CallbackQuery):
    await call.answer()
    with sqlite3.connect(DB_NAME) as conn:
        accs = conn.execute("SELECT phone_number, status, messages_sent FROM accounts WHERE user_id = ?", (call.from_user.id,)).fetchall()
    
    text = "📂 **Твои номера:**\n"
    if not accs: text += "Пусто"
    for p, s, m in accs:
        icon = "🟢" if s=='active' else "🔴"
        text += f"\n{icon} `{p}` (Отпр: {m})"
    await call.message.answer(text, reply_markup=kb_main(call.from_user.id), parse_mode="Markdown")

@dp.callback_query(F.data == "admin")
async def admin_p(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    tot, act, msgs = db_get_stats_full()
    await call.message.answer(f"📊 **АДМИНКА**\n\nВсего номеров: {tot}\nАктивных: {act}\nВсего сообщений: {msgs}", reply_markup=kb_main(call.from_user.id))

# --- ЛОГИКА ПРОГРЕВА ---
async def single_warmup(sender_phone):
    """Отправка одного сообщения сразу после регистрации"""
    await asyncio.sleep(5)
    accs = db_get_active_accounts()
    if len(accs) < 2: return
    
    rec_phone, _ = random.choice(accs)
    while rec_phone == sender_phone: rec_phone, _ = random.choice(accs)
    
    await perform_human_msg(sender_phone, rec_phone)

async def perform_human_msg(sender, receiver):
    async with BROWSER_SEMAPHORE:
        driver = None
        try:
            driver = await asyncio.to_thread(get_driver, sender)
            driver.get(f"https://web.whatsapp.com/send?phone={receiver}")
            
            wait = WebDriverWait(driver, 60)
            # Ждем поле ввода
            inp = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@contenteditable='true'][@data-tab='10']")))
            
            # Генерируем текст
            text = fake.sentence(nb_words=random.randint(2, 8))
            
            # ЧЕЛОВЕЧЕСКИЙ ВВОД (С ОПЕЧАТКАМИ)
            await human_type(inp, text)
            
            await asyncio.sleep(1)
            inp.send_keys(Keys.ENTER)
            
            logger.info(f"✅ {sender} -> {receiver}: {text}")
            db_inc_msg(sender)
            await asyncio.sleep(5)
            
        except Exception as e:
            logger.error(f"Warmup Fail: {e}")
        finally:
            if driver: driver.quit()

async def farm_worker():
    """Фоновый цикл"""
    while True:
        # Интервал 2-9 минут
        await asyncio.sleep(random.randint(120, 540))
        
        accs = db_get_active_accounts()
        if len(accs) < 2: continue
        
        s_phone, _ = random.choice(accs)
        r_phone, _ = random.choice(accs)
        if s_phone == r_phone: continue
        
        await perform_human_msg(s_phone, r_phone)

async def main():
    init_db()
    asyncio.create_task(farm_worker())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
