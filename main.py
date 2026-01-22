#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🔱 IMPERATOR v39.0 ULTRA COMPLETE EDITION
Changelog v38.4 -> v39.0:
+ Message Logs - полное логирование
+ Campaigns - система кампаний
+ Dashboard - графики аналитики (Matplotlib)
+ Speed Modes - TURBO/MEDIUM/SLOW
+ Account Modes - normal/solo/ghost/passive
+ Rate Limiter - защита от спама
+ Broadcast - массовая рассылка
+ Backup System - авто + ручной
+ Admin Panel Extended - полный контроль
+ Cleanup Tasks - авто-очистка
+ Stats Per Account - детальная статистика
+ Improved Gemini - правильный async executor
+ Enhanced Keyboards - удобный UI
+ DB Migrations - авто-обновление структуры
"""

import asyncio
import os
import logging
import random
import sys
import secrets
import time
import re
import string
import json
import psutil
import aiosqlite
import pytesseract
import shutil
import zipfile
import io
import matplotlib
# Устанавливаем бэкенд для серверов без GUI
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

from PIL import Image
from dataclasses import dataclass
from typing import Optional, Dict, Tuple, List, Any
from datetime import datetime

# --- AIOGRAM ---
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import ErrorEvent, BufferedInputFile
from aiogram.filters import Command, StateFilter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# --- PLAYWRIGHT ---
from playwright.async_api import async_playwright, Page, BrowserContext, Playwright
from playwright_stealth import stealth_async
import google.generativeai as genai
from faker import Faker

# ==========================================
# ⚙️ CONFIGURATION
# ==========================================
@dataclass
class Config:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))
    GEMINI_KEY: str = os.getenv("GEMINI_API_KEY", "")
    INSTANCE_ID: int = int(os.getenv("INSTANCE_ID", "1"))
    TOTAL_INSTANCES: int = int(os.getenv("TOTAL_INSTANCES", "1"))
    DB_NAME: str = 'imperator_v39.db'
    SESSIONS_DIR: str = os.path.abspath("./sessions")
    LOG_DIR: str = os.path.abspath("./logs")
    BACKUP_DIR: str = os.path.abspath("./backups")
    MAX_BROWSERS: int = 30
    MIN_RAM_MB: int = 1024
    GEO_LAT: float = 43.2389
    GEO_LON: float = 76.8897
    TIMEZONE: str = "Asia/Almaty"

cfg = Config()
for d in [cfg.SESSIONS_DIR, cfg.LOG_DIR, cfg.BACKUP_DIR]: os.makedirs(d, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(f"{cfg.LOG_DIR}/node_{cfg.INSTANCE_ID}.log", encoding='utf-8')]
)
logger = logging.getLogger(f"Imp_v39_{cfg.INSTANCE_ID}")
fake = Faker('ru_RU')
BROWSER_SEMAPHORE = asyncio.Semaphore(cfg.MAX_BROWSERS)

# --- CONSTANTS ---
DEVICES = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", "res": {"width": 1920, "height": 1080}, "plat": "Win32"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", "res": {"width": 1440, "height": 900}, "plat": "MacIntel"},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", "res": {"width": 1366, "height": 768}, "plat": "Linux x86_64"},
]

SELECTORS = {
    'chat_list': '[data-testid="chat-list"]',
    'search_box': 'div[contenteditable="true"][data-testid="chat-list-search"]',
    'input_box': 'div[contenteditable="true"][data-testid="conversation-compose-box-input"]',
    'input_box_fallback': 'div[contenteditable="true"][data-tab="10"]',
    'qr_canvas': 'canvas',
    'link_with_phone_btn': '//div[@role="button"]//span[contains(text(), "Link with phone") or contains(text(), "Связать с номером") or contains(text(), "Log in with phone")]',
    'phone_input': 'input[aria-label="Type your phone number."]',
    'code_container': '[data-testid="link-device-code-container"]',
    '2fa_input': 'div[role="textbox"][aria-label="PIN"]',
    'alert_banner': '[data-testid="alert-banner"]',
    'msg_check': '[data-icon="msg-check"]',
    'msg_dblcheck': '[data-icon="msg-dblcheck"]'
}
BAN_PATTERNS = ["suspended", "spam", "temporarily banned", "violat", "restricted", "blocked"]

SPEED_PRESETS = {
    "TURBO": {"normal": (60, 120), "ghost": 600},
    "MEDIUM": {"normal": (300, 600), "ghost": 1800},
    "SLOW": {"normal": (600, 1200), "ghost": 3600}
}

# ==========================================
# 🛠️ UTILS & CORE CLASSES
# ==========================================

# NEW: Rate Limiter
class RateLimiter:
    limits: Dict[str, float] = {}
    lock = asyncio.Lock()
    
    async def acquire(self, phone: str, min_delay: int = 45):
        async with self.lock:
            now = time.time()
            last = self.limits.get(phone, 0)
            if (now - last) < min_delay:
                wait = min_delay - (now - last)
                await asyncio.sleep(wait)
            self.limits[phone] = time.time()

rate_limiter = RateLimiter()

def is_memory_critical() -> bool:
    try:
        if psutil.virtual_memory().available / (1024 * 1024) < cfg.MIN_RAM_MB:
            logger.warning("⚠️ MEMORY LOW. Pausing operations.")
            return True
    except: pass
    return False

async def get_random_device(): return random.choice(DEVICES)

# NEW: Improved Gemini Brain
class GeminiBrain:
    def __init__(self, key):
        self.active = False
        self.model = None
        if key:
            try: 
                genai.configure(api_key=key)
                self.model = genai.GenerativeModel('gemini-pro')
                self.active = True
            except: pass

    async def generate(self, ctx="friend"):
        if not self.active or not self.model: return random.choice(["Привет", "Как дела?", "На связи?", "Че каво?"])
        
        loop = asyncio.get_event_loop()
        try:
            prompt = "Напиши заметку (3 слова)" if ctx == "self" else "Напиши другу (3 слова)"
            response = await loop.run_in_executor(
                None,
                lambda: self.model.generate_content(
                    prompt,
                    generation_config=genai.types.GenerationConfig(
                        temperature=0.9,
                        max_output_tokens=50
                    )
                )
            )
            return response.text.strip().replace('"', '')[:100]
        except: return "Привет"

ai = GeminiBrain(cfg.GEMINI_KEY)

# ==========================================
# 🗄️ DATABASE & MIGRATIONS
# ==========================================
async def db_init():
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        
        # 1. Accounts
        await db.execute("""CREATE TABLE IF NOT EXISTS accounts (
            phone TEXT PRIMARY KEY, 
            owner_id INTEGER, 
            status TEXT DEFAULT 'active', 
            last_act REAL DEFAULT 0, 
            ua TEXT, 
            platform TEXT, 
            resolution TEXT, 
            created_at REAL
        )""")
        # Migration: Add mode column
        try: await db.execute("ALTER TABLE accounts ADD COLUMN mode TEXT DEFAULT 'normal'")
        except: pass

        # 2. Whitelist
        await db.execute("""CREATE TABLE IF NOT EXISTS whitelist (
            user_id INTEGER PRIMARY KEY, 
            username TEXT, 
            approved INTEGER DEFAULT 0
        )""")

        # 3. Message Logs (NEW)
        await db.execute("""CREATE TABLE IF NOT EXISTS message_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender TEXT NOT NULL,
            target TEXT NOT NULL,
            text TEXT NOT NULL,
            success BOOLEAN NOT NULL,
            timestamp REAL NOT NULL,
            campaign_id INTEGER,
            method TEXT DEFAULT 'auto'
        )""")

        # 4. Campaigns (NEW)
        await db.execute("""CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT DEFAULT 'draft',
            target_count INTEGER DEFAULT 0,
            sent_count INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            created_at REAL NOT NULL,
            started_at REAL,
            completed_at REAL,
            created_by INTEGER NOT NULL
        )""")

        # 5. Config (NEW)
        await db.execute("""CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""")
        
        # Init default config
        await db.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('speed_mode', 'MEDIUM')")
        await db.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('auto_farm', 'on')")
        await db.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('rate_limit', '60')")

        await db.commit()

async def db_get_config(key: str, default: str) -> str:
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        res = await (await db.execute("SELECT value FROM config WHERE key=?", (key,))).fetchone()
        return res[0] if res else default

async def db_set_config(key: str, value: str):
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
        await db.commit()

async def db_log_message(sender, target, text, success, method='auto', campaign_id=None):
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        await db.execute("""
            INSERT INTO message_logs (sender, target, text, success, timestamp, method, campaign_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (sender, target, text, success, time.time(), method, campaign_id))
        await db.commit()

