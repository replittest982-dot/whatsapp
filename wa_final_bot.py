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

# --- БИБЛИОТЕКИ БРАУЗЕРА ---
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

# Разрешаем 2 потока (добавление + прогрев одновременно)
BROWSER_SEMAPHORE = asyncio.Semaphore(2)
DB_NAME = 'bot_database.db'
SESSIONS_DIR = "/app/sessions"

ACTIVE_DRIVERS = {} 
fake = Faker('ru_RU')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- СИСТЕМА ЛИЧНОСТЕЙ (ДЛЯ ЖИВОГО ОБЩЕНИЯ) ---
PERSONALITIES = {
    "student": [
        "Скинь домашку", "Я проспал, отметь", "Когда сессия?", "Го в столовку", 
        "Препод жестит сегодня", "Стипуха пришла?", "Есть конспект?"
    ],
    "gamer": [
        "Го в катку", "Заходи в дискорд", "Там обнова вышла", "Стим лежит", 
        "Я вчера тащил жестко", "Купил новую игруху", "Ну что там, рашим Б?"
    ],
    "worker": [
        "Когда зарплата?", "Отчет готов?", "Начальник достал", "Хочу в отпуск", 
        "Давай на перекур", "В пятницу по пиву?", "Я задержусь сегодня"
    ],
    "normie": [
        "Как дела?", "Что нового?", "Давно не виделись", "Скинь фотки", 
        "Какая погода у вас?", "Надо встретиться", "С добрым утром!"
    ]
}

def get_message_by_role(role):
    if role not in PERSONALITIES: role = "normie"
    return random.choice(PERSONALITIES[role])

