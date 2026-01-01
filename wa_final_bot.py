import sys
import asyncio
import os
import logging
import random
import psutil
import shutil
import re
import time
import io
import csv
from datetime import datetime, timedelta
from collections import defaultdict

# --- УСКОРЕНИЕ (Turbo Mode) ---
if sys.platform != 'win32':
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except ImportError: pass

# --- БИБЛИОТЕКИ ---
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from faker import Faker
import aiosqlite 

# --- SELENIUM ---
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import WebDriverException, TimeoutException

# ==========================================
# ⚙️ КОНФИГУРАЦИЯ v20.0 (LEGION ULTIMATE)
# ==========================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except:
    ADMIN_ID = 0

if not BOT_TOKEN:
    sys.exit("❌ FATAL: Переменная BOT_TOKEN не найдена!")

REQUIRED_CHANNEL_ID = "@WhatsAppstatpro" 
REQUIRED_CHANNEL_URL = "https://t.me/WhatsAppstatpro"

INSTANCE_ID = int(os.getenv("INSTANCE_ID", 1))
TOTAL_INSTANCES = int(os.getenv("TOTAL_INSTANCES", 1))

# ⚡ PERFORMANCE: Лимит 2 браузера (для 10GB RAM)
BROWSER_SEMAPHORE = asyncio.Semaphore(2)

DB_NAME = 'imperator_legion_v20.db'
SESSIONS_DIR = os.path.abspath("./sessions")
TMP_BASE = os.path.abspath("./tmp_chrome_data")

# Режимы грева
HEAT_MODES = {
    "TURBO": (15, 30),
    "MEDIUM": (60, 180),
    "SLOW": (300, 600),
    "ECO": (600, 1200)
}
CURRENT_MODE = "MEDIUM"

# 🛡️ БЕЗОПАСНОСТЬ: Лимиты
MAX_ACCOUNTS_PER_USER = 10
RATE_LIMIT_DELAY = 60 # секунд между добавлениями
user_last_action = defaultdict(float)

logging.basicConfig(level=logging.INFO, format='%(asctime)s | LEGION | %(levelname)s | %(message)s')
logger = logging.getLogger("Imperator")
fake = Faker('ru_RU')

for d in [SESSIONS_DIR, TMP_BASE]:
    if not os.path.exists(d): os.makedirs(d)

DEVICES = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36", "res": "1920,1080", "plat": "Win32"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "res": "1440,900", "plat": "MacIntel"}
]

ACTIVE_DRIVERS = {}

class BotStates(StatesGroup):
    waiting_phone = State()
    waiting_vip_id = State()

# ==========================================
# 🧠 AI & UTILS
# ==========================================
class DialogueAI:
    def __init__(self):
        self.greetings = ["Привет", "Ку", "Здарова", "Хай", "Салам"]
        self.questions = ["Как дела?", "Ты где?", "Скинь инфу", "На связи?", "Чего молчишь?"]
        self.answers = ["Норм", "Работаю", "Ок", "Принял", "Скоро буду", "На месте"]
    
    def generate(self):
        if random.random() < 0.2: return random.choice(self.answers)
        text = f"{random.choice(self.greetings)}. {random.choice(self.questions)}" if random.random() < 0.5 else fake.sentence(nb_words=random.randint(2, 6))
        return text

ai_engine = DialogueAI()

def cleanup_zombie_sync():
    """Синхронная очистка при старте"""
    killed = 0
    for p in psutil.process_iter(['name']):
        if p.info['name'] in ['chrome', 'chromedriver']:
            try: p.kill(); killed += 1
            except: pass
    if os.path.exists(TMP_BASE):
        try: shutil.rmtree(TMP_BASE)
        except: pass
        os.makedirs(TMP_BASE)
    logger.info(f"🧹 Startup Cleanup: {killed} zombies killed.")

async def aggressive_cleanup_loop():
    """🛡️ MAINTENANCE: Фоновая зачистка (Fix #4: Infinite loop crash fixed)"""
    while True:
        try:
            await asyncio.sleep(1800) # 30 минут
            mem = psutil.virtual_memory()
            if mem.available < 500 * 1024 * 1024:
                logger.warning("🧹 LOW RAM: Aggressive Cleanup Started")
                for p in psutil.process_iter(['name']):
                    if p.info['name'] in ['chrome', 'chromedriver']:
                        try: p.kill()
                        except: pass
            
            # Чистка старых TMP папок (>1 часа)
            now = time.time()
            if os.path.exists(TMP_BASE):
                for f in os.listdir(TMP_BASE):
                    p = os.path.join(TMP_BASE, f)
                    if os.path.getmtime(p) < now - 3600:
                        shutil.rmtree(p, ignore_errors=True)
        except Exception as e:
            logger.error(f"Cleanup Loop Error: {e}")

