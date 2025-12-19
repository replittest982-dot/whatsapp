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
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))

# Ограничение: 3 браузера одновременно (чтобы сервер не упал)
BROWSER_SEMAPHORE = asyncio.Semaphore(3)
DB_NAME = 'bot_database.db'
SESSIONS_DIR = "./sessions"

# Хранилище активных драйверов: {user_id: driver}
ACTIVE_DRIVERS = {}
fake = Faker('ru_RU')

# Настройки задержек (в секундах)
FARM_DELAY_MIN = 300  # 5 минут
FARM_DELAY_MAX = 900  # 15 минут

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("WA_ARCHITECT")

# --- DATABASE ---
def init_db():
    with sqlite3.connect(DB_NAME, timeout=10) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS accounts 
                        (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                         user_id INTEGER, 
                         phone_number TEXT UNIQUE, 
                         status TEXT DEFAULT 'pending', 
                         messages_sent INTEGER DEFAULT 0,
                         user_agent TEXT, resolution TEXT, platform TEXT,
                         last_active TIMESTAMP)''')

def db_get_active_phones():
    with sqlite3.connect(DB_NAME, timeout=10) as conn:
        return [row[0] for row in conn.execute("SELECT phone_number FROM accounts WHERE status = 'active'").fetchall()]

def db_update_status(phone, status):
    with sqlite3.connect(DB_NAME, timeout=10) as conn:
        conn.execute("UPDATE accounts SET status = ?, last_active = ? WHERE phone_number = ?", 
                     (status, datetime.now(), phone))

def db_inc_msg(phone):
    with sqlite3.connect(DB_NAME, timeout=10) as conn:
        conn.execute("UPDATE accounts SET messages_sent = messages_sent + 1, last_active = ? WHERE phone_number = ?", 
                     (datetime.now(), phone))

# --- SYSTEM MONITOR ---
async def zombie_killer():
    """Убивает зависшие процессы Chrome каждые 2 минуты"""
    while True:
        await asyncio.sleep(120)
        for proc in psutil.process_iter(['pid', 'name', 'create_time']):
            try:
                if 'chrome' in proc.info['name']:
                    # Если живет дольше 30 минут - убиваем
                    if (datetime.now().timestamp() - proc.info['create_time']) > 1800:
                        proc.kill()
            except: pass

def get_driver(phone):
    # Проверка памяти (если меньше 200мб свободно - стоп)
    if psutil.virtual_memory().available < 200 * 1024 * 1024:
        logger.warning("⚠️ Low RAM. Skip launch.")
        return None

    path = os.path.join(SESSIONS_DIR, str(phone))
    if not os.path.exists(path): os.makedirs(path)

    # Рандомный User-Agent для уникальности
    ua_list = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ]
    
    opt = Options()
    opt.add_argument("--headless=new")
    opt.add_argument("--no-sandbox")
    opt.add_argument("--disable-dev-shm-usage")
    opt.add_argument("--disable-gpu")
    opt.add_argument("--window-size=1920,1080")
    opt.add_argument(f"user-agent={random.choice(ua_list)}")
    opt.add_argument(f"--user-data-dir={path}")
    opt.page_load_strategy = 'eager'

    try:
        driver = webdriver.Chrome(options=opt)
        return driver
    except Exception as e:
        logger.error(f"Driver Error: {e}")
        return None

async def human_type(element, text):
    """Печатает как человек с рандомной задержкой"""
    for char in text:
        element.send_keys(char)
        await asyncio.sleep(random.uniform(0.05, 0.2))

# --- BOT SETUP ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class Form(StatesGroup):
    phone = State()

def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить Аккаунт", callback_data="add")],
        [InlineKeyboardButton(text="📂 Список", callback_data="list")]
    ])

# РАЗДЕЛЬНЫЕ КНОПКИ УПРАВЛЕНИЯ ВХОДОМ
def kb_manual_auth():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 ЧЕК (СКРИН)", callback_data="check")],
        [InlineKeyboardButton(text="🔗 1. Нажать Ссылку", callback_data="btn_click_link")],
        [InlineKeyboardButton(text="⌨️ 2. Ввести Номер", callback_data="btn_type_num")],
        [InlineKeyboardButton(text="🔑 3. Получить КОД", callback_data="btn_get_code")],
        [InlineKeyboardButton(text="✅ ГОТОВО / ВЫХОД", callback_data="done")]
    ])

# --- HANDLERS ---

@dp.message(Command("start"))
async def start(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    init_db()
    await msg.answer("🏛 **WA Farm Ultimate**\nРежимы: Solo + Network (Взаимная переписка).", reply_markup=kb_main())

# --- FLOW ДОБАВЛЕНИЯ АККАУНТА ---
@dp.callback_query(F.data == "add")
async def add_start(call: types.CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    if uid in ACTIVE_DRIVERS:
        try: ACTIVE_DRIVERS[uid].quit()
        except: pass
    
    await call.message.edit_text("Введи номер (7999...):")
    await state.set_state(Form.phone)

@dp.message(Form.phone)
async def proc_phone(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR IGNORE INTO accounts (user_id, phone_number) VALUES (?, ?)", (msg.from_user.id, phone))
    
    await state.update_data(phone=phone)
    await msg.answer(f"⏳ Запускаю браузер для **{phone}**...\nИспользуй кнопки ниже:", reply_markup=kb_manual_auth())
    
    # Запускаем браузер и держим его открытым для ручных действий
    asyncio.create_task(bg_browser_hold(msg.from_user.id, phone))

async def bg_browser_hold(uid, phone):
    async with BROWSER_SEMAPHORE:
        driver = await asyncio.to_thread(get_driver, phone)
        if not driver:
            await bot.send_message(uid, "❌ Ошибка запуска драйвера (мало памяти).")
            return
            
        ACTIVE_DRIVERS[uid] = driver
        try:
            driver.get("https://web.whatsapp.com/")
            # Держим сессию 10 минут, пока админ нажимает кнопки
            for _ in range(60): 
                if uid not in ACTIVE_DRIVERS: break
                await asyncio.sleep(10)
        except Exception as e:
            logger.error(f"Hold Error: {e}")
        finally:
            if uid in ACTIVE_DRIVERS:
                try: ACTIVE_DRIVERS[uid].quit()
                except: pass
                del ACTIVE_DRIVERS[uid]

# --- РУЧНОЕ УПРАВЛЕНИЕ (КНОПКИ) ---

@dp.callback_query(F.data == "check")
async def cb_check(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Браузер закрыт", show_alert=True)
    try:
        scr = driver.get_screenshot_as_png()
        await call.message.answer_photo(BufferedInputFile(scr, "s.png"), caption="Экран")
    except: await call.answer("Ошибка скрина")

@dp.callback_query(F.data == "btn_click_link")
async def cb_click_link(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Браузер закрыт", show_alert=True)
    
    await call.answer("Ищу кнопку...")
    try:
        # Пытаемся найти кнопку по разным признакам
        xpaths = [
            "//span[contains(text(), 'Link with phone')]", 
            "//span[contains(text(), 'Связать с номером')]",
            "//a[contains(@href, 'link-device')]"
        ]
        for xp in xpaths:
            try: 
                driver.find_element(By.XPATH, xp).click()
                await call.message.answer("✅ Нажал 'Link with phone number'")
                return
            except: continue
        await call.message.answer("⚠️ Кнопка не найдена. Проверь скрин.")
    except Exception as e: await call.message.answer(f"Ошибка: {e}")

@dp.callback_query(F.data == "btn_type_num")
async def cb_type_num(call: types.CallbackQuery, state: FSMContext):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Браузер закрыт", show_alert=True)
    
    data = await state.get_data()
    phone = data.get("phone")
    if not phone: return await call.answer("Нет номера в памяти")
    
    await call.message.answer(f"⌨️ Ввожу номер {phone}...")
    try:
        # Ищем поле ввода
        inp = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.XPATH, "//input[@aria-label='Type your phone number.'] | //input[@type='text']")))
        
        # Очищаем JS-ом для надежности
        driver.execute_script("arguments[0].value = '';", inp)
        inp.send_keys(Keys.CONTROL + "a" + Keys.BACKSPACE)
        
        # Вводим по цифре
        for ch in phone: 
            inp.send_keys(ch)
            await asyncio.sleep(0.05)
        
        await asyncio.sleep(0.5)
        inp.send_keys(Keys.ENTER) # Жмем Enter
        
        await call.message.answer("✅ Номер введен. Ждем переход...")
    except Exception as e:
        await call.message.answer(f"❌ Не нашел поле ввода: {e}")

@dp.callback_query(F.data == "btn_get_code")
async def cb_get_code(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Браузер закрыт", show_alert=True)
    
    try:
        code_el = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.XPATH, "//div[@aria-details='link-device-phone-number-code']")))
        code = code_el.text
        await call.message.answer(f"🔑 **КОД:** `{code}`", parse_mode="Markdown")
    except:
        scr = driver.get_screenshot_as_png()
        await call.message.answer_photo(BufferedInputFile(scr, "err.png"), caption="Код не вижу. Глянь скрин.")

@dp.callback_query(F.data == "done")
async def cb_done(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    phone = data.get("phone")
    
    uid = call.from_user.id
    if uid in ACTIVE_DRIVERS:
        ACTIVE_DRIVERS[uid].quit()
        del ACTIVE_DRIVERS[uid]
    
    if phone:
        db_update_status(phone, 'active')
        await call.message.edit_text(f"✅ Аккаунт {phone} активирован и добавлен в ротацию!")
    else:
        await call.message.edit_text("Завершено.")

@dp.callback_query(F.data == "list")
async def cb_list(call: types.CallbackQuery):
    phones = db_get_active_phones()
    txt = "\n".join([f"🟢 {p}" for p in phones]) if phones else "Пусто"
    await call.message.edit_text(f"Активные:\n{txt}", reply_markup=kb_main())

# --- FARM WORKER (SOLO & NETWORK) ---

async def farm_worker(phone):
    """
    Умный воркер:
    1. 70% шанс - пишет сам себе (Saved Messages).
    2. 30% шанс - пишет ДРУГОМУ боту из базы (взаимный прогрев).
    """
    logger.info(f"🚜 Worker started for {phone}")
    
    async with BROWSER_SEMAPHORE:
        driver = await asyncio.to_thread(get_driver, phone)
        if not driver: return

        try:
            # 1. Загрузка WA
            driver.get("https://web.whatsapp.com/")
            
            # Ждем загрузки (или бана)
            try:
                WebDriverWait(driver, 40).until(EC.presence_of_element_located((By.ID, "pane-side")))
            except:
                logger.warning(f"{phone} не прогрузился или бан.")
                driver.quit()
                return

            # 2. Выбор стратегии: SELF или NETWORK
            all_phones = db_get_active_phones()
            others = [p for p in all_phones if p != phone]
            
            target_phone = phone # По умолчанию пишем себе
            mode = "SOLO"
            
            # Если есть другие аккаунты, с шансом 30% пишем им
            if others and random.random() < 0.3:
                target_phone = random.choice(others)
                mode = "NETWORK"
            
            logger.info(f"⚔️ Strategy for {phone}: {mode} -> {target_phone}")

            # 3. Переход в чат (через прямую ссылку)
            driver.get(f"https://web.whatsapp.com/send?phone={target_phone}")
            
            # Ждем поле ввода
            try:
                inp = WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.XPATH, "//div[@contenteditable='true'][@data-tab='10']")))
                
                # Генерируем текст
                msgs = [fake.sentence(), fake.text(max_nb_chars=40), "Привет, как дела?", "Надо не забыть", "Купить продукты", "Meeting at 10"]
                text_to_send = random.choice(msgs)
                
                # Печатаем
                await human_type(inp, text_to_send)
                await asyncio.sleep(1)
                inp.send_keys(Keys.ENTER)
                
                db_inc_msg(phone)
                logger.info(f"✅ Sent ({mode}): {text_to_send}")
                
                # Немного висим онлайн
                await asyncio.sleep(random.randint(5, 15))
                
            except Exception as e:
                logger.warning(f"Failed to send msg: {e}")

        except Exception as e:
            logger.error(f"Worker Crash: {e}")
        finally:
            driver.quit()

# --- FARM LOOP (ГЛАВНЫЙ ЦИКЛ) ---
async def farm_loop():
    logger.info("📡 Farm Loop Active")
    asyncio.create_task(zombie_killer())
    
    while True:
        phones = db_get_active_phones()
        if phones:
            # Выбираем случайный аккаунт
            p = random.choice(phones)
            
            # Запускаем воркера
            asyncio.create_task(farm_worker(p))
            
            # Задержка перед запуском СЛЕДУЮЩЕГО аккаунта
            # Это и есть "режим с задержкой", чтобы не спамить
            delay = random.randint(FARM_DELAY_MIN, FARM_DELAY_MAX)
            logger.info(f"💤 Жду {delay} сек до следующего старта...")
            await asyncio.sleep(delay)
        else:
            await asyncio.sleep(60)

async def main():
    init_db()
    asyncio.create_task(farm_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
