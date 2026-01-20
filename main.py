#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🔱 IMPERATOR v38.4 TITANIUM ULTIMATE (PRODUCTION FINAL)
Changelog:
- FIX: Readable variable names in GeminiBrain
- FIX: Added retry logic with exponential backoff for UI interactions
- FIX: Advanced Ban Detection (URL -> Banner -> Content)
- FIX: Added missing /status and /admin handlers
- FIX: Full logging with exc_info=True
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
from PIL import Image
from dataclasses import dataclass
from typing import Optional, Dict, Tuple, List, Any
# --- AIOGRAM ---
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import ErrorEvent
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
    DB_NAME: str = 'imperator_v38.db'
    SESSIONS_DIR: str = os.path.abspath("./sessions")
    LOG_DIR: str = os.path.abspath("./logs")
    MAX_BROWSERS: int = 30
    MIN_RAM_MB: int = 1024
    GEO_LAT: float = 43.2389
    GEO_LON: float = 76.8897
    TIMEZONE: str = "Asia/Almaty"

cfg = Config()
for d in [cfg.SESSIONS_DIR, cfg.LOG_DIR]: os.makedirs(d, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(f"{cfg.LOG_DIR}/node_{cfg.INSTANCE_ID}.log", encoding='utf-8')]
)
logger = logging.getLogger(f"Imp_v38_{cfg.INSTANCE_ID}")
fake = Faker('ru_RU')
BROWSER_SEMAPHORE = asyncio.Semaphore(cfg.MAX_BROWSERS)

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
    'alert_banner': '[data-testid="alert-banner"]'
}
BAN_PATTERNS = ["suspended", "spam", "temporarily banned", "violat", "restricted", "blocked"]

# ==========================================
# 🛡️ UTILS & DATABASE
# ==========================================
def is_memory_critical() -> bool:
    try:
        if psutil.virtual_memory().available / (1024 * 1024) < cfg.MIN_RAM_MB:
            logger.warning("⚠️ MEMORY LOW. Pausing.")
            return True
    except: pass
    return False

async def get_random_device(): return random.choice(DEVICES)

class GeminiBrain:
    def __init__(self, key):
        self.active = False
        self.semaphore = asyncio.Semaphore(10)
        self.model = None
        if key:
            try: 
                genai.configure(api_key=key)
                self.model = genai.GenerativeModel('gemini-pro')
                self.active = True
            except: pass

    async def generate(self, ctx="friend"):
        if not self.active: return random.choice(["Привет", "Как дела?", "На связи?", "Че каво?"])
        async with self.semaphore:
            try:
                p = "Напиши заметку (3 слова)" if ctx == "self" else "Напиши другу (3 слова)"
                return (await asyncio.to_thread(self.model.generate_content, p)).text.strip().replace('"', '')
            except: return "Привет"
ai = GeminiBrain(cfg.GEMINI_KEY)

