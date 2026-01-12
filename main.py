#!/usr/bin/env python3
"""
SecureBot v39.0 - Production Ready
WhatsApp Web Automation Bot with Full Error Handling
"""

import sys
import asyncio
import os
import logging
import shutil
import aiosqlite
import time
import re
import signal
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass, field
from contextlib import asynccontextmanager

# UVLOOP для производительности
if sys.platform != 'win32':
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except ImportError:
        pass

# Основные импорты
import psutil
from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest, TelegramAPIError

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException, TimeoutException

# ==========================================
# 🔧 КОНФИГУРАЦИЯ
# ==========================================

@dataclass
class Config:
    """Конфигурация приложения"""
    BOT_TOKEN: str = ""
    ADMIN_IDS: List[int] = field(default_factory=list)
    CHANNEL_ID: str = "@WhatsAppstatpro"
    
    # Ресурсы
    MIN_RAM_MB: int = 800
    MAX_BROWSERS: int = 1
    
    # Пути
    DB_NAME: str = 'whatsapp_bot.db'
    SESSIONS_DIR: str = './sessions'
    TMP_DIR: str = './tmp'
    LOG_FILE: str = 'bot.log'
    
    # Таймауты
    TIMEOUT_PAGE: int = 60
    TIMEOUT_ELEMENT: int = 20
    TIMEOUT_SCREENSHOT: int = 10
    TIMEOUT_CALLBACK: int = 5
    
    def __post_init__(self):
        """Инициализация после создания"""
        # Загрузка токена
        self.BOT_TOKEN = os.getenv("BOT_TOKEN", "")
        if len(self.BOT_TOKEN) < 40:
            raise ValueError("❌ BOT_TOKEN is missing or invalid!")
        
        # Загрузка админов
        admin_str = os.getenv("ADMIN_IDS", "")
        self.ADMIN_IDS = [int(x.strip()) for x in admin_str.split(",") if x.strip().isdigit()]
        if not self.ADMIN_IDS:
            raise ValueError("❌ ADMIN_IDS is missing!")
        
        # Создание директорий
        for path in [self.SESSIONS_DIR, self.TMP_DIR]:
            os.makedirs(path, exist_ok=True)
        
        # Канал из переменной окружения
        self.CHANNEL_ID = os.getenv("CHANNEL_ID", self.CHANNEL_ID)

# Глобальный конфиг
cfg = Config()

# ==========================================
# 📝 ЛОГИРОВАНИЕ
# ==========================================

def setup_logging():
    """Настройка системы логирования"""
    log_format = '%(asctime)s | %(levelname)-8s | %(name)-12s | %(message)s'
    
    # Создаем корневой logger
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    # Консольный handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(log_format))
    logger.addHandler(console)
    
    # Файловый handler
    try:
        file_handler = logging.FileHandler(cfg.LOG_FILE, mode='a', encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(log_format))
        logger.addHandler(file_handler)
    except Exception as e:
        print(f"⚠️ Cannot create log file: {e}")
    
    return logging.getLogger("Bot")

logger = setup_logging()

# ==========================================
# 🛡️ БЕЗОПАСНОСТЬ И ВАЛИДАЦИЯ
# ==========================================

def validate_phone(phone: str) -> bool:
    """Валидация номера телефона"""
    return bool(re.match(r'^\d{7,15}$', phone))

def check_memory() -> bool:
    """Проверка доступной памяти"""
    try:
        mem = psutil.virtual_memory()
        free_mb = mem.available / (1024 * 1024)
        if free_mb < cfg.MIN_RAM_MB:
            logger.warning(f"⚠️ Low memory: {int(free_mb)}MB free")
            return False
        return True
    except Exception as e:
        logger.error(f"Memory check error: {e}")
        return False

