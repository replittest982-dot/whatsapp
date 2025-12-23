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

# --- СТОРОННИЕ БИБЛИОТЕКИ ---
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
from selenium.common.exceptions import WebDriverException, TimeoutException

# ==========================================
# ⚙️ КОНФИГУРАЦИЯ (Из твоего ZIP + Новые)
# ==========================================

# Основные переменные
BOT_TOKEN = os.environ.get("BOT_TOKEN")
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except ValueError:
    ADMIN_ID = 0

# Настройки мульти-инстанса (Sharding)
try:
    INSTANCE_ID = int(os.environ.get("INSTANCE_ID", 1))
    TOTAL_INSTANCES = int(os.environ.get("TOTAL_INSTANCES", 1))
except ValueError:
    INSTANCE_ID = 1
    TOTAL_INSTANCES = 1

# Лимиты и Пути
# СТРОГО 1 БРАУЗЕР (Для стабильности на BotHost)
BROWSER_SEMAPHORE = asyncio.Semaphore(1) 
DB_NAME = 'imperator_ultimate.db'
SESSIONS_DIR = os.path.abspath("./sessions")

# Настройки Фарма
FARM_DELAY_MIN = 40
FARM_DELAY_MAX = 80

# Инициализация
logging.basicConfig(
    level=logging.INFO,
    format=f'%(asctime)s | INST-{INSTANCE_ID} | %(levelname)s | %(name)s | %(message)s'
)
logger = logging.getLogger("ImperatorV16")
fake = Faker('ru_RU')

# Создание папок
if not os.path.exists(SESSIONS_DIR):
    os.makedirs(SESSIONS_DIR)

# База устройств (User-Agents)
DEVICES = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36", "res": "1920,1080", "plat": "Win32"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "res": "1440,900", "plat": "MacIntel"},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36", "res": "1366,768", "plat": "Linux x86_64"}
]

# Кэш активных драйверов
ACTIVE_DRIVERS = {}

# Состояния FSM
class BotStates(StatesGroup):
    waiting_phone = State()

# ==========================================
# 🛠 ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================

def cleanup_zombie_processes():
    """Убивает зависшие процессы Chrome при старте"""
    killed = 0
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            if proc.info['name'] in ['chrome', 'chromedriver', 'google-chrome']:
                proc.kill()
                killed += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if killed > 0:
        logger.info(f"🧹 Cleaned up {killed} zombie processes.")

def is_memory_critical():
    """Memory Guard: Проверка свободной RAM"""
    mem = psutil.virtual_memory()
    free_mb = mem.available / 1024 / 1024
    if free_mb < 200:
        logger.warning(f"⚠️ CRITICAL MEMORY: {free_mb:.1f} MB free. Operations paused.")
        return True
    return False

def validate_phone(phone: str) -> bool:
    return phone.isdigit() and 7 <= len(phone) <= 15

# ==========================================
# 🗄️ БАЗА ДАННЫХ (SQLite)
# ==========================================

def db_init():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # Таблица аккаунтов (расширенная)
    c.execute('''CREATE TABLE IF NOT EXISTS accounts (
        phone_number TEXT PRIMARY KEY,
        status TEXT DEFAULT 'pending',
        user_agent TEXT,
        resolution TEXT,
        platform TEXT,
        last_active TIMESTAMP,
        messages_sent INTEGER DEFAULT 0
    )''')
    
    # Таблица доступа (Whitelist)
    c.execute('''CREATE TABLE IF NOT EXISTS whitelist (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        approved INTEGER DEFAULT 0
    )''')
    
    conn.commit()
    conn.close()
    logger.info("✅ Database initialized.")

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

def db_save_account_config(phone, ua, res, plat):
    conn = sqlite3.connect(DB_NAME)
    conn.execute("""
        INSERT OR REPLACE INTO accounts (phone_number, status, user_agent, resolution, platform, last_active)
        VALUES (?, 'active', ?, ?, ?, ?)
    """, (phone, ua, res, plat, datetime.now()))
    conn.commit()
    conn.close()

def db_get_farm_target():
    """Шардинг: выбираем аккаунт только для этого INSTANCE_ID"""
    conn = sqlite3.connect(DB_NAME)
    # Формула: rowid % TOTAL == INSTANCE - 1
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
# 🌐 SELENIUM ENGINE (STEALTH V16.3)
# ==========================================

