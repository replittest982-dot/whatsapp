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

# Настройки скорости (Турбо-прогрев: 1-3 минуты)
FARM_DELAY_MIN = 60
FARM_DELAY_MAX = 180

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

# --- ДРАЙВЕР (KZ MASKING) ---
def get_driver(phone):
    opt = Options()
    opt.binary_location = "/usr/bin/google-chrome"
    opt.add_argument("--headless=new")
    opt.add_argument("--no-sandbox")
    opt.add_argument("--disable-dev-shm-usage")
    opt.add_argument("--window-size=1920,1080")
    
    # 1. Маскировка под Казахстан (Часовой пояс и Язык)
    opt.add_argument("--lang=ru-KZ")
    
    # User-Agent как у обычного ПК
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    opt.add_argument(f"user-agent={ua}")
    opt.add_argument(f"--user-data-dir={os.path.join(SESSIONS_DIR, str(phone))}")
    
    # Скрываем автоматизацию
    opt.add_experimental_option("excludeSwitches", ["enable-automation"])
    opt.add_experimental_option('useAutomationExtension', False)

    driver = webdriver.Chrome(service=Service("/usr/local/bin/chromedriver"), options=opt)
    
    # 2. ПОДМЕНА ГЕОЛОКАЦИИ (АЛМАТЫ)
    # Координаты центра Алматы
    params = {
        "latitude": 43.238949,
        "longitude": 76.889709,
        "accuracy": 100
    }
    driver.execute_cdp_cmd("Emulation.setGeolocationOverride", params)
    
    return driver

# --- ИМИТАЦИЯ ЧЕЛОВЕКА ---
async def human_type(element, text):
    for char in text:
        if random.random() < 0.03: # Опечатка
            element.send_keys(random.choice(string.ascii_lowercase))
            await asyncio.sleep(0.1)
            element.send_keys(Keys.BACKSPACE)
        element.send_keys(char)
        await asyncio.sleep(random.uniform(0.05, 0.15))

# --- КЛАВИАТУРЫ ---
def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Добавить Аккаунт", callback_data="add")],
        [InlineKeyboardButton(text="Статус Фермы", callback_data="list")]
    ])

def kb_auth():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="ЧЕК", callback_data="check"),
         InlineKeyboardButton(text="ГОТОВО", callback_data="done")],
        [InlineKeyboardButton(text="Вход по номеру", callback_data="force_link")],
        [InlineKeyboardButton(text="Ввести номер", callback_data="force_type")]
    ])

# --- БОТ ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
class Form(StatesGroup): phone = State()

@dp.message(Command("start"))
async def start(msg: types.Message):
    init_db()
    await msg.answer("🇰🇿 **WhatsApp Farm KZ-Edition**\nГеолокация подменена на Алматы.\nЕсли аккаунт один — пишу сам себе.", reply_markup=kb_main())

@dp.callback_query(F.data == "add")
async def add_flow(call: types.CallbackQuery, state: FSMContext):
    await call.message.edit_text("Введите номер (только цифры):")
    await state.set_state(Form.phone)

@dp.message(Form.phone)
async def proc_phone(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR IGNORE INTO accounts (user_id, phone_number) VALUES (?, ?)", (msg.from_user.id, phone))
    await state.update_data(phone=phone)
    await msg.answer(f"Запускаю {phone}...", reply_markup=kb_auth())
    asyncio.create_task(bg_login(msg.from_user.id, phone))

async def bg_login(uid, phone):
    async with BROWSER_SEMAPHORE:
        try:
            driver = await asyncio.to_thread(get_driver, phone)
            ACTIVE_DRIVERS[uid] = driver
            driver.get("https://web.whatsapp.com/")
            await asyncio.sleep(900) # Держим 15 минут для входа
        except: pass
        finally:
            if uid in ACTIVE_DRIVERS: ACTIVE_DRIVERS.pop(uid).quit()

# --- УМНЫЙ ЧЕКЕР (Восстанавливает браузер) ---
@dp.callback_query(F.data == "check")
async def check(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    # Если мы в процессе добавления - номер в state, если нет - ищем последний активный
    phone = data.get("phone")
    
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    
    # Если браузер закрыт — открываем на секунду для скрина!
    temp_driver = False
    if not driver:
        if not phone: return await call.answer("Нет активной сессии")
        await call.answer("Подгружаю экран...")
        driver = await asyncio.to_thread(get_driver, phone)
        driver.get("https://web.whatsapp.com/")
        await asyncio.sleep(10) # Ждем прогрузки
        temp_driver = True
    else:
        await call.answer("Делаю скрин...")

    try:
        scr = await asyncio.to_thread(driver.get_screenshot_as_png)
        code = ""
        try: 
            el = driver.find_element(By.XPATH, "//div[@aria-details='link-device-phone-number-code']")
            code = f"\nКОД: {el.text}"
        except: pass
        
        await call.message.answer_photo(BufferedInputFile(scr, "s.png"), caption=f"Экран{code}")
    except: await call.answer("Ошибка фото")
    finally:
        if temp_driver: driver.quit()

@dp.callback_query(F.data == "force_link")
async def f_link(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Браузер закрыт")
    
    xpaths = [
        "//span[contains(text(), 'Link with phone')]", "//span[contains(text(), 'Связать с номером')]",
        "//div[contains(text(), 'Link with phone')]", "//div[contains(text(), 'Связать с номером')]",
        "//span[@role='button']"
    ]
    for xp in xpaths:
        try:
            btn = driver.find_element(By.XPATH, xp)
            driver.execute_script("arguments[0].click();", btn)
            return await call.answer("Нажал!")
        except: continue
    await call.answer("Кнопка не найдена")

@dp.callback_query(F.data == "force_type")
async def f_type(call: types.CallbackQuery, state: FSMContext):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    data = await state.get_data()
    if not driver or not data.get("phone"): return await call.answer("Браузер закрыт")
    
    try:
        # Авто-клик по ссылке, если поле еще не открыто
        try:
            l = driver.find_element(By.XPATH, "//span[contains(text(), 'Link with phone')] | //span[contains(text(), 'Связать с номером')]")
            driver.execute_script("arguments[0].click();", l)
            await asyncio.sleep(2)
        except: pass

        inp = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "input")))
        driver.execute_script("arguments[0].value = '';", inp)
        inp.send_keys(Keys.CONTROL + "a" + Keys.BACKSPACE)
        for ch in f"+{data['phone']}":
            inp.send_keys(ch)
            await asyncio.sleep(0.05)
        inp.send_keys(Keys.ENTER)
        await call.message.answer("Номер введен!")
    except: await call.message.answer("Поле ввода не найдено")

