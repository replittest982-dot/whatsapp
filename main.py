#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
🔱 IMPERATOR v38.0 TITANIUM ULTIMATE (PRODUCTION RELEASE)
Архитектура: Monolith / Asyncio / Playwright / Aiogram 3.16+
Особенности:
- Multi-Instance Sharding (rowid % instances)
- Memory Guard (<200MB stop)
- Device Spoofing (Win/Mac/Linux + JS Injection)
- React Reactivity Bypass (Nuclear Input)
- OCR & Fallback Logic
"""

import asyncio
import os
import logging
import random
import sys
import secrets
import time
import shutil
import re
import string
import psutil
import io
import csv
from datetime import datetime
from typing import Optional, Dict, Tuple, List, Any
from dataclasses import dataclass

# --- СТОРОННИЕ БИБЛИОТЕКИ ---
# pip install aiogram playwright aiosqlite psutil faker pytesseract google-generativeai pillow
import aiosqlite
import pytesseract
from PIL import Image
from faker import Faker

# AIOGRAM 3.16+
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# PLAYWRIGHT
from playwright.async_api import async_playwright, Page, BrowserContext, Playwright, TimeoutError as PWTimeoutError

# GOOGLE GEMINI
import google.generativeai as genai

# ==========================================
# ⚙️ CONFIG & CONSTANTS
# ==========================================

@dataclass
class Config:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))
    GEMINI_KEY: str = os.getenv("GEMINI_API_KEY", "")
    INSTANCE_ID: int = int(os.getenv("INSTANCE_ID", "1"))
    TOTAL_INSTANCES: int = int(os.getenv("TOTAL_INSTANCES", "1"))
    
    DB_NAME: str = 'imperator_v38.db'
    SESSIONS_DIR: str = os.path.abspath("./sessions")
    LOG_DIR: str = os.path.abspath("./logs")
    
    MAX_BROWSERS: int = 4
    MIN_RAM_MB: int = 200  # Memory Guard Limit
    
    # Гео для Алматы
    GEO_LAT: float = 43.2389
    GEO_LON: float = 76.8897
    TIMEZONE: str = "Asia/Almaty"

cfg = Config()

# Создаем папки
for d in [cfg.SESSIONS_DIR, cfg.LOG_DIR]:
    os.makedirs(d, exist_ok=True)

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | [%(module)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"{cfg.LOG_DIR}/inst_{cfg.INSTANCE_ID}.log", encoding='utf-8')
    ]
)
logger = logging.getLogger(f"Imp_v38_{cfg.INSTANCE_ID}")

# Генераторы данных
fake = Faker('ru_RU')
BROWSER_SEMAPHORE = asyncio.Semaphore(cfg.MAX_BROWSERS)

# --- DEVICE SPOOFING CONFIGS ---
DEVICES = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", "res": {"width": 1920, "height": 1080}, "plat": "Win32"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", "res": {"width": 1440, "height": 900}, "plat": "MacIntel"},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", "res": {"width": 1366, "height": 768}, "plat": "Linux x86_64"},
]

# --- SELECTORS 2026 (DATA-TESTID + FALLBACKS) ---
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
    'menu_btn': '[data-testid="menu-bar-menu"]',
    'logout_btn': '[data-testid="menu-bar-logout"]'
}

BAN_PATTERNS = ["suspended", "spam", "temporarily banned", "violat", "restricted", "blocked"]

# ==========================================
# 🛠️ UTILITIES & MEMORY GUARD
# ==========================================

def is_memory_critical() -> bool:
    """True, если RAM < 200MB"""
    mem = psutil.virtual_memory()
    free_mb = mem.available / (1024 * 1024)
    if free_mb < cfg.MIN_RAM_MB:
        logger.warning(f"⚠️ MEMORY CRITICAL! Free: {free_mb:.2f}MB. Pause operations.")
        return True
    return False

async def get_random_device():
    return random.choice(DEVICES)

# ==========================================
# 🧠 GEMINI BRAIN
# ==========================================
class GeminiBrain:
    def __init__(self, api_key: str):
        self.active = False
        self.semaphore = asyncio.Semaphore(5)
        if api_key:
            try:
                genai.configure(api_key=api_key)
                self.model = genai.GenerativeModel('gemini-pro')
                self.active = True
            except: pass

    async def generate(self, context: str = "friend") -> str:
        if not self.active: 
            return random.choice(["Привет", "Как дела?", "Ку", "Че делаешь?", "На связи?"])
        async with self.semaphore:
            try:
                prompt = "Напиши короткую заметку для себя (1 предложение)" if context == "self" else "Напиши короткое сообщение другу, неформально (1 предложение)"
                resp = await asyncio.to_thread(self.model.generate_content, prompt)
                return resp.text.strip().replace('"', '')
            except: 
                return "Привет"

ai = GeminiBrain(cfg.GEMINI_KEY)

# ==========================================
# 🗄️ ASYNC DATABASE (AIOSQLITE)
# ==========================================
async def db_init():
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        
        # Accounts Table
        await db.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                phone TEXT PRIMARY KEY, 
                owner_id INTEGER, 
                status TEXT DEFAULT 'active', 
                last_act REAL DEFAULT 0, 
                ua TEXT, 
                platform TEXT,
                resolution TEXT,
                created_at REAL
            )
        """)
        
        # Subscriptions
        await db.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER PRIMARY KEY, 
                status TEXT DEFAULT 'inactive', 
                expires_at REAL DEFAULT 0, 
                max_slots INTEGER DEFAULT 1
            )
        """)
        
        # Whitelist (Admin Approval)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS whitelist (
                user_id INTEGER PRIMARY KEY, 
                username TEXT, 
                approved INTEGER DEFAULT 0
            )
        """)
        
        # Promo Codes
        await db.execute("""
            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY, 
                days INTEGER, 
                activations_left INTEGER
            )
        """)
        await db.commit()

