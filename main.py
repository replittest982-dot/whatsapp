#!/usr/bin/env python3
"""
🔱 IMPERATOR v40.0 ULTIMATE
Architecture: AsyncIO + ThreadPool (v39)
Interface: Full Features (v34)
"""

import sys
import asyncio
import os
import logging
import shutil
import aiosqlite
import time
import re
import random
import psutil
import signal
import json
from typing import Optional, List, Dict
from dataclasses import dataclass, field
from datetime import datetime

# 🚀 UVLOOP (Ускорение для Linux)
if sys.platform != 'win32':
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except ImportError: pass

# --- AIOGRAM ---
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest

# --- SELENIUM ---
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains

# ==========================================
# ⚙️ КОНФИГУРАЦИЯ
# ==========================================

@dataclass
class Config:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    ADMIN_IDS: List[int] = field(default_factory=list)
    CHANNEL_ID: str = os.getenv("CHANNEL_ID", "@WhatsAppstatpro")
    
    # Ресурсы
    MIN_RAM_MB: int = 800
    MAX_BROWSERS: int = 2 # Лимит одновременных браузеров (Semaphore)
    
    # Пути
    DB_NAME: str = 'imperator_v40.db'
    SESSIONS_DIR: str = os.path.abspath("./sessions")
    TMP_DIR: str = os.path.abspath("./tmp")
    
    # Тайминги
    TIMEOUT_PAGE: int = 60
    TIMEOUT_ELEMENT: int = 15

    def __post_init__(self):
        admins = os.getenv("ADMIN_IDS", "0")
        self.ADMIN_IDS = [int(x.strip()) for x in admins.split(",") if x.strip().isdigit()]
        for path in [self.SESSIONS_DIR, self.TMP_DIR]:
            os.makedirs(path, exist_ok=True)

cfg = Config()

if len(cfg.BOT_TOKEN) < 20:
    sys.exit("❌ FATAL: Нет токена! Установи переменную BOT_TOKEN.")

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | v40.0 | %(levelname)s | %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("Imperator")

# Глобальные настройки скоростей
SPEED_CONFIGS = {
    "TURBO": { "normal": (180, 300), "ghost": 900, "caller": 1800 },
    "MEDIUM": { "normal": (300, 600), "ghost": 1800, "caller": 3600 },
    "SLOW": { "normal": (600, 1500), "ghost": 3600, "caller": 7200 }
}
CURRENT_SPEED = "MEDIUM"

# ==========================================
# 🧠 AI & UTILS
# ==========================================
class DialogueAI:
    def __init__(self):
        self.msgs = [
            "Привет", "Ку", "Как дела?", "На связи", 
            "Окей", "Принял", "Скоро буду", "Работаю", 
            "Перезвоню потом", "Скинь инфу", "Понял тебя",
            "Хорошо", "Да", "Норм", "Договорились"
        ]
        self.emojis = [" :)", " ;)", " !", " +", " ok"]
    
    def generate(self):
        msg = random.choice(self.msgs)
        if random.random() < 0.3: msg += random.choice(self.emojis)
        return msg

ai_engine = DialogueAI()

def get_sys_status():
    mem = psutil.virtual_memory()
    return f"🖥 RAM: {mem.percent}% | CPU: {psutil.cpu_percent()}%"

def validate_phone(phone: str) -> bool:
    return bool(re.match(r'^\d{7,15}$', phone))

def check_memory() -> bool:
    mem = psutil.virtual_memory()
    return (mem.available / (1024 * 1024)) > cfg.MIN_RAM_MB

# ==========================================
# 🌐 ASYNC SELENIUM DRIVER (CORE)
# ==========================================