def get_sys_status():
    mem = psutil.virtual_memory()
    return f"RAM: {mem.available//1024//1024}MB | CPU: {psutil.cpu_percent()}%"

# ==========================================
# 🗄️ DATABASE (ASYNC FIXES #1, #2)
# ==========================================
async def db_init():
    async with aiosqlite.connect(DB_NAME) as db:
        # Optimizations: Indexes
        await db.execute("CREATE TABLE IF NOT EXISTS accounts (phone TEXT PRIMARY KEY, status TEXT, ua TEXT, res TEXT, plat TEXT, last_act DATETIME, created_at DATETIME, ban_date DATETIME)")
        await db.execute("CREATE TABLE IF NOT EXISTS whitelist (user_id INTEGER PRIMARY KEY, approved INTEGER DEFAULT 0, is_unlimited INTEGER DEFAULT 0)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_status ON accounts(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_last_act ON accounts(last_act)")
        await db.commit()

async def db_get_active_phones():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT phone FROM accounts WHERE status='active'") as cursor:
            res = await cursor.fetchall()
            return [r[0] for r in res]

async def db_get_my_targets():
    async with aiosqlite.connect(DB_NAME) as db:
        q = f"SELECT phone, created_at FROM accounts WHERE status='active' AND (rowid % {TOTAL_INSTANCES}) = ({INSTANCE_ID}-1)"
        async with db.execute(q) as cursor:
            return await cursor.fetchall()

async def db_save(phone, ua, res, plat):
    async with aiosqlite.connect(DB_NAME) as db:
        now = datetime.now()
        await db.execute("INSERT INTO accounts VALUES (?, 'active', ?, ?, ?, ?, ?, NULL) ON CONFLICT(phone) DO UPDATE SET status='active', last_act=excluded.last_act", (phone, ua, res, plat, now, now))
        await db.commit()

async def db_ban(phone):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE accounts SET status='banned', ban_date=? WHERE phone=?", (datetime.now(), phone))
        await db.commit()

async def db_check_perm(user_id):
    if user_id == ADMIN_ID: return (1, 1)
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT approved, is_unlimited FROM whitelist WHERE user_id=?", (user_id,)) as cursor:
            res = await cursor.fetchone()
            return res if res else (0, 0)

async def db_set_vip(uid):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE whitelist SET approved=1, is_unlimited=1 WHERE user_id=?", (uid,))
        await db.commit()

# ==========================================
# 🌐 SELENIUM (STABLE CORE)
# ==========================================
def get_driver(phone):
    d_profile = random.choice(DEVICES)
    ua, res, plat = d_profile['ua'], d_profile['res'], d_profile['plat']
    
    options = Options()
    prof = os.path.join(SESSIONS_DIR, phone)
    unique_tmp = os.path.join(TMP_BASE, f"tmp_{phone}_{random.randint(1000,9999)}")
    if not os.path.exists(unique_tmp): os.makedirs(unique_tmp)

    options.add_argument(f"--user-data-dir={prof}")
    options.add_argument(f"--data-path={unique_tmp}")
    options.add_argument(f"--disk-cache-dir={unique_tmp}")
    
    # 🚨 CRITICAL FIXES 1-10
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage") 
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    
    # ⚡ PERFORMANCE
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-images")
    options.add_argument("--blink-settings=imagesEnabled=false")
    options.add_argument("--memory-pressure-off")
    
    # 🛡️ ANTIDETECT
    options.add_experimental_option("useAutomationExtension", False)
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    
    options.add_argument(f"--remote-debugging-port={random.randint(9222, 9999)}")
    options.add_argument(f"--user-agent={ua}")
    options.add_argument(f"--window-size={res}")

    try:
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(60)
        return driver, ua, res, plat, unique_tmp
    except Exception as e:
        logger.error(f"❌ Driver Init Error: {e}")
        return None, None, None, None, None

# ==========================================
# 🤖 BOT LOGIC
# ==========================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

async def check_sub(uid):
    try:
        m = await bot.get_chat_member(REQUIRED_CHANNEL_ID, uid)
        return m.status in ['member', 'administrator', 'creator']
    except: return False