async def db_add_account(phone: str, ua: str, plat: str, res: str, owner_id: int):
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        await db.execute("""
            INSERT INTO accounts (phone, ua, platform, resolution, owner_id, last_act, created_at) 
            VALUES (?, ?, ?, ?, ?, ?, ?) 
            ON CONFLICT(phone) DO UPDATE SET status='active', last_act=?
        """, (phone, ua, plat, res, owner_id, time.time(), time.time(), time.time()))
        await db.commit()

async def db_get_shard_target() -> Optional[dict]:
    """Шардинг: берет аккаунт, если rowid % total == instance - 1"""
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        # Логика: берем активный, который давно не фармился и подходит под шард
        query = f"""
            SELECT rowid, * FROM accounts 
            WHERE status='active' 
            AND (rowid % {cfg.TOTAL_INSTANCES}) = ({cfg.INSTANCE_ID} - 1)
            ORDER BY last_act ASC LIMIT 1
        """
        async with db.execute(query) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

async def db_update_act(phone: str, status: str = 'active'):
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        await db.execute("UPDATE accounts SET last_act=?, status=? WHERE phone=?", (time.time(), status, phone))
        await db.commit()

async def db_get_random_peer(exclude_phone: str) -> Optional[str]:
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        async with db.execute("SELECT phone FROM accounts WHERE status='active' AND phone != ? ORDER BY RANDOM() LIMIT 1", (exclude_phone,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

# ==========================================
# 🎮 PLAYWRIGHT POOL & LOGIC
# ==========================================

class PlaywrightPool:
    _instance: Optional[Playwright] = None

    @classmethod
    async def get(cls) -> Playwright:
        if not cls._instance:
            cls._instance = await async_playwright().start()
        return cls._instance

    @classmethod
    async def stop(cls):
        if cls._instance:
            await cls._instance.stop()
            cls._instance = None

class ActiveSessions:
    """Хранилище активных сессий для Link/QR процесса"""
    sessions: Dict[str, dict] = {}
    lock = asyncio.Lock()

    @classmethod
    async def add(cls, phone, data):
        async with cls.lock: cls.sessions[phone] = data
    
    @classmethod
    async def get(cls, phone):
        async with cls.lock: return cls.sessions.get(phone)
    
    @classmethod
    async def remove(cls, phone):
        async with cls.lock:
            if phone in cls.sessions:
                s = cls.sessions.pop(phone)
                try: await s['context'].close()
                except: pass

async def setup_browser(playwright: Playwright, phone: str, device: dict) -> Tuple[BrowserContext, Page]:
    user_data = os.path.join(cfg.SESSIONS_DIR, phone)
    
    # Запуск контекста
    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=user_data,
        headless=True, # Headless NEW
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            f"--window-size={device['res']['width']},{device['res']['height']}"
        ],
        user_agent=device['ua'],
        viewport=device['res'],
        device_scale_factor=1,
        locale="ru-RU",
        timezone_id=cfg.TIMEZONE,
        geolocation={"latitude": cfg.GEO_LAT, "longitude": cfg.GEO_LON},
        permissions=["geolocation"]
    )
    
    page = context.pages[0] if context.pages else await context.new_page()
    
    # JS INJECTION (Stealth + Navigator)
    await page.add_init_script(f"""
        Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}});
        Object.defineProperty(navigator, 'platform', {{get: () => '{device['plat']}'}});
        window.chrome = {{ runtime: {{}} }};
    """)
    
    return context, page