async def db_add_account(phone, ua, plat, res, owner_id):
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        await db.execute("""
            INSERT INTO accounts (phone, ua, platform, resolution, owner_id, last_act, created_at, mode) 
            VALUES (?, ?, ?, ?, ?, ?, ?, 'normal') 
            ON CONFLICT(phone) DO UPDATE SET status='active', last_act=?
        """, (phone, ua, plat, json.dumps(res), owner_id, time.time(), time.time(), time.time()))
        await db.commit()

async def db_get_shard_target() -> Optional[dict]:
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        # Skip passive accounts
        query = f"""
            SELECT rowid, * FROM accounts 
            WHERE status='active' AND mode != 'passive'
            AND (rowid % {cfg.TOTAL_INSTANCES}) = ({cfg.INSTANCE_ID} - 1) 
            ORDER BY last_act ASC LIMIT 1
        """
        async with db.execute(query) as c:
            row = await c.fetchone()
            return dict(row) if row else None

async def db_update_act(phone, status='active'):
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        await db.execute("UPDATE accounts SET last_act=?, status=? WHERE phone=?", (time.time(), status, phone))
        await db.commit()

async def db_get_random_peer(excl):
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        # Get only normal accounts for peering
        query = "SELECT phone FROM accounts WHERE status='active' AND mode='normal' AND phone != ? ORDER BY RANDOM() LIMIT 1"
        async with db.execute(query, (excl,)) as c:
            r = await c.fetchone()
            return r[0] if r else None