class RateLimiter(BaseMiddleware):
    """Защита от флуда"""
    def __init__(self, rate: float = 1.0):
        self.rate = rate
        self.users = {}
    
    async def __call__(self, handler, event: Message, data: dict):
        if not isinstance(event, Message):
            return await handler(event, data)
        
        user_id = event.from_user.id
        
        # Админы без лимитов
        if user_id in cfg.ADMIN_IDS:
            return await handler(event, data)
        
        # Проверка лимита
        now = time.time()
        if user_id in self.users:
            if now - self.users[user_id] < self.rate:
                logger.debug(f"Rate limit for user {user_id}")
                return
        
        self.users[user_id] = now
        return await handler(event, data)

# ==========================================
# 🌐 ASYNC SELENIUM DRIVER
# ==========================================

class AsyncDriver:
    """Асинхронная обертка для Selenium WebDriver"""
    
    def __init__(self, driver, tmp_dir: str, pid: int):
        self.driver = driver
        self.tmp_dir = tmp_dir
        self.pid = pid
        self.loop = asyncio.get_running_loop()
        self.closed = False
    
    async def get(self, url: str, timeout: int = None):
        """Загрузка страницы"""
        if self.closed:
            raise RuntimeError("Driver is closed")
        
        timeout = timeout or cfg.TIMEOUT_PAGE
        try:
            await asyncio.wait_for(
                self.loop.run_in_executor(None, self.driver.get, url),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.error(f"Page load timeout: {url}")
            raise
    
    async def screenshot(self) -> bytes:
        """Создание скриншота"""
        if self.closed:
            raise RuntimeError("Driver is closed")
        
        try:
            return await asyncio.wait_for(
                self.loop.run_in_executor(None, self.driver.get_screenshot_as_png),
                timeout=cfg.TIMEOUT_SCREENSHOT
            )
        except asyncio.TimeoutError:
            logger.error("Screenshot timeout")
            raise
    
    async def execute_script(self, script: str, *args):
        """Выполнение JavaScript"""
        if self.closed:
            raise RuntimeError("Driver is closed")
        
        return await self.loop.run_in_executor(
            None, self.driver.execute_script, script, *args
        )
    
    async def find_element(self, by: str, value: str):
        """Поиск элемента"""
        if self.closed:
            raise RuntimeError("Driver is closed")
        
        return await asyncio.wait_for(
            self.loop.run_in_executor(None, self.driver.find_element, by, value),
            timeout=cfg.TIMEOUT_ELEMENT
        )
    
    async def quit(self):
        """Закрытие драйвера"""
        if self.closed:
            return
        
        self.closed = True
        
        try:
            # Попытка graceful quit
            await asyncio.wait_for(
                self.loop.run_in_executor(None, self._safe_quit),
                timeout=5
            )
        except asyncio.TimeoutError:
            logger.warning(f"Driver quit timeout (PID: {self.pid})")
            self._force_kill()
        except Exception as e:
            logger.error(f"Driver quit error: {e}")
            self._force_kill()
        finally:
            # Очистка временных файлов
            try:
                if os.path.exists(self.tmp_dir):
                    shutil.rmtree(self.tmp_dir, ignore_errors=True)
            except Exception as e:
                logger.error(f"Temp cleanup error: {e}")
    
    def _safe_quit(self):
        """Безопасное закрытие (синхронно)"""
        try:
            self.driver.quit()
        except Exception as e:
            logger.debug(f"Quit exception: {e}")
    
    def _force_kill(self):
        """Принудительное убийство процесса"""
        try:
            if self.pid:
                proc = psutil.Process(self.pid)
                proc.kill()
                logger.info(f"Force killed process {self.pid}")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

def create_driver_sync(phone: str) -> Tuple[webdriver.Chrome, str, int]:
    """Синхронное создание драйвера (для executor)"""
    
    # Настройки Chrome
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-extensions")
    options.add_argument("--blink-settings=imagesEnabled=false")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    
    # User Agent
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    options.add_argument(f"--user-agent={ua}")
    
    # Профиль и временная папка
    profile_dir = os.path.join(cfg.SESSIONS_DIR, phone)
    tmp_dir = os.path.join(cfg.TMP_DIR, f"tmp_{phone}_{int(time.time())}")
    
    os.makedirs(profile_dir, exist_ok=True)
    os.makedirs(tmp_dir, exist_ok=True)
    
    options.add_argument(f"--user-data-dir={profile_dir}")
    options.add_argument(f"--data-path={tmp_dir}")
    
    # Создание драйвера
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(cfg.TIMEOUT_PAGE)
    
    # Stealth патчи
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        """
    })
    
    # PID процесса
    pid = driver.service.process.pid if driver.service and driver.service.process else None
    
    return driver, tmp_dir, pid

async def create_driver(phone: str) -> Optional[AsyncDriver]:
    """Асинхронное создание драйвера"""
    
    if not check_memory():
        logger.error("Not enough memory to create driver")
        return None
    
    loop = asyncio.get_running_loop()
    
    try:
        driver, tmp_dir, pid = await loop.run_in_executor(
            None, create_driver_sync, phone
        )
        
        async_driver = AsyncDriver(driver, tmp_dir, pid)
        logger.info(f"Driver created for {phone} (PID: {pid})")
        return async_driver
        
    except Exception as e:
        logger.error(f"Driver creation failed: {e}", exc_info=True)
        return None

# ==========================================
# 🗄️ БАЗА ДАННЫХ
# ==========================================

class Database:
    """Управление базой данных"""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
    
    async def init(self):
        """Инициализация БД"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("""
                    CREATE TABLE IF NOT EXISTS accounts (
                        phone TEXT PRIMARY KEY,
                        status TEXT DEFAULT 'active',
                        created_at REAL NOT NULL,
                        last_active REAL
                    )
                """)
                await db.commit()
            logger.info("Database initialized")
        except Exception as e:
            logger.error(f"Database init failed: {e}", exc_info=True)
            raise
    
    async def add_account(self, phone: str) -> bool:
        """Добавление аккаунта"""
        if not validate_phone(phone):
            return False
        
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT OR REPLACE INTO accounts (phone, created_at) VALUES (?, ?)",
                    (phone, time.time())
                )
                await db.commit()
            logger.info(f"Account added: {phone}")
            return True
        except Exception as e:
            logger.error(f"Add account failed: {e}")
            return False
    
    async def get_all_accounts(self) -> List[str]:
        """Получение всех аккаунтов"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                async with db.execute("SELECT phone FROM accounts ORDER BY created_at DESC") as cur:
                    rows = await cur.fetchall()
                    return [row[0] for row in rows]
        except Exception as e:
            logger.error(f"Get accounts failed: {e}")
            return []
    
    async def delete_account(self, phone: str) -> bool:
        """Удаление аккаунта"""
        if not validate_phone(phone):
            return False
        
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("DELETE FROM accounts WHERE phone = ?", (phone,))
                await db.commit()
            logger.info(f"Account deleted: {phone}")
            return True
        except Exception as e:
            logger.error(f"Delete account failed: {e}")
            return False
    
    async def update_activity(self, phone: str):
        """Обновление времени активности"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE accounts SET last_active = ? WHERE phone = ?",
                    (time.time(), phone)
                )
                await db.commit()
        except Exception as e:
            logger.error(f"Update activity failed: {e}")

# ==========================================
# 🎮 SESSION MANAGER
# ==========================================

class SessionManager:
    """Управление сессиями браузеров"""
    
    def __init__(self, max_sessions: int = 1):
        self.sessions: Dict[str, AsyncDriver] = {}
        self.semaphore = asyncio.Semaphore(max_sessions)
        self.locks: Dict[str, asyncio.Lock] = {}
    
    async def create_session(self, phone: str) -> Optional[AsyncDriver]:
        """Создание новой сессии"""
        
        if phone in self.sessions:
            logger.warning(f"Session already exists: {phone}")
            return self.sessions[phone]
        
        async with self.semaphore:
            driver = await create_driver(phone)
            
            if driver:
                self.sessions[phone] = driver
                self.locks[phone] = asyncio.Lock()
                logger.info(f"Session created: {phone}")
            
            return driver
    
    async def close_session(self, phone: str):
        """Закрытие сессии"""
        
        if phone not in self.sessions:
            logger.debug(f"Session not found: {phone}")
            return
        
        try:
            # Получаем lock для безопасного закрытия
            if phone in self.locks:
                async with self.locks[phone]:
                    driver = self.sessions.pop(phone)
                    await driver.quit()
                self.locks.pop(phone)
            else:
                driver = self.sessions.pop(phone)
                await driver.quit()
            
            logger.info(f"Session closed: {phone}")
            
        except Exception as e:
            logger.error(f"Close session error: {e}", exc_info=True)
    
    async def close_all(self):
        """Закрытие всех сессий"""
        phones = list(self.sessions.keys())
        
        for phone in phones:
            try:
                await self.close_session(phone)
            except Exception as e:
                logger.error(f"Error closing {phone}: {e}")
        
        logger.info("All sessions closed")
    
    def get_session(self, phone: str) -> Optional[AsyncDriver]:
        """Получение сессии"""
        return self.sessions.get(phone)
    
    def list_active(self) -> List[str]:
        """Список активных сессий"""
        return list(self.sessions.keys())

# ==========================================
# 🤖 TELEGRAM BOT
# ==========================================

# Глобальные объекты
db = Database(cfg.DB_NAME)
sessions = SessionManager(max_sessions=cfg.MAX_BROWSERS)
bot = Bot(token=cfg.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Middleware
dp.message.middleware(RateLimiter(rate=1.5))

# States
class BotStates(StatesGroup):
    waiting_phone = State()

# ==========================================
# 🔧 ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================

async def safe_answer_callback(cb: CallbackQuery, text: str = "", show_alert: bool = False):
    """Безопасный ответ на callback"""
    try:
        await asyncio.wait_for(
            cb.answer(text, show_alert=show_alert),
            timeout=cfg.TIMEOUT_CALLBACK
        )
    except TelegramBadRequest as e:
        if "query is too old" in str(e).lower():
            logger.debug("Callback query expired")
        else:
            logger.error(f"Callback error: {e}")
    except asyncio.TimeoutError:
        logger.warning("Callback timeout")
    except Exception as e:
        logger.error(f"Callback unexpected error: {e}")

async def safe_edit_message(msg: Message, text: str, reply_markup=None):
    """Безопасное редактирование сообщения"""
    try:
        await msg.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            logger.debug("Message not modified")
        elif "message to edit not found" in str(e).lower():
            logger.debug("Message not found")
        else:
            logger.error(f"Edit error: {e}")
    except Exception as e:
        logger.error(f"Edit unexpected error: {e}")

def get_main_keyboard() -> InlineKeyboardBuilder:
    """Главное меню"""
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить номер", callback_data="add_phone")
    kb.button(text="📋 Список аккаунтов", callback_data="list_accounts")
    kb.button(text="🔄 Обновить статус", callback_data="refresh")
    kb.button(text="🚨 Закрыть все сессии", callback_data="close_all")
    kb.adjust(1)
    return kb

def get_session_keyboard(phone: str) -> InlineKeyboardBuilder:
    """Меню управления сессией"""
    kb = InlineKeyboardBuilder()
    kb.button(text="📸 Скриншот", callback_data=f"screenshot:{phone}")
    kb.button(text="🔄 Обновить", callback_data=f"refresh:{phone}")
    kb.button(text="🚪 Закрыть", callback_data=f"close:{phone}")
    kb.button(text="🗑️ Удалить аккаунт", callback_data=f"delete:{phone}")
    kb.button(text="🔙 Назад", callback_data="menu")
    kb.adjust(2, 2, 1)
    return kb

# ==========================================
# 📱 ОБРАБОТЧИКИ КОМАНД
# ==========================================

@dp.message(Command("start"))
async def cmd_start(msg: Message):
    """Команда /start"""
    
    # Проверка прав
    if msg.from_user.id not in cfg.ADMIN_IDS:
        logger.warning(f"Unauthorized access attempt: {msg.from_user.id}")
        return
    
    # Статистика
    mem = psutil.virtual_memory()
    mem_free = mem.available / (1024**2)
    active = len(sessions.list_active())
    
    text = (
        f"🔐 **WhatsApp Bot v39.0**\n\n"
        f"📊 **Статус:**\n"
        f"• RAM: {int(mem_free)}MB свободно\n"
        f"• Активных сессий: {active}\n"
        f"• Макс. браузеров: {cfg.MAX_BROWSERS}\n\n"
        f"Выберите действие:"
    )
    
    await msg.answer(text, reply_markup=get_main_keyboard().as_markup())

@dp.message(Command("status"))
async def cmd_status(msg: Message):
    """Команда /status"""
    
    if msg.from_user.id not in cfg.ADMIN_IDS:
        return
    
    active = sessions.list_active()
    accounts = await db.get_all_accounts()
    
    text = (
        f"📊 **Статус системы**\n\n"
        f"Аккаунтов в БД: {len(accounts)}\n"
        f"Активных сессий: {len(active)}\n\n"
    )
    
    if active:
        text += "**Активные:**\n"
        for phone in active:
            text += f"• {phone}\n"
    
    await msg.answer(text)

# ==========================================
# 🔘 ОБРАБОТЧИКИ CALLBACK
# ==========================================

@dp.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery):
    """Главное меню"""
    
    await safe_answer_callback(cb)
    
    mem = psutil.virtual_memory()
    mem_free = mem.available / (1024**2)
    active = len(sessions.list_active())
    
    text = (
        f"🔐 **WhatsApp Bot v39.0**\n\n"
        f"📊 **Статус:**\n"
        f"• RAM: {int(mem_free)}MB свободно\n"
        f"• Активных сессий: {active}\n"
        f"• Макс. браузеров: {cfg.MAX_BROWSERS}\n\n"
        f"Выберите действие:"
    )
    
    await safe_edit_message(cb.message, text, reply_markup=get_main_keyboard().as_markup())

@dp.callback_query(F.data == "refresh")
async def cb_refresh(cb: CallbackQuery):
    """Обновление статуса"""
    await cb_menu(cb)

@dp.callback_query(F.data == "add_phone")
async def cb_add_phone(cb: CallbackQuery, state: FSMContext):
    """Начало добавления номера"""
    
    await safe_answer_callback(cb)
    await safe_edit_message(
        cb.message,
        "📱 Введите номер телефона (только цифры, 7-15 символов):\n\n"
        "Например: 79123456789"
    )
    await state.set_state(BotStates.waiting_phone)

@dp.message(BotStates.waiting_phone)
async def process_phone_input(msg: Message, state: FSMContext):
    """Обработка ввода номера"""
    
    # Очистка от лишних символов
    phone = "".join(filter(str.isdigit, msg.text))
    
    if not validate_phone(phone):
        await msg.answer(
            "❌ Некорректный формат номера!\n"
            "Введите 7-15 цифр без пробелов и символов."
        )
        return
    
    # Проверка существования
    if sessions.get_session(phone):
        await msg.answer(f"⚠️ Сессия для {phone} уже активна!")
        await state.clear()
        return
    
    await state.clear()
    
    # Добавление в БД
    await db.add_account(phone)
    
    # Создание сессии
    status_msg = await msg.answer(f"🚀 Запуск браузера для +{phone}...")
    
    try:
        driver = await sessions.create_session(phone)
        
        if not driver:
            await status_msg.edit_text(
                "❌ Не удалось запустить браузер\n"
                "Возможные причины:\n"
                "• Недостаточно RAM\n"
                "• Превышен лимит сессий\n"
                "• Ошибка ChromeDriver"
            )
            return
        
        # Загрузка WhatsApp
        await status_msg.edit_text(f"⏳ Загрузка WhatsApp Web...")
        
        await driver.get("https://web.whatsapp.com")
        
        # Обновление активности
        await db.update_activity(phone)
        
        # Успех
        text = (
            f"✅ **Браузер запущен!**\n\n"
            f"📱 Номер: +{phone}\n"
            f"🆔 PID: {driver.pid}\n\n"
            f"Отсканируйте QR-код в браузере"
        )
        
        await status_msg.edit_text(
            text,
            reply_markup=get_session_keyboard(phone).as_markup()
        )
        
    except Exception as e:
        logger.error(f"Session creation error: {e}", exc_info=True)
        await sessions.close_session(phone)
        await status_msg.edit_text(f"❌ Ошибка: {str(e)[:200]}")

@dp.callback_query(F.data == "list_accounts")
async def cb_list_accounts(cb: CallbackQuery):
    """Список аккаунтов"""
    
    await safe_answer_callback(cb, "📋 Загрузка...")
    
    accounts = await db.get_all_accounts()
    active = sessions.list_active()
    
    if not accounts:
        text = "📋 Список аккаунтов пуст\n\nДобавьте номер через главное меню"
    else:
        text = f"📋 **Аккаунты ({len(accounts)}):**\n\n"
        
        for phone in accounts:
            status = "🟢" if phone in active else "⚪️"
            text += f"{status} `{phone}`\n"
        
        text += f"\n🟢 Активных: {len(active)}"
    
    kb = InlineKeyboardBuilder()
    kb.button(text="🔙 Назад", callback_data="menu")
    
    await safe_edit_message(cb.message, text, reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("screenshot:"))
async def cb_screenshot(cb: CallbackQuery):
    """Создание скриншота"""
    
    phone = cb.data.split(":", 1)[1]
    
    if not validate_phone(phone):
        await safe_answer_callback(cb, "❌ Некорректный номер", show_alert=True)
        return
    
    driver = sessions.get_session(phone)
    
    if not driver:
        await safe_answer_callback(cb, "❌ Сессия не активна", show_alert=True)
        return
    
    await safe_answer_callback(cb, "📸 Создаю скриншот...")
    
    try:
        screenshot_data = await driver.screenshot()
        
        await cb.message.answer_photo(
            BufferedInputFile(screenshot_data, f"screenshot_{phone}.png"),
            caption=f"📸 Скриншот: +{phone}"
        )
        
        await db.update_activity(phone)
        
    except asyncio.TimeoutError:
        await cb.message.answer("❌ Таймаут создания скриншота")
    except Exception as e:
        logger.error(f"Screenshot error: {e}", exc_info=True)
        await cb.message.answer(f"❌ Ошибка: {str(e)[:100]}")

@dp.callback_query(F.data.startswith("refresh:"))
async def cb_refresh_session(cb: CallbackQuery):
    """Обновление информации о сессии"""
    
    phone = cb.data.split(":", 1)[1]
    
    await safe_answer_callback(cb, "🔄")
    
    driver = sessions.get_session(phone)
    
    if not driver:
        await safe_edit_message(
            cb.message,
            f"❌ Сессия для +{phone} не активна"
        )
        return
    
    text = (
        f"✅ **Сессия активна**\n\n"
        f"📱 Номер: +{phone}\n"
        f"🆔 PID: {driver.pid}\n"
        f"📊 RAM: {psutil.virtual_memory().available // (1024**2)}MB"
    )
    
    await safe_edit_message(
        cb.message,
        text,
        reply_markup=get_session_keyboard(phone).as_markup()
    )

@dp.callback_query(F.data.startswith("close:"))
async def cb_close_session(cb: CallbackQuery):
    """Закрытие сессии"""
    
    phone = cb.data.split(":", 1)[1]
    
    await safe_answer_callback(cb, "🚪 Закрываю...")
    
    await sessions.close_session(phone)
    
    await safe_edit_message(
        cb.message,
        f"✅ Сессия +{phone} закрыта"
    )

@dp.callback_query(F.data.startswith("delete:"))
async def cb_delete_account(cb: CallbackQuery):
    """Удаление аккаунта"""
    
    phone = cb.data.split(":", 1)[1]
    
    await safe_answer_callback(cb, "🗑️")
    
    # Закрываем сессию если активна
    await sessions.close_session(phone)
    
    # Удаляем из БД
    await db.delete_account(phone)
    
    # Удаляем папку профиля
    profile_dir = os.path.join(cfg.SESSIONS_DIR, phone)
    if os.path.exists(profile_dir):
        try:
            shutil.rmtree(profile_dir)
        except Exception as e:
            logger.error(f"Profile delete error: {e}")
    
    await safe_edit_message(
        cb.message,
        f"✅ Аккаунт +{phone} удален"
    )

@dp.callback_query(F.data == "close_all")
async def cb_close_all(cb: CallbackQuery):
    """Закрытие всех сессий"""
    
    if cb.from_user.id not in cfg.ADMIN_IDS:
        await safe_answer_callback(cb, "❌ Недостаточно прав", show_alert=True)
        return
    
    await safe_answer_callback(cb, "🚨 Закрываю все сессии...")
    
    active_count = len(sessions.list_active())
    
    await sessions.close_all()
    
    text = (
        f"✅ **Все сессии закрыты**\n\n"
        f"Закрыто: {active_count}\n"
        f"RAM освобождено: ~{active_count * 300}MB"
    )
    
    await safe_edit_message(cb.message, text)

# ==========================================
# 🛑 GRACEFUL SHUTDOWN
# ==========================================

shutdown_event = asyncio.Event()

async def graceful_shutdown(sig_name: str = "Unknown"):
    """Корректное завершение работы"""
    
    logger.info(f"🛑 Shutdown initiated: {sig_name}")
    
    shutdown_event.set()
    
    # Закрытие всех сессий
    try:
        await sessions.close_all()
    except Exception as e:
        logger.error(f"Error closing sessions: {e}")
    
    # Очистка временных файлов
    try:
        if os.path.exists(cfg.TMP_DIR):
            shutil.rmtree(cfg.TMP_DIR, ignore_errors=True)
            os.makedirs(cfg.TMP_DIR, exist_ok=True)
    except Exception as e:
        logger.error(f"Temp cleanup error: {e}")
    
    # Отмена всех задач
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    
    logger.info(f"Cancelling {len(tasks)} tasks...")
    
    for task in tasks:
        task.cancel()
    
    await asyncio.gather(*tasks, return_exceptions=True)
    
    logger.info("✅ Shutdown complete")

def signal_handler(sig):
    """Обработчик сигналов"""
    asyncio.create_task(graceful_shutdown(signal.Signals(sig).name))

# ==========================================
# 🚀 MAIN
# ==========================================

async def main():
    """Главная функция"""
    
    logger.info("=" * 60)
    logger.info("🚀 WhatsApp Bot v39.0 Starting...")
    logger.info("=" * 60)
    
    # Проверка конфигурации
    logger.info(f"Bot Token: {cfg.BOT_TOKEN[:20]}...")
    logger.info(f"Admins: {cfg.ADMIN_IDS}")
    logger.info(f"Max browsers: {cfg.MAX_BROWSERS}")
    logger.info(f"Min RAM: {cfg.MIN_RAM_MB}MB")
    
    # Инициализация БД
    try:
        await db.init()
    except Exception as e:
        logger.critical(f"Database init failed: {e}")
        return
    
    # Регистрация обработчиков сигналов
    if sys.platform != 'win32':
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                sig,
                lambda s=sig: signal_handler(s)
            )
    
    # Очистка при старте
    try:
        if os.path.exists(cfg.TMP_DIR):
            shutil.rmtree(cfg.TMP_DIR, ignore_errors=True)
            os.makedirs(cfg.TMP_DIR, exist_ok=True)
    except Exception as e:
        logger.warning(f"Startup cleanup warning: {e}")
    
    # Запуск бота
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Bot started successfully")
        logger.info("=" * 60)
        
        await dp.start_polling(bot, handle_signals=False)
        
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
    finally:
        await graceful_shutdown("main_finally")
        
        # Закрытие бота
        try:
            await bot.session.close()
        except Exception as e:
            logger.error(f"Bot session close error: {e}")

# ==========================================
# 🎯 ENTRY POINT
# ==========================================

if __name__ == "__main__":
    # Настройка event loop для Windows
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.critical(f"Unhandled exception: {e}", exc_info=True)
        sys.exit(1)