async def human_type(page: Page, selector: str, text: str):
    """Эмуляция человека с опечатками (4%)"""
    try:
        loc = page.locator(selector)
        await loc.click()
        
        for char in text:
            # Логика опечатки
            if random.random() < 0.04:
                wrong_char = random.choice(string.ascii_letters)
                await page.keyboard.press(wrong_char)
                await asyncio.sleep(random.uniform(0.1, 0.3))
                await page.keyboard.press("Backspace")
                await asyncio.sleep(random.uniform(0.1, 0.2))
            
            await page.keyboard.type(char, delay=random.randint(50, 150))
    except Exception as e:
        logger.error(f"Typing error: {e}")

async def nuclear_input(page: Page, selector: str, text: str):
    """Ядерный метод для обхода React Input"""
    await page.evaluate("""([sel, txt]) => {
        const el = document.querySelector(sel);
        if(el) {
            el.focus();
            document.execCommand('insertText', false, txt);
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }
    }""", [selector, text])

async def extract_code_ocr(path: str) -> Optional[str]:
    """OCR для чтения кода линковки"""
    def _sync_ocr():
        try:
            img = Image.open(path).convert('L').point(lambda x: 0 if x < 128 else 255, '1')
            cfg_tess = r'--oem 3 --psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-'
            txt = pytesseract.image_to_string(img, config=cfg_tess)
            match = re.search(r'([A-Z0-9]{4})[\s\-]?([A-Z0-9]{4})', txt)
            return f"{match.group(1)}-{match.group(2)}" if match else None
        except: return None
    return await asyncio.to_thread(_sync_ocr)

# ==========================================
# 🚜 FARM LOOP (SHARDING + MODES)
# ==========================================