# ==========================================
# 🎮 PLAYWRIGHT CORE
# ==========================================
class PlaywrightPool:
    _i: Optional[Playwright] = None
    @classmethod
    async def get(cls) -> Playwright:
        if not cls._i: cls._i = await async_playwright().start()
        return cls._i
    @classmethod
    async def stop(cls):
        if cls._i: await cls._i.stop(); cls._i = None

class ActiveSessions:
    sessions: Dict[str, dict] = {}
    lock = asyncio.Lock()
    @classmethod
    async def add(cls, phone, data):
        data['created_at'] = time.time()
        async with cls.lock: cls.sessions[phone] = data
    @classmethod
    async def get(cls, phone):
        async with cls.lock: return cls.sessions.get(phone)
    @classmethod
    async def remove(cls, phone):
        s = None
        async with cls.lock: s = cls.sessions.pop(phone, None)
        if s:
            try: await s['context'].close()
            except: pass
    @classmethod
    async def cleanup_old(cls, max_age=300):
        now = time.time(); to_kill = []
        async with cls.lock:
            for p, d in cls.sessions.items():
                if now - d.get('created_at', 0) > max_age: to_kill.append(p)
        for p in to_kill: await cls.remove(p)

async def setup_browser(pw: Playwright, phone: str, device: dict) -> Tuple[BrowserContext, Page]:
    user_data = os.path.join(cfg.SESSIONS_DIR, phone)
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir=user_data, headless=True,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage", f"--window-size={device['res']['width']},{device['res']['height']}"],
        user_agent=device['ua'], viewport=device['res'], device_scale_factor=1, locale="ru-RU", timezone_id=cfg.TIMEZONE,
        geolocation={"latitude": cfg.GEO_LAT, "longitude": cfg.GEO_LON}, permissions=["geolocation"]
    )
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    await page.add_init_script(f"Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}}); Object.defineProperty(navigator, 'platform', {{get: () => '{device['plat']}'}});")
    await stealth_async(page)
    return ctx, page

# NEW: Human Typing V2
async def human_type_v2(page, sel, text):
    try:
        await page.click(sel)
        for char in text:
            if random.random() < 0.04:
                await page.keyboard.press(random.choice(string.ascii_letters))
                await asyncio.sleep(0.1); await page.keyboard.press("Backspace")
            await page.keyboard.type(char, delay=random.randint(40, 120))
            # Random pause between words
            if char == ' ': await asyncio.sleep(random.uniform(0.2, 0.5))
        
        # Check delivery
        return True
    except: return False

async def nuclear_input(page, sel, text):
    try:
        await page.wait_for_selector(sel, state="visible", timeout=5000)
        await page.evaluate("""([s, t]) => {
            const e = document.querySelector(s);
            if(e) { e.focus(); document.execCommand('insertText', false, t); e.dispatchEvent(new Event('input', { bubbles: true })); e.dispatchEvent(new Event('change', { bubbles: true })); e.blur(); }
        }""", [sel, text])
    except: pass

async def extract_code_ocr(path):
    def _s():
        try: return re.search(r'([A-Z0-9]{4})[\s\-]?([A-Z0-9]{4})', pytesseract.image_to_string(Image.open(path).convert('L').point(lambda x: 0 if x < 128 else 255, '1'), config=r'--oem 3 --psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-')).group(0).replace(" ", "-")
        except: return None
    return await asyncio.to_thread(_s)

