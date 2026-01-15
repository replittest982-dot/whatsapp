#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
🔱 WhatsApp Imperator v16.3 FINAL REVISION (Patch 2)
Production-Ready | Async | Secure | Multi-Instance | AI-Powered
"""

import asyncio
import logging
import os
import sys
import random
import json
import time
import shutil
import signal
import re
import csv
import base64
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any, Tuple, Union
from dataclasses import dataclass, field
from collections import defaultdict

# --- THIRD PARTY LIBS ---
try:
    import aiosqlite
    import psutil
    from cryptography.fernet import Fernet
    from faker import Faker
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    import google.generativeai as genai
    from webdriver_manager.chrome import ChromeDriverManager
    
    from aiogram import Bot, Dispatcher, Router, F
    from aiogram.filters import Command
    from aiogram.types import (
        Message, CallbackQuery, BufferedInputFile, 
        InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
    )
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.state import State, StatesGroup
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import (
        TimeoutException, NoSuchElementException, WebDriverException
    )
    
    # Optional: Matplotlib
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        plt = None

    # Optional: TTS
    try:
        from gtts import gTTS
        TTS_AVAILABLE = True
    except ImportError:
        TTS_AVAILABLE = False

except ImportError as e:
    sys.exit(f"❌ Critical Error: Missing libs. Install requirements.\nTrace: {e}")

# ==============================================================================
# ⚙️ CONFIGURATION
# ==============================================================================

@dataclass
class Config:
    # Env Vars
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    ADMIN_IDS: List[int] = field(default_factory=lambda: [int(x) for x in os.getenv("ADMIN_ID", "0").split(",") if x.isdigit()])
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    INSTANCE_ID: int = int(os.getenv("INSTANCE_ID", "1"))
    
    # Paths
    DB_NAME: str = 'imperator_final.db'
    SESSIONS_DIR: str = os.path.abspath("sessions")
    MEDIA_DIR: str = os.path.abspath("media")
    LOG_DIR: str = os.path.abspath("logs")
    KEY_FILE: str = "secret.key"

    # Limits & Settings
    BROWSER_LIMIT: int = 3 # MAX_CONCURRENT_FARMERS
    MIN_RAM_MB: int = 400
    PAGE_LOAD_TIMEOUT: int = 60
    MAX_MSGS_PER_HOUR: int = 40
    
    # Selectors (2025 Fallbacks)
    SELECTORS: Dict[str, List[str]] = field(default_factory=lambda: {
        "qr_canvas": ["canvas[aria-label='Scan this QR code']", "canvas"],
        "chat_list": ["div[aria-label='Chat list']", "#pane-side"],
        "search_box": ["div[contenteditable='true'][data-tab='3']", "div[title='Search input textbox']"],
        "message_box": ["div[contenteditable='true'][data-tab='10']", "footer div[contenteditable='true']", "div[role='textbox']"],
        "send_btn": ["span[data-icon='send']", "button[aria-label='Send']"],
        "attach_btn": ["div[title='Attach']", "span[data-icon='clip']"],
        "input_file": ["input[type='file']"], # Hidden input
        "ban_msg": ["div.landing-title", "div.landing-main"],
        "media_send": ["span[data-icon='send']"]
    })

cfg = Config()

# Validate Token
if not re.match(r'^\d+:[A-Za-z0-9_-]{35,}$', cfg.BOT_TOKEN):
    print("⚠️ WARNING: BOT_TOKEN format looks invalid!")

# Setup Dirs
for d in [cfg.SESSIONS_DIR, cfg.MEDIA_DIR, cfg.LOG_DIR]:
    os.makedirs(d, exist_ok=True)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
    handlers=[
        logging.FileHandler(f"{cfg.LOG_DIR}/bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(f"Node-{cfg.INSTANCE_ID}")

# ==============================================================================
# 🛡️ SECURITY & UTILS
# ==============================================================================

class CryptoManager:
    def __init__(self, key_file: str):
        self.key_file = key_file
        self.cipher = None
        self._load_key()

    def _load_key(self):
        if not os.path.exists(self.key_file):
            key = Fernet.generate_key()
            with open(self.key_file, "wb") as f:
                f.write(key)
        with open(self.key_file, "rb") as f:
            self.cipher = Fernet(f.read())

    def encrypt(self, text: str) -> str:
        if not text: return ""
        return self.cipher.encrypt(text.encode()).decode()

    def decrypt(self, token: str) -> str:
        if not token: return ""
        try:
            return self.cipher.decrypt(token.encode()).decode()
        except Exception:
            return token # Fallback

crypto = CryptoManager(cfg.KEY_FILE)

class RateLimiter:
    def __init__(self):
        self.limits = defaultdict(list)

    async def check(self, phone: str, max_per_hour: int) -> bool:
        now = time.time()
        # Cleanup
        self.limits[phone] = [t for t in self.limits[phone] if now - t < 3600]
        if len(self.limits[phone]) >= max_per_hour:
            return False
        self.limits[phone].append(now)
        return True

rate_limiter = RateLimiter()

# ==============================================================================
# 🧠 AI & VOICE SERVICES
# ==============================================================================

class AIMessageGenerator:
    def __init__(self):
        self.enabled = False
        if cfg.GEMINI_API_KEY:
            try:
                genai.configure(api_key=cfg.GEMINI_API_KEY)
                self.model = genai.GenerativeModel('gemini-pro')
                self.enabled = True
            except Exception as e:
                logger.error(f"AI Init Failed: {e}")

    async def generate(self, prompt: str) -> str:
        if not self.enabled:
            return random.choice(["Привет", "Как дела?", "На связи", "Добрый день"])
        
        try:
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None, 
                lambda: self.model.generate_content(prompt)
            )
            return response.text.strip()
        except Exception as e:
            logger.error(f"AI Generate Error: {e}")
            return "Привет!"

ai_gen = AIMessageGenerator()

class VoiceService:
    @staticmethod
    async def generate(text: str) -> Optional[str]:
        if not TTS_AVAILABLE: return None
        try:
            path = f"{cfg.MEDIA_DIR}/voice_{int(time.time())}.mp3"
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: gTTS(text=text, lang='ru').save(path))
            return path
        except Exception as e:
            logger.error(f"TTS Error: {e}")
            return None

# ==============================================================================
# 🗄️ DATABASE (ASYNC)
# ==============================================================================

class DatabaseManager:
    def __init__(self, db_path: str):
        self.path = db_path

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            
            # Accounts
            await db.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT UNIQUE, -- Encrypted
                    status TEXT DEFAULT 'init',
                    mode TEXT DEFAULT 'normal',
                    last_active REAL DEFAULT 0,
                    total_sent INTEGER DEFAULT 0,
                    meta_ua TEXT, -- Encrypted
                    instance_id INTEGER DEFAULT 1
                )
            """)
            
            # Queue
            await db.execute("""
                CREATE TABLE IF NOT EXISTS queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_phone TEXT, -- Encrypted
                    target_phone TEXT, -- Encrypted
                    message_text TEXT,
                    media_path TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at REAL
                )
            """)
            
            # Blacklist
            await db.execute("""
                CREATE TABLE IF NOT EXISTS blacklist (
                    phone TEXT PRIMARY KEY, -- Encrypted
                    reason TEXT,
                    added_at REAL
                )
            """)
            
            # Whitelist
            await db.execute("""
                CREATE TABLE IF NOT EXISTS whitelist (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    approved INTEGER DEFAULT 0
                )
            """)
            await db.commit()

    async def add_account(self, phone: str, ua: str):
        enc_phone = crypto.encrypt(phone)
        enc_ua = crypto.encrypt(ua)
        async with aiosqlite.connect(self.path) as db:
            try:
                await db.execute(
                    "INSERT INTO accounts (phone, meta_ua, instance_id, status) VALUES (?, ?, ?, 'init')",
                    (enc_phone, enc_ua, cfg.INSTANCE_ID)
                )
                await db.commit()
                return True
            except aiosqlite.IntegrityError:
                return False

    async def get_active_accounts(self) -> List[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(f"SELECT * FROM accounts WHERE status='active' AND instance_id={cfg.INSTANCE_ID}")
            return [dict(row) for row in await cursor.fetchall()]

    async def ban_phone(self, phone: str, reason: str = "manual"):
        enc_phone = crypto.encrypt(phone)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT OR REPLACE INTO blacklist (phone, reason, added_at) VALUES (?, ?, ?)",
                             (enc_phone, reason, time.time()))
            await db.commit()

