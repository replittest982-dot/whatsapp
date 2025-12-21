import os
import asyncio
import logging
import sqlite3
import random
import psutil
from datetime import datetime

# Библиотеки для работы с браузером
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException

# Библиотеки для Telegram и данных
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from faker import Faker

# --- НАСТРОЙКИ И КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВАШ_ТОКЕН")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0)) # Сюда придут запросы на доступ

# ОПТИМИЗАЦИЯ: Ставим 2 потока вместо 4. 
# Это разгрузит память и процессор, бот перестанет тупить.
BROWSER_SEMAPHORE = asyncio.Semaphore(2) 

SESSION_DIR = "./sessions"
DB_PATH = "imperator_v16.db"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("ImperatorV16")
fake = Faker("ru_RU")

if not os.path.exists(SESSION_DIR):
    os.makedirs(SESSION_DIR)

# --- СОСТОЯНИЯ FSM ---
class AddAccount(StatesGroup):
    waiting_for_phone = State()
    browser_active = State()

# --- БАЗА ДАННЫХ ---
def db_init():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    # Таблица аккаунтов (фарм)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            phone_number TEXT UNIQUE,
            status TEXT DEFAULT 'pending',
            messages_sent INTEGER DEFAULT 0,
            user_agent TEXT,
            last_active DATETIME
        )
    """)
    # Таблица доступа к боту (White List)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS whitelist (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            approved BOOLEAN DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def is_user_approved(user_id):
    """Проверка, есть ли доступ у юзера"""
    if user_id == ADMIN_ID: return True
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    res = cur.execute("SELECT approved FROM whitelist WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return res and res[0] == 1

def add_user_request(user_id, username):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO whitelist (user_id, username, approved) VALUES (?, ?, 0)", (user_id, username))
    conn.commit()
    conn.close()

def approve_user_db(user_id, status):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    if status:
        cur.execute("UPDATE whitelist SET approved = 1 WHERE user_id = ?", (user_id,))
    else:
        cur.execute("DELETE FROM whitelist WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def get_stats():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    total = cur.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
    active = cur.execute("SELECT COUNT(*) FROM accounts WHERE status='active'").fetchone()[0]
    banned = cur.execute("SELECT COUNT(*) FROM accounts WHERE status='banned'").fetchone()[0]
    sent = cur.execute("SELECT SUM(messages_sent) FROM accounts").fetchone()[0] or 0
    conn.close()
    return total, active, banned, sent

# --- SELENIUM CORE (ОПТИМИЗИРОВАННЫЙ) ---
def get_driver(phone):
    options = Options()
    user_data = os.path.join(os.getcwd(), "sessions", phone)
    
    options.add_argument(f"--user-data-dir={user_data}")
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer") # Откл программный рендеринг (экономия CPU)
    options.add_argument("--lang=ru-KZ")
    options.add_argument("--blink-settings=imagesEnabled=false") 
    
    # СУПЕР ОПТИМИЗАЦИЯ: EAGER
    # Браузер не ждет загрузки всех скриптов и картинок, начинает работать сразу как появился HTML
    options.page_load_strategy = 'eager'
    
    options.add_argument(f"--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36")

    driver = webdriver.Chrome(options=options)
    
    # KZ Stealth
    driver.execute_cdp_cmd("Emulation.setGeolocationOverride", {
        "latitude": 43.2389, "longitude": 76.8897, "accuracy": 100
    })
    
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Intl.DateTimeFormat.prototype.resolvedOptions = function() {
                return { timeZone: 'Asia/Almaty', locale: 'ru-KZ' };
            };
        """
    })
    return driver

async def human_type(element, text):
    """Быстрая печать с редкими ошибками"""
    for char in text:
        if random.random() < 0.04:
            wrong = random.choice("йцукенгшщзхъфывапролджэ")
            element.send_keys(wrong)
            await asyncio.sleep(0.05) # Уменьшил задержки для скорости
            element.send_keys(Keys.BACKSPACE)
        element.send_keys(char)
        await asyncio.sleep(random.uniform(0.02, 0.1)) # Печатаем быстрее

# --- ГЛОБАЛЬНЫЙ КЭШ ДРАЙВЕРОВ ---
active_drivers = {}

# --- AIOGRAM SETUP ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- КЛАВИАТУРЫ ---
def get_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить Аккаунт", callback_data="add_acc")],
        [InlineKeyboardButton(text="📊 Админ-панель", callback_data="admin_panel")]
    ])

