import sys
import asyncio
import os
import logging
import random
import psutil
import shutil
import aiosqlite
import time
import re
import signal
from typing import Optional, List, Dict
from dataclasses import dataclass
from contextlib import asynccontextmanager

# 🚀 UVLOOP
if sys.platform != 'win32':
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except ImportError: 
        pass

# --- AIOGRAM ---
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile, Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

# --- SELENIUM & THREADING ---
from functools import partial
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import WebDriverException, TimeoutException

# ==========================================
# 🛡️ 1. CONFIG & SECURITY
# ==========================================

@dataclass
class Config:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    ADMIN_IDS: List[int] = None
    CHANNEL_ID: str = os.getenv("CHANNEL_ID", "@WhatsAppstatpro")
    
    # Ресурсы
    MIN_RAM_MB: int = 1000
    MAX_BROWSERS: int = 1
    
    # Пути
    DB_NAME: str = 'imperator_secure.db'
    SESSIONS_DIR: str = os.path.abspath("./sessions")
    TMP_BASE: str = os.path.abspath("./tmp")
    
    # Тайминги (сек)
    TIMEOUT_PAGE_LOAD: int = 60
    TIMEOUT_ELEMENT: int = 30
    TIMEOUT_SCREENSHOT: int = 10
    CALLBACK_ANSWER_TIMEOUT: int = 5
    
    def __post_init__(self):
        admins = os.getenv("ADMIN_IDS", "0")
        self.ADMIN_IDS = [int(x) for x in admins.split(",") if x.isdigit()]

cfg = Config()

# Проверка токена
if len(cfg.BOT_TOKEN) < 40:
    logging.critical("❌ SECURITY ALERT: BOT_TOKEN is missing or too short!")
    sys.exit(1)

# Создание папок
for d in [cfg.SESSIONS_DIR, cfg.TMP_BASE]:
    os.makedirs(d, exist_ok=True)

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | SECURE | %(levelname)s | %(name)s | %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("secure_bot.log", mode='a')
    ]
)
logger = logging.getLogger("Core")

# Семафор
BROWSER_SEMAPHORE = asyncio.Semaphore(cfg.MAX_BROWSERS)
ACTIVE_SESSIONS = {}  # {phone: {'driver': driver, 'lock': asyncio.Lock(), 'pid': int, 'tmp': str}}
SHUTDOWN_EVENT = asyncio.Event()

# ==========================================
# 🛡️ 2. SECURITY UTILS (VALIDATION & LIMITS)
# ==========================================

def validate_phone(phone: str) -> bool:
    """Строгая проверка: только цифры, длина 7-15."""
    return bool(re.match(r'^\d{7,15}$', phone))

class RateLimitMiddleware(BaseMiddleware):
    """Простая защита от флуда (Rate Limiting)"""
    def __init__(self, limit=1.5):
        self.rate_limit = limit
        self.last_seen = {}

    async def __call__(self, handler, event, data):
        if not isinstance(event, Message):
            return await handler(event, data)
            
        user_id = event.from_user.id
        if user_id in cfg.ADMIN_IDS:
            return await handler(event, data)

        now = time.time()
        if user_id in self.last_seen:
            if now - self.last_seen[user_id] < self.rate_limit:
                return 
        
        self.last_seen[user_id] = now
        return await handler(event, data)

def memory_guard():
    """Проверка памяти. True если безопасно."""
    try:
        mem = psutil.virtual_memory()
        free_mb = mem.available / (1024 * 1024)
        if free_mb < cfg.MIN_RAM_MB:
            logger.warning(f"🚨 RAM CRITICAL: {int(free_mb)}MB free. Blocking new tasks.")
            return False
        return True
    except Exception as e:
        logger.error(f"Memory check failed: {e}")
        return False

def kill_zombies():
    """Убивает только наши процессы Chrome/Driver по сохраненным PID."""
    logger.info("🧟 Hunting zombie processes...")
    pids_to_kill = set()
    
    # Собираем PID из активных сессий
    for phone, sess in ACTIVE_SESSIONS.items():
        if 'pid' in sess and sess['pid']:
            pids_to_kill.add(sess['pid'])
    
    # Убиваем только наши процессы
    for pid in pids_to_kill:
        try:
            proc = psutil.Process(pid)
            proc.kill()
            logger.info(f"Killed process {pid}")
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            logger.debug(f"Could not kill {pid}: {e}")