async def kill_timer(phone, chat_id, tmp):
    """Таймер 120 сек"""
    await asyncio.sleep(120)
    if phone in ACTIVE_DRIVERS:
        d = ACTIVE_DRIVERS.pop(phone, None)
        if d: 
            try: await asyncio.to_thread(d['driver'].quit)
            except: pass
        shutil.rmtree(os.path.join(SESSIONS_DIR, phone), ignore_errors=True)
        if tmp and os.path.exists(tmp): shutil.rmtree(tmp, ignore_errors=True)
        try: await bot.send_message(chat_id, f"⌛️ **Время вышло.** Сессия +{phone} удалена.")
        except: pass

# --- UI ---
def kb_main(uid):
    btns = [
        [InlineKeyboardButton(text="➕ ДОБАВИТЬ АККАУНТ", callback_data="add_acc")],
        [InlineKeyboardButton(text="📊 СТАТИСТИКА", callback_data="stats")],
        [InlineKeyboardButton(text="⚙️ НАСТРОЙКИ", callback_data="settings"), InlineKeyboardButton(text="📥 EXPORT CSV", callback_data="export")]
    ]
    if uid == ADMIN_ID: btns.append([InlineKeyboardButton(text="👑 ДАТЬ VIP", callback_data="vip")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_set():
    # Fix #3: ECO mode added
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{'✅' if CURRENT_MODE=='TURBO' else ''} TURBO", callback_data="set_TURBO")],
        [InlineKeyboardButton(text=f"{'✅' if CURRENT_MODE=='MEDIUM' else ''} MEDIUM", callback_data="set_MEDIUM")],
        [InlineKeyboardButton(text=f"{'✅' if CURRENT_MODE=='SLOW' else ''} SLOW", callback_data="set_SLOW")],
        [InlineKeyboardButton(text=f"{'✅' if CURRENT_MODE=='ECO' else ''} ECO", callback_data="set_ECO")],
        [InlineKeyboardButton(text="🔙", callback_data="menu")]
    ])

def kb_code(phone):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 ОБНОВИТЬ КОД", callback_data=f"getcode_{phone}")],
        [InlineKeyboardButton(text="✅ Я ВВЕЛ КОД", callback_data=f"finish_{phone}")]
    ])

def kb_retry(phone):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔄 Повторить (+{phone})", callback_data=f"retry_{phone}")]
    ])

# --- HANDLERS ---
@dp.message(Command("start"))
async def start(msg: types.Message):
    if not await check_sub(msg.from_user.id):
        return await msg.answer(f"❌ Подпишись: {REQUIRED_CHANNEL_URL}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Подписаться", url=REQUIRED_CHANNEL_URL)]]))
    
    ok, vip = await db_check_perm(msg.from_user.id)
    if not ok:
        await db_init()
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("INSERT OR IGNORE INTO whitelist (user_id) VALUES (?)", (msg.from_user.id,))
            await db.commit()
        if ADMIN_ID: await bot.send_message(ADMIN_ID, f"Заявка: {msg.from_user.id}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅", callback_data=f"ap_{msg.from_user.id}")]]))
        return await msg.answer("🔒 Заявка отправлена.")
    
    st = "👑 VIP (Legion)" if vip else "👤 Юзер"
    await msg.answer(f"🔱 **Imperator v20.0 LEGION ULTIMATE**\nСтатус: {st}", reply_markup=kb_main(msg.from_user.id))

@dp.callback_query(F.data.startswith("ap_"))
async def ap(cb: types.CallbackQuery):
    u = int(cb.data.split("_")[1]); 
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE whitelist SET approved=1 WHERE user_id=?", (u,))
        await db.commit()
    await bot.send_message(u, "✅ Доступ открыт!")
    await cb.answer()

@dp.callback_query(F.data == "menu")
async def menu(cb: types.CallbackQuery): await cb.message.edit_text("Меню", reply_markup=kb_main(cb.from_user.id))

@dp.callback_query(F.data == "stats")
async def stat(cb: types.CallbackQuery): 
    act = await db_get_active_phones() # Async Fix #1
    await cb.answer(f"📱 Активных: {len(act)}\n{get_sys_status()}", show_alert=True)

@dp.callback_query(F.data == "settings")
async def sett(cb: types.CallbackQuery): await cb.message.edit_text(f"Режим: {CURRENT_MODE}", reply_markup=kb_set())

@dp.callback_query(F.data.startswith("set_"))
async def smode(cb: types.CallbackQuery):
    global CURRENT_MODE; CURRENT_MODE = cb.data.split("_")[1]
    await cb.message.edit_text(f"✅ Режим: {CURRENT_MODE}", reply_markup=kb_main(cb.from_user.id))

