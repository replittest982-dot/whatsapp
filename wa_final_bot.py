import asyncio
import os
import logging
import sqlite3
import random
import re
import string
import psutil
import shutil
import signal
import sys
from datetime import datetime
from typing import Optional, List, Dict

# --- СТОРОННИЕ БИБЛИОТЕКИ (Из requirements.txt) ---
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from faker import Faker

# --- SELENIUM (Версия 4.x) ---
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import WebDriverException, TimeoutException, NoSuchElementException

# ==========================================
# ⚙️ КОНФИГУРАЦИЯ (BASE + NEW)
# ==========================================

# 1. Основные токены
BOT_TOKEN = os.environ.get("BOT_TOKEN")
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except ValueError:
    ADMIN_ID = 0

# 2. Настройки Инстансов (Sharding)
try:
    INSTANCE_ID = int(os.environ.get("INSTANCE_ID", 1))
    TOTAL_INSTANCES = int(os.environ.get("TOTAL_INSTANCES", 1))
except ValueError:
    INSTANCE_ID = 1
    TOTAL_INSTANCES = 1

# 3. Лимиты ресурсов
# СТРОГО 1 БРАУЗЕР НА КОНТЕЙНЕР (Для стабильности на BotHost)
BROWSER_SEMAPHORE = asyncio.Semaphore(1) 

# 4. Пути и БД
DB_NAME = 'imperator_ultimate_v16.db'
SESSIONS_DIR = os.path.abspath("./sessions")

# 5. Настройки Фарма (из твоего кода)
FARM_DELAY_MIN = 40
FARM_DELAY_MAX = 80

# 6. Логирование (Твой формат)
logging.basicConfig(
    level=logging.INFO,
    format=f'%(asctime)s | INST-{INSTANCE_ID} | %(levelname)s | %(name)s | %(message)s'
)
logger = logging.getLogger("ImperatorBot")
fake = Faker('ru_RU')

# Создаем папку сессий
if not os.path.exists(SESSIONS_DIR):
    os.makedirs(SESSIONS_DIR)

# 7. База устройств (User-Agents + Resolution + Platform)
DEVICES = [
    {
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "res": "1920,1080",
        "plat": "Win32"
    },
    {
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "res": "1440,900",
        "plat": "MacIntel"
    },
    {
        "ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "res": "1366,768",
        "plat": "Linux x86_64"
    }
]

# Кэш активных драйверов
ACTIVE_DRIVERS = {}

# Состояния бота
class BotStates(StatesGroup):
    waiting_phone = State()

# ==========================================
# 🛠 SYSTEM GUARD & UTILS
# ==========================================

def cleanup_zombie_processes():
    """
    Убивает зависшие процессы Chrome/Chromedriver при старте скрипта.
    Это критично для Linux-контейнеров.
    """
    killed_count = 0
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            if proc.info['name'] in ['chrome', 'chromedriver', 'google-chrome']:
                proc.kill()
                killed_count += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if killed_count > 0:
        logger.warning(f"🧹 Zombie Cleanup: Killed {killed_count} processes.")

def get_server_load_status():
    """
    Проверяет нагрузку на сервер.
    Возвращает строку с ошибкой или None, если все ок.
    """
    # 1. Проверка RAM (Критический порог 200MB)
    mem = psutil.virtual_memory()
    free_mb = mem.available / 1024 / 1024
    if free_mb < 200:
        return f"CRITICAL RAM ({free_mb:.0f}MB free)"
    
    # 2. Проверка CPU (Критический порог 85%)
    cpu_usage = psutil.cpu_percent(interval=0.2)
    if cpu_usage > 85:
        return f"CPU OVERLOAD ({cpu_usage}%)"
    
    return None

def validate_phone(phone: str) -> bool:
    """Проверка длины номера (7-15 цифр)"""
    return phone.isdigit() and 7 <= len(phone) <= 15

# ==========================================
# 🗄️ DATABASE ENGINE (SQLite)
# ==========================================

def db_init():
    conn = sqlite3.connect(DB_NAME, timeout=10)
    c = conn.cursor()
    
    # Таблица аккаунтов (расширенная для хранения конфига устройства)
    c.execute('''CREATE TABLE IF NOT EXISTS accounts (
        phone_number TEXT PRIMARY KEY,
        status TEXT DEFAULT 'pending',
        user_agent TEXT,
        resolution TEXT,
        platform TEXT,
        last_active TIMESTAMP,
        messages_sent INTEGER DEFAULT 0
    )''')
    
    # Таблица доступа (Whitelist для админки)
    c.execute('''CREATE TABLE IF NOT EXISTS whitelist (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        approved INTEGER DEFAULT 0
    )''')
    
    conn.commit()
    conn.close()
    logger.info("✅ Database initialized successfully.")