db = DatabaseManager(cfg.DB_NAME)

# ==============================================================================
# 🤖 BROWSER MANAGER
# ==============================================================================

class BrowserCrashError(Exception): pass

class BrowserManager:
    def __init__(self, account: dict):
        self.phone_enc = account['phone']
        self.phone = crypto.decrypt(self.phone_enc)
        self.ua = crypto.decrypt(account['meta_ua'])
        self.driver: Optional[webdriver.Chrome] = None
        self.wait: Optional[WebDriverWait] = None

    async def start(self):
        """Initializes Chrome with Anti-Detect"""
        options = Options()
        options.add_argument(f"user-agent={self.ua}")
        options.add_argument(f"--user-data-dir={os.path.abspath(cfg.SESSIONS_DIR)}/{self.phone}")
        options.add_argument("--headless=new")
        options.add_argument("--disable-gpu")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-notifications")
        options.add_argument("--blink-settings=imagesEnabled=false")
        options.page_load_strategy = 'eager'

        try:
            loop = asyncio.get_running_loop()
            
            # Use ChromeDriverManager
            service = Service(ChromeDriverManager().install())
            
            self.driver = await loop.run_in_executor(None, lambda: webdriver.Chrome(service=service, options=options))
            self.wait = WebDriverWait(self.driver, 15)
            
            # CDP Patching
            self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            })
            
            await loop.run_in_executor(None, lambda: self.driver.get("https://web.whatsapp.com"))
            return True
        except Exception as e:
            logger.error(f"Startup Failed {self.phone}: {e}")
            await self.quit()
            raise BrowserCrashError(str(e))

    async def _find_with_retry(self, selectors: List[str], timeout: int = 10) -> Optional[Any]:
        """Retry logic with exponential backoff"""
        end_time = time.time() + timeout
        delay = 1
        
        while time.time() < end_time:
            for sel in selectors:
                try:
                    return self.driver.find_element(By.CSS_SELECTOR, sel)
                except NoSuchElementException:
                    continue
            await asyncio.sleep(delay)
            delay = min(delay * 2, 4)
            
        return None

    async def check_state(self) -> str:
        try:
            src = self.driver.page_source
            if "Account suspended" in src or "banned" in src:
                return "BANNED"
            
            if await self._find_with_retry(cfg.SELECTORS['qr_canvas'], timeout=5):
                return "QR"
            
            if await self._find_with_retry(cfg.SELECTORS['chat_list'], timeout=10):
                return "LOGGED_IN"
                
            return "LOADING"
        except Exception:
            return "ERROR"

    async def send_message(self, target: str, message: str, media_path: str = None) -> bool:
        if not await rate_limiter.check(self.phone, cfg.MAX_MSGS_PER_HOUR):
            logger.warning(f"Rate Limit {self.phone}")
            return False

        try:
            # Navigate
            url = f"https://web.whatsapp.com/send?phone={target}"
            await asyncio.to_thread(self.driver.get, url)
            
            # Wait for Box
            box = None
            try:
                # Custom wait loop
                for _ in range(3):
                    box = await self._find_with_retry(cfg.SELECTORS['message_box'], timeout=20)
                    if box: break
                    await asyncio.sleep(2)
            except Exception: pass
            
            if not box:
                logger.warning(f"{self.phone}: Chat not opened for {target}")
                return False

            # Media Upload (Direct Injection)
            if media_path and os.path.exists(media_path):
                try:
                    # Find hidden input
                    inp = self.driver.find_element(By.CSS_SELECTOR, "input[type='file']")
                    inp.send_keys(os.path.abspath(media_path))
                    await asyncio.sleep(2)
                    
                    # Click send on media preview
                    send_btn = await self._find_with_retry(cfg.SELECTORS['media_send'], timeout=5)
                    if send_btn:
                        send_btn.click()
                        await asyncio.sleep(2)
                except Exception as e:
                    logger.error(f"Media Upload Error: {e}")

            # Text Sending
            if message:
                for line in message.split('\n'):
                    await self._human_type(box, line)
                    box.send_keys(Keys.SHIFT + Keys.ENTER)
                
                await asyncio.sleep(0.5)
                box.send_keys(Keys.ENTER)

            await asyncio.sleep(2)
            return True

        except Exception as e:
            logger.error(f"Send Error: {e}")
            return False

    async def _human_type(self, element, text):
        for char in text:
            element.send_keys(char)
            await asyncio.sleep(random.uniform(0.03, 0.15))

    async def get_screenshot(self) -> bytes:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.driver.get_screenshot_as_png)

    async def quit(self):
        if self.driver:
            try:
                await asyncio.to_thread(self.driver.quit)
            except: pass
            self.driver = None

