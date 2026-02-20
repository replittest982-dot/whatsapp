"""
🔱 IMPERATOR v26.0 — WARLORD EDITION (Selenium + Aiogram 3)
- Движок: Selenium WebDriver (Chrome 143+).
- Сетка прогрева (Hive Mind): 50% Соло / 50% Перекрестный грев.
- Защита: Aggressive Cleanup, Memory Guard, Semaphore.
- Доступ: Whitelist + Обязательная подписка на @WhatsAppstatpro.
"""

import asyncio
import os
import logging
import random
import psutil
import shutil
import sys
import re
import time
from datetime import datetime

import aiosqlite
from faker import Faker

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
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

BOT_TOKEN            = os.environ.get("BOT_TOKEN", "")
ADMIN_ID             = int(os.environ.get("ADMIN_ID", 0))
REQUIRED_CHANNEL_ID  = "@WhatsAppstatpro"
REQUIRED_CHANNEL_URL = "https://t.me/WhatsAppstatpro"

DB_NAME              = "bot_database.db"
SESSIONS_DIR         = os.path.join(os.getcwd(), "sessions")
TMP_DIR              = os.path.join(os.getcwd(), "tmp_chrome")

os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(TMP_DIR, exist_ok=True)

# Ограничители ресурсов
BROWSER_SEMAPHORE    = asyncio.Semaphore(2)  # Максимум 2 браузера одновременно
FARM_DELAY_MIN       = 40
FARM_DELAY_MAX       = 90

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("WARLORD")
fake = Faker('ru_RU')

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ==========================================
# 🛡 SYSTEM UTILS & ANTI-CRASH
# ==========================================

def aggressive_cleanup():
    """Убивает зомби-процессы и чистит кэш Chrome."""
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
        
    logger.info(f"✅ Убито процессов: {killed}. Временные папки очищены.")

def is_memory_critical() -> bool:
    """True, если свободной ОЗУ < 200MB."""
    free_mb = psutil.virtual_memory().available / (1024 * 1024)
    if free_mb < 200:
        logger.warning(f"⚠️ КРИТИЧЕСКАЯ ПАМЯТЬ: {free_mb:.2f} MB")
        return True
    return False

# ==========================================
# 💾 БАЗА ДАННЫХ (aiosqlite)
# ==========================================