async def farm_worker(account: dict):
    phone = account['phone']
    ua = account['ua'] or DEVICES[0]['ua']
    res = eval(account['resolution']) if account['resolution'] else DEVICES[0]['res']
    plat = account['platform'] or DEVICES[0]['plat']
    device = {"ua": ua, "res": res, "plat": plat}

    pw = await PlaywrightPool.get()
    context = None
    
    try:
        context, page = await setup_browser(pw, phone, device)
        
        # Загрузка
        try:
            await page.goto("https://web.whatsapp.com", timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_selector(SELECTORS['chat_list'], timeout=45000)
        except:
            # Проверка на бан
            content = await page.content()
            if any(p in content.lower() for p in BAN_PATTERNS):
                await db_update_act(phone, 'banned')
                logger.warning(f"🚫 {phone} BANNED")
            else:
                logger.warning(f"⌛ {phone} TIMEOUT")
            return

        # Режим: 50% SOLO / 50% PAIR
        mode = "SOLO" if random.random() < 0.5 else "PAIR"
        
        if mode == "SOLO":
            # Пишем себе (в заметки)
            await page.click(SELECTORS['search_box'])
            await human_type(page, SELECTORS['search_box'], phone) # Ищем себя
            await page.keyboard.press("Enter")
            await asyncio.sleep(2)
            
            # Ввод
            inp = page.locator(SELECTORS['input_box'])
            if not await inp.is_visible(): inp = page.locator(SELECTORS['input_box_fallback'])
            
            if await inp.is_visible():
                msg = await ai.generate("self")
                await human_type(page, SELECTORS['input_box'], msg)
                await page.keyboard.press("Enter")
                logger.info(f"✅ {phone} SOLO msg sent")

        else: # PAIR MODE
            peer = await db_get_random_peer(phone)
            if peer:
                await page.goto(f"https://web.whatsapp.com/send?phone={peer}")
                try:
                    inp = page.locator(SELECTORS['input_box'])
                    await inp.wait_for(state="visible", timeout=20000)
                    msg = await ai.generate("friend")
                    await human_type(page, SELECTORS['input_box'], msg)
                    await page.keyboard.press("Enter")
                    logger.info(f"✅ {phone} -> {peer} sent")
                except:
                    logger.info(f"⚠️ {phone} -> {peer} failed (chat not loaded)")
        
        await db_update_act(phone, 'active')
        await asyncio.sleep(random.randint(2, 5))

    except Exception as e:
        logger.error(f"Worker {phone} error: {e}")
    finally:
        if context: await context.close()

async def farm_manager():
    logger.info(f"🚜 FARM MANAGER STARTED [ID: {cfg.INSTANCE_ID}/{cfg.TOTAL_INSTANCES}]")
    while True:
        try:
            if is_memory_critical():
                await asyncio.sleep(30)
                continue

            # Берем аккаунт под свой шард
            target = await db_get_shard_target()
            
            if target:
                # Проверяем, не слишком ли рано (минимум 15 мин отдыха)
                if time.time() - target['last_act'] > 900:
                    async with BROWSER_SEMAPHORE:
                        await farm_worker(target)
                else:
                    await asyncio.sleep(10)
            else:
                await asyncio.sleep(30) # Нет аккаунтов, спим

            await asyncio.sleep(random.randint(5, 15))
        except Exception as e:
            logger.error(f"Manager Loop Error: {e}")
            await asyncio.sleep(10)

# ==========================================
# 🤖 AIOGRAM BOT HANDLERS
# ==========================================
bot = Bot(token=cfg.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class States(StatesGroup):
    add_phone = State()
    wait_code = State()
    promo_input = State()

def main_kb(is_admin: bool):
    btns = [
        [InlineKeyboardButton(text="📱 Добавить аккаунт", callback_data="add_menu")],
        [InlineKeyboardButton(text="🔑 Ввести промокод", callback_data="enter_promo")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")]
    ]
    if is_admin:
        btns.append([InlineKeyboardButton(text="👑 Админ Панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

@dp.message(Command("start"))
async def start(msg: types.Message):
    user_id = msg.from_user.id
    # Проверка whitelist
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        row = await (await db.execute("SELECT approved FROM whitelist WHERE user_id=?", (user_id,))).fetchone()
        
    if user_id != cfg.ADMIN_ID and (not row or not row[0]):
        if not row:
            async with aiosqlite.connect(cfg.DB_NAME) as db:
                await db.execute("INSERT INTO whitelist (user_id, username) VALUES (?, ?)", (user_id, msg.from_user.username))
                await db.commit()
        return await msg.answer("🔒 Доступ ограничен. Ожидайте подтверждения админа.")

    await msg.answer(f"🔱 **IMPERATOR v38.0 TITANIUM**\nNode: {cfg.INSTANCE_ID}", reply_markup=main_kb(user_id == cfg.ADMIN_ID))

# --- ADD ACCOUNT FLOW ---
@dp.callback_query(F.data == "add_menu")
async def add_menu(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.edit_text("Выберите метод:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Link Code (Рекомендуется)", callback_data="meth_code")],
        [InlineKeyboardButton(text="📷 QR Code", callback_data="meth_qr")]
    ]))

@dp.callback_query(F.data.startswith("meth_"))
async def set_method(cb: types.CallbackQuery, state: FSMContext):
    await state.update_data(method=cb.data.split("_")[1])
    await cb.message.edit_text("📱 Введите номер телефона (7900...):")
    await state.set_state(States.add_phone)

@dp.message(StateFilter(States.add_phone))
async def process_phone(msg: types.Message, state: FSMContext):
    phone_clean = re.sub(r'\D', '', msg.text)
    if len(phone_clean) < 10: return await msg.answer("❌ Неверный формат")
    
    data = await state.get_data()
    method = data.get('method')
    status_msg = await msg.answer("🚀 Инициализация драйвера...")
    
    device = await get_random_device()
    pw = await PlaywrightPool.get()
    
    try:
        context, page = await setup_browser(pw, phone_clean, device)
        await page.goto("https://web.whatsapp.com")
        
        if method == "qr":
            await page.wait_for_selector(SELECTORS['qr_canvas'], timeout=30000)
            path = f"qr_{phone_clean}.png"
            await page.screenshot(path=path)
            
            await ActiveSessions.add(phone_clean, {"context": context, "ua": device['ua'], "plat": device['plat'], "res": str(device['res'])})
            
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ ГОТОВО", callback_data=f"done_{phone_clean}")]])
            await msg.answer_photo(FSInputFile(path), caption=f"Скан для +{phone_clean}", reply_markup=kb)
            os.remove(path)
            
        elif method == "code":
            # Click Link with phone
            try:
                # Используем XPath для кнопки, так как она надежнее
                btn = page.locator(SELECTORS['link_with_phone_btn'])
                await btn.wait_for(timeout=10000)
                await btn.click()
            except:
                # Fallback JS click
                await page.evaluate(f"document.evaluate('{SELECTORS['link_with_phone_btn']}', document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue.click()")
            
            # Ввод номера (Nuclear)
            await page.wait_for_selector(SELECTORS['phone_input'])
            await nuclear_input(page, SELECTORS['phone_input'], phone_clean)
            await page.keyboard.press("Enter")
            
            # Ждем код
            await page.wait_for_selector(SELECTORS['code_container'], timeout=15000)
            await asyncio.sleep(2) # Анимация
            
            path = f"code_{phone_clean}.png"
            await page.screenshot(path=path)
            code_txt = await extract_code_ocr(path)
            
            await ActiveSessions.add(phone_clean, {"context": context, "ua": device['ua'], "plat": device['plat'], "res": str(device['res'])})
            
            txt = f"🔗 Код: `{code_txt}`" if code_txt else "⚠️ OCR не прочитал код. См. фото."
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Я ВВЕЛ КОД", callback_data=f"done_{phone_clean}")]])
            
            await msg.answer_photo(FSInputFile(path), caption=txt, reply_markup=kb)
            if os.path.exists(path): os.remove(path)

    except Exception as e:
        logger.error(f"Login fail: {e}")
        await msg.answer("❌ Ошибка запуска. Попробуйте позже.")
        if context: await context.close()
    
    await status_msg.delete()
    await state.clear()

@dp.callback_query(F.data.startswith("done_"))
async def finish_login(cb: types.CallbackQuery):
    phone = cb.data.split("_")[1]
    session = await ActiveSessions.get(phone)
    if not session: return await cb.answer("❌ Сессия истекла", show_alert=True)
    
    await cb.message.edit_text("⏳ Проверка входа...")
    context = session['context']
    page = context.pages[0]
    
    try:
        await page.wait_for_selector(SELECTORS['chat_list'], timeout=60000)
        # Сохранение в БД
        await db_add_account(phone, session['ua'], session['plat'], session['res'], cb.from_user.id)
        await cb.message.edit_text(f"✅ Аккаунт +{phone} успешно добавлен!")
        await ActiveSessions.remove(phone) # Закрываем эту сессию, дальше работает фарм
    except:
        await cb.message.edit_text("❌ Не удалось подтвердить вход. Попробуйте снова.")
        await ActiveSessions.remove(phone)

# --- PROMO & ADMIN ---
@dp.callback_query(F.data == "enter_promo")
async def ask_promo(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("Введите промокод:")
    await state.set_state(States.promo_input)

@dp.message(StateFilter(States.promo_input))
async def apply_promo(msg: types.Message, state: FSMContext):
    code = msg.text.strip().upper()
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        res = await (await db.execute("SELECT days, activations_left FROM promo_codes WHERE code=?", (code,))).fetchone()
        
        if res and res[1] > 0:
            days, left = res
            # Обновление подписки
            exp = time.time() + (days * 86400)
            await db.execute("INSERT OR REPLACE INTO subscriptions (user_id, status, expires_at, max_slots) VALUES (?, 'active', ?, 5)", (msg.from_user.id, exp))
            await db.execute("UPDATE promo_codes SET activations_left = ? WHERE code=?", (left-1, code))
            await db.commit()
            await msg.answer(f"✅ Промокод активирован! +{days} дней.")
        else:
            await msg.answer("❌ Неверный или использованный код.")
    await state.clear()

@dp.callback_query(F.data == "admin_panel")
async def admin_dash(cb: types.CallbackQuery):
    if cb.from_user.id != cfg.ADMIN_ID: return
    
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        cnt = await (await db.execute("SELECT COUNT(*) FROM accounts WHERE status='active'")).fetchone()
        wait = await (await db.execute("SELECT COUNT(*) FROM whitelist WHERE approved=0")).fetchone()
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"👥 Заявки ({wait[0]})", callback_data="adm_users")],
        [InlineKeyboardButton(text="🧹 Очистить баны", callback_data="adm_clean")],
        [InlineKeyboardButton(text="➕ Gen Promo", callback_data="adm_promo")]
    ])
    await cb.message.edit_text(f"📊 Active Bots: {cnt[0]}\nNode: {cfg.INSTANCE_ID}", reply_markup=kb)