# ==============================================================================
# 🚜 FARM WORKER & LOGIC
# ==============================================================================

BROWSER_SEMAPHORE = asyncio.Semaphore(cfg.BROWSER_LIMIT)

async def farm_worker(account: dict):
    phone_enc = account['phone']
    
    # WRAPPER: Protect resources
    async with BROWSER_SEMAPHORE:
        browser = BrowserManager(account)
        try:
            await browser.start()
            state = await browser.check_state()
            logger.info(f"Worker {crypto.decrypt(phone_enc)} State: {state}")
            
            async with aiosqlite.connect(cfg.DB_NAME) as db_conn:
                if state == "BANNED":
                    await db_conn.execute("UPDATE accounts SET status='banned' WHERE phone=?", (phone_enc,))
                    await db_conn.commit() # FIXED: Added commit
                    return
                
                if state == "QR":
                    await db_conn.execute("UPDATE accounts SET status='qr' WHERE phone=?", (phone_enc,))
                    await db_conn.commit() # FIXED: Added commit
                    png = await browser.get_screenshot()
                    for admin in cfg.ADMIN_IDS:
                        try:
                            await bot.send_photo(admin, BufferedInputFile(png, "qr.png"), caption=f"QR: +{crypto.decrypt(phone_enc)}")
                        except: pass
                    return

                if state == "LOGGED_IN":
                    await db_conn.execute("UPDATE accounts SET status='active', last_active=? WHERE phone=?", 
                                     (time.time(), phone_enc))
                    await db_conn.commit() # FIXED: Added commit
                    
                    # Mode Logic
                    mode = account['mode']
                    if mode == 'pair':
                        # Get active accounts inside a fresh connection or reuse helper if safe
                        # Using direct query here for safety inside transaction
                        db_conn.row_factory = aiosqlite.Row
                        cur = await db_conn.execute(f"SELECT * FROM accounts WHERE status='active' AND instance_id={cfg.INSTANCE_ID}")
                        active_accs = [dict(r) for r in await cur.fetchall()]
                        
                        targets = [crypto.decrypt(a['phone']) for a in active_accs if a['phone'] != phone_enc]
                        
                        if targets:
                            target = random.choice(targets)
                            prompt = "Напиши короткое (3-5 слов) сообщение коллеге о работе на русском."
                            msg = await ai_gen.generate(prompt)
                            await browser.send_message(target, msg)
                        else:
                            await browser.send_message(crypto.decrypt(phone_enc), "Ping self")

            await asyncio.sleep(5)
            
        except BrowserCrashError:
            pass
        except Exception as e:
            logger.error(f"Worker Error: {e}")
        finally:
            await browser.quit()