class AsyncDriver:
    """
    Обертка для Selenium, выполняющая блокирующие операции в ThreadPool.
    Предотвращает зависание бота.
    """
    def __init__(self, driver, tmp_dir, pid):
        self.driver = driver
        self.tmp_dir = tmp_dir
        self.pid = pid
        self.loop = asyncio.get_running_loop()
        self.closed = False

    async def run(self, func, *args):
        if self.closed: raise RuntimeError("Driver closed")
        return await self.loop.run_in_executor(None, func, *args)

    async def get(self, url):
        await self.run(self.driver.get, url)

    async def screenshot(self):
        return await self.run(self.driver.get_screenshot_as_png)

    async def quit(self):
        if self.closed: return
        self.closed = True
        try:
            await self.run(self.driver.quit)
        except: pass
        finally:
            self._force_kill()
            if os.path.exists(self.tmp_dir):
                shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _force_kill(self):
        try:
            if self.pid: psutil.Process(self.pid).kill()
        except: pass

    # --- ФУНКЦИИ ВЗАИМОДЕЙСТВИЯ ---

    async def safe_click(self, by, value, timeout=5):
        def _click():
            try:
                wait = WebDriverWait(self.driver, timeout)
                elem = wait.until(EC.element_to_be_clickable((by, value)))
                self.driver.execute_script("arguments[0].click();", elem)
                return True
            except: 
                try:
                    # Fallback strategies
                    elem = self.driver.find_element(by, value)
                    elem.click()
                    return True
                except: return False
        return await self.run(_click)

    async def get_elements(self, by, value):
        def _find_all():
            return self.driver.find_elements(by, value)
        return await self.run(_find_all)

    async def execute_script(self, script, *args):
        return await self.run(self.driver.execute_script, script, *args)

# ==========================================
# 🏭 DRIVER FACTORY
# ==========================================

def create_driver_sync(phone: str):
    opts = Options()
    prof = os.path.join(cfg.SESSIONS_DIR, phone)
    tmp = os.path.join(cfg.TMP_DIR, f"tmp_{phone}_{int(time.time())}")
    os.makedirs(tmp, exist_ok=True)
    os.makedirs(prof, exist_ok=True)

    opts.add_argument(f"--user-data-dir={prof}")
    opts.add_argument(f"--data-path={tmp}")
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--blink-settings=imagesEnabled=false")
    opts.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    driver = webdriver.Chrome(options=opts)
    driver.set_page_load_timeout(cfg.TIMEOUT_PAGE)
    
    # Anti-detect
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    })
    
    pid = driver.service.process.pid if driver.service.process else None
    return driver, tmp, pid

async def create_driver(phone: str) -> Optional[AsyncDriver]:
    if not check_memory(): return None
    try:
        loop = asyncio.get_running_loop()
        driver, tmp, pid = await loop.run_in_executor(None, create_driver_sync, phone)
        return AsyncDriver(driver, tmp, pid)
    except Exception as e:
        logger.error(f"Driver Create Error: {e}")
        return None

# ==========================================
# 🗄️ DATABASE
# ==========================================