def get_control_kb(phone):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 ЧЕК", callback_data=f"check_{phone}")],
        [InlineKeyboardButton(text="🔗 Вход по ссылке", callback_data=f"link_{phone}")],
        [InlineKeyboardButton(text="⌨️ Ввести номер", callback_data=f"type_{phone}")],
        [InlineKeyboardButton(text="✅ ГОТОВО", callback_data=f"ready_{phone}")]
    ])

# --- ЛОГИКА ДОСТУПА И СТАРТА ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or "NoUsername"

    # 1. Если Админ или Одобрен
    if is_user_approved(user_id):
        await message.answer("🛠 **WhatsApp Imperator v16.0**\nДоступ разрешен. Оптимизированный режим.", reply_markup=get_main_kb())
        return

    # 2. Если нет доступа - отправляем запрос
    add_user_request(user_id, username)
    
    # Сообщение пользователю
    await message.answer("🚫 **Вход заблокирован.**\nВладельцу бота отправлен запрос на доступ. Ожидайте решения.")
    
    # Сообщение Админу
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Принять", callback_data=f"approve_{user_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{user_id}")
        ]
    ])
    await bot.send_message(
        ADMIN_ID, 
        f"👤 **Запрос доступа!**\n\nID: `{user_id}`\nUser: @{username}\n\nЧто делаем?", 
        reply_markup=kb
    )