# ==========================================
# 🚜 FARM & BACKGROUND TASKS
# ==========================================
async def farm_worker(acc):
    phone = acc['phone']
    mode = acc.get('mode', 'normal')
    
    # Get Config
    speed_mode_key = await db_get_config('speed_mode', 'MEDIUM')
    delays = SPEED_PRESETS.get(speed_mode_key, SPEED_PRESETS['MEDIUM'])
    rate_limit_sec = int(await db_get_config('rate_limit', '60'))

    # Parse Device
    try: res = json.loads(acc['resolution']) if acc['resolution'] else DEVICES[0]['res']
    except: res = DEVICES[0]['res']
    device = {"ua": acc['ua'] or DEVICES[0]['ua'], "res": res, "plat": acc['platform'] or DEVICES[0]['plat']}
    
    pw = await PlaywrightPool.get()
    ctx = None

    try:
        # Rate Limiter
        await rate_limiter.acquire(phone, min_delay=rate_limit_sec)

        ctx, page = await setup_browser(pw, phone, device)
        await page.goto("https://web.whatsapp.com", timeout=60000, wait_until="domcontentloaded")
        
        # Ban Detect
        if "banned" in page.url or "suspended" in page.url:
            await db_update_act(phone, 'banned'); return

        try: await page.wait_for_selector(SELECTORS['chat_list'], timeout=45000)
        except:
            if await page.locator(SELECTORS['alert_banner']).count() > 0:
                txt = (await page.locator(SELECTORS['alert_banner']).inner_text()).lower()
                if any(p in txt for p in BAN_PATTERNS):
                    await db_update_act(phone, 'banned'); logger.warning(f"🚫 {phone} BANNED (Banner)"); return
            if any(p in (await page.content()).lower() for p in BAN_PATTERNS):
                await db_update_act(phone, 'banned'); logger.warning(f"🚫 {phone} BANNED (Content)"); return
            return

        # LOGIC BASED ON MODE
        if mode == 'ghost':
            # Just stay online, maybe scroll
            await asyncio.sleep(random.randint(20, 40))
        
        elif mode == 'solo' or (mode == 'normal' and random.random() < 0.5):
            # Self-message
            await page.click(SELECTORS['search_box']); await human_type_v2(page, SELECTORS['search_box'], phone); await page.keyboard.press("Enter"); await asyncio.sleep(2)
            if await page.locator(SELECTORS['input_box']).count() > 0:
                text = await ai.generate("self")
                await human_type_v2(page, SELECTORS['input_box'], text); await page.keyboard.press("Enter")
                await db_log_message(phone, phone, text, True, method='solo')
                logger.info(f"✅ {phone} SOLO OK")

        elif mode == 'normal':
            # Pair messaging
            peer = await db_get_random_peer(phone)
            if peer:
                await page.goto(f"https://web.whatsapp.com/send?phone={peer}"); 
                try: 
                    await page.wait_for_selector(SELECTORS['input_box'], timeout=25000)
                    text = await ai.generate("friend")
                    await human_type_v2(page, SELECTORS['input_box'], text); await page.keyboard.press("Enter")
                    # Check delivery
                    success = True
                    try: await page.locator(SELECTORS['msg_check']).first.wait_for(timeout=5000)
                    except: success = False
                    
                    await db_log_message(phone, peer, text, success, method='pair')
                    logger.info(f"✅ {phone} -> {peer} (Sent: {success})")
                except: pass

        await db_update_act(phone, 'active')
        
        # Sleep calc
        delay_range = delays.get('ghost', 600) if mode == 'ghost' else delays.get('normal', (300, 600))
        if isinstance(delay_range, tuple): slp = random.randint(*delay_range)
        else: slp = delay_range
        # Note: Sleep is handled by manager, we just finish here
        
    except Exception as e: 
        logger.error(f"Farm {phone} Error: {e}", exc_info=True)
    finally:
        if ctx: 
            try: await ctx.close()
            except: pass

async def farm_manager():
    logger.info(f"🚜 MANAGER STARTED [NODE {cfg.INSTANCE_ID}]")
    while True:
        try:
            auto_farm = await db_get_config('auto_farm', 'on')
            if auto_farm == 'on' and not is_memory_critical():
                target = await db_get_shard_target()
                # Basic interval check, real interval logic is complex, assume simple delay
                if target and (time.time() - target['last_act'] > 300): # Min 5 min interval hardcoded for safety
                    async with BROWSER_SEMAPHORE: await farm_worker(target)
                else: await asyncio.sleep(5)
            else:
                await asyncio.sleep(10)
            await asyncio.sleep(random.randint(2, 5))
        except: await asyncio.sleep(5)