@dp.callback_query(F.data == "adm_clean")
async def clean_bans(cb: types.CallbackQuery):
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        await db.execute("DELETE FROM accounts WHERE status='banned'")
        await db.commit()
    await cb.answer("Deleted!")

@dp.callback_query(F.data == "adm_promo")
async def gen_promo_handler(cb: types.CallbackQuery):
    code = "IMP-" + secrets.token_hex(3).upper()
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        await db.execute("INSERT INTO promo_codes VALUES (?, 30, 10)", (code,))
        await db.commit()
    await cb.message.answer(f"New Code: `{code}` (30 days, 10 uses)")

@dp.callback_query(F.data == "adm_users")
async def show_requests(cb: types.CallbackQuery):
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        rows = await (await db.execute("SELECT user_id, username FROM whitelist WHERE approved=0")).fetchall()
    
    if not rows: return await cb.answer("No requests")
    
    for uid, uname in rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Approve", callback_data=f"approve_{uid}")]])
        await cb.message.answer(f"User: {uname} ({uid})", reply_markup=kb)

@dp.callback_query(F.data.startswith("approve_"))
async def approve_user(cb: types.CallbackQuery):
    uid = int(cb.data.split("_")[1])
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        await db.execute("UPDATE whitelist SET approved=1 WHERE user_id=?", (uid,))
        await db.commit()
    await cb.answer("Approved")
    await cb.message.delete()

# ==========================================
# 🚀 LAUNCHER
# ==========================================

async def main():
    if not cfg.BOT_TOKEN:
        logger.critical("❌ BOT_TOKEN missed")
        return

    await db_init()
    
    # Запуск параллельных задач
    tasks = [
        asyncio.create_task(farm_manager()),
        asyncio.create_task(dp.start_polling(bot))
    ]
    
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("🔥 SYSTEM STARTED")
        await asyncio.gather(*tasks)
    except Exception as e:
        logger.error(f"CRITICAL: {e}")
    finally:
        await PlaywrightPool.stop()
        await bot.session.close()

if __name__ == "__main__":
    try:
        if sys.platform != 'win32':
            import uvloop
            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except: pass
    asyncio.run(main())