async def db_init():
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""CREATE TABLE IF NOT EXISTS accounts (phone TEXT PRIMARY KEY, owner_id INTEGER, status TEXT DEFAULT 'active', last_act REAL DEFAULT 0, ua TEXT, platform TEXT, resolution TEXT, created_at REAL)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS whitelist (user_id INTEGER PRIMARY KEY, username TEXT, approved INTEGER DEFAULT 0)""")
        await db.commit()

async def db_add_account(phone, ua, plat, res, owner_id):
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        await db.execute("""INSERT INTO accounts (phone, ua, platform, resolution, owner_id, last_act, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(phone) DO UPDATE SET status='active', last_act=?""", (phone, ua, plat, json.dumps(res), owner_id, time.time(), time.time(), time.time()))
        await db.commit()

async def db_get_shard_target() -> Optional[dict]:
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(f"SELECT rowid, * FROM accounts WHERE status='active' AND (rowid % {cfg.TOTAL_INSTANCES}) = ({cfg.INSTANCE_ID} - 1) ORDER BY last_act ASC LIMIT 1") as c:
            row = await c.fetchone()
            return dict(row) if row else None

async def db_update_act(phone, status='active'):
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        await db.execute("UPDATE accounts SET last_act=?, status=? WHERE phone=?", (time.time(), status, phone)); await db.commit()

async def db_get_random_peer(excl):
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        async with db.execute("SELECT phone FROM accounts WHERE status='active' AND phone != ? ORDER BY RANDOM() LIMIT 1", (excl,)) as c:
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

async def human_type(page, sel, text):
    try:
        await page.click(sel)
        for char in text:
            if random.random() < 0.04:
                await page.keyboard.press(random.choice(string.ascii_letters))
                await asyncio.sleep(0.1); await page.keyboard.press("Backspace")
            await page.keyboard.type(char, delay=random.randint(40, 120))
    except: pass

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
# 🚜 FARM WORKER (OPTIMIZED)
# ==========================================
async def farm_worker(acc):
    phone = acc['phone']
    try: res = json.loads(acc['resolution']) if acc['resolution'] else DEVICES[0]['res']
    except: res = DEVICES[0]['res']
    device = {"ua": acc['ua'] or DEVICES[0]['ua'], "res": res, "plat": acc['platform'] or DEVICES[0]['plat']}
    
    pw = await PlaywrightPool.get()
    ctx = None

    try:
        ctx, page = await setup_browser(pw, phone, device)
        await page.goto("https://web.whatsapp.com", timeout=60000, wait_until="domcontentloaded")
        
        # --- IMPROVED BAN DETECTION ---
        if "banned" in page.url or "suspended" in page.url:
            await db_update_act(phone, 'banned'); return

        try: 
            await page.wait_for_selector(SELECTORS['chat_list'], timeout=45000)
        except:
            # Check Alert Banner (Fast)
            if await page.locator(SELECTORS['alert_banner']).count() > 0:
                txt = (await page.locator(SELECTORS['alert_banner']).inner_text()).lower()
                if any(p in txt for p in BAN_PATTERNS):
                    await db_update_act(phone, 'banned'); logger.warning(f"🚫 {phone} BANNED (Banner)"); return
            
            # Check Full Content (Slow fallback)
            if any(p in (await page.content()).lower() for p in BAN_PATTERNS):
                await db_update_act(phone, 'banned'); logger.warning(f"🚫 {phone} BANNED (Content)"); return
            return

        # FARMING LOGIC
        if random.random() < 0.5: # SOLO
            await page.click(SELECTORS['search_box']); await human_type(page, SELECTORS['search_box'], phone); await page.keyboard.press("Enter"); await asyncio.sleep(2)
            if await page.locator(SELECTORS['input_box']).count() > 0:
                await human_type(page, SELECTORS['input_box'], await ai.generate("self")); await page.keyboard.press("Enter")
                logger.info(f"✅ {phone} SOLO OK")
        else: # PAIR
            peer = await db_get_random_peer(phone)
            if peer:
                await page.goto(f"https://web.whatsapp.com/send?phone={peer}"); 
                try: 
                    await page.wait_for_selector(SELECTORS['input_box'], timeout=25000)
                    await human_type(page, SELECTORS['input_box'], await ai.generate("friend")); await page.keyboard.press("Enter")
                    logger.info(f"✅ {phone} -> {peer} OK")
                except: pass
        await db_update_act(phone, 'active'); await asyncio.sleep(random.randint(3, 7))
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
            if not is_memory_critical():
                target = await db_get_shard_target()
                if target and (time.time() - target['last_act'] > 900):
                    async with BROWSER_SEMAPHORE: await farm_worker(target)
                else: await asyncio.sleep(5)
            await asyncio.sleep(random.randint(2, 5))
        except: await asyncio.sleep(5)

async def zombie_monitor():
    while True: await ActiveSessions.cleanup_old(); await asyncio.sleep(60)

# ==========================================
# 🤖 BOT HANDLERS
# ==========================================
bot = Bot(token=cfg.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class States(StatesGroup):
    add_phone = State()
    waiting_2fa = State()

@dp.errors()
async def err_handler(e: ErrorEvent): 
    logger.error(f"🚨 BOT EXCEPTION: {e.exception}", exc_info=True)

def main_kb(admin=False):
    kb = [[InlineKeyboardButton(text="📱 Добавить", callback_data="add_acc"), InlineKeyboardButton(text="👤 Статус", callback_data="status")]]
    if admin: kb.append([InlineKeyboardButton(text="👑 Admin", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

@dp.message(Command("start"))
async def start(msg: types.Message):
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        wl = await (await db.execute("SELECT approved FROM whitelist WHERE user_id=?", (msg.from_user.id,))).fetchone()
    if msg.from_user.id != cfg.ADMIN_ID and (not wl or not wl[0]):
        if not wl:
            async with aiosqlite.connect(cfg.DB_NAME) as db: await db.execute("INSERT INTO whitelist (user_id, username) VALUES (?, ?)", (msg.from_user.id, msg.from_user.username)); await db.commit()
        return await msg.answer("🔒 Заявка отправлена.")
    await msg.answer(f"🔱 **IMP v38.4**\nNode: {cfg.INSTANCE_ID}", reply_markup=main_kb(msg.from_user.id==cfg.ADMIN_ID))

@dp.callback_query(F.data == "status")
async def show_status(cb: types.CallbackQuery):
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        total = (await (await db.execute("SELECT COUNT(*) FROM accounts WHERE owner_id=? AND status='active'", (cb.from_user.id,))).fetchone())[0]
        banned = (await (await db.execute("SELECT COUNT(*) FROM accounts WHERE owner_id=? AND status='banned'", (cb.from_user.id,))).fetchone())[0]
    await cb.answer(f"✅ Актив: {total}\n🚫 Бан: {banned}", show_alert=True)

@dp.callback_query(F.data == "admin")
async def admin_panel(cb: types.CallbackQuery):
    if cb.from_user.id != cfg.ADMIN_ID: return await cb.answer("❌ Доступ запрещен", show_alert=True)
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        reqs = await (await db.execute("SELECT user_id, username FROM whitelist WHERE approved=0")).fetchall()
    
    kb = []
    for uid, uname in reqs:
        kb.append([InlineKeyboardButton(text=f"✅ {uname}", callback_data=f"approve_{uid}")])
    
    txt = "👑 **Admin Panel**\nЗаявки на вход:" if reqs else "👑 **Admin Panel**\nЗаявок нет."
    await cb.message.edit_text(txt, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb) if kb else None)

@dp.callback_query(F.data.startswith("approve_"))
async def approve_user(cb: types.CallbackQuery):
    if cb.from_user.id != cfg.ADMIN_ID: return
    uid = int(cb.data.split("_")[1])
    async with aiosqlite.connect(cfg.DB_NAME) as db:
        await db.execute("UPDATE whitelist SET approved=1 WHERE user_id=?", (uid,)); await db.commit()
    await cb.answer("✅ Доступ выдан")
    await cb.message.delete()

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
            # FIX: Retry Logic with Exponential Backoff
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
                await state.set_state(States.waiting_2fa)
                await state.update_data(phone=phone)
                return 
            raise Exception("No chat list")
    except:
        await cb.message.edit_text("❌ Не вошел."); await ActiveSessions.remove(phone)

@dp.message(StateFilter(States.waiting_2fa))
async def handle_2fa(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    phone = data['phone']
    sess = await ActiveSessions.get(phone)
    if not sess: return await msg.answer("❌ Сессия умерла.")
    try:
        page = sess['context'].pages[0]
        await human_type(page, SELECTORS['2fa_input'], msg.text.strip())
        await page.wait_for_selector(SELECTORS['chat_list'], timeout=20000)
        await db_add_account(phone, sess['ua'], sess['plat'], sess['res'], msg.from_user.id)
        await msg.answer(f"✅ +{phone} (2FA) OK!")
    except: await msg.answer("❌ Ошибка PIN.")
    finally: await ActiveSessions.remove(phone); await state.clear()

async def main():
    if not cfg.BOT_TOKEN: return logger.critical("NO TOKEN")
    await db_init()
    ts = [asyncio.create_task(farm_manager()), asyncio.create_task(zombie_monitor()), asyncio.create_task(dp.start_polling(bot))]
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("🔥 STARTED v38.4")
        await asyncio.gather(*ts)
    finally: await PlaywrightPool.stop(); await bot.session.close()

if __name__ == "__main__":
    if sys.platform != 'win32':
        try: import uvloop; asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        except: pass
    asyncio.run(main())