@dp.callback_query(F.data == "vip")
async def vip_s(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("ID юзера для VIP:"); await state.set_state(BotStates.waiting_vip_id)

@dp.message(BotStates.waiting_vip_id)
async def vip_f(msg: types.Message, state: FSMContext):
    try: await db_set_vip(int(msg.text)); await msg.answer("✅ VIP выдан.")
    except: await msg.answer("Ошибка")
    await state.clear()

# --- EXPORT CSV (New Feature) ---
@dp.callback_query(F.data == "export")
async def export_csv(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return await cb.answer("Только для админа", show_alert=True)
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM accounts") as cursor:
            rows = await cursor.fetchall()
    
    if not rows: return await cb.answer("Пусто", show_alert=True)
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Phone', 'Status', 'UA', 'Res', 'Plat', 'Last Act', 'Created', 'Ban Date'])
    writer.writerows(rows)
    
    output.seek(0)
    await cb.message.answer_document(BufferedInputFile(output.getvalue().encode(), filename="accounts.csv"))
    await cb.answer()

# --- ADD ACCOUNT LOGIC (Rate Limit + Retry + Fixes) ---
@dp.callback_query(F.data == "add_acc")
async def add_a(cb: types.CallbackQuery, state: FSMContext):
    # Security #1: Rate Limit
    if time.time() - user_last_action[cb.from_user.id] < RATE_LIMIT_DELAY and cb.from_user.id != ADMIN_ID:
        return await cb.answer(f"⏳ Жди {RATE_LIMIT_DELAY} сек.", show_alert=True)
    user_last_action[cb.from_user.id] = time.time()
    
    await cb.message.answer("📞 Введи номер (только цифры):"); await state.set_state(BotStates.waiting_phone)

# Handler for Retry Button
@dp.callback_query(F.data.startswith("retry_"))
async def retry_handler(cb: types.CallbackQuery, state: FSMContext):
    phone = cb.data.split("_")[1]
    await start_login_process(cb.message, phone, state)

@dp.message(BotStates.waiting_phone)
async def add_p(msg: types.Message, state: FSMContext):
    phone = "".join(filter(str.isdigit, msg.text))
    if not re.match(r"^\d{10,15}$", phone): return await msg.answer("❌ Неверный формат!")
    await state.clear()
    await start_login_process(msg, phone, state)

async def start_login_process(msg: types.Message, phone: str, state: FSMContext):
    s = await msg.answer(f"🚀 Запуск для +{phone}...\n⏳ 10% | Инициализация...")
    
    async with BROWSER_SEMAPHORE:
        try:
            driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, phone)
            if not driver: return await s.edit_text("❌ Ошибка драйвера (Crash).", reply_markup=kb_retry(phone))
            
            ACTIVE_DRIVERS[phone] = {"driver": driver, "ua": ua, "res": res, "plat": plat, "tmp": tmp}
            
            # 1. Open WA
            await s.edit_text(f"⏳ 30% | Открываю WhatsApp...")
            await asyncio.to_thread(driver.get, "https://web.whatsapp.com/?lang=en")
            wait = WebDriverWait(driver, 45)
            
            # 2. Find Link Button
            await s.edit_text(f"⏳ 50% | Ищу кнопку входа...")
            try:
                link_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//*[contains(text(), 'Link with phone')]")))
                link_btn.click()
            except:
                driver.execute_script("var b=document.querySelector('span[role=\"button\"]'); if(b && b.innerText.includes('Link')) b.click();")

            # 3. Input Number & CLICK NEXT
            await s.edit_text(f"⏳ 70% | Ввожу номер...")
            try:
                inp = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='text']")))
                inp.click(); inp.clear()
                for digit in f"+{phone}":
                    inp.send_keys(digit)
                    await asyncio.sleep(random.uniform(0.1, 0.3)) 
                
                await asyncio.sleep(0.5)
                # Next button click logic
                try:
                    next_btn = driver.find_element(By.XPATH, "//div[text()='Next']")
                    next_btn.click()
                except:
                    inp.send_keys(Keys.ENTER)
                
            except Exception as e:
                png = await asyncio.to_thread(driver.get_screenshot_as_png)
                await s.delete()
                await msg.answer_photo(BufferedInputFile(png, "err.png"), caption=f"❌ Ошибка ввода: {e}", reply_markup=kb_retry(phone))
                return

            # 4. Wait for Code
            await s.edit_text(f"⏳ 90% | Генерация кода...")
            await asyncio.sleep(15)
            
            png = await asyncio.to_thread(driver.get_screenshot_as_png)
            await s.delete()
            
            await msg.answer_photo(
                BufferedInputFile(png, "code.png"), 
                caption=f"✅ **Код для +{phone}**\n\n⏱️ Осталось: 120 сек", 
                reply_markup=kb_code(phone)
            )
            asyncio.create_task(kill_timer(phone, msg.chat.id, tmp))
            
        except Exception as e:
            await s.edit_text(f"❌ Ошибка: {e}", reply_markup=kb_retry(phone))