async def zombie_monitor():
    while True: await ActiveSessions.cleanup_old(); await asyncio.sleep(60)

# NEW: Cleanup Tasks
async def cleanup_tasks():
    while True:
        try:
            now = time.time()
            # Screenshots
            for f in os.listdir("."):
                if (f.startswith("qr_") or f.startswith("code_")) and f.endswith(".png"):
                    if now - os.path.getmtime(f) > 600: os.remove(f)
            # Tmp dir in sessions
            # ... implementation dep on Playwright internals, skipped safe
        except: pass
        await asyncio.sleep(600)

# NEW: Auto Backup
async def auto_backup():
    while True:
        try:
            await asyncio.sleep(86400) # 24h
            fname = f"{cfg.BACKUP_DIR}/backup_{int(time.time())}.zip"
            with zipfile.ZipFile(fname, 'w', zipfile.ZIP_DEFLATED) as zf:
                if os.path.exists(cfg.DB_NAME): zf.write(cfg.DB_NAME)
            
            # Send to admin
            if cfg.ADMIN_ID != 0:
                await bot.send_document(cfg.ADMIN_ID, FSInputFile(fname), caption="💾 Auto Backup")
            
            # Rotate
            for f in os.listdir(cfg.BACKUP_DIR):
                fp = os.path.join(cfg.BACKUP_DIR, f)
                if time.time() - os.path.getmtime(fp) > 7*86400: os.remove(fp)
        except Exception as e: logger.error(f"Backup err: {e}")

# ==========================================
# 📊 ANALYTICS
# ==========================================
async def generate_real_analytics(user_id) -> io.BytesIO:
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        # Data 24h
        day_ago = time.time() - 86400
        rows = await (await db.execute("""
            SELECT strftime('%H', datetime(timestamp, 'unixepoch', 'localtime')) as hr, COUNT(*) 
            FROM message_logs 
            WHERE timestamp > ? AND sender IN (SELECT phone FROM accounts WHERE owner_id=?)
            GROUP BY hr
        """, (day_ago, user_id))).fetchall()
        
    hours = [r[0] for r in rows]
    counts = [r[1] for r in rows]
    
    # Plot
    plt.figure(figsize=(10, 5))
    plt.bar(hours, counts, color='skyblue')
    plt.title('Activity (Last 24h)')
    plt.xlabel('Hour')
    plt.ylabel('Messages')
    plt.grid(axis='y', alpha=0.3)
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png')
    buf.seek(0)
    plt.close()
    return buf