def get_chromedriver(phone, ua=None, res=None, plat=None):
    """Создает драйвер с максимальной маскировкой"""
    
    # Если конфиг не передан, берем из базы или генерируем новый
    if not ua:
        conn = sqlite3.connect(DB_NAME)
        acc = conn.execute("SELECT user_agent, resolution, platform FROM accounts WHERE phone_number=?", (phone,)).fetchone()
        conn.close()
        if acc:
            ua, res, plat = acc
        else:
            dev = random.choice(DEVICES)
            ua, res, plat = dev['ua'], dev['res'], dev['plat']
    
    # Настройки Chrome
    options = Options()
    user_data_dir = os.path.join(SESSIONS_DIR, phone)
    options.add_argument(f"--user-data-dir={user_data_dir}")
    options.add_argument("--headless=new") # PRO режим
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"--user-agent={ua}")
    options.add_argument(f"--window-size={res}")
    options.add_argument("--lang=ru-RU,ru")
    options.page_load_strategy = 'eager'

    try:
        driver = webdriver.Chrome(options=options)
    except Exception as e:
        logger.error(f"Failed to start Chrome: {e}")
        return None, None, None, None

    # 🔥 CDP STEALTH INJECTION 🔥
    # 1. Скрываем webdriver
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.navigator.chrome = { runtime: {} };
        """
    })
    
    # 2. Подмена Платформы
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": f"Object.defineProperty(navigator, 'platform', {{get: () => '{plat}'}});"
    })

    # 3. Подмена Гео (Алматы)
    driver.execute_cdp_cmd("Emulation.setGeolocationOverride", {
        "latitude": 43.2389,
        "longitude": 76.8897,
        "accuracy": 100
    })

    # 4. Подмена Времени
    driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {
        "timezoneId": "Asia/Almaty"
    })

    return driver, ua, res, plat

# ==========================================
# 🤖 BOT HANDLERS & UI
# ==========================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Клавиатуры
def kb_admin_approval(user_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"approve_{user_id}"),
         InlineKeyboardButton(text="🚫 Отклонить", callback_data=f"reject_{user_id}")]
    ])

def kb_main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Новый аккаунт", callback_data="add_account")],
        [InlineKeyboardButton(text="📊 Статус системы", callback_data="system_status")]
    ])

def kb_browser_control(phone):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 1. ЧЕК (Скрин)", callback_data=f"screen_{phone}")],
        [InlineKeyboardButton(text="🔗 2. КЛИК 'ВХОД'", callback_data=f"link_{phone}")],
        [InlineKeyboardButton(text="⌨️ 3. ВВОД НОМЕРА", callback_data=f"type_{phone}")],
        [InlineKeyboardButton(text="➡️ 4. НАЖАТЬ 'ОК'", callback_data=f"next_{phone}")],
        [InlineKeyboardButton(text="✅ 5. Я ВОШЕЛ (Сохранить)", callback_data=f"save_{phone}")],
        [InlineKeyboardButton(text="🗑 УДАЛИТЬ СЕССИЮ", callback_data=f"delete_{phone}")]
    ])

# --- START & AUTH ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username
    
    if db_check_access(user_id):
        await message.answer(f"🔱 **Imperator v16.3 Ultimate**\nИнстанс: {INSTANCE_ID}\nRAM Guard: Active", reply_markup=kb_main_menu())
    else:
        db_register_request(user_id, username)
        await message.answer("🔒 **Доступ заблокирован.**\nВаша заявка отправлена администратору.")
        if ADMIN_ID:
            await bot.send_message(
                ADMIN_ID, 
                f"👤 **Новая заявка!**\nID: {user_id}\nUser: @{username}", 
                reply_markup=kb_admin_approval(user_id)
            )

@dp.callback_query(F.data.startswith("approve_"))
async def cb_approve(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    target_id = int(callback.data.split("_")[1])
    db_approve_user(target_id, True)
    await callback.message.edit_text(f"✅ Пользователь {target_id} допущен.")
    try: await bot.send_message(target_id, "✅ **Доступ разрешен!** Нажмите /start")
    except: pass

@dp.callback_query(F.data.startswith("reject_"))
async def cb_reject(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    target_id = int(callback.data.split("_")[1])
    db_approve_user(target_id, False)
    await callback.message.edit_text(f"🚫 Пользователь {target_id} отклонен.")

# --- ADD ACCOUNT FLOW ---
@dp.callback_query(F.data == "add_account")
async def cb_add_account(callback: types.CallbackQuery, state: FSMContext):
    if is_memory_critical():
        return await callback.answer("❌ Недостаточно памяти (RAM < 200MB)", show_alert=True)
    
    await callback.message.answer("📞 Введите номер телефона (только цифры, 7-15 символов):")
    await state.set_state(BotStates.waiting_phone)

@dp.message(BotStates.waiting_phone)
async def process_phone_input(message: types.Message, state: FSMContext):
    phone = "".join(filter(str.isdigit, message.text))
    await state.clear()
    
    if not validate_phone(phone):
        return await message.answer("❌ Некорректный формат номера. Попробуйте снова.")
    
    status_msg = await message.answer(f"🚀 Инициализация драйвера для {phone}...")
    
    async with BROWSER_SEMAPHORE:
        try:
            driver, ua, res, plat = await asyncio.to_thread(get_chromedriver, phone)
            if not driver:
                return await status_msg.edit_text("❌ Ошибка запуска драйвера.")
            
            ACTIVE_DRIVERS[phone] = {"driver": driver, "ua": ua, "res": res, "plat": plat}
            
            await asyncio.to_thread(driver.get, "https://web.whatsapp.com")
            
            await status_msg.edit_text(
                f"✅ **Браузер запущен!**\n📱 Номер: `{phone}`\n💻 Plat: {plat}\n\n👇 **Используй кнопки по порядку:**", 
                reply_markup=kb_browser_control(phone)
            )
        except Exception as e:
            logger.error(f"Manual start error: {e}")
            await status_msg.edit_text(f"❌ Критическая ошибка: {e}")

# --- BROWSER CONTROL ACTIONS ---
@dp.callback_query(F.data.startswith("screen_"))
async def cb_screen(callback: types.CallbackQuery):
    phone = callback.data.split("_")[1]
    data = ACTIVE_DRIVERS.get(phone)
    if not data: return await callback.answer("Сессия не найдена!", show_alert=True)
    
    try:
        png = await asyncio.to_thread(data['driver'].get_screenshot_as_png)
        await callback.message.answer_photo(BufferedInputFile(png, "screen.png"), caption=f"Status: {phone}")
    except Exception as e:
        await callback.answer(f"Ошибка скрина: {e}", show_alert=True)
    await callback.answer()

@dp.callback_query(F.data.startswith("link_"))
async def cb_click_link(callback: types.CallbackQuery):
    phone = callback.data.split("_")[1]
    driver = ACTIVE_DRIVERS.get(phone, {}).get('driver')
    if not driver: return
    
    # JS Clicker v2
    js = """
    var xpaths = ["//*[contains(text(), 'Link with phone')]", "//*[contains(text(), 'Связать')]", "//*[contains(text(), 'Log in')]"];
    for(var i=0; i<xpaths.length; i++){
        var res = document.evaluate(xpaths[i], document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
        if(res.singleNodeValue){
            res.singleNodeValue.click();
            return true;
        }
    }
    return false;
    """
    res = driver.execute_script(js)
    await callback.answer("Клик выполнен" if res else "Кнопка не найдена", show_alert=not res)

@dp.callback_query(F.data.startswith("type_"))
async def cb_type_number(callback: types.CallbackQuery):
    phone = callback.data.split("_")[1]
    driver = ACTIVE_DRIVERS.get(phone, {}).get('driver')
    if not driver: return
    
    # Nuclear JS Input
    js = f"""
    var input = document.querySelector('input[type="text"]') || document.querySelector('div[contenteditable="true"]');
    if(input) {{
        input.focus();
        document.execCommand('insertText', false, '{phone}');
        input.dispatchEvent(new Event('input', {{bubbles: true}}));
        input.dispatchEvent(new Event('change', {{bubbles: true}}));
        return true;
    }}
    return false;
    """
    res = driver.execute_script(js)
    await callback.answer("Номер введен!" if res else "Поле ввода не найдено", show_alert=not res)

@dp.callback_query(F.data.startswith("next_"))
async def cb_click_next(callback: types.CallbackQuery):
    phone = callback.data.split("_")[1]
    driver = ACTIVE_DRIVERS.get(phone, {}).get('driver')
    if not driver: return
    
    # Smart Button Finder
    js = """
    var buttons = document.querySelectorAll('button, [role="button"]');
    for(var i=0; i<buttons.length; i++) {
        var t = buttons[i].innerText.toLowerCase();
        if(t.includes('next') || t.includes('далее') || t.includes('ok')) {
            buttons[i].click();
            return true;
        }
    }
    // Fallback: Primary button
    var p = document.querySelector('button.type-primary');
    if(p) { p.click(); return true; }
    return false;
    """
    res = driver.execute_script(js)
    await callback.answer("Нажато ОК/Далее" if res else "Кнопка не найдена", show_alert=not res)

@dp.callback_query(F.data.startswith("save_"))
async def cb_save_session(callback: types.CallbackQuery):
    phone = callback.data.split("_")[1]
    data = ACTIVE_DRIVERS.pop(phone, None)
    
    if data:
        db_save_account_config(phone, data['ua'], data['res'], data['plat'])
        try:
            await asyncio.to_thread(data['driver'].quit)
        except: pass
        
    await callback.message.edit_text(f"✅ **Сессия {phone} сохранена!**\nБраузер закрыт для экономии памяти.\nАккаунт добавлен в очередь фарма.")

@dp.callback_query(F.data.startswith("delete_"))
async def cb_delete_session(callback: types.CallbackQuery):
    phone = callback.data.split("_")[1]
    
    # 1. Kill driver
    data = ACTIVE_DRIVERS.pop(phone, None)
    if data:
        try: await asyncio.to_thread(data['driver'].quit)
        except: pass
    
    # 2. Delete Folder
    try:
        shutil.rmtree(os.path.join(SESSIONS_DIR, phone), ignore_errors=True)
    except: pass
    
    # 3. DB Clean
    db_delete_account(phone)
    
    await callback.message.edit_text(f"🗑 Аккаунт {phone} полностью удален.")

@dp.callback_query(F.data == "system_status")
async def cb_system_status(callback: types.CallbackQuery):
    mem = psutil.virtual_memory()
    msg = (
        f"🖥 **System Status (Inst #{INSTANCE_ID})**\n"
        f"🧠 RAM Free: {mem.available / 1024 / 1024:.0f} MB\n"
        f"🔌 Active Manual Sessions: {len(ACTIVE_DRIVERS)}"
    )
    await callback.answer(msg, show_alert=True)

# ==========================================
# 🚜 ФАРМИНГ (SOLO MODE - WRITE TO SELF)
# ==========================================

async def farm_worker(phone):
    """Рабочий процесс одного аккаунта"""
    if is_memory_critical(): return

    async with BROWSER_SEMAPHORE:
        driver = None
        try:
            logger.info(f"🚜 Farming: {phone}")
            driver, ua, res, plat = await asyncio.to_thread(get_chromedriver, phone)
            
            # 1. Открываем чат с самим собой
            target_url = f"https://web.whatsapp.com/send?phone={phone}"
            await asyncio.to_thread(driver.get, target_url)
            
            wait = WebDriverWait(driver, 60)
            
            # 2. Ищем поле ввода (Универсальный селектор через Footer)
            inp = wait.until(EC.presence_of_element_located((By.XPATH, "//footer//div[@contenteditable='true']")))
            
            # 3. Печатаем уникальный текст
            message = fake.sentence()
            for char in message:
                inp.send_keys(char)
                await asyncio.sleep(random.uniform(0.05, 0.15))
            
            await asyncio.sleep(1)
            inp.send_keys(Keys.ENTER)
            
            # 4. Фиксируем успех
            db_update_activity(phone)
            logger.info(f"✅ Farm done for {phone}")
            
            await asyncio.sleep(5)
            
        except Exception as e:
            logger.error(f"Farm failed for {phone}: {e}")
        finally:
            if driver:
                try: await asyncio.to_thread(driver.quit)
                except: pass

async def farm_loop():
    """Фоновый цикл распределения задач"""
    logger.info("🔥 Farm Loop Started")
    while True:
        try:
            # Пауза
            await asyncio.sleep(random.randint(FARM_DELAY_MIN, FARM_DELAY_MAX))
            
            # 1. Берем кандидата из базы (только для этого инстанса)
            target = db_get_farm_target() # Возвращает кортеж или None
            
            if target:
                phone = target[0]
                # Если этот номер сейчас не занят ручным управлением
                if phone not in ACTIVE_DRIVERS:
                     # Запускаем таск (Семафор внутри не даст запустить лишнего)
                     asyncio.create_task(farm_worker(phone))
            
        except Exception as e:
            logger.error(f"Farm Loop Error: {e}")
            await asyncio.sleep(10)

# ==========================================
# 🚀 MAIN ENTRY POINT
# ==========================================

async def main():
    # Очистка зомби
    cleanup_zombie_processes()
    
    # Инит
    db_init()
    
    # Запуск фонового фарма
    asyncio.create_task(farm_loop())
    
    # Запуск бота
    logger.info(f"🚀 Bot Instance {INSTANCE_ID} Started!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