# --- Access Logic ---
def db_check_access(user_id):
    if user_id == ADMIN_ID: return True
    conn = sqlite3.connect(DB_NAME)
    res = conn.execute("SELECT approved FROM whitelist WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return res and res[0] == 1

def db_register_request(user_id, username):
    conn = sqlite3.connect(DB_NAME)
    conn.execute("INSERT OR IGNORE INTO whitelist (user_id, username, approved) VALUES (?, ?, 0)", (user_id, username))
    conn.commit()
    conn.close()

def db_approve_user(user_id, is_approved):
    conn = sqlite3.connect(DB_NAME)
    if is_approved:
        conn.execute("UPDATE whitelist SET approved=1 WHERE user_id=?", (user_id,))
    else:
        conn.execute("DELETE FROM whitelist WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

# --- Account Logic ---
def db_save_account(phone, ua, res, plat):
    conn = sqlite3.connect(DB_NAME)
    conn.execute("""
        INSERT OR REPLACE INTO accounts (phone_number, status, user_agent, resolution, platform, last_active)
        VALUES (?, 'active', ?, ?, ?, ?)
    """, (phone, ua, res, plat, datetime.now()))
    conn.commit()
    conn.close()

def db_get_farm_target():
    """
    SHARDING LOGIC:
    Возвращает аккаунт, который должен обрабатываться ИМЕННО ЭТИМ инстансом.
    Формула: (rowid % TOTAL_INSTANCES) == (INSTANCE_ID - 1)
    """
    conn = sqlite3.connect(DB_NAME)
    query = f"""
        SELECT phone_number, user_agent, resolution, platform 
        FROM accounts 
        WHERE status='active' 
        AND (rowid % {TOTAL_INSTANCES}) = ({INSTANCE_ID} - 1)
        ORDER BY last_active ASC LIMIT 1
    """
    res = conn.execute(query).fetchone()
    conn.close()
    return res

def db_update_activity(phone):
    conn = sqlite3.connect(DB_NAME)
    conn.execute("UPDATE accounts SET last_active=?, messages_sent=messages_sent+1 WHERE phone_number=?", 
                 (datetime.now(), phone))
    conn.commit()
    conn.close()

def db_delete_account(phone):
    conn = sqlite3.connect(DB_NAME)
    conn.execute("DELETE FROM accounts WHERE phone_number=?", (phone,))
    conn.commit()
    conn.close()

# ==========================================
# 🌐 SELENIUM ENGINE (ULTIMATE STEALTH)
# ==========================================

def get_chromedriver(phone, ua=None, res=None, plat=None):
    """
    Запускает Chrome с CDP-инъекциями для маскировки под Алматы.
    """
    # Если параметры не переданы, пытаемся найти в БД
    if not ua:
        conn = sqlite3.connect(DB_NAME)
        acc = conn.execute("SELECT user_agent, resolution, platform FROM accounts WHERE phone_number=?", (phone,)).fetchone()
        conn.close()
        if acc:
            ua, res, plat = acc
        else:
            # Генерация нового устройства
            dev = random.choice(DEVICES)
            ua, res, plat = dev['ua'], dev['res'], dev['plat']
    
    options = Options()
    user_data_dir = os.path.join(SESSIONS_DIR, phone)
    options.add_argument(f"--user-data-dir={user_data_dir}")
    
    # High-Load Config
    options.add_argument("--headless=new") 
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-software-rasterizer")
    
    # Device Spoofing
    options.add_argument(f"--user-agent={ua}")
    options.add_argument(f"--window-size={res}")
    options.add_argument("--lang=ru-RU,ru")
    options.page_load_strategy = 'eager' # Быстрая загрузка

    try:
        driver = webdriver.Chrome(options=options)
    except Exception as e:
        logger.critical(f"Driver Start Fail: {e}")
        return None, None, None, None

    # 🔥 CDP STEALTH MAGIC 🔥
    
    # 1. Скрытие WebDriver
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.navigator.chrome = { runtime: {} };
        """
    })
    
    # 2. Подмена Platform
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": f"Object.defineProperty(navigator, 'platform', {{get: () => '{plat}'}});"
    })

    # 3. Подмена Гео (Kazakhstan, Almaty)
    driver.execute_cdp_cmd("Emulation.setGeolocationOverride", {
        "latitude": 43.2389,
        "longitude": 76.8897,
        "accuracy": 100
    })

    # 4. Подмена Timezone
    driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {
        "timezoneId": "Asia/Almaty"
    })

    return driver, ua, res, plat

# ==========================================
# 🤖 BOT INTERFACE (AIOGRAM)
# ==========================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- KEYBOARDS ---

def kb_admin_decision(user_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"approve_{user_id}"),
         InlineKeyboardButton(text="🚫 Отклонить", callback_data=f"reject_{user_id}")]
    ])

def kb_main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Новый аккаунт", callback_data="add_acc")],
        [InlineKeyboardButton(text="📊 Статус системы", callback_data="sys_stat")]
    ])

def kb_browser_actions(phone):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 1. ЧЕК (Скрин)", callback_data=f"scr_{phone}")],
        [InlineKeyboardButton(text="🔗 2. НАЖАТЬ 'ВХОД'", callback_data=f"lnk_{phone}")],
        [InlineKeyboardButton(text="⌨️ 3. ВВЕСТИ НОМЕР", callback_data=f"typ_{phone}")],
        [InlineKeyboardButton(text="➡️ 4. ЖМИ 'ДАЛЕЕ/ОК'", callback_data=f"nxt_{phone}")],
        [InlineKeyboardButton(text="✅ 5. Я ВОШЕЛ (Сохр.)", callback_data=f"sav_{phone}")],
        [InlineKeyboardButton(text="🗑 УДАЛИТЬ СЕССИЮ", callback_data=f"del_{phone}")]
    ])

# --- ACCESS HANDLERS ---

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username
    
    # Проверка доступа
    if db_check_access(user_id):
        # Проверка нагрузки на сервер
        load_warning = get_server_load_status()
        status_text = f"Online 🟢" if not load_warning else f"⚠️ High Load: {load_warning}"
        
        await message.answer(
            f"🔱 **Imperator v16.3 Ultimate**\n"
            f"👤 User: {username}\n"
            f"🤖 Instance: {INSTANCE_ID}/{TOTAL_INSTANCES}\n"
            f"🖥 Status: {status_text}",
            reply_markup=kb_main_menu()
        )
    else:
        # Регистрация заявки
        db_register_request(user_id, username)
        await message.answer("🔒 **Доступ ограничен.**\nВаша заявка отправлена администратору.")
        if ADMIN_ID:
            await bot.send_message(
                ADMIN_ID,
                f"👤 **Новая заявка на доступ!**\nID: {user_id}\nUser: @{username}",
                reply_markup=kb_admin_decision(user_id)
            )

@dp.callback_query(F.data.startswith("approve_"))
async def cb_approve(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    target_id = int(call.data.split("_")[1])
    db_approve_user(target_id, True)
    await call.message.edit_text(f"✅ Пользователь {target_id} одобрен.")
    try: await bot.send_message(target_id, "✅ **Доступ разрешен!**\nНажмите /start для начала.")
    except: pass

@dp.callback_query(F.data.startswith("reject_"))
async def cb_reject(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    target_id = int(call.data.split("_")[1])
    db_approve_user(target_id, False)
    await call.message.edit_text(f"🚫 Пользователь {target_id} отклонен.")

# --- BROWSER MANAGEMENT HANDLERS ---

@dp.callback_query(F.data == "add_acc")
async def cb_add_acc(call: types.CallbackQuery, state: FSMContext):
    # Guard: Проверка нагрузки
    load_err = get_server_load_status()
    if load_err:
        return await call.answer(f"⚠️ Сервер перегружен: {load_err}\nИспользуйте другой инстанс!", show_alert=True)
    
    await call.message.answer("📞 Введите номер телефона (только цифры):")
    await state.set_state(BotStates.waiting_phone)

@dp.message(BotStates.waiting_phone)
async def process_phone(message: types.Message, state: FSMContext):
    phone = "".join(filter(str.isdigit, message.text))
    await state.clear()
    
    # 1. Валидация
    if not validate_phone(phone):
        return await message.answer("❌ Некорректный номер (должен быть 7-15 цифр).")
    
    status_msg = await message.answer(f"🚀 Запускаю Chrome для {phone}...")
    
    async with BROWSER_SEMAPHORE:
        try:
            # 2. Запуск драйвера
            driver, ua, res, plat = await asyncio.to_thread(get_chromedriver, phone)
            
            if not driver:
                return await status_msg.edit_text("❌ Ошибка запуска драйвера.")
            
            ACTIVE_DRIVERS[phone] = {"driver": driver, "ua": ua, "res": res, "plat": plat}
            
            await asyncio.to_thread(driver.get, "https://web.whatsapp.com")
            
            await status_msg.edit_text(
                f"✅ **Браузер готов!**\n📱 Номер: `{phone}`\n💻 Device: {plat}\n👇 Управляйте кнопками:",
                reply_markup=kb_browser_actions(phone)
            )
        except Exception as e:
            logger.error(f"Init Error: {e}")
            await status_msg.edit_text(f"❌ Критическая ошибка: {str(e)[:100]}")

@dp.callback_query(F.data.startswith("scr_"))
async def cb_screen(call: types.CallbackQuery):
    phone = call.data.split("_")[1]
    data = ACTIVE_DRIVERS.get(phone)
    if not data: return await call.answer("Сессия потеряна", show_alert=True)
    
    try:
        png = await asyncio.to_thread(data['driver'].get_screenshot_as_png)
        await call.message.answer_photo(BufferedInputFile(png, "screen.png"), caption=f"Status: {phone}")
    except Exception as e:
        await call.answer(f"Ошибка скрина: {e}", show_alert=True)
    await call.answer()

@dp.callback_query(F.data.startswith("lnk_"))
async def cb_link(call: types.CallbackQuery):
    phone = call.data.split("_")[1]
    data = ACTIVE_DRIVERS.get(phone)
    if not data: return
    
    # JS: Поиск кнопки "Вход по номеру"
    js = """
    var xpaths = ["//*[contains(text(), 'Link with phone')]", "//*[contains(text(), 'Связать')]", "//*[contains(text(), 'Log in')]"];
    for(var i=0; i<xpaths.length; i++){
        var r = document.evaluate(xpaths[i], document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
        if(r){ r.click(); return true; }
    }
    return false;
    """
    res = data['driver'].execute_script(js)
    await call.answer("Нажато!" if res else "Кнопка не найдена", show_alert=not res)

@dp.callback_query(F.data.startswith("typ_"))
async def cb_type(call: types.CallbackQuery):
    phone = call.data.split("_")[1]
    data = ACTIVE_DRIVERS.get(phone)
    if not data: return
    
    # 🔥 JS: Ввод номера с принудительным '+' (Фикс для Valid Number)
    js = f"""
    var i = document.querySelector('input[type="text"]') || document.querySelector('div[contenteditable="true"]');
    if(i) {{
        i.focus();
        // Принудительно ставим + перед номером
        document.execCommand('insertText', false, '+{phone}');
        i.dispatchEvent(new Event('input', {{bubbles: true}}));
        i.dispatchEvent(new Event('change', {{bubbles: true}}));
        return true;
    }}
    return false;
    """
    res = data['driver'].execute_script(js)
    await call.answer(f"Введено: +{phone}" if res else "Поле ввода не найдено", show_alert=not res)

@dp.callback_query(F.data.startswith("nxt_"))
async def cb_next(call: types.CallbackQuery):
    phone = call.data.split("_")[1]
    data = ACTIVE_DRIVERS.get(phone)
    if not data: return
    
    # JS: Умный поиск кнопки Далее/ОК
    js = """
    var b = document.querySelectorAll('button, [role="button"]');
    for(var i=0; i<b.length; i++) {
        var t = b[i].innerText.toLowerCase();
        if(t.includes('next') || t.includes('далее') || t.includes('ok')) {
            b[i].click(); return true;
        }
    }
    var p = document.querySelector('button.type-primary');
    if(p){ p.click(); return true; }
    return false;
    """
    res = data['driver'].execute_script(js)
    await call.answer("Нажато ОК" if res else "Кнопка не найдена", show_alert=not res)

@dp.callback_query(F.data.startswith("sav_"))
async def cb_save(call: types.CallbackQuery):
    phone = call.data.split("_")[1]
    data = ACTIVE_DRIVERS.pop(phone, None)
    
    if data:
        # Сохраняем в БД
        db_save_account(phone, data['ua'], data['res'], data['plat'])
        # Убиваем процесс для очистки RAM
        try: await asyncio.to_thread(data['driver'].quit)
        except: pass
        
    await call.message.edit_text(f"✅ **Сессия {phone} сохранена!**\nБраузер закрыт. Аккаунт в очереди фарма.")

@dp.callback_query(F.data.startswith("del_"))
async def cb_del(call: types.CallbackQuery):
    phone = call.data.split("_")[1]
    
    # 1. Kill
    data = ACTIVE_DRIVERS.pop(phone, None)
    if data:
        try: await asyncio.to_thread(data['driver'].quit)
        except: pass
        
    # 2. Delete Files
    try: shutil.rmtree(os.path.join(SESSIONS_DIR, phone), ignore_errors=True)
    except: pass
    
    # 3. Delete DB
    db_delete_account(phone)
    
    await call.message.edit_text(f"🗑 Аккаунт {phone} полностью удален.")

@dp.callback_query(F.data == "sys_stat")
async def cb_sys_stat(call: types.CallbackQuery):
    mem = psutil.virtual_memory()
    cpu = psutil.cpu_percent()
    msg = (f"🖥 **System Status**\n"
           f"RAM Free: {mem.available/1024/1024:.0f} MB\n"
           f"CPU Load: {cpu}%\n"
           f"Drivers: {len(ACTIVE_DRIVERS)}\n"
           f"Instance: {INSTANCE_ID}")
    await call.answer(msg, show_alert=True)

# ==========================================
# 🚜 FARM LOOP (BACKGROUND WORKER)
# ==========================================

async def farm_worker(phone):
    """
    Процесс прогрева: Заходит -> Пишет самому себе -> Выходит
    """
    # Guard: Если сервер перегружен, пропускаем ход
    if get_server_load_status():
        logger.warning(f"Skipping farm for {phone} due to high load.")
        return

    async with BROWSER_SEMAPHORE:
        driver = None
        try:
            logger.info(f"🚜 Farming: {phone}")
            driver, ua, res, plat = await asyncio.to_thread(get_chromedriver, phone)
            
            # 1. Прямой заход в чат с собой
            target_url = f"https://web.whatsapp.com/send?phone={phone}"
            await asyncio.to_thread(driver.get, target_url)
            
            wait = WebDriverWait(driver, 60)
            
            # 2. Поиск поля ввода (Footer Selector - самый надежный)
            inp = wait.until(EC.presence_of_element_located((By.XPATH, "//footer//div[@contenteditable='true']")))
            
            # 3. Печать сообщения (Faker)
            msg_text = fake.sentence()
            for char in msg_text:
                inp.send_keys(char)
                await asyncio.sleep(random.uniform(0.05, 0.15))
            
            await asyncio.sleep(1)
            inp.send_keys(Keys.ENTER)
            
            # 4. Обновление статуса
            db_update_activity(phone)
            logger.info(f"✅ Farm Success: {phone}")
            
            await asyncio.sleep(5) # Короткая пауза перед выходом
            
        except Exception as e:
            logger.error(f"Farm Fail {phone}: {e}")
        finally:
            if driver:
                try: await asyncio.to_thread(driver.quit)
                except: pass

async def farm_loop():
    """Бесконечный цикл распределения задач"""
    logger.info("🔥 IMPERATOR FARM STARTED")
    while True:
        try:
            # Случайная задержка (имитация человека)
            await asyncio.sleep(random.randint(FARM_DELAY_MIN, FARM_DELAY_MAX))
            
            # Получаем аккаунт из БД (Только для этого INSTANCE_ID)
            target = db_get_farm_target()
            
            if target:
                phone = target[0]
                # Если аккаунт не занят ручным управлением
                if phone not in ACTIVE_DRIVERS:
                    # Запускаем в фоне (create_task не блокирует цикл)
                    asyncio.create_task(farm_worker(phone))
            
        except Exception as e:
            logger.error(f"Farm Loop Error: {e}")
            await asyncio.sleep(10)

# ==========================================
# 🚀 MAIN ENTRY POINT
# ==========================================

async def main():
    # 1. Очистка мусора
    cleanup_zombie_processes()
    
    # 2. Инициализация БД
    db_init()
    
    # 3. Запуск Фарма
    asyncio.create_task(farm_loop())
    
    # 4. Запуск Бота
    logger.info(f"🚀 Bot Instance {INSTANCE_ID} Started!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        if not BOT_TOKEN:
            logger.critical("BOT_TOKEN is missing!")
            sys.exit(1)
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
