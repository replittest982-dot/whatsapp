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
import json
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Union

# --- СТОРОННИЕ БИБЛИОТЕКИ (Aiogram 3.x) ---
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from faker import Faker

# --- SELENIUM 4.x ---
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import (
    WebDriverException, 
    TimeoutException, 
    NoSuchElementException, 
    StaleElementReferenceException
)

# ==========================================
# ⚙️ ПОЛНАЯ КОНФИГУРАЦИЯ ПРОЕКТА
# ==========================================

# 1. Основные переменные окружения
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# ID администратора (обязательно число)
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except (ValueError, TypeError):
    ADMIN_ID = 0

# 2. Настройки масштабирования (Sharding)
try:
    INSTANCE_ID = int(os.environ.get("INSTANCE_ID", 1))
    TOTAL_INSTANCES = int(os.environ.get("TOTAL_INSTANCES", 1))
except (ValueError, TypeError):
    INSTANCE_ID = 1
    TOTAL_INSTANCES = 1

# 3. Лимиты ресурсов
# Используем Semaphore(1), чтобы на одном инстансе работал только 1 браузер одновременно.
# Это критично для BotHost с ограниченной RAM.
BROWSER_SEMAPHORE = asyncio.Semaphore(1) 

# 4. Пути и База Данных
DB_NAME = 'imperator_ultimate_v16.db'
SESSIONS_DIR = os.path.abspath("./sessions")

# 5. Настройки Фарма
FARM_DELAY_MIN = 45  # Минимальная пауза между аккаунтами
FARM_DELAY_MAX = 90  # Максимальная пауза

