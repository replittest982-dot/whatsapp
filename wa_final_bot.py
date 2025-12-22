import os
import asyncio
import sqlite3
import random
import logging
import psutil
import json
from datetime import datetime
from typing import Optional

# --- СТОРОННИЕ БИБЛИОТЕКИ ---
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile

from faker import Faker # ГЕНЕРАТОР УНИКАЛЬНОГО КОНТЕНТА

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import WebDriverException, TimeoutException

# ==========================================
# ⚙️ КОНФИГУРАЦИЯ И ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# ==========================================

# Настройки Инстанса (для мульти-контейнерной работы)
try:
    INSTANCE_ID = int(os.getenv("INSTANCE_ID", 1))
    TOTAL_INSTANCES = int(os.getenv("TOTAL_INSTANCES", 1))
    ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
except ValueError:
    INSTANCE_ID = 1
    TOTAL_INSTANCES = 1
    ADMIN_ID = 0

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "imperator_v16.db"
SESSION_DIR = "./sessions"

# Лимиты ресурсов
BROWSER_SEMAPHORE = asyncio.Semaphore(1) # Строго 1 браузер на контейнер
MIN_RAM_MB = 200                         # Минимальная свободная память для старта

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format=f'%(asctime)s | INST-{INSTANCE_ID} | %(levelname)s | %(message)s'
)
logger = logging.getLogger("Imperator")

# Инициализация Faker (Русская локаль для реалистичности)
fake = Faker('ru_RU')

# Создаем папку сессий
if not os.path.exists(SESSION_DIR):
    os.makedirs(SESSION_DIR)

# Список реальных устройств для ротации отпечатков
DEVICES = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "res": "1920,1080", "plat": "Win32"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36", "res": "1440,900", "plat": "MacIntel"},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", "res": "1366,768", "plat": "Linux x86_64"}
]

# Кэш активных драйверов (для ручного управления)
active_drivers = {}

# ==========================================
# 🛡️ SYSTEM & MEMORY GUARD
# ==========================================

def is_memory_critical():
    """Проверяет, достаточно ли RAM для запуска Chrome"""
    mem = psutil.virtual_memory()
    free_mb = mem.available / 1024 / 1024
    if free_mb < MIN_RAM_MB:
        logger.warning(f"⚠️ LOW MEMORY: {free_mb:.1f}MB free. Блокировка запуска.")
        return True
    return False

# ==========================================
# 🗄️ БАЗА ДАННЫХ (SQLite)
# ==========================================

def db_init():
    conn = sqlite3.connect(DB_PATH, timeout=10) # Timeout важен при мульти-доступе
    cur = conn.cursor()
    # Таблица аккаунтов
    cur.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            phone_number TEXT PRIMARY KEY,
            status TEXT DEFAULT 'pending',
            user_agent TEXT,
            resolution TEXT,
            platform TEXT,
            last_active DATETIME,
            messages_sent INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def db_get_account_config(phone):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cur = conn.cursor()
    cur.execute("SELECT user_agent, resolution, platform FROM accounts WHERE phone_number=?", (phone,))
    res = cur.fetchone()
    conn.close()
    return res

def db_save_account(phone, ua, res, plat):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO accounts (phone_number, status, user_agent, resolution, platform, last_active)
        VALUES (?, 'active', ?, ?, ?, ?)
    """, (phone, ua, res, plat, datetime.now()))
    conn.commit()
    conn.close()

def db_update_activity(phone):
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("UPDATE accounts SET last_active=?, messages_sent=messages_sent+1 WHERE phone_number=?", (datetime.now(), phone))
    conn.commit()
    conn.close()

# ==========================================
# 🤖 HUMANIZATION & INPUT LOGIC
# ==========================================

async def human_type(element, text):
    """Печать с опечатками (4% шанс) и задержками"""
    for char in text:
        # Эмуляция ошибки
        if random.random() < 0.04:
            wrong_char = random.choice('абвгдеёжзийклмнопрстуфхцчшщъыьэюя')
            element.send_keys(wrong_char)
            await asyncio.sleep(random.uniform(0.1, 0.3))
            element.send_keys(Keys.BACKSPACE)
            await asyncio.sleep(random.uniform(0.05, 0.15))
        
        element.send_keys(char)
        await asyncio.sleep(random.uniform(0.04, 0.15))

# ==========================================
# 🌐 SELENIUM CORE (STEALTH)
# ==========================================

def get_driver(phone, headless=True):
    # 1. Загружаем или создаем конфиг устройства
    config = db_get_account_config(phone)
    if config and config[0]:
        ua, res, plat = config
    else:
        dev = random.choice(DEVICES)
        ua, res, plat = dev['ua'], dev['res'], dev['plat']
    
    # 2. Настройка Chrome
    options = Options()
    user_data = os.path.abspath(os.path.join(SESSION_DIR, phone))
    options.add_argument(f"--user-data-dir={user_data}")
    
    if headless:
        options.add_argument("--headless=new")
    
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"--user-agent={ua}")
    options.add_argument(f"--window-size={res}")
    options.add_argument("--lang=ru-RU,ru")
    options.page_load_strategy = 'eager' # Не ждать полной загрузки рекламы

    # 3. Инициализация
    driver = webdriver.Chrome(options=options)

    # 4. 🔥 HARDCORE STEALTH INJECTION (CDP)
    # Скрываем WebDriver
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.navigator.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'ru', 'en-US', 'en']});
        """
    })
    
    # Подмена Платформы
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": f"Object.defineProperty(navigator, 'platform', {{get: () => '{plat}'}});"
    })

    # Подмена Геолокации (Алматы)
    driver.execute_cdp_cmd("Emulation.setGeolocationOverride", {
        "latitude": 43.2389,
        "longitude": 76.8897,
        "accuracy": 100
    })

    # Подмена Времени (Asia/Almaty)
    driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {
        "timezoneId": "Asia/Almaty"
    })

    return driver, ua, res, plat

