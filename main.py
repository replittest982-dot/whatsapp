"""
🔱 IMPERATOR v26.0 — WARLORD EDITION (Вацап бот В3)
- Движок: Selenium WebDriver (Chrome).
- Архитектура: Неблокирующий (asyncio.to_thread для Selenium).
- Защита: Aggressive Cleanup (убийство зомби-процессов), Memory Guard (RAM < 200MB).
- Фичи: Whitelist (система доступа), Полные скриншоты QR/кода, Hive Mind (Соло фарм).
"""

import asyncio
import os
import logging
import random
import shutil
import psutil
import sys
import re
from datetime import datetime

import aiosqlite
from faker import Faker

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys

# ==========================================
# ⚙️ КОНФИГУРАЦИЯ
# ==========================================

BOT_TOKEN       = os.environ.get("BOT_TOKEN", "")
ADMIN_ID        = int(os.environ.get("ADMIN_ID", 0))
INSTANCE_ID     = int(os.environ.get("INSTANCE_ID", 1))
DB_NAME         = "warlord26.db"
SESS_DIR        = os.path.join(os.getcwd(), "sessions")
TMP_DIR         = os.path.join(os.getcwd(), "tmp_chrome")

os.makedirs(SESS_DIR, exist_ok=True)
os.makedirs(TMP_DIR, exist_ok=True)

FARM_MIN        = 5 * 60
FARM_MAX        = 15 * 60
BROWSER_LIMIT   = asyncio.Semaphore(2)  # Не более 2 браузеров одновременно
ACTIVE_DRIVERS  = {}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("WARLORD")
fake = Faker('ru_RU')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ==========================================
# 🛡 SYSTEM UTILS (ANTI-CRASH)
# ==========================================

def aggressive_cleanup():
    """Убивает зависшие процессы Chrome и очищает временные папки."""
    logger.info("🧹 Запуск Aggressive Cleanup...")
    killed = 0
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            name = proc.info['name'].lower()
            if 'chrome' in name or 'chromedriver' in name:
                proc.kill()
                killed += 1
        except Exception:
            pass
    
    if os.path.exists(TMP_DIR):
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        os.makedirs(TMP_DIR, exist_ok=True)
        
    logger.info(f"✅ Убито зомби-процессов: {killed}")

def is_memory_critical() -> bool:
    """True, если свободной RAM меньше 200MB."""
    free_mb = psutil.virtual_memory().available / (1024 * 1024)
    if free_mb < 200:
        logger.warning(f"⚠️ КРИТИЧЕСКАЯ ПАМЯТЬ: Доступно {free_mb:.2f} MB")
        return True
    return False

# ==========================================
# 💾 БАЗА ДАННЫХ (aiosqlite)
# ==========================================