# 6. Логирование (Детальный формат из твоего архива)
logging.basicConfig(
    level=logging.INFO,
    format=f'%(asctime)s | INST-{INSTANCE_ID} | %(levelname)s | %(name)s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger("ImperatorBot")
fake = Faker('ru_RU')

# Создаем директорию сессий, если нет
if not os.path.exists(SESSIONS_DIR):
    os.makedirs(SESSIONS_DIR)

# 7. База устройств (Spoofing)
# Реальные User-Agent'ы для маскировки под разные ОС
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

# Глобальный словарь активных драйверов (для ручного управления)
ACTIVE_DRIVERS = {}

# FSM Состояния
class BotStates(StatesGroup):
    waiting_phone = State()

# ==========================================
# 🛠 СИСТЕМНЫЕ УТИЛИТЫ И ЗАЩИТА
# ==========================================

def cleanup_zombie_processes():
    """
    Убивает 'зомби' процессы Chrome, которые могли остаться 
    после падения контейнера или ошибки скрипта.
    """
    killed_count = 0
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            # Ищем процессы хрома
            if proc.info['name'] in ['chrome', 'chromedriver', 'google-chrome']:
                proc.kill()
                killed_count += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if killed_count > 0:
        logger.warning(f"🧹 Zombie Cleanup: Killed {killed_count} processes to free RAM.")

def get_server_load_status() -> Optional[str]:
    """
    Проверяет нагрузку на сервер (CPU и RAM).
    Возвращает текст ошибки или None, если всё ок.
    """
    # 1. Проверка RAM (Критический порог < 200MB)
    mem = psutil.virtual_memory()
    free_mb = mem.available / 1024 / 1024
    if free_mb < 200:
        return f"CRITICAL RAM LOW ({free_mb:.0f}MB free)"
    
    # 2. Проверка CPU (Критический порог > 85%)
    # interval=0.5 нужен для точного замера моментальной нагрузки
    cpu_usage = psutil.cpu_percent(interval=0.5)
    if cpu_usage > 85:
        return f"CPU OVERLOAD ({cpu_usage}%)"
    
    return None

def validate_phone(phone: str) -> bool:
    """
    Валидация номера телефона.
    Должен состоять только из цифр и иметь длину от 7 до 15 символов.
    """
    return phone.isdigit() and 7 <= len(phone) <= 15

def format_duration(delta: timedelta) -> str:
    """Красивый вывод времени жизни аккаунта"""
    days = delta.days
    seconds = delta.seconds
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"{days}д {hours}ч {minutes}м"

# ==========================================
# 🗄️ ДВИЖОК БАЗЫ ДАННЫХ (SQLite)
# ==========================================

def db_init():
    """Инициализация таблиц базы данных"""
    conn = sqlite3.connect(DB_NAME, timeout=10)
    c = conn.cursor()
    
    # Таблица аккаунтов
    # Добавлено поле created_at для отслеживания времени жизни
    c.execute('''CREATE TABLE IF NOT EXISTS accounts (
        phone_number TEXT PRIMARY KEY,
        status TEXT DEFAULT 'pending',
        user_agent TEXT,
        resolution TEXT,
        platform TEXT,
        last_active TIMESTAMP,
        created_at TIMESTAMP,
        messages_sent INTEGER DEFAULT 0
    )''')
    
    # Таблица вайтлиста (доступ к боту)
    c.execute('''CREATE TABLE IF NOT EXISTS whitelist (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        approved INTEGER DEFAULT 0
    )''')
    
    conn.commit()
    conn.close()
    logger.info("✅ Database tables initialized.")

# --- Функции доступа ---
def db_check_access(user_id: int) -> bool:
    if user_id == ADMIN_ID: return True
    conn = sqlite3.connect(DB_NAME)
    res = conn.execute("SELECT approved FROM whitelist WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return res and res[0] == 1

def db_register_request(user_id: int, username: str):
    conn = sqlite3.connect(DB_NAME)
    conn.execute("INSERT OR IGNORE INTO whitelist (user_id, username, approved) VALUES (?, ?, 0)", (user_id, username))
    conn.commit()
    conn.close()

def db_approve_user(user_id: int, is_approved: bool):
    conn = sqlite3.connect(DB_NAME)
    if is_approved:
        conn.execute("UPDATE whitelist SET approved=1 WHERE user_id=?", (user_id,))
    else:
        conn.execute("DELETE FROM whitelist WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

# --- Функции аккаунтов ---
def db_save_account(phone, ua, res, plat):
    """Сохраняет или обновляет аккаунт"""
    conn = sqlite3.connect(DB_NAME)
    now = datetime.now()
    
    # Логика: если аккаунт новый, ставим created_at. Если старый - не трогаем created_at.
    conn.execute("""
        INSERT INTO accounts (phone_number, status, user_agent, resolution, platform, last_active, created_at)
        VALUES (?, 'active', ?, ?, ?, ?, ?)
        ON CONFLICT(phone_number) DO UPDATE SET
            status='active',
            last_active=excluded.last_active,
            user_agent=excluded.user_agent,
            resolution=excluded.resolution,
            platform=excluded.platform
    """, (phone, ua, res, plat, now, now))
    conn.commit()
    conn.close()

def db_get_carousel_targets():
    """
    Возвращает список аккаунтов для фарма (Карусель).
    Шардинг: выбираем только те, что принадлежат этому INSTANCE_ID.
    """
    conn = sqlite3.connect(DB_NAME)
    query = f"""
        SELECT phone_number, created_at 
        FROM accounts 
        WHERE status='active' 
        AND (rowid % {TOTAL_INSTANCES}) = ({INSTANCE_ID} - 1)
        ORDER BY last_active ASC
    """
    res = conn.execute(query).fetchall()
    conn.close()
    return res

def db_update_activity(phone):
    conn = sqlite3.connect(DB_NAME)
    conn.execute("UPDATE accounts SET last_active=?, messages_sent=messages_sent+1 WHERE phone_number=?", 
                 (datetime.now(), phone))
    conn.commit()
    conn.close()

def db_mark_banned(phone):
    conn = sqlite3.connect(DB_NAME)
    conn.execute("UPDATE accounts SET status='banned' WHERE phone_number=?", (phone,))
    conn.commit()
    conn.close()

def db_delete_account(phone):
    conn = sqlite3.connect(DB_NAME)
    conn.execute("DELETE FROM accounts WHERE phone_number=?", (phone,))
    conn.commit()
    conn.close()

# ==========================================
# 🌐 SELENIUM ENGINE (STEALTH v16.3)
# ==========================================

def get_chromedriver(phone, ua=None, res=None, plat=None):
    """
    Создает экземпляр Chrome с продвинутой маскировкой.
    """
    # 1. Если конфиг не передан, ищем в базе или генерируем
    if not ua:
        conn = sqlite3.connect(DB_NAME)
        acc = conn.execute("SELECT user_agent, resolution, platform FROM accounts WHERE phone_number=?", (phone,)).fetchone()
        conn.close()
        if acc:
            ua, res, plat = acc
        else:
            dev = random.choice(DEVICES)
            ua, res, plat = dev['ua'], dev['res'], dev['plat']
    
    # 2. Настройки Chrome Options
    options = Options()
    user_data_dir = os.path.join(SESSIONS_DIR, phone)
    options.add_argument(f"--user-data-dir={user_data_dir}")
    
    # Критические настройки для BotHost
    options.add_argument("--headless=new") # Новый Headless режим (более скрытный)
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-software-rasterizer")
    
    # Spoofing
    options.add_argument(f"--user-agent={ua}")
    options.add_argument(f"--window-size={res}")
    options.add_argument("--lang=ru-RU,ru")
    options.page_load_strategy = 'eager' # Не ждать полной загрузки всех картинок

    try:
        driver = webdriver.Chrome(options=options)
    except Exception as e:
        logger.critical(f"❌ Failed to start driver for {phone}: {e}")
        return None, None, None, None

    # 3. 🔥 CDP INJECTIONS (Ядерная маскировка) 🔥
    
    # Скрытие navigator.webdriver
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.navigator.chrome = { runtime: {} };
        """
    })
    
    # Подмена Платформы (чтобы Linux сервер выглядел как Windows/Mac)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": f"Object.defineProperty(navigator, 'platform', {{get: () => '{plat}'}});"
    })

    # Подмена Геолокации (Kazakhstan, Almaty)
    driver.execute_cdp_cmd("Emulation.setGeolocationOverride", {
        "latitude": 43.2389,
        "longitude": 76.8897,
        "accuracy": 100
    })

    # Подмена Timezone
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

def kb_admin_approval(user_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"approve_{user_id}"),
         InlineKeyboardButton(text="🚫 Отклонить", callback_data=f"reject_{user_id}")]
    ])

def kb_main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Новый аккаунт", callback_data="add_acc")],
        [InlineKeyboardButton(text="📊 Статус системы", callback_data="sys_stat")]
    ])

def kb_browser_control(phone):
    """Панель управления браузером (Пошаговая)"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 1. ЧЕК (Скрин)", callback_data=f"scr_{phone}")],
        [InlineKeyboardButton(text="🔗 2. КЛИК 'ВХОД'", callback_data=f"lnk_{phone}")],
        [InlineKeyboardButton(text="⌨️ 3. ВВЕСТИ НОМЕР", callback_data=f"typ_{phone}")],
        [InlineKeyboardButton(text="➡️ 4. ЖМИ 'ДАЛЕЕ'", callback_data=f"nxt_{phone}")],
        [InlineKeyboardButton(text="✅ 5. ВОШЕЛ (Сохр.)", callback_data=f"sav_{phone}")],
        [InlineKeyboardButton(text="🗑 УДАЛИТЬ СЕССИЮ", callback_data=f"del_{phone}")]
    ])