class Database:
    def __init__(self):
        self.path = cfg.DB_NAME

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""CREATE TABLE IF NOT EXISTS accounts 
                                (phone TEXT PRIMARY KEY, 
                                 status TEXT DEFAULT 'active', 
                                 mode TEXT DEFAULT 'normal',
                                 created_at REAL,
                                 last_act REAL DEFAULT 0,
                                 total_sent INTEGER DEFAULT 0,
                                 total_calls INTEGER DEFAULT 0)""")
            
            await db.execute("""CREATE TABLE IF NOT EXISTS whitelist 
                                (user_id INTEGER PRIMARY KEY, 
                                 approved INTEGER DEFAULT 0, 
                                 username TEXT, 
                                 request_time REAL)""")
            
            await db.execute("""CREATE TABLE IF NOT EXISTS logs 
                                (id INTEGER PRIMARY KEY, type TEXT, sender TEXT, target TEXT, val TEXT, time REAL)""")
            await db.commit()

    async def add_account(self, phone):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT OR REPLACE INTO accounts (phone, created_at, status) VALUES (?, ?, 'active')", 
                             (phone, time.time()))
            await db.commit()

    async def get_all_phones(self):
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT phone FROM accounts WHERE status='active'") as cur:
                return [r[0] for r in await cur.fetchall()]

    async def get_account_data(self, phone):
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT mode, last_act FROM accounts WHERE phone=?", (phone,)) as cur:
                return await cur.fetchone()

    async def update_mode(self, phone, mode):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE accounts SET mode=? WHERE phone=?", (mode, phone))
            await db.commit()

    async def update_stats(self, phone, msg=False, call=False):
        async with aiosqlite.connect(self.path) as db:
            sql = "UPDATE accounts SET last_act=?"
            if msg: sql += ", total_sent=total_sent+1"
            if call: sql += ", total_calls=total_calls+1"
            sql += " WHERE phone=?"
            await db.execute(sql, (time.time(), phone))
            await db.commit()
            
    async def delete_account(self, phone):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM accounts WHERE phone=?", (phone,))
            await db.commit()

    # Whitelist
    async def check_perm(self, user_id):
        if user_id in cfg.ADMIN_IDS: return True
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT approved FROM whitelist WHERE user_id=?", (user_id,)) as cur:
                res = await cur.fetchone()
                return res and res[0] == 1

    async def add_request(self, user_id, username):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT OR IGNORE INTO whitelist (user_id, username, request_time) VALUES (?, ?, ?)", 
                             (user_id, username, time.time()))
            await db.commit()

    async def get_requests(self):
        async with aiosqlite.connect(self.path) as db:
            async with db.execute("SELECT user_id, username, request_time FROM whitelist WHERE approved=0") as cur:
                return await cur.fetchall()
    
    async def approve_user(self, user_id):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE whitelist SET approved=1 WHERE user_id=?", (user_id,))
            await db.commit()

# ==========================================
# 🎮 SESSION MANAGER
# ==========================================

class SessionManager:
    def __init__(self):
        self.sessions: Dict[str, AsyncDriver] = {}
        self.semaphore = asyncio.Semaphore(cfg.MAX_BROWSERS)

    async def get_or_create(self, phone):
        # Если сессия уже есть - возвращаем
        if phone in self.sessions: 
            if self.sessions[phone].closed:
                del self.sessions[phone]
            else:
                return self.sessions[phone]
        
        # Создаем новую под семафором
        async with self.semaphore:
            driver = await create_driver(phone)
            if driver:
                self.sessions[phone] = driver
            return driver

    async def close(self, phone):
        if phone in self.sessions:
            await self.sessions[phone].quit()
            del self.sessions[phone]

    async def close_all(self):
        phones = list(self.sessions.keys())
        for p in phones: await self.close(p)

# ==========================================
# 🤖 BOT LOGIC & HANDLERS
# ==========================================

db = Database()
sm = SessionManager()
bot = Bot(token=cfg.BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class States(StatesGroup):
    waiting_phone = State()

async def safe_reply(cb: CallbackQuery, text, alert=False):
    try: await cb.answer(text, show_alert=alert)
    except: pass

async def check_sub(user_id):
    try:
        m = await bot.get_chat_member(chat_id=cfg.CHANNEL_ID, user_id=user_id)
        return m.status in ['member', 'administrator', 'creator']
    except: return False

# --- KEYBOARDS ---
def kb_main(is_admin=False):
    btns = [
        [InlineKeyboardButton(text="📱 МОИ НОМЕРА", callback_data="my_numbers")],
        [InlineKeyboardButton(text="⚙️ КОНФИГ", callback_data="config_speed")],
        [InlineKeyboardButton(text="➕ ДОБАВИТЬ", callback_data="add_manual"),
         InlineKeyboardButton(text="📊 СТАТУС", callback_data="dashboard")]
    ]
    if is_admin: btns.append([InlineKeyboardButton(text="🔒 АДМИН", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_manual(phone):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 ЧЕК", callback_data=f"m1_{phone}"),
         InlineKeyboardButton(text="🔗 ВХОД", callback_data=f"m2_{phone}")],
        [InlineKeyboardButton(text="⌨️ НОМЕР", callback_data=f"m3_{phone}"),
         InlineKeyboardButton(text="➡️ NEXT", callback_data=f"m4_{phone}")],
        [InlineKeyboardButton(text="✅ СОХРАНИТЬ", callback_data=f"m5_{phone}"),
         InlineKeyboardButton(text="🗑 ОТМЕНА", callback_data=f"mc_{phone}")]
    ])

# --- START ---
@dp.message(Command("start"))
async def start(msg: Message):
    if not await check_sub(msg.from_user.id):
        kb = InlineKeyboardBuilder().button(text="✅ ПРОВЕРИТЬ", callback_data="check_sub").as_markup()
        return await msg.answer(f"🔒 Подпишись: {cfg.CHANNEL_ID}", reply_markup=kb)

    if await db.check_perm(msg.from_user.id):
        await msg.answer("🔱 **IMPERATOR v40 ULTIMATE**", 
                         reply_markup=kb_main(msg.from_user.id in cfg.ADMIN_IDS))
    else:
        await db.add_request(msg.from_user.id, msg.from_user.username)
        await msg.answer("⏳ Заявка отправлена админу.")
        for admin in cfg.ADMIN_IDS:
            try:
                kb = InlineKeyboardBuilder()
                kb.button(text="✅", callback_data=f"app_{msg.from_user.id}")
                await bot.send_message(admin, f"Заявка: {msg.from_user.id}", reply_markup=kb.as_markup())
            except: pass

@dp.callback_query(F.data == "check_sub")
async def sub_check(cb: CallbackQuery):
    await cb.message.delete()
    await start(cb.message)

# --- ADMIN ---
@dp.callback_query(F.data == "admin_panel")
async def admin_panel(cb: CallbackQuery):
    if cb.from_user.id not in cfg.ADMIN_IDS: return
    reqs = await db.get_requests()
    txt = f"Заявок: {len(reqs)}"
    kb = InlineKeyboardBuilder()
    for uid, uname, _ in reqs:
        kb.button(text=f"👤 {uname or uid}", callback_data=f"app_{uid}")
    kb.button(text="🔙", callback_data="menu")
    kb.adjust(1)
    await cb.message.edit_text(txt, reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("app_"))
async def approve(cb: CallbackQuery):
    uid = int(cb.data.split("_")[1])
    await db.approve_user(uid)
    await cb.answer("Одобрено")
    await admin_panel(cb)

# --- MANUAL LOGIN ---
@dp.callback_query(F.data == "add_manual")
async def add_manual(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("📱 Введите номер (только цифры):")
    await state.set_state(States.waiting_phone)

@dp.message(States.waiting_phone)
async def process_phone(msg: Message, state: FSMContext):
    phone = "".join(filter(str.isdigit, msg.text))
    if not validate_phone(phone): return await msg.answer("❌ Формат!")
    await state.clear()
    
    s = await msg.answer("🚀 Запуск...")
    drv = await sm.get_or_create(phone)
    if not drv: return await s.edit_text("💥 Ошибка драйвера (RAM/Crash)")
    
    await drv.get("https://web.whatsapp.com")
    await s.edit_text(f"✅ Пульт +{phone}", reply_markup=kb_manual(phone))

@dp.callback_query(F.data.startswith("m"))
async def manual_handler(cb: CallbackQuery):
    parts = cb.data.split("_")
    action = parts[0][1:] # m1 -> 1
    phone = parts[1]
    
    if phone not in sm.sessions:
        return await safe_reply(cb, "❌ Сессия закрыта", alert=True)
    
    drv = sm.sessions[phone]
    
    try:
        if action == "1": # Screen
            png = await drv.screenshot()
            await cb.message.answer_photo(BufferedInputFile(png, "s.png"))
            await safe_reply(cb, "📸")

        elif action == "2": # Link (4 Strategies)
            await safe_reply(cb, "🔍 Ищу кнопку...")
            found = False
            # 1. By Text
            for txt in ["Link with phone", "Link with phone number", "Привязать", "Войти по номеру"]:
                xp = f"//div[contains(text(), '{txt}')] | //span[contains(text(), '{txt}')]"
                if await drv.safe_click(By.XPATH, xp): found = True; break
            
            # 2. By Label
            if not found: found = await drv.safe_click(By.CSS_SELECTOR, "[aria-label*='phone']")
            
            # 3. Last Button
            if not found:
                btns = await drv.get_elements(By.TAG_NAME, "button")
                if len(btns) >= 2:
                    await drv.execute_script("arguments[0].click()", btns[-1])
                    found = True
            
            # 4. JS Injection
            if not found:
                js = "const t=[...document.querySelectorAll('div,span,button')].find(e=>e.innerText&&(e.innerText.includes('phone')||e.innerText.includes('номер')));if(t)t.click()"
                await drv.execute_script(js)
                found = True
            
            if found: await cb.message.answer("✅ Clicked")
            else: await cb.message.answer("❌ Not Found")

        elif action == "3": # Input
            await safe_reply(cb, "⌨️ Typing...")
            inputs = await drv.get_elements(By.TAG_NAME, "input")
            target_input = None
            for i in inputs:
                is_vis = await drv.run(i.is_displayed)
                if is_vis: target_input = i; break
            
            if target_input:
                await drv.execute_script("arguments[0].value = '';", target_input)
                for char in f"+{phone}":
                    await drv.run(target_input.send_keys, char)
                    await asyncio.sleep(0.1)
                await cb.message.answer("✅ Введено")
            else:
                await cb.message.answer("❌ Input not found")

        elif action == "4": # Next
            await safe_reply(cb, "➡️ Next")
            found = False
            for txt in ["Next", "Далее", "Siguiente"]:
                if await drv.safe_click(By.XPATH, f"//div[text()='{txt}']"): found = True; break
            
            if not found:
                btns = await drv.get_elements(By.TAG_NAME, "button")
                for btn in btns:
                    is_en = await drv.run(btn.is_enabled)
                    is_vis = await drv.run(btn.is_displayed)
                    if is_en and is_vis:
                        await drv.run(btn.click)
                        break

        elif action == "5": # Save
            await db.add_account(phone)
            await sm.close(phone)
            await cb.message.edit_text(f"🎉 +{phone} сохранен!")

        elif action == "c":
            await sm.close(phone)
            await cb.message.edit_text("❌ Отмена")

    except Exception as e:
        logger.error(f"Manual Err: {e}")
        await cb.message.answer("Ошибка выполнения")

# --- MENUS ---
@dp.callback_query(F.data == "menu")
async def menu(cb: CallbackQuery):
    await cb.message.edit_text("Меню", reply_markup=kb_main(cb.from_user.id in cfg.ADMIN_IDS))

@dp.callback_query(F.data == "my_numbers")
async def my_numbers(cb: CallbackQuery):
    phones = await db.get_all_phones()
    kb = InlineKeyboardBuilder()
    for p in phones:
        kb.button(text=f"📱 +{p}", callback_data=f"manage_{p}")
    kb.button(text="🔙", callback_data="menu")
    kb.adjust(1)
    await cb.message.edit_text("📂 Номера:", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("manage_"))
async def manage(cb: CallbackQuery):
    phone = cb.data.split("_")[1]
    data = await db.get_account_data(phone)
    mode = data[0] if data else "normal"
    
    kb = InlineKeyboardBuilder()
    for m in ["normal", "solo", "ghost", "caller"]:
        ico = "✅ " if mode == m else ""
        kb.button(text=f"{ico}{m.upper()}", callback_data=f"set_{m}_{phone}")
    kb.button(text="🗑 Удалить", callback_data=f"del_{phone}")
    kb.button(text="🔙", callback_data="my_numbers")
    kb.adjust(1)
    await cb.message.edit_text(f"⚙️ +{phone}\nРежим: {mode}", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("set_"))
async def set_mode(cb: CallbackQuery):
    _, mode, phone = cb.data.split("_")
    await db.update_mode(phone, mode)
    await cb.answer(f"Режим {mode}")
    await manage(cb)

@dp.callback_query(F.data.startswith("del_"))
async def del_acc(cb: CallbackQuery):
    phone = cb.data.split("_")[1]
    await db.delete_account(phone)
    shutil.rmtree(os.path.join(cfg.SESSIONS_DIR, phone), ignore_errors=True)
    await cb.answer("Удалено")
    await my_numbers(cb)

@dp.callback_query(F.data == "config_speed")
async def config_speed(cb: CallbackQuery):
    global CURRENT_SPEED
    kb = InlineKeyboardBuilder()
    for s in SPEED_CONFIGS:
        ico = "✅ " if CURRENT_SPEED == s else ""
        kb.button(text=f"{ico}{s}", callback_data=f"spd_{s}")
    kb.button(text="🔙", callback_data="menu")
    kb.adjust(1)
    await cb.message.edit_text(f"Скорость: {CURRENT_SPEED}", reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("spd_"))
async def set_spd(cb: CallbackQuery):
    global CURRENT_SPEED
    CURRENT_SPEED = cb.data.split("_")[1]
    await config_speed(cb)

@dp.callback_query(F.data == "dashboard")
async def dashboard(cb: CallbackQuery):
    phones = await db.get_all_phones()
    txt = f"📊 STATUS\nActive: {len(phones)}\n{get_sys_status()}"
    kb = InlineKeyboardBuilder().button(text="🔙", callback_data="menu").as_markup()
    await cb.message.edit_text(txt, reply_markup=kb)

# ==========================================
# 🚜 HIVE MIND (WORKER LOOP)
# ==========================================

async def farm_worker(phone):
    """Один цикл работы аккаунта (Send or Call)"""
    data = await db.get_account_data(phone)
    if not data: return
    mode, last_act = data
    
    # Check delays
    cfg_spd = SPEED_CONFIGS[CURRENT_SPEED]
    delay_range = cfg_spd['normal'] if mode == 'normal' else (cfg_spd[mode], cfg_spd[mode]+10) if isinstance(cfg_spd.get(mode), int) else cfg_spd['normal']
    if isinstance(delay_range, int): min_d = delay_range 
    else: min_d = delay_range[0]
    
    if (time.time() - last_act) < min_d: return

    # Action
    drv = await sm.get_or_create(phone)
    if not drv: return

    try:
        targets = await db.get_all_phones()
        target = random.choice([t for t in targets if t != phone]) if len(targets) > 1 else phone
        
        if mode == "ghost":
            await drv.get("https://web.whatsapp.com")
            await asyncio.sleep(10)
            await db.update_stats(phone)
            
        elif mode in ["normal", "solo"]:
            if mode == "solo": target = phone
            await drv.get(f"https://web.whatsapp.com/send?phone={target}")
            
            # Find input
            wait_scr = "return document.querySelector('footer div[contenteditable=\"true\"]');"
            inp = None
            for _ in range(20):
                inp = await drv.execute_script(wait_scr)
                if inp: break
                await asyncio.sleep(1)
            
            if inp:
                txt = ai_engine.generate()
                # Human Type implementation
                await drv.execute_script("arguments[0].focus();", inp)
                for c in txt:
                    await drv.execute_script("document.execCommand('insertText', false, arguments[0])", c)
                    await asyncio.sleep(random.uniform(0.05, 0.2))
                
                await asyncio.sleep(1)
                await drv.run(inp.send_keys, Keys.ENTER)
                await db.update_stats(phone, msg=True)
                logger.info(f"✅ Sent {phone}->{target}")

        elif mode == "caller":
            if len(targets) > 1:
                target_call = random.choice([t for t in targets if t != phone])
                await drv.get(f"https://web.whatsapp.com/send?phone={target_call}")
                await asyncio.sleep(10)
                
                # Try Call
                voice_btn_xpath = "//*[@data-icon='voice-call'] | //span[@data-icon='voice-call']"
                if await drv.safe_click(By.XPATH, voice_btn_xpath):
                    logger.info(f"📞 Call {phone}->{target_call}")
                    await asyncio.sleep(15)
                    await drv.safe_click(By.CSS_SELECTOR, "[aria-label*='End']")
                    await db.update_stats(phone, call=True)
                
    except Exception as e:
        logger.error(f"Worker {phone}: {e}")
    finally:
        await sm.close(phone)

async def hive_mind():
    logger.info("🐝 HIVE MIND STARTED")
    while True:
        phones = await db.get_all_phones()
        random.shuffle(phones)
        for p in phones:
            await farm_worker(p)
            await asyncio.sleep(random.randint(5, 15))
        await asyncio.sleep(10)

# ==========================================
# 🚀 MAIN
# ==========================================

async def main():
    logger.info("🚀 IMPERATOR v40 ULTIMATE STARTED")
    
    # Cleanup tmp
    if os.path.exists(cfg.TMP_DIR): shutil.rmtree(cfg.TMP_DIR)
    os.makedirs(cfg.TMP_DIR)
    
    await db.init()
    
    # Start Worker
    asyncio.create_task(hive_mind())
    
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