def cleanup_temp_folders():
    """Очистка временных папок"""
    try:
        if os.path.exists(cfg.TMP_BASE):
            for item in os.listdir(cfg.TMP_BASE):
                item_path = os.path.join(cfg.TMP_BASE, item)
                try:
                    if os.path.isdir(item_path):
                        shutil.rmtree(item_path, ignore_errors=True)
                except Exception as e:
                    logger.error(f"Failed to remove {item_path}: {e}")
    except Exception as e:
        logger.error(f"Cleanup failed: {e}")

# ==========================================
# ⚡ 3. ASYNC SELENIUM WRAPPER (NON-BLOCKING)
# ==========================================

class AsyncDriver:
    """Обертка для запуска блокирующих методов Selenium в ThreadPool"""
    
    def __init__(self, driver, tmp_dir: str):
        self.driver = driver
        self.tmp_dir = tmp_dir
        self.loop = asyncio.get_running_loop()
        self._closed = False

    async def get(self, url):
        if self._closed:
            raise RuntimeError("Driver already closed")
        try:
            await asyncio.wait_for(
                self.loop.run_in_executor(None, self.driver.get, url),
                timeout=cfg.TIMEOUT_PAGE_LOAD
            )
        except asyncio.TimeoutError:
            logger.error(f"Page load timeout for {url}")
            raise

    async def find_element(self, by, value):
        if self._closed:
            raise RuntimeError("Driver already closed")
        return await asyncio.wait_for(
            self.loop.run_in_executor(None, self.driver.find_element, by, value),
            timeout=cfg.TIMEOUT_ELEMENT
        )

    async def execute_script(self, script, *args):
        if self._closed:
            raise RuntimeError("Driver already closed")
        return await self.loop.run_in_executor(None, self.driver.execute_script, script, *args)
    
    async def screenshot(self):
        if self._closed:
            raise RuntimeError("Driver already closed")
        try:
            return await asyncio.wait_for(
                self.loop.run_in_executor(None, self.driver.get_screenshot_as_png),
                timeout=cfg.TIMEOUT_SCREENSHOT
            )
        except asyncio.TimeoutError:
            logger.error("Screenshot timeout")
            raise

    async def quit(self):
        if self._closed:
            return
        self._closed = True
        try:
            await asyncio.wait_for(
                self.loop.run_in_executor(None, self.driver.quit),
                timeout=10
            )
        except asyncio.TimeoutError:
            logger.warning("Driver quit timeout, force killing")
        except Exception as e:
            logger.error(f"Error during quit: {e}")
        finally:
            # Всегда удаляем временную папку
            try:
                if os.path.exists(self.tmp_dir):
                    shutil.rmtree(self.tmp_dir, ignore_errors=True)
            except Exception as e:
                logger.error(f"Failed to remove tmp dir {self.tmp_dir}: {e}")

    async def wait_click(self, by, value, timeout=10):
        """Безопасный клик с ожиданием"""
        if self._closed:
            raise RuntimeError("Driver already closed")
            
        def _blocking_click():
            try:
                wait = WebDriverWait(self.driver, timeout)
                el = wait.until(EC.element_to_be_clickable((by, value)))
                self.driver.execute_script("arguments[0].click();", el)
                return True
            except Exception as e:
                logger.error(f"Click failed: {e}")
                return False
        
        return await asyncio.wait_for(
            self.loop.run_in_executor(None, _blocking_click),
            timeout=timeout + 2
        )

def get_driver_sync(phone: str):
    """Синхронная часть создания драйвера (запускается в executor)"""
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    
    opts = Options()
    prof = os.path.join(cfg.SESSIONS_DIR, phone)
    tmp = os.path.join(cfg.TMP_BASE, f"tmp_{phone}_{int(time.time())}")
    os.makedirs(tmp, exist_ok=True)
    os.makedirs(prof, exist_ok=True)

    opts.add_argument(f"--user-data-dir={prof}")
    opts.add_argument(f"--data-path={tmp}")
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument(f"--user-agent={ua}")
    opts.add_argument("--blink-settings=imagesEnabled=false")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option('useAutomationExtension', False)

    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(cfg.TIMEOUT_PAGE_LOAD)
    
    # JS Stealth Patch
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    })
    
    # Получаем PID процесса
    pid = driver.service.process.pid if driver.service and driver.service.process else None
    
    return driver, tmp, pid