@dp.callback_query(F.data.startswith("getcode_"))
async def upd(cb: types.CallbackQuery):
    p = cb.data.split("_")[1]
    if p in ACTIVE_DRIVERS:
        await asyncio.sleep(1)
        try:
            png = await asyncio.to_thread(ACTIVE_DRIVERS[p]['driver'].get_screenshot_as_png)
            await cb.message.answer_photo(BufferedInputFile(png, "c.png"), caption="Скрин:")
        except: pass
    await cb.answer()

@dp.callback_query(F.data.startswith("finish_"))
async def fin(cb: types.CallbackQuery):
    p = cb.data.split("_")[1]; d = ACTIVE_DRIVERS.pop(p, None)
    if d:
        # Async Fix #2: await db_save
        await db_save(p, d['ua'], d['res'], d['plat'])
        try: await asyncio.to_thread(d['driver'].quit)
        except: pass
        if d['tmp'] and os.path.exists(d['tmp']): shutil.rmtree(d['tmp'], ignore_errors=True)
        # UX Feature: Success animation
        await cb.message.edit_text("🔄 Проверка...")
        await asyncio.sleep(1)
        await cb.message.edit_text(f"✅ +{p} Успешно добавлен! 🎉\nАккаунт передан в Сетку.")
    else: await cb.message.edit_text("❌ Время вышло или сессия удалена.")

# ==========================================
# 🚜 HIVE MIND (FIXED ASYNC)
# ==========================================
async def worker(phone):
    driver = None; tmp = None
    try:
        # Async Fix #1: await db_get...
        targs = await db_get_active_phones()
        if not targs: return

        t = random.choice([x for x in targs if x!=phone]) if len(targs)>1 else phone
        
        async with BROWSER_SEMAPHORE:
            driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, phone)
            if not driver: return 
            
            # Performance: window.stop()
            try:
                driver.set_page_load_timeout(30)
                await asyncio.to_thread(driver.get, f"https://web.whatsapp.com/send?phone={t}")
            except TimeoutException:
                driver.execute_script("window.stop();")
            
            wait = WebDriverWait(driver, 40)
            try:
                inp = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "footer div[contenteditable='true']")))
                txt = ai_engine.generate()
                for c in txt:
                    inp.send_keys(c); await asyncio.sleep(random.uniform(0.05, 0.2))
                inp.send_keys(Keys.ENTER)
                
                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute("UPDATE accounts SET last_act=? WHERE phone=?",(datetime.now(),phone))
                    await db.commit()
                
                logger.info(f"✅ {phone}->{t}: {txt}")
                await asyncio.sleep(2)
            except TimeoutException:
                src = driver.page_source.lower()
                if "not allowed" in src or "spam" in src:
                    await db_ban(phone)
                    shutil.rmtree(os.path.join(SESSIONS_DIR, phone), ignore_errors=True)
                    logger.error(f"💀 BAN DETECTED: {phone}")

    except Exception as e:
        logger.error(f"Worker Error: {e}")
    finally:
        if driver: 
            try: await asyncio.to_thread(driver.quit)
            except: pass
        if tmp and os.path.exists(tmp): shutil.rmtree(tmp, ignore_errors=True)

async def loop():
    while True:
        # Async Fix #1: await
        accs = await db_get_my_targets()
        if not accs: await asyncio.sleep(30); continue
        
        # Async Optimization: create_task instead of sequential await
        tasks = []
        for p, _ in accs:
            if p not in ACTIVE_DRIVERS:
                tasks.append(asyncio.create_task(worker(p)))
                await asyncio.sleep(5) # Stagger start
        
        await asyncio.sleep(random.randint(*HEAT_MODES[CURRENT_MODE]))

async def main():
    cleanup_zombie_sync() # Fix #5: Startup cleanup
    await db_init()
    asyncio.create_task(loop())
    asyncio.create_task(aggressive_cleanup_loop()) # Fix #4: Background task
    
    logger.info("🚀 LEGION ULTIMATE v20.0 STARTED")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__": asyncio.run(main())