async def db_init():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS accounts (
            phone TEXT PRIMARY KEY,
            user_agent TEXT,
            status TEXT DEFAULT 'active',
            last_active TEXT
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS whitelist (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            approved INTEGER DEFAULT 0
        )""")
        # Добавляем админа в вайтлист по умолчанию
        if ADMIN_ID:
            await db.execute("INSERT OR IGNORE INTO whitelist (user_id, username, approved) VALUES (?, 'admin', 1)", (ADMIN_ID,))
        await db.commit()

async def db_check_access(user_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT approved FROM whitelist WHERE user_id=?", (user_id,)) as cur:
            res = await cur.fetchone()
            return bool(res and res[0] == 1)

async def db_request_access(user_id: int, username: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO whitelist (user_id, username, approved) VALUES (?, ?, 0)", (user_id, username))
        await db.commit()

async def db_get_active_phones():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT phone FROM accounts WHERE status='active'") as cur:
            return [row[0] for row in await cur.fetchall()]

async def db_save_account(phone: str, ua: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO accounts (phone, user_agent, status, last_active) VALUES (?, ?, 'active', ?)",
            (phone, ua, datetime.now().isoformat())
        )
        await db.commit()

# ==========================================
# 🌐 SELENIUM ENGINE
# ==========================================

def get_random_ua():
    uas = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    ]
    return random.choice(uas)

def create_driver(phone: str) -> webdriver.Chrome:
    profile_path = os.path.join(SESS_DIR, phone)
    tmp_path = os.path.join(TMP_DIR, f"tmp_{phone}")
    os.makedirs(tmp_path, exist_ok=True)

    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument(f"--user-data-dir={profile_path}")
    options.add_argument(f"--crash-dumps-dir={tmp_path}")
    options.add_argument(f"--user-agent={get_random_ua()}")
    options.add_argument("--window-size=1920,1080")
    
    # Скрытие автоматизации (Stealth)
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    options.add_argument("--disable-blink-features=AutomationControlled")

    service = Service()
    driver = webdriver.Chrome(service=service, options=options)
    
    # JS Injection (Timezone + Platform)
    driver.execute_cdp_cmd('Emulation.setTimezoneOverride', {'timezoneId': 'Asia/Almaty'})
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return driver

# ==========================================
# 🧠 WHATSAPP ЛОГИКА (В потоках)
# ==========================================

def _take_screenshot(driver: webdriver.Chrome) -> bytes:
    """Скриншот на весь экран."""
    return driver.get_screenshot_as_png()

def _check_logged_in(driver: webdriver.Chrome) -> bool:
    try:
        WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.ID, "pane-side")))
        return True
    except:
        return False

def _human_type(element, text: str):
    for char in text:
        if random.random() < 0.04:
            element.send_keys(random.choice("фывапролдж"))
            time.sleep(random.uniform(0.1, 0.3))
            element.send_keys(Keys.BACKSPACE)
        element.send_keys(char)
        time.sleep(random.uniform(0.05, 0.2))

def sync_whatsapp_login_qr(phone: str) -> tuple[bool, bytes]:
    """Генерирует QR и возвращает (успех, скриншот). Работает синхронно."""
    driver = None
    try:
        driver = create_driver(phone)
        driver.get("https://web.whatsapp.com")
        if _check_logged_in(driver):
            return True, b""
        
        # Ждем QR код
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.CSS_SELECTOR, "canvas")))
        time.sleep(3) # Даем отрисоваться
        scr = _take_screenshot(driver)
        return False, scr
    except Exception as e:
        logger.error(f"QR Error {phone}: {e}")
        return False, b""
    finally:
        if driver: driver.quit()

def sync_wait_for_login(phone: str) -> bool:
    """Ждет входа 2 минуты после сканирования."""
    driver = None
    try:
        driver = create_driver(phone)
        driver.get("https://web.whatsapp.com")
        WebDriverWait(driver, 120).until(EC.presence_of_element_located((By.ID, "pane-side")))
        return True
    except:
        return False
    finally:
        if driver: driver.quit()

def sync_farm_step(phone: str):
    """Единичный шаг прогрева (соло: смена био, сообщение себе)."""
    driver = None
    try:
        driver = create_driver(phone)
        driver.get("https://web.whatsapp.com")
        if not _check_logged_in(driver):
            logger.warning(f"[FARM] {phone} сессия вылетела.")
            return False

        # Пишем сами себе (Избранное)
        driver.get(f"https://web.whatsapp.com/send?phone={phone}")
        inp = WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.CSS_SELECTOR, "div[contenteditable='true'][data-tab]")))
        time.sleep(random.uniform(2, 4))
        
        msg = fake.sentence(nb_words=5)
        _human_type(inp, msg)
        time.sleep(0.5)
        inp.send_keys(Keys.ENTER)
        time.sleep(2)
        logger.info(f"[FARM] {phone} отправил: {msg}")
        return True
    except Exception as e:
        logger.error(f"[FARM] Ошибка {phone}: {e}")
        return False
    finally:
        if driver: driver.quit()

# ==========================================
# 🤖 BOT HANDLERS & FSM
# ==========================================

class AuthState(StatesGroup):
    wait_phone_qr = State()
    wait_confirm  = State()

@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    user_id = msg.from_user.id
    if not await db_check_access(user_id):
        await db_request_access(user_id, msg.from_user.username or "unknown")
        await msg.answer("⛔ Нет доступа. Заявка отправлена администратору.")
        if ADMIN_ID:
            await bot.send_message(
                ADMIN_ID, 
                f"🛡 Новый запрос доступа от @{msg.from_user.username} ({user_id}).\nИспользуй /allow {user_id}"
            )
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 Привязать по QR", callback_data="add_qr")],
        [InlineKeyboardButton(text="📋 Список аккаунтов", callback_data="list_accs")]
    ])
    await msg.answer("🔱 *IMPERATOR v26.0 WARLORD*\nДоступ разрешен. Выберите действие:", parse_mode="Markdown", reply_markup=kb)

@dp.message(Command("allow"))
async def cmd_allow(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return
    try:
        target_id = int(msg.text.split()[1])
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE whitelist SET approved=1 WHERE user_id=?", (target_id,))
            await db.commit()
        await msg.answer(f"✅ Доступ разрешен пользователю {target_id}")
        await bot.send_message(target_id, "✅ Администратор одобрил вам доступ. Нажмите /start")
    except:
        await msg.answer("Использование: /allow <user_id>")

@dp.callback_query(F.data == "list_accs")
async def cb_list(cb: types.CallbackQuery):
    accs = await db_get_active_phones()
    text = f"📋 Активных сессий: {len(accs)}\n" + "\n".join(f"• `{p}`" for p in accs) if accs else "Пусто."
    await cb.message.answer(text, parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data == "add_qr")
async def cb_add_qr(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("Введите номер телефона (для создания папки профиля, напр. 77001234567):")
    await state.set_state(AuthState.wait_phone_qr)
    await cb.answer()

@dp.message(AuthState.wait_phone_qr)
async def process_qr_phone(msg: types.Message, state: FSMContext):
    phone = re.sub(r"\D", "", msg.text)
    if is_memory_critical():
        return await msg.answer("⚠️ Сервер перегружен. Попробуйте позже.")

    status = await msg.answer("⏳ Запускаю браузер и генерирую QR (около 15-20 сек)...")
    
    # 💥 Вызов Selenium в отдельном потоке, чтобы бот не вис
    is_logged, screenshot = await asyncio.to_thread(sync_whatsapp_login_qr, phone)
    
    if is_logged:
        await db_save_account(phone, get_random_ua())
        await status.edit_text("✅ Этот номер уже авторизован! Фарм продолжится автоматически.")
        await state.clear()
        return

    if screenshot:
        await status.delete()
        await msg.answer_photo(
            photo=BufferedInputFile(screenshot, filename="qr.png"),
            caption="📷 Отсканируйте этот QR-код полным экраном.\n⏳ После сканирования у вас есть 2 минуты. Ожидаю..."
        )
        
        # Ждем логина
        success = await asyncio.to_thread(sync_wait_for_login, phone)
        if success:
            await db_save_account(phone, get_random_ua())
            await msg.answer(f"✅ Успешный вход для {phone}! Аккаунт добавлен в ферму.")
        else:
            await msg.answer("❌ Время ожидания вышло или ошибка авторизации.")
    else:
        await status.edit_text("❌ Не удалось получить QR-код. Проверьте логи.")
    
    await state.clear()

# ==========================================
# 🐝 HIVE MIND (ФАРМ ПРОЦЕССОР)
# ==========================================

async def farm_worker(phone: str):
    """Обёртка для работы с Семафором (контроль ОЗУ)"""
    async with BROWSER_LIMIT:
        if is_memory_critical():
            logger.warning("Пропуск цикла из-за ОЗУ.")
            return
            
        logger.info(f"▶️ Запуск прогрева для {phone}")
        success = await asyncio.to_thread(sync_farm_step, phone)
        if success:
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("UPDATE accounts SET last_active=? WHERE phone=?", (datetime.now().isoformat(), phone))
                await db.commit()

async def hive_loop():
    logger.info("🐝 HIVE MIND ЗАПУЩЕН")
    while True:
        try:
            accs = await db_get_active_phones()
            if not accs:
                await asyncio.sleep(30)
                continue
            
            # Рандомный выбор аккаунта, подходящего под текущий INSTANCE
            valid_accs = [p for i, p in enumerate(accs) if (i % 1) == (INSTANCE_ID - 1)] # Пока 1 инстанс
            if valid_accs:
                target = random.choice(valid_accs)
                asyncio.create_task(farm_worker(target))
            
            pause = random.randint(FARM_MIN, FARM_MAX)
            logger.info(f"💤 Hive Mind спит {pause} сек...")
            await asyncio.sleep(pause)
            
        except Exception as e:
            logger.error(f"Hive Loop Error: {e}")
            await asyncio.sleep(15)

# ==========================================
# 🚀 ЗАПУСК
# ==========================================

async def main():
    if not BOT_TOKEN:
        logger.critical("❌ НЕТ ТОКЕНА!")
        sys.exit(1)

    aggressive_cleanup()
    await db_init()
    
    # Запуск фонового прогрева
    asyncio.create_task(hive_loop())
    
    logger.info("🚀 Imperator v26.0 (Warlord Edition) запущен!")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Выключение бота...")
    finally:
        aggressive_cleanup()