async def get_async_driver(phone: str):
    """Асинхронная фабрика драйверов"""
    if not memory_guard():
        return None
        
    loop = asyncio.get_running_loop()
    try:
        driver, tmp, pid = await loop.run_in_executor(None, get_driver_sync, phone)
        async_driver = AsyncDriver(driver, tmp)
        return async_driver, tmp, pid
    except Exception as e:
        logger.error(f"Driver Init Failed: {e}", exc_info=True)
        return None, None, None

async def close_session(phone: str):
    """Безопасное закрытие сессии с очисткой ресурсов"""
    if phone not in ACTIVE_SESSIONS:
        return
    
    try:
        sess = ACTIVE_SESSIONS.pop(phone)
        driver = sess.get('driver')
        
        if driver:
            await driver.quit()
        
        # Убиваем процесс если еще жив
        if 'pid' in sess and sess['pid']:
            try:
                proc = psutil.Process(sess['pid'])
                proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        
        logger.info(f"Session {phone} closed successfully")
    except Exception as e:
        logger.error(f"Error closing session {phone}: {e}", exc_info=True)

# ==========================================
# 🗄️ 4. DATABASE (SECURE)
# ==========================================

async def db_init():
    try:
        async with aiosqlite.connect(cfg.DB_NAME) as db:
            await db.execute("""CREATE TABLE IF NOT EXISTS accounts 
                                (phone TEXT PRIMARY KEY, 
                                 status TEXT DEFAULT 'active', 
                                 mode TEXT DEFAULT 'normal',
                                 created_at REAL)""")
            await db.commit()
    except Exception as e:
        logger.error(f"DB init failed: {e}", exc_info=True)
        raise

async def db_add_account(phone: str):
    if not validate_phone(phone): 
        return False
    try:
        async with aiosqlite.connect(cfg.DB_NAME) as db:
            await db.execute("INSERT OR REPLACE INTO accounts (phone, created_at) VALUES (?, ?)", 
                             (phone, time.time()))
            await db.commit()
        return True
    except Exception as e:
        logger.error(f"DB add failed: {e}", exc_info=True)
        return False

async def db_get_all():
    try:
        async with aiosqlite.connect(cfg.DB_NAME) as db:
            async with db.execute("SELECT phone FROM accounts") as cur:
                return [r[0] for r in await cur.fetchall()]
    except Exception as e:
        logger.error(f"DB get_all failed: {e}", exc_info=True)
        return []

async def db_delete(phone: str):
    if not validate_phone(phone): 
        return
    try:
        async with aiosqlite.connect(cfg.DB_NAME) as db:
            await db.execute("DELETE FROM accounts WHERE phone=?", (phone,))
            await db.commit()
    except Exception as e:
        logger.error(f"DB delete failed: {e}", exc_info=True)

# ==========================================
# 🤖 5. BOT LOGIC
# ==========================================