@dp.callback_query(F.data == "done")
async def done(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    phone = data.get("phone")
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET status = 'active' WHERE phone_number = ?", (phone,))
    if call.from_user.id in ACTIVE_DRIVERS:
        ACTIVE_DRIVERS.pop(call.from_user.id).quit()
    await call.message.answer(f"{phone} сохранен. Прогрев начинается.")
    asyncio.create_task(single_warmup(phone))

@dp.callback_query(F.data == "list")
async def list_a(call: types.CallbackQuery):
    with sqlite3.connect(DB_NAME) as conn:
        accs = conn.execute("SELECT phone_number, status, messages_sent FROM accounts").fetchall()
    txt = "📊 Статистика:\n"
    for p, s, m in accs:
        txt += f"\n{'🟢' if s=='active' else '🔴'} {p} | Смс: {m}"
    await call.message.answer(txt, reply_markup=kb_main())

# --- ПРОГРЕВ (САМ СЕБЕ + МАСКИРОВКА) ---
async def perform_msg(sender, receiver):
    async with BROWSER_SEMAPHORE:
        driver = None
        try:
            logger.info(f"START: {sender} -> {receiver}")
            driver = await asyncio.to_thread(get_driver, sender)
            
            # 1. Заходим на главную
            driver.get("https://web.whatsapp.com/")
            await asyncio.sleep(random.randint(15, 30))
            
            # 2. Переходим в чат (Если sender == receiver, это чат с самим собой)
            driver.get(f"https://web.whatsapp.com/send?phone={receiver}")
            
            wait = WebDriverWait(driver, 60)
            inp = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@contenteditable='true'][@data-tab='10']")))
            
            # 3. Печатаем
            await asyncio.sleep(random.randint(5, 10))
            text = fake.sentence(nb_words=random.randint(3, 10))
            await human_type(inp, text)
            await asyncio.sleep(1)
            inp.send_keys(Keys.ENTER)
            
            db_inc_msg(sender)
            logger.info(f"SENT: {text}")
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Err: {e}")
        finally:
            if driver: driver.quit()

async def single_warmup(sender):
    await asyncio.sleep(5)
    accs = db_get_active()
    # Если аккаунтов < 2, пишем САМОМУ СЕБЕ
    if not accs: return
    
    if len(accs) == 1:
        target = sender # Сам себе
    else:
        targets = [a[0] for a in accs if a[0] != sender]
        target = random.choice(targets)
    
    await perform_msg(sender, target)

async def farm_loop():
    while True:
        await asyncio.sleep(random.randint(FARM_DELAY_MIN, FARM_DELAY_MAX))
        accs = db_get_active()
        if not accs: continue
        
        # Выбираем случайного отправителя
        sender = random.choice(accs)[0]
        
        # Выбираем получателя
        if len(accs) == 1:
            receiver = sender # Пишем сами себе (Избранное)
        else:
            targets = [a[0] for a in accs if a[0] != sender]
            receiver = random.choice(targets)
            
        await perform_msg(sender, receiver)

async def main():
    init_db()
    asyncio.create_task(farm_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
