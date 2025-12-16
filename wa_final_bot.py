import asyncio
import os
import logging
import sqlite3
import random
import re
import string
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from faker import Faker

# --- БИБЛИОТЕКИ БРАУЗЕРА ---
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except:
    ADMIN_ID = 0

# Разрешаем 2 браузера одновременно, чтобы ты мог добавлять новый, пока другой работает
BROWSER_SEMAPHORE = asyncio.Semaphore(2) 
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

# --- НАСТРОЙКИ БРАУЗЕРА ---
def get_driver(phone_number):
    options = Options()
    options.binary_location = "/usr/bin/google-chrome"
    options.add_argument("--headless=new") 
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1366,768") # Важно для отображения кнопок
    options.add_argument(f"user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    options.add_argument("--log-level=3")
    
    # Папка профиля (чтобы сессия сохранялась)
    profile_path = os.path.join(SESSIONS_DIR, str(phone_number))
    options.add_argument(f"--user-data-dir={profile_path}")

    service = Service(executable_path="/usr/local/bin/chromedriver")
    return webdriver.Chrome(service=service, options=options)

# --- ИМИТАЦИЯ ЧЕЛОВЕКА (ПЕЧАТЬ) ---
async def human_type(element, text):
    """Печатает с опечатками"""
    for char in text:
        if random.random() < 0.04: # 4% шанс ошибки
            wrong = random.choice(string.ascii_lowercase)
            element.send_keys(wrong)
            await asyncio.sleep(random.uniform(0.1, 0.2))
            element.send_keys(Keys.BACKSPACE)
            await asyncio.sleep(random.uniform(0.05, 0.1))
        element.send_keys(char)
        await asyncio.sleep(random.uniform(0.05, 0.2))

# --- КНОПКИ ---
def kb_main(uid):
    kb = [[InlineKeyboardButton(text="➕ Добавить Аккаунт", callback_data="add")],
          [InlineKeyboardButton(text="📂 Список", callback_data="list")]]
    if uid == ADMIN_ID:
        kb.append([InlineKeyboardButton(text="📊 Админка", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def kb_auth_process():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 ЧЕК (Экран)", callback_data="check_browser")],
        [InlineKeyboardButton(text="🔗 Вход по номеру", callback_data="force_link")],
        [InlineKeyboardButton(text="⌨️ Ввести номер", callback_data="force_type")],
        [InlineKeyboardButton(text="✅ Я вошел (Сохранить)", callback_data="check_scan")]
    ])

# --- ЛОГИКА БОТА ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
class Form(StatesGroup): phone = State()

@dp.message(Command("start"))
async def start(msg: types.Message):
    init_db()
    await msg.answer("🤖 **Автобот для прогрева**\nГотов к работе.", reply_markup=kb_main(msg.from_user.id))

@dp.callback_query(F.data == "add")
async def add_start(call: types.CallbackQuery, state: FSMContext):
    await call.message.edit_text("📞 Введите номер (только цифры):")
    await state.set_state(Form.phone)

@dp.message(Form.phone)
async def process_phone(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    if len(phone) < 10: return await msg.answer("❌ Номер слишком короткий")
    
    # Сохраняем в БД
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR IGNORE INTO accounts (user_id, phone_number) VALUES (?, ?)", (msg.from_user.id, phone))
    
    await state.update_data(phone=phone)
    
    await msg.answer(
        f"🚀 **Запускаю {phone}...**\n\n"
        "1. Подожди 10-15 сек.\n"
        "2. Жми **ЧЕК**.\n"
        "3. Если кнопки 'Вход по номеру' нет на экране — нажми её тут в боте, я найду.", 
        reply_markup=kb_auth_process()
    )
    
    # Запускаем браузер
    asyncio.create_task(bg_login_task(msg.from_user.id, phone))

async def bg_login_task(user_id, phone):
    async with BROWSER_SEMAPHORE:
        driver = None
        try:
            # 1. Сначала создаем драйвер и кладем в словарь, ЧТОБЫ КНОПКИ РАБОТАЛИ
            driver = await asyncio.to_thread(get_driver, phone)
            ACTIVE_DRIVERS[user_id] = driver
            driver.set_page_load_timeout(60)
            
            # 2. Открываем сайт
            driver.get("https://web.whatsapp.com/")
            
            # 3. Держим браузер открытым 15 минут, чтобы ты успел все сделать
            # Бот будет висеть тут, пока ты жмешь кнопки
            await asyncio.sleep(900) 
            
        except Exception as e:
            logger.error(f"Login Error: {e}")
        finally:
            # Очистка только когда время вышло
            if user_id in ACTIVE_DRIVERS:
                d = ACTIVE_DRIVERS.pop(user_id)
                try: d.quit()
                except: pass

# --- РУЧНОЕ УПРАВЛЕНИЕ (КНОПКИ) ---

@dp.callback_query(F.data == "check_browser")
async def check_browser(call: types.CallbackQuery):
    await call.answer()
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.message.answer("⚠️ Браузер закрылся или не запустился. Начни заново.")
    
    try:
        screen = await asyncio.to_thread(driver.get_screenshot_as_png)
        # Пробуем найти код, если он вдруг есть
        code_txt = ""
        try:
            el = driver.find_element(By.XPATH, "//div[@aria-details='link-device-phone-number-code']")
            code_txt = f"\n🔑 КОД: `{el.text}`"
        except: pass
        
        await call.message.answer_photo(BufferedInputFile(screen, "view.png"), caption=f"Экран{code_txt}")
    except: await call.message.answer("Ошибка скриншота")

@dp.callback_query(F.data == "force_link")
async def force_link(call: types.CallbackQuery):
    await call.answer("Ищу кнопку...")
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return
    
    try:
        # Попытка 1: Обычная кнопка
        btn = driver.find_element(By.XPATH, "//span[contains(text(), 'Link with phone')] | //div[contains(text(), 'Link with phone')]")
        driver.execute_script("arguments[0].click();", btn)
        await call.message.answer("✅ Нажал! Жми '⌨️ Ввести номер'.")
    except:
        # Попытка 2: Если она спрятана в меню (бывает на узких экранах)
        try:
            menu = driver.find_element(By.XPATH, "//span[@data-icon='menu']")
            menu.click()
            await asyncio.sleep(1)
            btn = driver.find_element(By.XPATH, "//div[contains(text(), 'Link with phone')]")
            btn.click()
            await call.message.answer("✅ Нашел в меню и нажал!")
        except:
            await call.message.answer("❌ Не вижу кнопку. Попробуй обновить страницу (добавь номер заново).")

@dp.callback_query(F.data == "force_type")
async def force_type(call: types.CallbackQuery, state: FSMContext):
    await call.answer("Ввожу...")
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    data = await state.get_data()
    phone = data.get("phone")
    
    if not driver or not phone: return await call.message.answer("Ошибка данных.")

    try:
        inp = driver.find_element(By.XPATH, "//input[@aria-label='Type your phone number.'] | //input[@type='text']")
        # Жесткая очистка
        inp.click()
        inp.send_keys(Keys.CONTROL + "a")
        inp.send_keys(Keys.BACKSPACE)
        
        # Ввод
        for ch in f"+{phone}":
            inp.send_keys(ch)
            await asyncio.sleep(0.05)
        
        await asyncio.sleep(0.5)
        inp.send_keys(Keys.ENTER)
        await call.message.answer(f"✅ Ввел +{phone}. Жми ЧЕК, ищи код.")
    except:
        await call.message.answer("❌ Не нашел поле ввода. Сначала нажми '🔗 Вход по номеру'.")

@dp.callback_query(F.data == "check_scan")
async def check_scan(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()
    phone = data.get("phone")
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    
    try:
        # Если есть панель чатов - значит вошли
        if driver:
            driver.find_element(By.XPATH, "//div[@id='pane-side'] | //span[@data-icon='chat']")
        
        db_update_status(phone, 'active')
        await call.message.answer(f"✅ **{phone} СОХРАНЕН!**\nТеперь он участвует в диалогах.")
        
        # Моментальный прогрев (1 сообщение)
        asyncio.create_task(single_warmup(phone))
        
        # Закрываем браузер, сессия сохранена в папке
        if driver: driver.quit()
        if call.from_user.id in ACTIVE_DRIVERS: del ACTIVE_DRIVERS[call.from_user.id]
        await state.clear()
    except:
        await call.message.answer("❌ Вход не выполнен. Я не вижу чатов.")

@dp.callback_query(F.data == "list")
async def list_accs(call: types.CallbackQuery):
    await call.answer()
    with sqlite3.connect(DB_NAME) as conn:
        accs = conn.execute("SELECT phone_number, status, messages_sent FROM accounts WHERE user_id = ?", (call.from_user.id,)).fetchall()
    
    text = "📂 **Аккаунты:**\n"
    if not accs: text += "Нет аккаунтов"
    for p, s, m in accs:
        icon = "🟢" if s=='active' else "🔴"
        text += f"\n{icon} `{p}` (Сообщений: {m})"
    await call.message.answer(text, reply_markup=kb_main(call.from_user.id))

@dp.callback_query(F.data == "admin")
async def admin_panel(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    tot, act, msgs = db_get_stats_full()
    await call.message.answer(f"Статистика:\nВсего: {tot}\nАктив: {act}\nСмс: {msgs}")

# --- ФОНОВЫЙ ПРОГРЕВ (ДИАЛОГИ) ---
async def single_warmup(sender):
    """Шлет приветственное сообщение"""
    await asyncio.sleep(5)
    accs = db_get_active_accounts()
    if len(accs) < 2: return
    
    rec, _ = random.choice(accs)
    while rec == sender: rec, _ = random.choice(accs)
    
    await perform_msg(sender, rec)

async def perform_msg(sender, receiver):
    # Используем семафор, чтобы не грузить сервер при прогреве
    async with BROWSER_SEMAPHORE:
        driver = None
        try:
            driver = await asyncio.to_thread(get_driver, sender)
            driver.get(f"https://web.whatsapp.com/send?phone={receiver}")
            
            wait = WebDriverWait(driver, 45)
            inp = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@contenteditable='true'][@data-tab='10']")))
            
            text = fake.sentence(nb_words=random.randint(2, 7))
            await human_type(inp, text) # Печать как человек
            
            await asyncio.sleep(1)
            inp.send_keys(Keys.ENTER)
            
            logger.info(f"MSG: {sender} -> {receiver}")
            db_inc_msg(sender)
            await asyncio.sleep(3)
            
        except Exception as e:
            logger.error(f"Warmup Err: {e}")
        finally:
            if driver: driver.quit()

async def farm_loop():
    while True:
        await asyncio.sleep(random.randint(120, 400)) # 2-6 минут
        
        accs = db_get_active_accounts()
        if len(accs) < 2: continue
        
        s, _ = random.choice(accs)
        r, _ = random.choice(accs)
        if s == r: continue
        
        await perform_msg(s, r)

async def main():
    init_db()
    asyncio.create_task(farm_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