async def msg_queue_processor():
    while True:
        try:
            async with aiosqlite.connect(cfg.DB_NAME) as dbase:
                dbase.row_factory = aiosqlite.Row
                # Fetch pending
                cursor = await dbase.execute("SELECT * FROM queue WHERE status='pending' LIMIT 5")
                rows = await cursor.fetchall()
                
                for row in rows:
                    sender_enc = row['account_phone']
                    target_enc = row['target_phone']
                    target = crypto.decrypt(target_enc)
                    
                    # Fetch sender config
                    acc_cur = await dbase.execute("SELECT * FROM accounts WHERE phone=?", (sender_enc,))
                    sender_acc = await acc_cur.fetchone()
                    
                    if not sender_acc:
                        await dbase.execute("UPDATE queue SET status='failed' WHERE id=?", (row['id'],))
                        await dbase.commit()
                        continue
                        
                    msg_text = row['message_text']
                    media_path = row['media_path']
                    
                    if msg_text and msg_text.startswith("voice:"):
                        clean = msg_text.replace("voice:", "").strip()
                        gen_path = await VoiceService.generate(clean)
                        if gen_path:
                            media_path = gen_path
                            msg_text = ""
                    
                    # FIXED: Protect Queue Processor with Semaphore
                    status = 'failed'
                    async with BROWSER_SEMAPHORE:
                        browser = BrowserManager(dict(sender_acc))
                        try:
                            await browser.start()
                            success = await browser.send_message(target, msg_text, media_path)
                            status = 'sent' if success else 'failed'
                        except:
                            status = 'failed'
                        finally:
                            await browser.quit()
                    
                    await dbase.execute("UPDATE queue SET status=? WHERE id=?", (status, row['id']))
                    await dbase.commit()
            
            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Queue Error: {e}")
            await asyncio.sleep(10)

