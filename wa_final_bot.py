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
from selenium.common.exceptions import TimeoutException

# --- КОНФИГ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except:
    ADMIN_ID = 0

# Ссылка на группу
GROUP_INVITE_LINK = "https://chat.whatsapp.com/KtKFYIMlAmSH8U0OKhWI8f?mode=hqrt2"

# Лимиты
BROWSER_SEMAPHORE = asyncio.Semaphore(3)
DB_NAME = 'bot_database.db'
SESSIONS_DIR = "./sessions"

ACTIVE_DRIVERS = {}
fake = Faker('ru_RU')

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("WA_FARM_GOD_MODE")

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

def db_inc_msg(phone):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET messages_sent = messages_sent + 1, last_active = ? WHERE phone_number = ?", (datetime.now(), phone))

# --- ZOMBIE KILLER & MEMORY ---
def is_memory_safe():
    try:
        if psutil.virtual_memory().available < 200 * 1024 * 1024: return False
    except: pass
    return True

async def zombie_killer():
    while True:
        await asyncio.sleep(120)
        for proc in psutil.process_iter(['pid', 'name', 'create_time']):
            try:
                if 'chrome' in proc.info['name']:
                    if (datetime.now().timestamp() - proc.info['create_time']) > 1800:
                        proc.kill()
            except: pass

# --- SELENIUM DRIVER ---
def get_driver(phone):
    if not is_memory_safe(): return None
    path = os.path.join(SESSIONS_DIR, str(phone))
    if not os.path.exists(path): os.makedirs(path)

    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    res = "1920,1080"
    
    opt = Options()
    opt.add_argument("--headless=new")
    opt.add_argument("--no-sandbox")
    opt.add_argument("--disable-dev-shm-usage")
    opt.add_argument(f"--window-size={res}")
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
        await asyncio.sleep(random.uniform(0.05, 0.15))

# --- BOT INTERFACE ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class Form(StatesGroup):
    phone = State()         # Для добавления аккаунта
    unban_email = State()   # Шаг 1: Почта для разбана
    unban_phone = State()   # Шаг 2: Номер для разбана

def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить Аккаунт", callback_data="add")],
        [InlineKeyboardButton(text="🚑 UNBAN CENTER (Разбан)", callback_data="unban_start")],
        [InlineKeyboardButton(text="📂 Список", callback_data="list")]
    ])

# ЕДИНАЯ ПАНЕЛЬ УПРАВЛЕНИЯ
def kb_manual_control():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 ЧЕК (Экран)", callback_data="check"),
         InlineKeyboardButton(text="✅ ГОТОВО / ВЫХОД", callback_data="done")],
        [InlineKeyboardButton(text="--- ВХОД (LOGIN) ---", callback_data="none")],
        [InlineKeyboardButton(text="🔗 Log with phone number", callback_data="click_link_btn")],
        [InlineKeyboardButton(text="⌨️ Ввести номер (+Enter)", callback_data="type_phone_btn")],
        [InlineKeyboardButton(text="🔑 Получить КОД", callback_data="get_code_btn")],
        [InlineKeyboardButton(text="--- РАЗБАН (UNBAN) ---", callback_data="none")],
        [InlineKeyboardButton(text="📨 ОТПРАВИТЬ ФОРМУ (SEND)", callback_data="submit_unban_btn")]
    ])

# --- HANDLERS ---