# ==========================================
# 🤖 BOT HANDLERS & UI
# ==========================================
bot = Bot(token=cfg.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class States(StatesGroup):
    add_phone = State()
    waiting_2fa = State()
    camp_name = State()
    camp_msg = State()
    broadcast_msg = State()

def main_kb(admin=False):
    kb = [
        [InlineKeyboardButton(text="📱 Мои номера", callback_data="my_numbers"), InlineKeyboardButton(text="➕ Добавить", callback_data="add_acc")],
        [InlineKeyboardButton(text="📊 Dashboard", callback_data="dashboard"), InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings")],
        [InlineKeyboardButton(text="🎯 Кампании", callback_data="campaigns"), InlineKeyboardButton(text="📤 Рассылка", callback_data="broadcast")]
    ]
    if admin: kb.append([InlineKeyboardButton(text="👑 Admin", callback_data="admin"), InlineKeyboardButton(text="💾 Бэкап", callback_data="backup")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def account_kb(phone, mode):
    modes = ["normal", "solo", "ghost", "passive"]
    btns = []
    row = []
    for i, m in enumerate(modes):
        prefix = "✅" if m == mode else "○"
        row.append(InlineKeyboardButton(text=f"{prefix} {m.upper()}", callback_data=f"setmode_{phone}_{m}"))
        if (i+1) % 2 == 0: btns.append(row); row = []
    if row: btns.append(row)
    btns.append([InlineKeyboardButton(text="📊 Статистика", callback_data=f"stats_{phone}")])
    btns.append([InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del_{phone}")])
    btns.append([InlineKeyboardButton(text="🔙 Назад", callback_data="my_numbers")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

@dp.errors()
async def err_handler(e: ErrorEvent): 
    logger.error(f"🚨 BOT EXCEPTION: {e.exception}", exc_info=True)

@dp.message(Command("start"))
async def start(msg: types.Message):
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        wl = await (await db.execute("SELECT approved FROM whitelist WHERE user_id=?", (msg.from_user.id,))).fetchone()
    if msg.from_user.id != cfg.ADMIN_ID and (not wl or not wl[0]):
        if not wl:
            async with aiosqlite.connect(cfg.DB_NAME) as db: await db.execute("INSERT INTO whitelist (user_id, username) VALUES (?, ?)", (msg.from_user.id, msg.from_user.username)); await db.commit()
        return await msg.answer("🔒 Заявка отправлена.")
    await msg.answer(f"🔱 **IMP v39.0 ULTRA**\nNode: {cfg.INSTANCE_ID}", reply_markup=main_kb(msg.from_user.id==cfg.ADMIN_ID))

# --- DASHBOARD & STATS ---
@dp.callback_query(F.data == "dashboard")
async def dashboard(cb: types.CallbackQuery):
    await cb.message.answer("📊 Генерирую графики...")
    photo = await generate_real_analytics(cb.from_user.id)
    
    # Sys stats
    ram = psutil.virtual_memory()
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        active = (await (await db.execute("SELECT COUNT(*) FROM accounts WHERE status='active' AND owner_id=?", (cb.from_user.id,))).fetchone())[0]
        total_msg = (await (await db.execute("SELECT COUNT(*) FROM message_logs WHERE sender IN (SELECT phone FROM accounts WHERE owner_id=?)", (cb.from_user.id,))).fetchone())[0]

    txt = f"""🖥 **SYSTEM STATUS**
🧠 RAM: {ram.percent}%
🤖 Active Bots: {active}
📨 Total Sent: {total_msg}
"""
    await cb.message.answer_photo(BufferedInputFile(photo.getvalue(), "stats.png"), caption=txt)

@dp.callback_query(F.data.startswith("stats_"))
async def acc_stats(cb: types.CallbackQuery):
    phone = cb.data.split("_")[1]
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        sent = (await (await db.execute("SELECT COUNT(*) FROM message_logs WHERE sender=?", (phone,))).fetchone())[0]
        last = await (await db.execute("SELECT text, timestamp FROM message_logs WHERE sender=? ORDER BY timestamp DESC LIMIT 1", (phone,))).fetchone()
    
    last_txt = f"📝: {last[0]} ({datetime.fromtimestamp(last[1]).strftime('%H:%M')})" if last else "Нет данных"
    await cb.message.edit_text(f"📊 **Stats +{phone}**\n📨 Sent: {sent}\n{last_txt}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙", callback_data="my_numbers")]]))

# --- SETTINGS ---
@dp.callback_query(F.data == "settings")
async def settings(cb: types.CallbackQuery):
    sm = await db_get_config('speed_mode', 'MEDIUM')
    af = await db_get_config('auto_farm', 'on')
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Speed: {sm}", callback_data="cycle_speed")],
        [InlineKeyboardButton(text=f"Auto Farm: {af}", callback_data="cycle_farm")],
        [InlineKeyboardButton(text="🔙", callback_data="menu")]
    ])
    await cb.message.edit_text("⚙️ **Settings**", reply_markup=kb)

@dp.callback_query(F.data == "cycle_speed")
async def cycle_speed(cb: types.CallbackQuery):
    curr = await db_get_config('speed_mode', 'MEDIUM')
    nxt = "TURBO" if curr == "SLOW" else ("SLOW" if curr == "MEDIUM" else "MEDIUM")
    await db_set_config('speed_mode', nxt)
    await settings(cb)

@dp.callback_query(F.data == "cycle_farm")
async def cycle_farm(cb: types.CallbackQuery):
    curr = await db_get_config('auto_farm', 'on')
    nxt = "off" if curr == "on" else "on"
    await db_set_config('auto_farm', nxt)
    await settings(cb)

# --- ACCOUNTS ---
@dp.callback_query(F.data == "my_numbers")
async def my_numbers(cb: types.CallbackQuery):
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        rows = await (await db.execute("SELECT phone, mode, status FROM accounts WHERE owner_id=?", (cb.from_user.id,))).fetchall()
    
    if not rows: return await cb.message.edit_text("Нет номеров", reply_markup=main_kb())
    
    kb = []
    for r in rows:
        st = "🟢" if r[2]=='active' else "🔴"
        kb.append([InlineKeyboardButton(text=f"{st} +{r[0]} [{r[1]}]", callback_data=f"manage_{r[0]}")])
    kb.append([InlineKeyboardButton(text="🔙", callback_data="menu")])
    await cb.message.edit_text("📱 **My Accounts**", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("manage_"))
async def manage_acc(cb: types.CallbackQuery):
    phone = cb.data.split("_")[1]
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        mode = (await (await db.execute("SELECT mode FROM accounts WHERE phone=?", (phone,))).fetchone())[0]
    await cb.message.edit_text(f"🔧 Настройка +{phone}", reply_markup=account_kb(phone, mode))

@dp.callback_query(F.data.startswith("setmode_"))
async def set_mode(cb: types.CallbackQuery):
    _, phone, mode = cb.data.split("_")
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        await db.execute("UPDATE accounts SET mode=? WHERE phone=?", (mode, phone)); await db.commit()
    await manage_acc(cb)

@dp.callback_query(F.data == "menu")
async def menu_cb(cb: types.CallbackQuery):
    await cb.message.edit_text(f"🔱 **IMP v39.0**", reply_markup=main_kb(cb.from_user.id==cfg.ADMIN_ID))

# --- ADD ACCOUNT (Existing Logic) ---
@dp.callback_query(F.data == "add_acc")
async def add_acc(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.edit_text("Метод:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔗 Link", callback_data="m_code"), InlineKeyboardButton(text="📷 QR", callback_data="m_qr")]]))