# --- ACCESS & START HANDLERS ---

@dp.message(Command("start"))
async def start_handler(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username
    
    # 1. Проверка доступа
    if db_check_access(user_id):
        # 2. Проверка нагрузки
        load_warning = get_server_load_status()
        status_text = f"Online 🟢"
        if load_warning:
            status_text = f"⚠️ HIGH LOAD: {load_warning}"
            
        await message.answer(
            f"🔱 **Imperator v16.5 Ultimate**\n"
            f"👤 User: {username}\n"
            f"🤖 Inst: {INSTANCE_ID}/{TOTAL_INSTANCES}\n"
            f"🖥 Stat: {status_text}",
            reply_markup=kb_main_menu()
        )
    else:
        # Регистрация заявки
        db_register_request(user_id, username)
        await message.answer("🔒 **Доступ закрыт.**\nВаша заявка отправлена администратору.")
        if ADMIN_ID:
            await bot.send_message(
                ADMIN_ID,
                f"👤 **Новая заявка!**\nID: {user_id}\nUser: @{username}",
                reply_markup=kb_admin_approval(user_id)
            )

@dp.callback_query(F.data.startswith("approve_"))
async def cb_approve(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    target_id = int(call.data.split("_")[1])
    db_approve_user(target_id, True)
    await call.message.edit_text(f"✅ Пользователь {target_id} одобрен.")
    try: await bot.send_message(target_id, "✅ **Доступ открыт!**\nЖми /start")
    except: pass

@dp.callback_query(F.data.startswith("reject_"))
async def cb_reject(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    target_id = int(call.data.split("_")[1])
    db_approve_user(target_id, False)
    await call.message.edit_text(f"🚫 Пользователь {target_id} отклонен.")

# --- ADD ACCOUNT HANDLERS ---

@dp.callback_query(F.data == "add_acc")
async def cb_add_acc(call: types.CallbackQuery, state: FSMContext):
    # Guard: Не даем создавать, если сервер перегружен
    load_err = get_server_load_status()
    if load_err:
        return await call.answer(f"⚠️ Сервер занят: {load_err}\nИспользуйте другой инстанс!", show_alert=True)
    
    await call.message.answer("📞 Введите номер телефона (только цифры):")
    await state.set_state(BotStates.waiting_phone)

@dp.message(BotStates.waiting_phone)
async def process_phone(message: types.Message, state: FSMContext):
    phone = "".join(filter(str.isdigit, message.text))
    await state.clear()
    
    # Валидация
    if not validate_phone(phone):
        return await message.answer("❌ Ошибка: Номер должен быть 7-15 цифр.")
    
    # Информирование
    status_msg = await message.answer(f"🚀 Инициализация драйвера для {phone}...")
    
    async with BROWSER_SEMAPHORE:
        try:
            # Запуск Selenium
            driver, ua, res, plat = await asyncio.to_thread(get_chromedriver, phone)
            
            if not driver:
                return await status_msg.edit_text("❌ Не удалось запустить драйвер (см. логи).")
            
            # Сохраняем в кэш для ручного управления
            ACTIVE_DRIVERS[phone] = {"driver": driver, "ua": ua, "res": res, "plat": plat}
            
            await asyncio.to_thread(driver.get, "https://web.whatsapp.com")
            
            await status_msg.edit_text(
                f"✅ **Браузер запущен!**\n📱 {phone}\n🖥 {plat}\n👇 Управляй по шагам:",
                reply_markup=kb_browser_control(phone)
            )
        except Exception as e:
            logger.error(f"Manual start error: {e}")
            await status_msg.edit_text(f"❌ Критическая ошибка: {e}")

# --- BROWSER ACTION HANDLERS ---

@dp.callback_query(F.data.startswith("scr_"))
async def cb_screen(call: types.CallbackQuery):
    phone = call.data.split("_")[1]
    data = ACTIVE_DRIVERS.get(phone)
    if not data: return await call.answer("Сессия не активна!", show_alert=True)
    
    try:
        png = await asyncio.to_thread(data['driver'].get_screenshot_as_png)
        await call.message.answer_photo(BufferedInputFile(png, "s.png"), caption=f"Status: {phone}")
    except Exception as e:
        await call.answer(f"Error: {e}", show_alert=True)
    await call.answer()

@dp.callback_query(F.data.startswith("lnk_"))
async def cb_link(call: types.CallbackQuery):
    phone = call.data.split("_")[1]
    data = ACTIVE_DRIVERS.get(phone)
    if not data: return
    
    # JS: Умный поиск кнопки "Link with phone"
    js = """
    var xpaths = ["//*[contains(text(), 'Link with phone')]", "//*[contains(text(), 'Связать')]", "//*[contains(text(), 'Log in')]"];
    for(var i=0; i<xpaths.length; i++){
        var r = document.evaluate(xpaths[i], document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
        if(r){ r.click(); return true; }
    }
    return false;
    """
    res = data['driver'].execute_script(js)
    await call.answer("Клик: ОК" if res else "Кнопка не найдена", show_alert=not res)

@dp.callback_query(F.data.startswith("typ_"))
async def cb_type_number(call: types.CallbackQuery):
    phone = call.data.split("_")[1]
    data = ACTIVE_DRIVERS.get(phone)
    if not data: return
    
    # 🔥 JS: ОЧИСТКА ПОЛЯ + ВВОД С ПЛЮСОМ 🔥
    # 1. Фокус
    # 2. Select All -> Delete (чтобы очистить +7 или мусор)
    # 3. Вставка +номер
    js = f"""
    var i = document.querySelector('input[type="text"]') || document.querySelector('div[contenteditable="true"]');
    if(i) {{
        i.focus();
        document.execCommand('selectAll', false, null);
        document.execCommand('delete', false, null);
        document.execCommand('insertText', false, '+{phone}');
        i.dispatchEvent(new Event('input', {{bubbles: true}}));
        i.dispatchEvent(new Event('change', {{bubbles: true}}));
        return true;
    }}
    return false;
    """
    res = data['driver'].execute_script(js)
    await call.answer(f"Очищено и введено: +{phone}" if res else "Поле ввода не найдено", show_alert=not res)

@dp.callback_query(F.data.startswith("nxt_"))
async def cb_next(call: types.CallbackQuery):
    phone = call.data.split("_")[1]
    data = ACTIVE_DRIVERS.get(phone)
    if not data: return
    
    # JS: Поиск кнопок Next/Далее/OK
    js = """
    var b = document.querySelectorAll('button, [role="button"]');
    for(var i=0; i<b.length; i++) {
        var t = b[i].innerText.toLowerCase();
        if(t.includes('next') || t.includes('далее') || t.includes('ok')) {
            b[i].click(); return true;
        }
    }
    // Fallback
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
        # Сохранение метаданных
        db_save_account(phone, data['ua'], data['res'], data['plat'])
        # ⚠️ ВАЖНО: Закрываем браузер, чтобы не жрать память
        try: await asyncio.to_thread(data['driver'].quit)
        except: pass
        
    await call.message.edit_text(f"✅ **Сессия {phone} сохранена!**\nБраузер закрыт. Аккаунт добавлен в карусель фарма.")

@dp.callback_query(F.data.startswith("del_"))
async def cb_del(call: types.CallbackQuery):
    phone = call.data.split("_")[1]
    
    # 1. Останавливаем драйвер
    d = ACTIVE_DRIVERS.pop(phone, None)
    if d:
        try: await asyncio.to_thread(d['driver'].quit)
        except: pass
    
    # 2. Удаляем папку
    try: shutil.rmtree(os.path.join(SESSIONS_DIR, phone), ignore_errors=True)
    except: pass
    
    # 3. Удаляем из БД
    db_delete_account(phone)
    
    await call.message.edit_text(f"🗑 Аккаунт {phone} полностью удален.")

@dp.callback_query(F.data == "sys_stat")
async def cb_stat(call: types.CallbackQuery):
    mem = psutil.virtual_memory()
    cpu = psutil.cpu_percent()
    msg = (f"🖥 **System Status**\n"
           f"RAM Free: {mem.available/1024/1024:.0f} MB\n"
           f"CPU Load: {cpu}%\n"
           f"Active Manual Sessions: {len(ACTIVE_DRIVERS)}\n"
           f"Instance: {INSTANCE_ID}")
    await call.answer(msg, show_alert=True)

# ==========================================
# 🚜 КАРУСЕЛЬ ФАРМА (ROUND-ROBIN)
# ==========================================

async def process_account_cycle(phone, created_at):
    """
    Один цикл фарма для одного номера:
    Зашел -> Проверил бан -> Написал себе -> Вышел
    """
    driver = None
    try:
        # Guard: Если сервер перегружен, пропускаем, чтобы не положить его
        if get_server_load_status():
            logger.warning(f"Skipping farm cycle for {phone} due to High Load")
            return

        async with BROWSER_SEMAPHORE:
            logger.info(f"🔄 Processing: {phone}")
            driver, ua, res, plat = await asyncio.to_thread(get_chromedriver, phone)
            
            # Заходим по прямой ссылке в чат с собой
            target = f"https://web.whatsapp.com/send?phone={phone}"
            await asyncio.to_thread(driver.get, target)
            
            wait = WebDriverWait(driver, 50)
            
            # --- ЛОВУШКА ДЛЯ БАНА ---
            # Проверяем на наличие текста о бане до полной загрузки
            # (Можно добавить проверку на редирект на страницу logout)
            
            try:
                # Ищем поле ввода в футере
                inp = wait.until(EC.presence_of_element_located((By.XPATH, "//footer//div[@contenteditable='true']")))
                
                # Если нашли - всё ок, пишем сообщение
                text = fake.sentence()
                for char in text:
                    inp.send_keys(char)
                    await asyncio.sleep(random.uniform(0.05, 0.15))
                inp.send_keys(Keys.ENTER)
                
                # Обновляем активность
                db_update_activity(phone)
                logger.info(f"✅ Farm Success: {phone}")
                
                # Небольшая задержка перед выходом
                await asyncio.sleep(3)

            except TimeoutException:
                # Если поле ввода не появилось за 50 сек
                logger.warning(f"⚠️ Timeout {phone}. Possible BAN or Logout.")
                
                # Простейшая эвристика: если не загрузился чат, считаем аккаунт подозрительным
                # Или проверяем page_source на наличие слов "spam", "not allowed"
                src = driver.page_source.lower()
                if "not allowed" in src or "spam" in src:
                    # РЕАЛЬНЫЙ СЛЕТ
                    if isinstance(created_at, str):
                        created_at = datetime.fromisoformat(created_at)
                    lifespan = datetime.now() - created_at
                    
                    db_mark_banned(phone)
                    shutil.rmtree(os.path.join(SESSIONS_DIR, phone), ignore_errors=True)
                    logger.error(f"💀 BAN CONFIRMED: {phone}. Lifespan: {format_duration(lifespan)}")

    except Exception as e:
        logger.error(f"Farm Error {phone}: {e}")
    finally:
        # ВСЕГДА ЗАКРЫВАЕМ БРАУЗЕР
        if driver:
            try: await asyncio.to_thread(driver.quit)
            except: pass

async def farm_carousel_loop():
    """
    Карусель: берет список всех аккаунтов и проходит по ним по кругу.
    """
    logger.info("🎠 Farm Carousel Started")
    while True:
        try:
            # 1. Получаем список целей (Round-Robin)
            targets = db_get_carousel_targets()
            
            if not targets:
                await asyncio.sleep(60)
                continue
            
            # 2. Итерируемся по списку
            for phone, created_at in targets:
                # Если аккаунт сейчас занят пользователем вручную - пропускаем
                if phone in ACTIVE_DRIVERS: continue
                
                # Обрабатываем аккаунт
                await process_account_cycle(phone, created_at)
                
                # Пауза между аккаунтами в очереди (чтобы CPU остыл)
                await asyncio.sleep(random.randint(15, 30))
            
            # 3. Пауза после полного круга
            logger.info("💤 Carousel cycle finished. Sleeping...")
            await asyncio.sleep(random.randint(FARM_DELAY_MIN, FARM_DELAY_MAX))
            
        except Exception as e:
            logger.error(f"Carousel Loop Error: {e}")
            await asyncio.sleep(10)

# ==========================================
# 🚀 ЗАПУСК
# ==========================================

async def main():
    if not BOT_TOKEN:
        logger.critical("❌ BOT_TOKEN is missing!")
        sys.exit(1)
        
    # 1. Очистка старых процессов
    cleanup_zombie_processes()
    
    # 2. БД
    db_init()
    
    # 3. Фоновая карусель
    asyncio.create_task(farm_carousel_loop())
    
    # 4. Бот
    logger.info(f"🚀 Started Instance {INSTANCE_ID}")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