@dp.message(Command("start"))
async def start(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return 
    init_db()
    await msg.answer("🏛 **WA Farm: GOD MODE**\nВсё в одном: Ферма + Ручной Вход + Конструктор Разбана.", reply_markup=kb_main())

# --- БЛОК 1: ДОБАВЛЕНИЕ АККАУНТА ---
@dp.callback_query(F.data == "add")
async def add_flow(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID: return
    if call.from_user.id in ACTIVE_DRIVERS:
        try: ACTIVE_DRIVERS.pop(call.from_user.id).quit()
        except: pass
    await call.message.edit_text("Введи номер для входа (7XXXXXXXXXX):")
    await state.set_state(Form.phone)

@dp.message(Form.phone)
async def proc_phone(msg: types.Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID: return
    phone = re.sub(r'\D', '', msg.text)
    
    # Сохраняем в БД
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR IGNORE INTO accounts (user_id, phone_number) VALUES (?, ?)", (msg.from_user.id, phone))
    await state.update_data(phone=phone)
    
    await msg.answer(f"🚀 Запускаю браузер для **{phone}**...\nИспользуй кнопки ниже:", reply_markup=kb_manual_control())
    asyncio.create_task(bg_login_process(msg.from_user.id, phone))

async def bg_login_process(uid, phone):
    async with BROWSER_SEMAPHORE:
        driver = await asyncio.to_thread(get_driver, phone)
        if not driver: return
        ACTIVE_DRIVERS[uid] = driver
        try:
            driver.get("https://web.whatsapp.com/")
            await asyncio.sleep(600) # 10 минут на ручные действия
        except: pass
        finally:
            if uid in ACTIVE_DRIVERS: ACTIVE_DRIVERS.pop(uid).quit()

# --- БЛОК 2: UNBAN CENTER (НОВАЯ ИМБА) ---
@dp.callback_query(F.data == "unban_start")
async def unban_step1(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID: return
    if call.from_user.id in ACTIVE_DRIVERS:
        try: ACTIVE_DRIVERS.pop(call.from_user.id).quit()
        except: pass
        
    await call.message.edit_text("📧 Введи **EMAIL**, который укажем в жалобе\n(например: `genarapes@gmail.com`):")
    await state.set_state(Form.unban_email)

@dp.message(Form.unban_email)
async def unban_step2(msg: types.Message, state: FSMContext):
    email = msg.text.strip()
    await state.update_data(unban_email=email)
    await msg.answer("📞 Теперь введи **ЗАБАНЕННЫЙ НОМЕР** (7XXXXXXXXXX):")
    await state.set_state(Form.unban_phone)

@dp.message(Form.unban_phone)
async def unban_step3(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    data = await state.get_data()
    email = data.get("unban_email")
    
    await msg.answer(f"🚑 **Unban Process**\nEmail: {email}\nPhone: {phone}\n\nЗахожу на сайт... Жди кнопку ЧЕК.", reply_markup=kb_manual_control())
    
    # Запуск браузера для разбана
    asyncio.create_task(bg_unban_process(msg.from_user.id, phone, email))

async def bg_unban_process(uid, phone, email):
    async with BROWSER_SEMAPHORE:
        # Чистый драйвер без профиля
        opt = Options()
        opt.add_argument("--headless=new")
        opt.add_argument("--no-sandbox")
        opt.add_argument("--disable-dev-shm-usage")
        opt.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        
        driver = webdriver.Chrome(options=opt)
        ACTIVE_DRIVERS[uid] = driver
        
        try:
            driver.get("https://www.whatsapp.com/contact/nsc")
            await asyncio.sleep(3)
            
            # Заполняем форму
            driver.find_element(By.ID, "phone_number").send_keys(phone)
            driver.find_element(By.ID, "email").send_keys(email)
            driver.find_element(By.ID, "email_confirm").send_keys(email)
            
            # Рандомный текст, чтобы не палили
            appeals = [
                "Hello. My number is banned by mistake. I use WA for work. Please unban.",
                "Здравствуйте! Мой номер заблокирован. Я не рассылал спам. Прошу восстановить.",
                "Dear Support, I lost access to my account. It says banned. Please help.",
                "Бан по ошибке. Я соблюдаю правила. Разблокируйте пожалуйста."
            ]
            msg_box = driver.find_element(By.ID, "message")
            await human_type(msg_box, random.choice(appeals))
            
            # Ждем действий админа (Чек или Отправить)
            await asyncio.sleep(300) 
            
        except Exception as e:
            logger.error(f"Unban Error: {e}")
        finally:
            driver.quit()
            if uid in ACTIVE_DRIVERS: del ACTIVE_DRIVERS[uid]

# --- КНОПКИ УПРАВЛЕНИЯ (ОБЩИЕ) ---

@dp.callback_query(F.data == "check")
async def check_screen(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Браузер закрыт", show_alert=True)
    
    try:
        scr = driver.get_screenshot_as_png()
        await call.message.answer_photo(BufferedInputFile(scr, "s.png"), caption="🖥 Текущий экран")
    except: await call.answer("Ошибка скрина")

# Кнопки для ВХОДА (LOGIN)
@dp.callback_query(F.data == "click_link_btn")
async def btn_link(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return
    try:
        # Ищем кнопку "Link with phone number"
        xpaths = ["//span[contains(text(), 'Link with phone')]", "//a[contains(@href, 'link-device')]", "//span[contains(text(), 'Связать с номером')]"]
        for xp in xpaths:
            try: driver.find_element(By.XPATH, xp).click(); break
            except: continue
        await call.answer("Нажал!")
    except: await call.answer("Не нашел кнопку")

@dp.callback_query(F.data == "type_phone_btn")
async def btn_type(call: types.CallbackQuery, state: FSMContext):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return
    data = await state.get_data()
    phone = data.get("phone") # Берем номер из памяти, если это вход
    if not phone: return await call.answer("Нет номера в памяти")
    
    try:
        inp = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.XPATH, "//input[@aria-label='Type your phone number.'] | //input[@type='text']")))
        driver.execute_script("arguments[0].value = '';", inp)
        inp.send_keys(Keys.CONTROL + "a" + Keys.BACKSPACE)
        for ch in phone: inp.send_keys(ch); await asyncio.sleep(0.05)
        inp.send_keys(Keys.ENTER)
        await call.answer(f"Ввел {phone}")
    except: await call.answer("Ошибка ввода")

@dp.callback_query(F.data == "get_code_btn")
async def btn_code(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return
    try:
        el = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.XPATH, "//div[@aria-details='link-device-phone-number-code']")))
        await call.message.answer(f"🔑 КОД: `{el.text}`", parse_mode="Markdown")
    except: await call.answer("Код не найден")

# Кнопка для РАЗБАНА (UNBAN) - ОТПРАВИТЬ ФОРМУ
@dp.callback_query(F.data == "submit_unban_btn")
async def btn_submit(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Браузер закрыт", show_alert=True)
    
    await call.message.answer("🚀 Жму 'Отправить' (Next Step)...")
    try:
        # Кнопка обычно называется "Next Step" или "Send Question"
        # Ищем по нескольким признакам
        btn = driver.find_element(By.XPATH, "//button[contains(text(), 'Next Step') or contains(text(), 'Отправить') or contains(text(), 'Send')]")
        btn.click()
        
        await asyncio.sleep(2)
        scr = driver.get_screenshot_as_png()
        await call.message.answer_photo(BufferedInputFile(scr, "sent.png"), caption="✅ Форма отправлена! Проверь скрин.")
        
        # Можно завершать
        driver.quit()
        if call.from_user.id in ACTIVE_DRIVERS: del ACTIVE_DRIVERS[call.from_user.id]
        
    except Exception as e:
        await call.message.answer(f"❌ Ошибка нажатия: {e}")

@dp.callback_query(F.data == "done")
async def done_action(call: types.CallbackQuery):
    if call.from_user.id in ACTIVE_DRIVERS:
        ACTIVE_DRIVERS.pop(call.from_user.id).quit()
    await call.message.edit_text("✅ Готово. Процесс завершен.")

@dp.callback_query(F.data == "list")
async def list_a(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    phones = db_get_active_phones()
    txt = "\n".join([f"🟢 {p}" for p in phones]) if phones else "Пусто"
    await call.message.edit_text(f"Активные:\n{txt}", reply_markup=kb_main())

# --- ФАРМ (ТИХИЙ ФОН) ---
async def farm_loop():
    logger.info("🚜 Farm Loop Active")
    asyncio.create_task(zombie_killer())
    while True:
        phones = db_get_active_phones()
        if phones:
            p = random.choice(phones)
            hour = datetime.now().hour
            # Днем работаем, Ночью (23-7) спим на 90%
            if (hour >= 23 or hour < 7):
                if random.random() < 0.1: # Редкий ночной заход
                     asyncio.create_task(farm_bg_worker(p))
            else:
                 asyncio.create_task(farm_bg_worker(p))
        
        await asyncio.sleep(random.randint(300, 900))

async def farm_bg_worker(phone):
    if not is_memory_safe(): return
    path = os.path.join(SESSIONS_DIR, str(phone))
    if not os.path.exists(path): return
    
    try:
        opt = Options()
        opt.add_argument("--headless=new")
        opt.add_argument("--no-sandbox")
        opt.add_argument("--disable-dev-shm-usage")
        opt.add_argument(f"user-data-dir={path}")
        driver = webdriver.Chrome(options=opt)
        driver.get("https://web.whatsapp.com/")
        await asyncio.sleep(30)
        driver.quit()
    except: pass

async def main():
    init_db()
    asyncio.create_task(farm_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