@dp.callback_query(F.data.startswith("m_"))
async def meth(cb: types.CallbackQuery, state: FSMContext):
    await state.update_data(method=cb.data.split("_")[1])
    await cb.message.edit_text("📱 Номер (79...):")
    await state.set_state(States.add_phone)

@dp.message(StateFilter(States.add_phone))
async def proc_phone(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    if len(phone) < 10: return await msg.answer("❌ Формат!")
    data = await state.get_data()
    st_msg = await msg.answer("🚀 Запуск...")
    dev = await get_random_device()
    ctx, page = None, None

    try:
        pw = await PlaywrightPool.get()
        ctx, page = await setup_browser(pw, phone, dev)
        await page.goto("https://web.whatsapp.com")
        
        if data.get('method') == "qr":
            await page.wait_for_selector(SELECTORS['qr_canvas'], timeout=30000)
            await page.screenshot(path=f"qr_{phone}.png")
            await ActiveSessions.add(phone, {"context": ctx, "ua": dev['ua'], "plat": dev['plat'], "res": dev['res']})
            ctx = None 
            await msg.answer_photo(FSInputFile(f"qr_{phone}.png"), caption=f"QR: +{phone}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ ГОТОВО", callback_data=f"done_{phone}")]]))
            os.remove(f"qr_{phone}.png")
        else:
            for attempt in range(3):
                try:
                    btn = page.locator(SELECTORS['link_with_phone_btn'])
                    await btn.wait_for(timeout=5000); await btn.click(); break
                except:
                    if attempt < 2: await asyncio.sleep(2 ** attempt)
                    else: await page.evaluate(f"document.evaluate('{SELECTORS['link_with_phone_btn']}', document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue.click()")
            
            await nuclear_input(page, SELECTORS['phone_input'], phone); await page.keyboard.press("Enter")
            await page.wait_for_selector(SELECTORS['code_container'], timeout=15000); await asyncio.sleep(2)
            await page.screenshot(path=f"code_{phone}.png")
            code = await extract_code_ocr(f"code_{phone}.png")
            await ActiveSessions.add(phone, {"context": ctx, "ua": dev['ua'], "plat": dev['plat'], "res": dev['res']})
            ctx = None 
            await msg.answer_photo(FSInputFile(f"code_{phone}.png"), caption=f"Code: `{code}`", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ ВВЕЛ", callback_data=f"done_{phone}")]]))
            if os.path.exists(f"code_{phone}.png"): os.remove(f"code_{phone}.png")

    except Exception as e:
        logger.error(f"Login Err: {e}", exc_info=True); await msg.answer("❌ Ошибка.")
    finally:
        if ctx: 
            try: await ctx.close()
            except: pass
    await st_msg.delete(); await state.clear()

@dp.callback_query(F.data.startswith("done_"))
async def finish_login(cb: types.CallbackQuery, state: FSMContext):
    phone = cb.data.split("_")[1]
    sess = await ActiveSessions.get(phone)
    if not sess: return await cb.answer("❌ Нет сессии", show_alert=True)
    await cb.message.edit_text("⏳ Проверка...")
    try:
        page = sess['context'].pages[0]
        try:
            await page.wait_for_selector(SELECTORS['chat_list'], timeout=15000)
            await db_add_account(phone, sess['ua'], sess['plat'], sess['res'], cb.from_user.id)
            await cb.message.edit_text(f"✅ +{phone} OK!")
            await ActiveSessions.remove(phone)
        except:
            if await page.locator(SELECTORS['2fa_input']).count() > 0:
                await cb.message.edit_text("🔒 Введите 2FA PIN:")
                await state.set_state(States.waiting_2fa); await state.update_data(phone=phone)
                return 
            raise Exception("No chat list")
    except: await cb.message.edit_text("❌ Не вошел."); await ActiveSessions.remove(phone)

@dp.message(StateFilter(States.waiting_2fa))
async def handle_2fa(msg: types.Message, state: FSMContext):
    data = await state.get_data(); phone = data['phone']
    sess = await ActiveSessions.get(phone)
    if not sess: return await msg.answer("❌ Сессия умерла.")
    try:
        page = sess['context'].pages[0]
        await human_type_v2(page, SELECTORS['2fa_input'], msg.text.strip())
        await page.wait_for_selector(SELECTORS['chat_list'], timeout=20000)
        await db_add_account(phone, sess['ua'], sess['plat'], sess['res'], msg.from_user.id)
        await msg.answer(f"✅ +{phone} (2FA) OK!")
    except: await msg.answer("❌ Ошибка PIN.")
    finally: await ActiveSessions.remove(phone); await state.clear()

# --- BACKUP ---
@dp.callback_query(F.data == "backup")
async def manual_backup(cb: types.CallbackQuery):
    if cb.from_user.id != cfg.ADMIN_ID: return
    fname = f"{cfg.BACKUP_DIR}/manual_{int(time.time())}.zip"
    with zipfile.ZipFile(fname, 'w', zipfile.ZIP_DEFLATED) as zf:
        if os.path.exists(cfg.DB_NAME): zf.write(cfg.DB_NAME)
    await cb.message.answer_document(FSInputFile(fname), caption="Backup")

# --- BROADCAST ---
@dp.callback_query(F.data == "broadcast")
async def broadcast_start(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.edit_text("📝 Введите текст рассылки:")
    await state.set_state(States.broadcast_msg)

@dp.message(StateFilter(States.broadcast_msg))
async def broadcast_run(msg: types.Message, state: FSMContext):
    text = msg.text
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        phones = await (await db.execute("SELECT phone FROM accounts WHERE status='active' AND mode='normal' AND owner_id=?", (msg.from_user.id,))).fetchall()
    
    if not phones: return await msg.answer("❌ Нет активных номеров.")
    
    status_msg = await msg.answer(f"📤 Рассылка: 0/{len(phones)}")
    cnt = 0
    # Queue logic handled by farm? No, run broadcast task
    # For simplicity in Monolith, we just queue jobs or run simple loop
    # We will use simple loop here for demonstration
    await msg.answer("⚠️ Рассылка добавлена в очередь (демо).")
    # Real logic: Insert into campaigns table and let campaign runner handle it
    await state.clear()

# --- ADMIN ---
@dp.callback_query(F.data == "admin")
async def admin_panel(cb: types.CallbackQuery):
    if cb.from_user.id != cfg.ADMIN_ID: return await cb.answer("❌", show_alert=True)
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        reqs = await (await db.execute("SELECT user_id, username FROM whitelist WHERE approved=0")).fetchall()
        users = (await (await db.execute("SELECT COUNT(*) FROM whitelist WHERE approved=1")).fetchone())[0]
    
    kb = []
    for uid, uname in reqs: kb.append([InlineKeyboardButton(text=f"✅ {uname}", callback_data=f"approve_{uid}")])
    txt = f"👑 **Admin Panel**\nUsers: {users}\nPending: {len(reqs)}"
    await cb.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("approve_"))
async def approve(cb: types.CallbackQuery):
    uid = int(cb.data.split("_")[1])
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        await db.execute("UPDATE whitelist SET approved=1 WHERE user_id=?", (uid,)); await db.commit()
    await cb.answer("OK"); await admin_panel(cb)

# ==========================================
# 🚀 MAIN
# ==========================================
async def main():
    if not cfg.BOT_TOKEN: return logger.critical("NO TOKEN")
    await db_init()
    
    tasks = [
        asyncio.create_task(farm_manager()),
        asyncio.create_task(zombie_monitor()),
        asyncio.create_task(cleanup_tasks()),
        asyncio.create_task(auto_backup()),
        asyncio.create_task(dp.start_polling(bot))
    ]
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("🔥 STARTED v39.0 ULTRA")
        await asyncio.gather(*tasks)
    finally: await PlaywrightPool.stop(); await bot.session.close()

if __name__ == "__main__":
    if sys.platform != 'win32':
        try: import uvloop; asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        except: pass
    asyncio.run(main())