bot = Bot(token=cfg.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
dp.message.middleware(RateLimitMiddleware(limit=1.5))

class States(StatesGroup):
    add_phone = State()

async def safe_answer_callback(cb: CallbackQuery, text: str, show_alert: bool = False):
    """Безопасный ответ на callback с обработкой устаревших query"""
    try:
        await asyncio.wait_for(
            cb.answer(text, show_alert=show_alert),
            timeout=cfg.CALLBACK_ANSWER_TIMEOUT
        )
    except TelegramBadRequest as e:
        if "query is too old" in str(e):
            logger.debug(f"Callback expired: {e}")
        else:
            logger.error(f"Telegram error: {e}")
    except asyncio.TimeoutError:
        logger.warning("Callback answer timeout")
    except Exception as e:
        logger.error(f"Callback answer error: {e}", exc_info=True)

async def safe_edit_message(message: Message, text: str, reply_markup=None):
    """Безопасное редактирование сообщения"""
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            logger.debug("Message not modified")
        else:
            logger.error(f"Edit failed: {e}")
    except Exception as e:
        logger.error(f"Edit error: {e}", exc_info=True)

@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    if msg.from_user.id not in cfg.ADMIN_IDS:
        return
    
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить номер", callback_data="add_new")
    kb.button(text="📋 Список", callback_data="list_all")
    kb.button(text="🚨 Panic Button (Kill All)", callback_data="panic_kill")
    kb.adjust(1)
    
    ram_free = psutil.virtual_memory().available // (1024**2)
    active_sessions = len(ACTIVE_SESSIONS)
    
    await msg.answer(
        f"🔒 **SecureBot v38.0 Fixed**\n"
        f"RAM Free: {ram_free}MB\n"
        f"Active Sessions: {active_sessions}",
        reply_markup=kb.as_markup()
    )

@dp.callback_query(F.data == "add_new")
async def cb_add(cb: types.CallbackQuery, state: FSMContext):
    await safe_edit_message(cb.message, "📱 Введите номер (только цифры, 7-15 знаков):")
    await state.set_state(States.add_phone)
    await safe_answer_callback(cb, "Ожидаю номер")

@dp.message(States.add_phone)
async def input_phone(msg: types.Message, state: FSMContext):
    phone = "".join(filter(str.isdigit, msg.text))
    
    if not validate_phone(phone):
        return await msg.answer("❌ Некорректный формат номера. Попробуйте еще раз.")
    
    if phone in ACTIVE_SESSIONS:
        return await msg.answer(f"⚠️ Номер {phone} уже активен!")
    
    await db_add_account(phone)
    await state.clear()
    
    status_msg = await msg.answer(f"🚀 Инициализация драйвера для {phone}...")
    
    try:
        async with BROWSER_SEMAPHORE:
            drv, tmp, pid = await get_async_driver(phone)
            
            if not drv:
                return await status_msg.edit_text("❌ Не удалось запустить драйвер (OOM or Crash).")
            
            ACTIVE_SESSIONS[phone] = {
                'driver': drv, 
                'tmp': tmp, 
                'pid': pid,
                'created': time.time()
            }
            
            try:
                await status_msg.edit_text(f"⏳ Загрузка WhatsApp Web для {phone}...")
                await drv.get("https://web.whatsapp.com")
                
                kb = InlineKeyboardBuilder()
                kb.button(text="📸 Скрин", callback_data=f"scr_{phone}")
                kb.button(text="🚪 Закрыть", callback_data=f"exit_{phone}")
                kb.adjust(1)
                
                await status_msg.edit_text(
                    f"✅ Браузер запущен: +{phone}\n"
                    f"PID: {pid}",
                    reply_markup=kb.as_markup()
                )
                
            except Exception as e:
                logger.error(f"WhatsApp load error: {e}", exc_info=True)
                await close_session(phone)
                await status_msg.edit_text(f"❌ Ошибка при загрузке WhatsApp: {e}")
                
    except Exception as e:
        logger.error(f"Session init error: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Критическая ошибка: {e}")

@dp.callback_query(F.data.startswith("scr_"))
async def cb_screenshot(cb: types.CallbackQuery):
    phone = cb.data.split("_", 1)[1]
    
    if not validate_phone(phone):
        return await safe_answer_callback(cb, "❌ Некорректный номер", show_alert=True)
    
    if phone not in ACTIVE_SESSIONS:
        return await safe_answer_callback(cb, "❌ Сессия не активна", show_alert=True)
    
    await safe_answer_callback(cb, "📸 Создаю скриншот...")
    
    sess = ACTIVE_SESSIONS[phone]
    drv = sess['driver']
    
    try:
        png_data = await drv.screenshot()
        await cb.message.answer_photo(
            BufferedInputFile(png_data, f"screen_{phone}.png"),
            caption=f"📸 Скриншот: +{phone}"
        )
    except asyncio.TimeoutError:
        await cb.message.answer("❌ Таймаут создания скриншота")
    except Exception as e:
        logger.error(f"Screenshot error: {e}", exc_info=True)
        await cb.message.answer(f"❌ Ошибка: {str(e)[:100]}")

@dp.callback_query(F.data.startswith("exit_"))
async def cb_exit(cb: types.CallbackQuery):
    phone = cb.data.split("_", 1)[1]
    
    if phone not in ACTIVE_SESSIONS:
        return await safe_answer_callback(cb, "Уже остановлено")
    
    await safe_answer_callback(cb, "🛑 Останавливаю...")
    
    try:
        await close_session(phone)
        await safe_edit_message(cb.message, f"🛑 Сессия {phone} остановлена.")
    except Exception as e:
        logger.error(f"Exit error: {e}", exc_info=True)
        await safe_edit_message(cb.message, f"⚠️ Ошибка остановки: {e}")

@dp.callback_query(F.data == "list_all")
async def cb_list(cb: types.CallbackQuery):
    await safe_answer_callback(cb, "📋 Загружаю список...")
    
    try:
        phones = await db_get_all()
        
        if not phones:
            return await safe_edit_message(cb.message, "📋 Список пуст")
        
        text = "📋 **Аккаунты:**\n\n"
        for phone in phones:
            status = "🟢 Active" if phone in ACTIVE_SESSIONS else "⚪️ Inactive"
            text += f"• {phone} — {status}\n"
        
        kb = InlineKeyboardBuilder()
        kb.button(text="🔙 Назад", callback_data="start")
        
        await safe_edit_message(cb.message, text, reply_markup=kb.as_markup())
    except Exception as e:
        logger.error(f"List error: {e}", exc_info=True)
        await safe_answer_callback(cb, f"Ошибка: {e}", show_alert=True)

@dp.callback_query(F.data == "start")
async def cb_start(cb: types.CallbackQuery):
    await safe_answer_callback(cb, "🏠")
    
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить номер", callback_data="add_new")
    kb.button(text="📋 Список", callback_data="list_all")
    kb.button(text="🚨 Panic Button (Kill All)", callback_data="panic_kill")
    kb.adjust(1)
    
    ram_free = psutil.virtual_memory().available // (1024**2)
    active_sessions = len(ACTIVE_SESSIONS)
    
    await safe_edit_message(
        cb.message,
        f"🔒 **SecureBot v38.0 Fixed**\n"
        f"RAM Free: {ram_free}MB\n"
        f"Active Sessions: {active_sessions}",
        reply_markup=kb.as_markup()
    )

@dp.callback_query(F.data == "panic_kill")
async def cb_panic(cb: types.CallbackQuery):
    if cb.from_user.id not in cfg.ADMIN_IDS: 
        return
    
    await safe_answer_callback(cb, "💀 KILLING ALL...", show_alert=True)
    await safe_edit_message(cb.message, "💀 KILLING ALL PROCESSES...")
    
    # Закрываем все сессии
    sessions_to_close = list(ACTIVE_SESSIONS.keys())
    for phone in sessions_to_close:
        try:
            await close_session(phone)
        except Exception as e:
            logger.error(f"Error killing {phone}: {e}")
    
    kill_zombies()
    cleanup_temp_folders()
    
    await asyncio.sleep(1)
    await cb.message.answer("✅ Система очищена.")

# ==========================================
# 🚀 GRACEFUL SHUTDOWN
# ==========================================

async def shutdown(signal_name=None):
    """Graceful shutdown с очисткой всех ресурсов"""
    if signal_name:
        logger.info(f"Received exit signal {signal_name}")
    
    SHUTDOWN_EVENT.set()
    
    # Закрываем все сессии
    logger.info("Closing all active sessions...")
    sessions_to_close = list(ACTIVE_SESSIONS.keys())
    for phone in sessions_to_close:
        try:
            await close_session(phone)
        except Exception as e:
            logger.error(f"Error during shutdown of {phone}: {e}")
    
    # Убиваем зомби
    kill_zombies()
    cleanup_temp_folders()
    
    # Отменяем все задачи
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    logger.info(f"Cancelling {len(tasks)} outstanding tasks")
    
    for task in tasks:
        task.cancel()
    
    await asyncio.gather(*tasks, return_exceptions=True)
    
    logger.info("Shutdown complete")

def handle_signal(sig):
    """Обработчик сигналов"""
    asyncio.create_task(shutdown(sig))

# ==========================================
# 🚀 MAIN LOOP
# ==========================================

async def main():
    logger.info("🔒 Starting SecureBot v38.0 Fixed...")
    
    # Очистка перед стартом
    kill_zombies()
    cleanup_temp_folders()
    
    try:
        await db_init()
    except Exception as e:
        logger.critical(f"Failed to initialize database: {e}")
        return
    
    # Регистрация обработчиков сигналов
    if sys.platform != 'win32':
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda s=sig: handle_signal(s))
    
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Starting polling...")
        await dp.start_polling(bot, handle_signals=False)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received")
    except Exception as e:
        logger.critical(f"Main Loop Crash: {e}", exc_info=True)
    finally:
        await shutdown("main_finally")
        await bot.session.close()

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Exiting...")