# ==========================================
# 📱 TELEGRAM BOT LOGIC
# ==========================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class AddState(StatesGroup):
    waiting_phone = State()

# --- КЛАВИАТУРЫ ---
def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Новый аккаунт", callback_data="add_new")],
        [InlineKeyboardButton(text="📊 Статус системы", callback_data="sys_status")]
    ])

def kb_control(phone):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 ЧЕК (Скрин)", callback_data=f"scr_{phone}")],
        [InlineKeyboardButton(text="🔗 Нажать 'Связать'", callback_data=f"lnk_{phone}")],
        [InlineKeyboardButton(text="⌨️ Ввести номер (JS)", callback_data=f"typ_{phone}")],
        [InlineKeyboardButton(text="💾 Сохранить и Выйти", callback_data=f"sav_{phone}")]
    ])

# --- ХЕНДЛЕРЫ ---

@dp.message(Command("start"))
async def start_cmd(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    await msg.answer(f"🔱 **Imperator v16.3 Ultimate**\nИнстанс: {INSTANCE_ID}/{TOTAL_INSTANCES}", reply_markup=kb_main())

@dp.callback_query(F.data == "sys_status")
async def status_handler(cb: types.CallbackQuery):
    mem = psutil.virtual_memory()
    msg = (f"🖥 **System Status**\n"
           f"RAM Free: {mem.available / 1024 / 1024:.1f} MB\n"
           f"Active Drivers: {len(active_drivers)}\n"
           f"Instance ID: {INSTANCE_ID}")
    await cb.answer(msg, show_alert=True)

@dp.callback_query(F.data == "add_new")
async def add_start(cb: types.CallbackQuery, state: FSMContext):
    if is_memory_critical():
        return await cb.answer("❌ Мало памяти! Освободите ресурсы.", show_alert=True)
    await cb.message.answer("📞 Введите номер телефона (только цифры):")
    await state.set_state(AddState.waiting_phone)

@dp.message(AddState.waiting_phone)
async def add_process(msg: types.Message, state: FSMContext):
    phone = "".join(filter(str.isdigit, msg.text))
    await state.clear()
    
    m = await msg.answer(f"🚀 Запуск Chrome для {phone}...")
    
    async with BROWSER_SEMAPHORE:
        try:
            # Запускаем и сохраняем драйвер в памяти для управления
            driver, ua, res, plat = await asyncio.to_thread(get_driver, phone, headless=True)
            active_drivers[phone] = {
                "driver": driver, 
                "ua": ua, 
                "res": res, 
                "plat": plat
            }
            
            await asyncio.to_thread(driver.get, "https://web.whatsapp.com")
            await m.edit_text(f"✅ Браузер запущен!\nUA: {plat}\nЖми кнопки:", reply_markup=kb_control(phone))
            
        except Exception as e:
            logger.error(f"Error launching {phone}: {e}")
            await m.edit_text(f"❌ Ошибка запуска: {str(e)[:50]}")

@dp.callback_query(F.data.startswith("scr_"))
async def make_screenshot(cb: types.CallbackQuery):
    phone = cb.data.split("_")[1]
    data = active_drivers.get(phone)
    if not data: return await cb.answer("Браузер закрыт", show_alert=True)
    
    try:
        png = await asyncio.to_thread(data["driver"].get_screenshot_as_png)
        await cb.message.answer_photo(BufferedInputFile(png, filename="screen.png"), caption=f"Status: {phone}")
        await cb.answer()
    except Exception as e:
        await cb.answer(f"Ошибка скрина: {e}", show_alert=True)

@dp.callback_query(F.data.startswith("lnk_"))
async def click_link(cb: types.CallbackQuery):
    phone = cb.data.split("_")[1]
    data = active_drivers.get(phone)
    if not data: return
    
    driver = data["driver"]
    # Универсальный кликер по тексту
    script = """
    var xpaths = [
        "//*[contains(text(), 'Link with phone')]", 
        "//*[contains(text(), 'Связать с номером')]",
        "//*[contains(text(), 'Log in with phone')]"
    ];
    for (var i=0; i<xpaths.length; i++) {
        var el = document.evaluate(xpaths[i], document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
        if (el) { el.click(); return true; }
    }
    return false;
    """
    res = driver.execute_script(script)
    if res: await cb.answer("✅ Клик прошел")
    else: await cb.answer("❌ Кнопка не найдена (проверь скрин)", show_alert=True)

@dp.callback_query(F.data.startswith("typ_"))
async def nuclear_input_handler(cb: types.CallbackQuery):
    phone = cb.data.split("_")[1]
    data = active_drivers.get(phone)
    if not data: return

    driver = data["driver"]
    
    # ☢️ NUCLEAR INPUT METHOD ☢️
    js_input = f"""
    var input = document.querySelector('input[type="text"]') || document.querySelector('div[contenteditable="true"]');
    if (input) {{
        input.focus();
        document.execCommand('insertText', false, '{phone}');
        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
        input.dispatchEvent(new Event('change', {{ bubbles: true }}));
        return true;
    }}
    return false;
    """
    
    if driver.execute_script(js_input):
        await cb.answer("✅ Номер введен через JS Engine!")
        # Авто-клик на Далее
        await asyncio.sleep(1)
        driver.execute_script("var b = document.querySelector('button.type-primary') || document.querySelector('[role=\"button\"]'); if(b) b.click();")
    else:
        await cb.answer("❌ Поле ввода не найдено", show_alert=True)

@dp.callback_query(F.data.startswith("sav_"))
async def save_account(cb: types.CallbackQuery):
    phone = cb.data.split("_")[1]
    data = active_drivers.pop(phone, None)
    
    if data:
        # Сохраняем метаданные в БД
        db_save_account(phone, data['ua'], data['res'], data['plat'])
        # Закрываем браузер для экономии памяти
        try: data["driver"].quit()
        except: pass
    
    await cb.message.edit_text(f"✅ Аккаунт {phone} сохранен и добавлен в очередь фарма.")

# ==========================================
# 🚜 FARMING ENGINE (BACKGROUND)
# ==========================================

async def farm_task(phone):
    """Один цикл активности для аккаунта"""
    driver = None
    try:
        if is_memory_critical(): return

        async with BROWSER_SEMAPHORE:
            driver, ua, res, plat = await asyncio.to_thread(get_driver, phone, headless=True)
            logger.info(f"🚜 Farm start: {phone}")
            
            await asyncio.to_thread(driver.get, "https://web.whatsapp.com")
            
            # Ждем прогрузки (по элементу чатов)
            wait = WebDriverWait(driver, 40)
            try:
                wait.until(EC.presence_of_element_located((By.ID, "side")))
            except TimeoutException:
                logger.warning(f"Timeout login: {phone}")
                return

            await asyncio.sleep(random.randint(5, 10))

            # --- SOLO MODE: Пишем в "Избранное" (своему номеру) ---
            # Это самый безопасный прогрев
            if random.random() < 0.7: # 70% вероятность действия
                driver.get(f"https://web.whatsapp.com/send?phone={phone}")
                
                inp_xpath = "//div[@contenteditable='true'][@data-tab='10']"
                inp = wait.until(EC.presence_of_element_located((By.XPATH, inp_xpath)))
                
                # 🔥 FAKER ДЕЛАЕТ УНИКАЛЬНОЕ СООБЩЕНИЕ 🔥
                unique_text = fake.sentence(nb_words=random.randint(3, 10))
                
                await human_type(inp, unique_text)
                await asyncio.sleep(1)
                inp.send_keys(Keys.ENTER)
                
                logger.info(f"Message sent for {phone}: {unique_text}")
                db_update_activity(phone)

            await asyncio.sleep(random.randint(5, 10))

    except Exception as e:
        logger.error(f"Farm Error {phone}: {e}")
    finally:
        if driver:
            try: await asyncio.to_thread(driver.quit)
            except: pass

async def farm_loop():
    """Главный цикл распределения задач"""
    logger.info("🔥 Farm Loop Started")
    while True:
        await asyncio.sleep(45) # Пауза между проверками
        
        try:
            conn = sqlite3.connect(DB_PATH, timeout=10)
            cur = conn.cursor()
            
            # 🧠 SHARDING LOGIC:
            # (ID аккаунта % Общее кол-во инстансов) должно совпадать с (Мой ID - 1)
            # Это гарантирует, что разные контейнеры не возьмут один аккаунт
            query = f"""
                SELECT phone_number FROM accounts 
                WHERE status='active' 
                AND (rowid % {TOTAL_INSTANCES}) = ({INSTANCE_ID} - 1)
                ORDER BY last_active ASC LIMIT 1
            """
            target = cur.execute(query).fetchone()
            conn.close()

            if target:
                # Если нашли подходящий аккаунт, запускаем задачу
                # create_task позволяет не блокировать основной цикл, но семафор внутри farm_task не даст запустить лишнее
                asyncio.create_task(farm_task(target[0]))
            
        except Exception as e:
            logger.error(f"Loop error: {e}")

# ==========================================
# 🚀 ЗАПУСК
# ==========================================

async def main():
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN is missing!")
        return

    db_init()
    
    # Запускаем фоновый процесс фарма
    asyncio.create_task(farm_loop())
    
    # Запускаем бота
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info(f"Bot started on Instance {INSTANCE_ID}...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