# --- ОБРАБОТКА ЗАПРОСОВ ДОСТУПА (ДЛЯ АДМИНА) ---
@dp.callback_query(F.data.startswith("approve_"))
async def approve_user(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    target_id = int(callback.data.split("_")[1])
    
    approve_user_db(target_id, True)
    await callback.message.edit_text(f"✅ Пользователь {target_id} одобрен!")
    try:
        await bot.send_message(target_id, "✅ **Ваш запрос одобрен!**\nНажмите /start для начала работы.")
    except:
        pass # Если юзер заблочил бота

@dp.callback_query(F.data.startswith("reject_"))
async def reject_user(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    target_id = int(callback.data.split("_")[1])
    
    approve_user_db(target_id, False) # Удаляем из базы или ставим флаг 0
    await callback.message.edit_text(f"❌ Пользователь {target_id} отклонен.")
    try:
        await bot.send_message(target_id, "❌ **Ваш запрос отклонен владельцем.**")
    except:
        pass

# --- ОСНОВНОЙ ФУНКЦИОНАЛ ---
@dp.callback_query(F.data == "admin_panel")
async def admin_menu(callback: CallbackQuery):
    if not is_user_approved(callback.from_user.id): return
    
    ram = psutil.virtual_memory().percent
    total, active, banned, sent = get_stats()
    
    text = (
        f"🏰 **ADMIN PANEL** (Optimized)\n\n"
        f"🖥 RAM Load: {ram}%\n"
        f"📱 Accs: {total} (Act: {active} | Ban: {banned})\n"
        f"📩 Sent: {sent}\n"
        f"⚙️ Threads: 2 (Safe Mode)"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_panel")],
        [InlineKeyboardButton(text="🧹 Очистить Pending", callback_data="clear_pending")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)

@dp.callback_query(F.data == "add_acc")
async def start_add_acc(callback: CallbackQuery, state: FSMContext):
    if not is_user_approved(callback.from_user.id): return
    await callback.message.answer("Введите номер телефона (например, 77071234567):")
    await state.set_state(AddAccount.waiting_for_phone)

@dp.message(AddAccount.waiting_for_phone)
async def process_phone(message: Message, state: FSMContext):
    phone = message.text.strip().replace("+", "")
    await state.update_data(phone=phone)
    msg = await message.answer(f"🚀 Запуск движка для {phone}...")
    
    try:
        # Используем семафор даже при добавлении, чтобы не положить сервер
        async with BROWSER_SEMAPHORE:
            driver = await asyncio.to_thread(get_driver, phone)
            active_drivers[phone] = driver
            await asyncio.to_thread(driver.get, "https://web.whatsapp.com")
            
        await msg.edit_text(f"Браузер активен ({phone}).\nВыберите действие:", reply_markup=get_control_kb(phone))
        await state.set_state(AddAccount.browser_active)
    except Exception as e:
        await msg.edit_text(f"Ошибка запуска: {e}")

# Обработчики ручного управления
@dp.callback_query(F.data.startswith("check_"))
async def screen_check(callback: CallbackQuery):
    phone = callback.data.split("_")[1]
    driver = active_drivers.get(phone)
    if not driver: return await callback.answer("Сессия потеряна")
    
    try:
        screenshot = await asyncio.to_thread(driver.get_screenshot_as_png)
        file = BufferedInputFile(screenshot, filename=f"{phone}.png")
        await callback.message.answer_photo(file, caption=f"Статус: {phone}")
    except:
        await callback.answer("Ошибка скрина")

@dp.callback_query(F.data.startswith("link_"))
async def link_by_phone(callback: CallbackQuery):
    phone = callback.data.split("_")[1]
    driver = active_drivers.get(phone)
    if not driver: return
    try:
        xpaths = ["//*[contains(text(), 'Link with phone number')]", "//*[contains(text(), 'Связать по номеру телефона')]"]
        found = False
        for xpath in xpaths:
            btns = driver.find_elements(By.XPATH, xpath)
            if btns:
                btns[0].click()
                found = True
                break
        await callback.answer("Нажато!" if found else "Кнопка не найдена")
    except Exception as e:
        await callback.answer(f"Err: {e}")

@dp.callback_query(F.data.startswith("type_"))
async def type_number_js(callback: CallbackQuery):
    phone = callback.data.split("_")[1]
    driver = active_drivers.get(phone)
    if not driver: return
    # Быстрый ввод через JS для скорости
    try:
        script = f"""
        const input = document.querySelector('input[aria-label="Type your phone number."]') || document.querySelector('input');
        if(input) {{
            input.value = "{phone}";
            input.dispatchEvent(new Event('input', {{ bubbles: true }}));
            input.dispatchEvent(new Event('change', {{ bubbles: true }}));
        }}
        """
        driver.execute_script(script)
        await callback.answer("Номер введен (JS injection)")
    except:
        await callback.answer("Поле ввода не найдено")

@dp.callback_query(F.data.startswith("ready_"))
async def finalize_acc(callback: CallbackQuery, state: FSMContext):
    phone = callback.data.split("_")[1]
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO accounts (user_id, phone_number, status, last_active) VALUES (?, ?, 'active', ?)",
                (callback.from_user.id, phone, datetime.now()))
    conn.commit()
    conn.close()
    
    if phone in active_drivers:
        driver = active_drivers.pop(phone)
        await asyncio.to_thread(driver.quit)
    
    await callback.message.answer(f"✅ Аккаунт {phone} в работе!")
    await state.clear()

# --- ФАРМ ПРОЦЕССОР (ОПТИМИЗИРОВАННЫЙ) ---
async def farm_loop():
    while True:
        await asyncio.sleep(60)
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        # Берем только аккаунты, которые давно не активничали
        cur.execute("SELECT phone_number FROM accounts WHERE status='active' ORDER BY last_active ASC LIMIT 10")
        targets = cur.fetchall()
        conn.close()

        if not targets:
            continue

        # Запускаем задачи, но ограничиваем их количество семафором (2 шт)
        tasks = []
        for (phone,) in targets:
            tasks.append(safe_farm_session(phone))
        
        await asyncio.gather(*tasks)

async def safe_farm_session(phone):
    async with BROWSER_SEMAPHORE:
        await run_farm_session(phone)

async def run_farm_session(phone):
    driver = None
    try:
        logger.info(f"FARM START: {phone}")
        driver = await asyncio.to_thread(get_driver, phone)
        # Благодаря eager стратегии, не ждем полной загрузки
        await asyncio.to_thread(driver.get, "https://web.whatsapp.com")
        
        # Ждем ключевой элемент, а не просто sleep (быстрее)
        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.XPATH, "//div[@contenteditable='true'] | //span[@data-icon='chat']"))
            )
        except:
            pass # Если тайм-аут, пробуем работать дальше или выходим

        mode = random.choice(["SOLO", "NETWORK"])
        if mode == "SOLO":
            # Быстрая имитация активности
            await asyncio.sleep(random.randint(5, 10))
        else:
            # NETWORK
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT phone_number FROM accounts WHERE status='active' AND phone_number != ? ORDER BY RANDOM() LIMIT 1", (phone,))
            peer = cur.fetchone()
            conn.close()
            if peer:
                # Прямой переход в чат (экономит клики)
                await asyncio.to_thread(driver.get, f"https://web.whatsapp.com/send?phone={peer[0]}")
                await asyncio.sleep(10)
        
        # Обновление времени
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("UPDATE accounts SET last_active=?, messages_sent = messages_sent + 1 WHERE phone_number=?", (datetime.now(), phone))
        conn.commit()
        conn.close()

    except Exception as e:
        logger.error(f"FARM ERROR {phone}: {e}")
    finally:
        if driver:
            # Обязательно убиваем процесс
            try:
                await asyncio.to_thread(driver.quit)
            except:
                pass

# --- ЗАПУСК ---
async def main():
    db_init()
    # Фоновая задача фарма
    asyncio.create_task(farm_loop())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