# --- БАЗА ДАННЫХ ---
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS accounts 
                        (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                         user_id INTEGER, 
                         phone_number TEXT UNIQUE, 
                         status TEXT DEFAULT 'pending', 
                         role TEXT DEFAULT 'normie',
                         messages_sent INTEGER DEFAULT 0,
                         created_at TIMESTAMP)''')
        conn.commit()

def db_add_account(uid, phone):
    role = random.choice(list(PERSONALITIES.keys()))
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR IGNORE INTO accounts (user_id, phone_number, role, created_at) VALUES (?, ?, ?, ?)", 
                     (uid, phone, role, datetime.now()))

def db_get_active_accounts():
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT phone_number, role, user_id FROM accounts WHERE status = 'active'").fetchall()

def db_update_status(phone, status):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET status = ? WHERE phone_number = ?", (status, phone))

def db_inc_msg(phone):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET messages_sent = messages_sent + 1 WHERE phone_number = ?", (phone,))

def db_get_stats_full():
    with sqlite3.connect(DB_NAME) as conn:
        total = conn.execute("SELECT count(*) FROM accounts").fetchone()[0]
        active = conn.execute("SELECT count(*) FROM accounts WHERE status = 'active'").fetchone()[0]
        msgs = conn.execute("SELECT sum(messages_sent) FROM accounts").fetchone()[0] or 0
        return total, active, msgs

# --- БРАУЗЕР ---
def get_driver(phone_number):
    options = Options()
    options.binary_location = "/usr/bin/google-chrome"
    options.add_argument("--headless=new") 
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1366,768")
    
    # Рандомный агент
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    options.add_argument(f"user-agent={ua}")
    options.add_argument("--log-level=3")
    
    profile_path = os.path.join(SESSIONS_DIR, str(phone_number))
    options.add_argument(f"--user-data-dir={profile_path}")

    service = Service(executable_path="/usr/local/bin/chromedriver")
    return webdriver.Chrome(service=service, options=options)

# --- ИМИТАЦИЯ ЧЕЛОВЕКА ---
async def human_type(element, text):
    for char in text:
        if random.random() < 0.03: # Опечатка
            wrong = random.choice(string.ascii_lowercase)
            element.send_keys(wrong)
            await asyncio.sleep(random.uniform(0.1, 0.2))
            element.send_keys(Keys.BACKSPACE)
            await asyncio.sleep(random.uniform(0.1, 0.15))
        element.send_keys(char)
        await asyncio.sleep(random.uniform(0.05, 0.2))

# --- КЛАВИАТУРЫ ---
def kb_main(uid):
    kb = [[InlineKeyboardButton(text="➕ Добавить Аккаунт", callback_data="add")],
          [InlineKeyboardButton(text="📂 Список и Статус", callback_data="list")]]
    if uid == ADMIN_ID:
        kb.append([InlineKeyboardButton(text="📊 Админка", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def kb_auth_process():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 ЧЕК (Экран)", callback_data="check_browser")],
        [InlineKeyboardButton(text="🔗 Жми 'Link with phone'", callback_data="force_link")],
        [InlineKeyboardButton(text="⌨️ Ввести номер", callback_data="force_type")],
        [InlineKeyboardButton(text="✅ ГОТОВО (Активировать)", callback_data="check_scan")]
    ])

# --- БОТ ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
class Form(StatesGroup): phone = State()

@dp.message(Command("start"))
async def start(msg: types.Message):
    init_db()
    await msg.answer("🔥 **WhatsApp Farm 24/7**\nНочной режим отключен. Работаем круглосуточно.", reply_markup=kb_main(msg.from_user.id))

@dp.callback_query(F.data == "add")
async def add_start(call: types.CallbackQuery, state: FSMContext):
    await call.message.edit_text("📞 Введите номер (только цифры):")
    await state.set_state(Form.phone)

@dp.message(Form.phone)
async def process_phone(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    if len(phone) < 10: return await msg.answer("❌ Номер слишком короткий")
    
    db_add_account(msg.from_user.id, phone)
    await state.update_data(phone=phone)
    
    await msg.answer(
        f"🚀 **Запуск {phone}...**\n"
        "1. Подожди 15 сек.\n2. Жми ЧЕК.\n3. Если зависло — юзай кнопки ручного ввода.", 
        reply_markup=kb_auth_process()
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
                inp.send_keys(Keys.ENTER)
            except: pass

            await asyncio.sleep(900) # 15 минут на вход
        except Exception as e:
            logger.error(f"Login Fail: {e}")
        finally:
            if user_id in ACTIVE_DRIVERS:
                d = ACTIVE_DRIVERS.pop(user_id)
                try: d.quit()
                except: pass

# --- РУЧНОЕ УПРАВЛЕНИЕ ---
@dp.callback_query(F.data == "check_browser")
async def check_browser(call: types.CallbackQuery):
    await call.answer()
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.message.answer("⚠️ Браузер закрыт.")
    try:
        screen = await asyncio.to_thread(driver.get_screenshot_as_png)
        code_txt = ""
        try:
            el = driver.find_element(By.XPATH, "//div[@aria-details='link-device-phone-number-code']")
            code_txt = f"\n🔑 КОД: `{el.text}`"
        except: pass
        await call.message.answer_photo(BufferedInputFile(screen, "view.png"), caption=f"Экран{code_txt}")
    except: await call.message.answer("Ошибка фото")

@dp.callback_query(F.data == "force_link")
async def force_link(call: types.CallbackQuery):
    await call.answer("Жму...")
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return
    try:
        btn = driver.find_element(By.XPATH, "//*[contains(text(), 'Link with phone')]")
        driver.execute_script("arguments[0].click();", btn)
        await call.message.answer("✅ Нажал!")
    except:
        await call.message.answer("❌ Кнопка не найдена.")

@dp.callback_query(F.data == "force_type")
async def force_type(call: types.CallbackQuery, state: FSMContext):
    await call.answer("Печатаю...")
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    data = await state.get_data()
    phone = data.get("phone")
    if not driver or not phone: return
    try:
        inp = driver.find_element(By.XPATH, "//input[@type='text']")
        inp.send_keys(Keys.CONTROL + "a" + Keys.BACKSPACE)
        for ch in f"+{phone}":
            inp.send_keys(ch)
            await asyncio.sleep(0.05)
        inp.send_keys(Keys.ENTER)
        await call.message.answer("✅ Номер введен.")
    except:
        await call.message.answer("❌ Поле не найдено.")

@dp.callback_query(F.data == "check_scan")
async def check_scan(call: types.CallbackQuery, state: FSMContext):
    await call.answer()
    data = await state.get_data()
    phone = data.get("phone")
    db_update_status(phone, 'active')
    await call.message.answer(f"✅ **{phone} АКТИВИРОВАН!**\nРоль назначена. Начинаю прогрев.")
    
    # Моментальный пинок
    asyncio.create_task(single_warmup(phone))
    
    if call.from_user.id in ACTIVE_DRIVERS: 
        ACTIVE_DRIVERS.pop(call.from_user.id).quit()
    await state.clear()

@dp.callback_query(F.data == "list")
async def list_accs(call: types.CallbackQuery):
    await call.answer()
    with sqlite3.connect(DB_NAME) as conn:
        accs = conn.execute("SELECT phone_number, status, role, messages_sent FROM accounts").fetchall()
    text = "📂 **Аккаунты:**\n"
    for p, s, r, m in accs:
        icon = "🟢" if s=='active' else "🔴"
        text += f"\n{icon} `{p}` ({r}) | Смс: {m}"
    await call.message.answer(text, reply_markup=kb_main(call.from_user.id), parse_mode="Markdown")

@dp.callback_query(F.data == "admin")
async def admin_panel(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    tot, act, msgs = db_get_stats_full()
    await call.message.answer(f"📊 Всего: {tot}\nЖивых: {act}\nСмс: {msgs}")

# --- ФОНОВЫЙ ПРОГРЕВ (24/7) ---
async def single_warmup(sender):
    await asyncio.sleep(5)
    accs = db_get_active_accounts()
    if len(accs) < 2: return
    rec_data = random.choice(accs)
    receiver = rec_data[0]
    while receiver == sender: 
        rec_data = random.choice(accs)
        receiver = rec_data[0]
    
    sender_role = next((a[1] for a in accs if a[0] == sender), "normie")
    await perform_msg(sender, receiver, sender_role)

async def perform_msg(sender, receiver, role):
    async with BROWSER_SEMAPHORE:
        driver = None
        try:
            logger.info(f"MSG: {sender} -> {receiver}")
            driver = await asyncio.to_thread(get_driver, sender)
            driver.get(f"https://web.whatsapp.com/send?phone={receiver}")
            
            wait = WebDriverWait(driver, 45)
            inp = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@contenteditable='true'][@data-tab='10']")))
            
            msg_text = get_message_by_role(role)
            await human_type(inp, msg_text)
            
            await asyncio.sleep(1)
            inp.send_keys(Keys.ENTER)
            
            db_inc_msg(sender)
            await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"Error: {e}")
        finally:
            if driver: driver.quit()

async def farm_loop():
    logger.info("FARM STARTED (24/7 MODE)")
    while True:
        # Рандомная пауза 2-7 минут (без ночного сна)
        await asyncio.sleep(random.randint(120, 420))
        
        accs = db_get_active_accounts()
        if len(accs) < 2: continue
        
        s_data = random.choice(accs)
        r_data = random.choice(accs)
        
        sender, role, _ = s_data
        receiver = r_data[0]
        
        if sender == receiver: continue
        
        await perform_msg(sender, receiver, role)

async def main():
    init_db()
    asyncio.create_task(farm_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