async def db_init():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS accounts (
            phone TEXT PRIMARY KEY,
            status TEXT DEFAULT 'active',
            messages_sent INTEGER DEFAULT 0,
            last_active TEXT
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS whitelist (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            approved INTEGER DEFAULT 0
        )""")
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

async def db_get_active_phones() -> list:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT phone FROM accounts WHERE status='active'") as cur:
            return [row[0] for row in await cur.fetchall()]

async def db_save_account(phone: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO accounts (phone, status, last_active) VALUES (?, 'active', ?)",
            (phone, datetime.now().isoformat())
        )
        await db.commit()

async def db_inc_msg(phone: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE accounts SET messages_sent = messages_sent + 1 WHERE phone=?", (phone,))
        await db.commit()

# ==========================================
# 🌐 SELENIUM ENGINE
# ==========================================

def get_random_ua() -> str:
    uas = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    ]
    return random.choice(uas)

def create_driver(phone: str) -> webdriver.Chrome:
    profile_path = os.path.join(SESSIONS_DIR, phone)
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
    
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    options.add_argument("--disable-blink-features=AutomationControlled")

    service = Service()
    driver = webdriver.Chrome(service=service, options=options)
    driver.execute_cdp_cmd('Emulation.setTimezoneOverride', {'timezoneId': 'Asia/Almaty'})
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return driver

# ==========================================
# 🧠 WHATSAPP ЛОГИКА (Синхронные воркеры)
# ==========================================

def _human_type(element, text: str):
    for char in text:
        if random.random() < 0.03:
            element.send_keys(random.choice("фывапролдж"))
            time.sleep(random.uniform(0.1, 0.3))
            element.send_keys(Keys.BACKSPACE)
        element.send_keys(char)
        time.sleep(random.uniform(0.05, 0.2))

def sync_login_qr(phone: str) -> tuple[bool, bytes]:
    """Генерация полноэкранного QR."""
    driver = None
    try:
        driver = create_driver(phone)
        driver.get("https://web.whatsapp.com")
        
        try:
            WebDriverWait(driver, 7).until(EC.presence_of_element_located((By.ID, "pane-side")))
            return True, b""
        except:
            pass
            
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.CSS_SELECTOR, "canvas")))
        time.sleep(4) 
        return False, driver.get_screenshot_as_png()
    except Exception as e:
        logger.error(f"QR Error {phone}: {e}")
        return False, b""
    finally:
        if driver: driver.quit()

def sync_wait_login(phone: str) -> bool:
    """Ожидание успешного сканирования."""
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

def sync_farm_step(sender: str, is_solo: bool, target: str = None) -> bool:
    """Единичный шаг прогрева (соло или парный)."""
    driver = None
    try:
        driver = create_driver(sender)
        driver.get("https://web.whatsapp.com")
        
        try:
            WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.ID, "pane-side")))
        except:
            logger.warning(f"[FARM] {sender} вылетел из сессии.")
            return False

        if is_solo:
            # Пишем сами себе
            driver.get(f"https://web.whatsapp.com/send?phone={sender}")
        else:
            # Пишем другому аккаунту из базы
            driver.get(f"https://web.whatsapp.com/send?phone={target}")
            
        inp = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div[contenteditable='true'][data-tab]"))
        )
        time.sleep(random.uniform(2, 5))
        
        msg = fake.sentence(nb_words=6)
        _human_type(inp, msg)
        time.sleep(1)
        inp.send_keys(Keys.ENTER)
        time.sleep(2)
        
        mode_str = "СОЛО" if is_solo else f"ПАРА -> {target}"
        logger.info(f"[FARM] {sender} ({mode_str}): {msg}")
        return True
    except Exception as e:
        logger.error(f"[FARM] Ошибка {sender}: {e}")
        return False
    finally:
        if driver: driver.quit()

# ==========================================
# 🤖 BOT HANDLERS & MIDDLEWARES
# ==========================================

class AuthState(StatesGroup):
    wait_phone = State()

async def check_subscription(user_id: int) -> bool:
    """Проверка подписки на обязательный канал."""
    if not REQUIRED_CHANNEL_ID: return True
    try:
        member = await bot.get_chat_member(chat_id=REQUIRED_CHANNEL_ID, user_id=user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception:
        return False

@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    user_id = msg.from_user.id
    
    # 1. Проверка подписки
    if not await check_subscription(user_id):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Подписаться", url=REQUIRED_CHANNEL_URL)]
        ])
        return await msg.answer("⛔ Для доступа к боту подпишитесь на канал!", reply_markup=kb)

    # 2. Проверка вайтлиста
    if not await db_check_access(user_id):
        await db_request_access(user_id, msg.from_user.username or "unknown")
        await msg.answer("⏳ Заявка на доступ отправлена администратору.")
        if ADMIN_ID:
            await bot.send_message(ADMIN_ID, f"🛡 Новый запрос от @{msg.from_user.username} ({user_id}).\nКоманда: `/allow {user_id}`", parse_mode="Markdown")
        return

    # 3. Главное меню
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 Добавить аккаунт (QR)", callback_data="add_qr")],
        [InlineKeyboardButton(text="📋 База аккаунтов", callback_data="list_accs")]
    ])
    await msg.answer("🔱 *IMPERATOR v26.0 WARLORD*\nДоступ разрешен. Выберите действие:", parse_mode="Markdown", reply_markup=kb)

@dp.message(Command("allow"))
async def cmd_allow(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    try:
        target_id = int(msg.text.split()[1])
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE whitelist SET approved=1 WHERE user_id=?", (target_id,))
            await db.commit()
        await msg.answer(f"✅ Доступ разрешен: {target_id}")
        await bot.send_message(target_id, "✅ Доступ одобрен! Нажмите /start")
    except:
        await msg.answer("Использование: `/allow <user_id>`", parse_mode="Markdown")

@dp.callback_query(F.data == "list_accs")
async def cb_list(cb: types.CallbackQuery):
    accs = await db_get_active_phones()
    text = f"📋 База активных: *{len(accs)}*\n" + "\n".join(f"• `{p}`" for p in accs) if accs else "База пуста."
    await cb.message.answer(text, parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data == "add_qr")
async def cb_add_qr(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("📱 Введите номер (создание профиля, напр. 77001234567):")
    await state.set_state(AuthState.wait_phone)
    await cb.answer()

@dp.message(AuthState.wait_phone)
async def process_phone(msg: types.Message, state: FSMContext):
    phone = re.sub(r"\D", "", msg.text)
    
    if is_memory_critical():
        return await msg.answer("⚠️ Сервер перегружен. Очистите память.")

    status = await msg.answer("⏳ Генерирую QR-код (полный экран)...")
    is_logged, screenshot = await asyncio.to_thread(sync_login_qr, phone)
    
    if is_logged:
        await db_save_account(phone)
        await status.edit_text("✅ Аккаунт уже авторизован! Включен в Hive Mind.")
        await state.clear()
        return

    if screenshot:
        await status.delete()
        await msg.answer_photo(
            photo=BufferedInputFile(screenshot, filename="qr.png"),
            caption="📷 Отсканируйте этот QR-код.\n⏳ Ожидаю входа (до 2 мин)..."
        )
        
        success = await asyncio.to_thread(sync_wait_login, phone)
        if success:
            await db_save_account(phone)
            await msg.answer(f"✅ Успех! Аккаунт `{phone}` добавлен в ферму.", parse_mode="Markdown")
        else:
            await msg.answer("❌ Время вышло. Попробуйте снова.")
    else:
        await status.edit_text("❌ Ошибка генерации QR. Проверьте логи.")
    
    await state.clear()

# ==========================================
# 🐝 HIVE MIND (ФАРМ ПРОЦЕССОР)
# ==========================================

async def farm_worker(sender: str, is_solo: bool, target: str = None):
    """Обёртка для семафора и обновления БД."""
    async with BROWSER_SEMAPHORE:
        if is_memory_critical():
            logger.warning(f"Пропуск {sender} из-за нехватки ОЗУ.")
            return
            
        success = await asyncio.to_thread(sync_farm_step, sender, is_solo, target)
        if success:
            await db_inc_msg(sender)
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("UPDATE accounts SET last_active=? WHERE phone=?", (datetime.now().isoformat(), sender))
                await db.commit()

async def hive_loop():
    logger.info("🔥 IMPERATOR FARM STARTED (HIVE MIND)")
    while True:
        try:
            accs = await db_get_active_phones()
            if not accs:
                await asyncio.sleep(30)
                continue
            
            sender = random.choice(accs)
            is_solo = random.random() < 0.5  # 50% шанс соло
            
            target = None
            if not is_solo and len(accs) > 1:
                targets = [a for a in accs if a != sender]
                target = random.choice(targets)
            else:
                is_solo = True # Принудительно соло, если аккаунт один
            
            asyncio.create_task(farm_worker(sender, is_solo, target))
            
            delay = random.randint(FARM_DELAY_MIN, FARM_DELAY_MAX)
            logger.info(f"💤 Пауза перед следующим ботом: {delay}с...")
            await asyncio.sleep(delay)
            
        except Exception as e:
            logger.error(f"Hive Loop Error: {e}")
            await asyncio.sleep(15)

# ==========================================
# 🚀 MAIN
# ==========================================

async def main():
    if not BOT_TOKEN:
        logger.critical("❌ BOT_TOKEN НЕ НАЙДЕН!")
        sys.exit(1)

    aggressive_cleanup()
    await db_init()
    
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