# ==============================================================================
# 🎮 BOT HANDLERS
# ==============================================================================

bot = Bot(token=cfg.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

@router.message(Command("start"))
async def cmd_start(msg: Message):
    if msg.from_user.id not in cfg.ADMIN_IDS: return
    kb = InlineKeyboardBuilder()
    kb.button(text="📱 Accounts", callback_data="acc_list")
    kb.button(text="➕ Add", callback_data="acc_add")
    kb.adjust(2)
    await msg.answer("🔱 Imperator v16.3 Ready.", reply_markup=kb.as_markup())

@router.message(Command("accounts"))
async def cmd_accounts(msg: Message):
    async with aiosqlite.connect(cfg.DB_NAME) as db_conn:
        # FIXED: Set row_factory before execution
        db_conn.row_factory = aiosqlite.Row
        cursor = await db_conn.execute("SELECT * FROM accounts")
        rows = await cursor.fetchall()
    
    text = "📱 **Accounts List:**\n"
    for r in rows:
        ph = crypto.decrypt(r['phone'])
        st = r['status']
        text += f"`+{ph}` | {st}\n"
    await msg.answer(text, parse_mode="Markdown")

@router.message(Command("ban"))
async def cmd_ban(msg: Message):
    try:
        target = re.sub(r"\D", "", msg.text.split()[1])
        await db.ban_phone(target, "manual")
        await msg.answer(f"🚫 +{target} added to blacklist.")
    except:
        await msg.answer("Usage: /ban <phone>")

@router.message(Command("export"))
async def cmd_export(msg: Message):
    # Export logs/stats to CSV
    path = f"{cfg.LOG_DIR}/export_{int(time.time())}.csv"
    async with aiosqlite.connect(cfg.DB_NAME) as dbase:
        cursor = await dbase.execute("SELECT * FROM queue")
        rows = await cursor.fetchall()
        
        with open(path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['ID', 'Sender', 'Target', 'Status', 'Time'])
            for r in rows:
                writer.writerow([r[0], crypto.decrypt(r[1]), crypto.decrypt(r[2]), r[5], r[6]])
                
    await msg.answer_document(FSInputFile(path))

@router.message(Command("stats"))
async def cmd_stats(msg: Message):
    if not plt: return await msg.answer("Matplotlib not installed.")
    
    async with aiosqlite.connect(cfg.DB_NAME) as dbase:
        cursor = await dbase.execute("SELECT status, COUNT(*) FROM queue GROUP BY status")
        rows = await cursor.fetchall()
        
    labels = [r[0] for r in rows]
    counts = [r[1] for r in rows]
    
    def make_plot():
        plt.figure(figsize=(6, 4))
        plt.bar(labels, counts, color=['green', 'red', 'gray'])
        plt.title("Message Stats")
        buf = BytesIO()
        plt.savefig(buf, format='png')
        buf.seek(0)
        plt.close()
        return buf

    loop = asyncio.get_running_loop()
    buf = await loop.run_in_executor(None, make_plot)
    await msg.answer_photo(BufferedInputFile(buf.read(), "stats.png"))

# ==============================================================================
# 🛠 UTILS & TASKS
# ==============================================================================

async def cleanup_zombies():
    """Smart cleanup targeting only this bot's processes"""
    me = os.getpid()
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            if proc.pid == me: continue
            if 'chrome' in proc.info['name']:
                # Check if this chrome belongs to our sessions
                cmd = proc.info['cmdline'] or []
                for arg in cmd:
                    if f"--user-data-dir={os.path.abspath(cfg.SESSIONS_DIR)}" in arg:
                        proc.kill()
        except: pass

async def farm_loop():
    logger.info("🚜 Farm Loop Started")
    while True:
        try:
            # RAM Guard
            if psutil.virtual_memory().available / 1024 / 1024 < cfg.MIN_RAM_MB:
                logger.critical("⚠️ Memory Critical! Cleaning up...")
                await cleanup_zombies()
                await asyncio.sleep(60)
                continue

            async with aiosqlite.connect(cfg.DB_NAME) as dbase:
                dbase.row_factory = aiosqlite.Row
                cursor = await dbase.execute("SELECT * FROM accounts WHERE instance_id=?", (cfg.INSTANCE_ID,))
                accounts = [dict(r) for r in await cursor.fetchall()]

            random.shuffle(accounts)
            
            tasks = []
            
            # FIXED: Do not spam create_task if Semaphore is full
            # Only create tasks if we have room in the semaphore
            # Since semaphore is 3, we limit batch size
            
            active_tasks = [t for t in asyncio.all_tasks() if not t.done()]
            # Crude approximation of load, better to rely on semaphore acquisition inside worker
            
            for acc in accounts:
                if acc['status'] == 'banned': continue
                
                # Check Last Active
                if time.time() - acc['last_active'] > random.randint(60, 300):
                     # Limit pending tasks to avoid loop overload
                    if len(tasks) < cfg.BROWSER_LIMIT * 2: 
                        tasks.append(asyncio.create_task(farm_worker(acc)))
            
            if tasks:
                await asyncio.gather(*tasks)
            
            await asyncio.sleep(10)
        except Exception as e:
            logger.error(f"Farm Loop Error: {e}")
            await asyncio.sleep(10)

# ==============================================================================
# 🚀 MAIN ENTRY
# ==============================================================================

async def main():
    if not cfg.BOT_TOKEN:
        logger.critical("❌ BOT_TOKEN missing")
        return

    # DB Init
    await db.init()
    
    # Scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(cleanup_zombies, 'interval', minutes=30)
    scheduler.start()
    
    # Background Tasks
    asyncio.create_task(farm_loop())
    asyncio.create_task(msg_queue_processor())
    
    # Start Bot
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    except asyncio.CancelledError:
        pass

if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # Windows/Linux Signal Handling
    if sys.platform != 'win32':
        for s in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(s, lambda s=s: asyncio.create_task(cleanup_zombies()) or sys.exit(0))
            
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        pass
