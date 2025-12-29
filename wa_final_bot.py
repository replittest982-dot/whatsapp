import asyncio
import os
import logging
import sqlite3
import random
import psutil
import shutil
import sys
from datetime import datetime, timedelta
from typing import Optional, List, Dict

# --- БИБЛИОТЕКИ ---
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from faker import Faker

# --- SELENIUM (КЛАССИКА) ---
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import WebDriverException, TimeoutException

# ==========================================
# ⚙️ КОНФИГУРАЦИЯ v20.0 (LEGACY HYBRID)
# ==========================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except:
    ADMIN_ID = 0

REQUIRED_CHANNEL_ID = "@WhatsAppstatpro" 
REQUIRED_CHANNEL_URL = "https://t.me/WhatsAppstatpro"

INSTANCE_ID = int(os.getenv("INSTANCE_ID", 1))
TOTAL_INSTANCES = int(os.getenv("TOTAL_INSTANCES", 1))

BROWSER_SEMAPHORE = asyncio.Semaphore(2)

DB_NAME = 'imperator_legacy_v20.db'
SESSIONS_DIR = os.path.abspath("./sessions")
TMP_BASE = os.path.abspath("./tmp_chrome_data")

HEAT_MODES = {
    "TURBO": (15, 30),
    "MEDIUM": (60, 180),
    "SLOW": (300, 600)
}
CURRENT_MODE = "MEDIUM"

logging.basicConfig(level=logging.INFO, format='%(asctime)s | INST-1 | %(levelname)s | %(message)s')
logger = logging.getLogger("Imperator")
fake = Faker('ru_RU')

for d in [SESSIONS_DIR, TMP_BASE]:
    if not os.path.exists(d): os.makedirs(d)

# БАЗА УСТРОЙСТВ
DEVICES = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36", "res": "1920,1080", "plat": "Win32"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/124.0.0.0 Safari/537.36", "res": "1440,900", "plat": "MacIntel"}
]

ACTIVE_DRIVERS = {}

class BotStates(StatesGroup):
    waiting_phone = State()
    waiting_vip_id = State()

# ==========================================
# 🧠 AI DIALOGUE (ОСТАВИЛИ ЛУЧШЕЕ)
# ==========================================
class DialogueAI:
    def __init__(self):
        self.greetings = ["Привет", "Ку", "Здарова", "Хай"]
        self.questions = ["Как дела?", "Ты где?", "Скинь инфу", "На связи?"]
        self.answers = ["Норм", "Работаю", "Ок", "Принял"]
    
    def generate(self):
        text = ""
        mode = random.choice(['greet', 'ask', 'answer', 'fake'])
        if mode == 'greet': text = f"{random.choice(self.greetings)}. {random.choice(self.questions)}"
        elif mode == 'ask': text = random.choice(self.questions)
        elif mode == 'answer': text = random.choice(self.answers)
        else: text = fake.sentence(nb_words=random.randint(2, 5))
        
        if random.random() < 0.2: text += " ))"
        return text

ai_engine = DialogueAI()

# ==========================================
# 🛠 УТИЛИТЫ
# ==========================================
def cleanup_zombie():
    for p in psutil.process_iter(['name']):
        if p.info['name'] in ['chrome', 'chromedriver']:
            try: p.kill()
            except: pass
    if os.path.exists(TMP_BASE):
        try: shutil.rmtree(TMP_BASE)
        except: pass
        os.makedirs(TMP_BASE)

def get_sys_status():
    mem = psutil.virtual_memory()
    return f"RAM: {mem.available//1024//1024}MB | CPU: {psutil.cpu_percent()}%"

# ==========================================
# 🗄️ БАЗА ДАННЫХ
# ==========================================
def db_init():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS accounts (phone TEXT PRIMARY KEY, status TEXT, ua TEXT, res TEXT, plat TEXT, last_act DATETIME, created_at DATETIME, ban_date DATETIME)")
    c.execute("CREATE TABLE IF NOT EXISTS whitelist (user_id INTEGER PRIMARY KEY, approved INTEGER DEFAULT 0, is_unlimited INTEGER DEFAULT 0)")
    conn.commit(); conn.close()

def db_get_active():
    conn = sqlite3.connect(DB_NAME); res = conn.execute("SELECT phone FROM accounts WHERE status='active'").fetchall(); conn.close()
    return [r[0] for r in res]

def db_get_targets():
    conn = sqlite3.connect(DB_NAME)
    q = f"SELECT phone, created_at FROM accounts WHERE status='active' AND (rowid % {TOTAL_INSTANCES}) = ({INSTANCE_ID}-1)"
    res = conn.execute(q).fetchall(); conn.close()
    return res

def db_save(phone, ua, res, plat):
    conn = sqlite3.connect(DB_NAME); now = datetime.now()
    conn.execute("INSERT INTO accounts VALUES (?, 'active', ?, ?, ?, ?, ?, NULL) ON CONFLICT(phone) DO UPDATE SET status='active', last_act=excluded.last_act", (phone, ua, res, plat, now, now))
    conn.commit(); conn.close()

def db_ban(phone):
    conn = sqlite3.connect(DB_NAME); conn.execute("UPDATE accounts SET status='banned', ban_date=? WHERE phone=?", (datetime.now(), phone)); conn.commit(); conn.close()

def db_get_user(user_id):
    if user_id == ADMIN_ID: return (1, 1)
    conn = sqlite3.connect(DB_NAME); res = conn.execute("SELECT approved, is_unlimited FROM whitelist WHERE user_id=?", (user_id,)).fetchone(); conn.close()
    return res if res else (0, 0)

def db_set_vip(uid):
    conn = sqlite3.connect(DB_NAME); conn.execute("UPDATE whitelist SET approved=1, is_unlimited=1 WHERE user_id=?", (uid,)); conn.commit(); conn.close()

# ==========================================
# 🌐 SELENIUM (STABLE FLAGS ONLY)
# ==========================================
def get_driver(phone):
    conn = sqlite3.connect(DB_NAME)
    row = conn.execute("SELECT ua, res, plat FROM accounts WHERE phone=?", (phone,)).fetchone()
    conn.close()
    
    if row: ua, res, plat = row
    else: 
        d = random.choice(DEVICES)
        ua, res, plat = d['ua'], d['res'], d['plat']

    options = Options()
    prof = os.path.join(SESSIONS_DIR, phone)
    tmp = os.path.join(TMP_BASE, f"tmp_{phone}_{random.randint(1000,9999)}")
    if not os.path.exists(tmp): os.makedirs(tmp)

    options.add_argument(f"--user-data-dir={prof}")
    options.add_argument(f"--data-path={tmp}")
    options.add_argument(f"--disk-cache-dir={tmp}")
    options.add_argument("--headless=new")
    
    # ⚠️ Эти флаги ОБЯЗАТЕЛЬНЫ для BotHost, иначе крашнется даже старый код
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument(f"--remote-debugging-port={random.randint(9223, 9999)}")
    
    options.add_argument(f"--user-agent={ua}")
    options.add_argument(f"--window-size={res}")

    try:
        driver = webdriver.Chrome(options=options)
        # Маскировка (минимальная)
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"})
        return driver, ua, res, plat, tmp
    except Exception as e:
        logger.error(f"Driver Error: {e}")
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
    await asyncio.sleep(120)
    if phone in ACTIVE_DRIVERS:
        d = ACTIVE_DRIVERS.pop(phone, None)
        if d: 
            try: d['driver'].quit()
            except: pass
        shutil.rmtree(os.path.join(SESSIONS_DIR, phone), ignore_errors=True)
        if tmp: shutil.rmtree(tmp, ignore_errors=True)
        try: await bot.send_message(chat_id, f"⌛️ Время вышло. Сессия +{phone} удалена.")
        except: pass

# --- KEYBOARDS ---
def kb_main(uid):
    btns = [
        [InlineKeyboardButton(text="➕ ДОБАВИТЬ АККАУНТ", callback_data="add_acc")],
        [InlineKeyboardButton(text="📊 СТАТИСТИКА", callback_data="stats")],
        [InlineKeyboardButton(text="⚙️ НАСТРОЙКИ", callback_data="settings"), InlineKeyboardButton(text="🆘 ПОМОЩЬ", callback_data="help")]
    ]
    if uid == ADMIN_ID: btns.append([InlineKeyboardButton(text="👑 ДАТЬ VIP", callback_data="vip")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_set():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{'✅' if CURRENT_MODE=='TURBO' else ''} TURBO", callback_data="set_TURBO")],
        [InlineKeyboardButton(text=f"{'✅' if CURRENT_MODE=='MEDIUM' else ''} MEDIUM", callback_data="set_MEDIUM")],
        [InlineKeyboardButton(text=f"{'✅' if CURRENT_MODE=='SLOW' else ''} SLOW", callback_data="set_SLOW")],
        [InlineKeyboardButton(text="🔙", callback_data="menu")]
    ])

def kb_code(phone):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 ОБНОВИТЬ КОД", callback_data=f"getcode_{phone}")],
        [InlineKeyboardButton(text="✅ Я ВВЕЛ КОД", callback_data=f"finish_{phone}")]
    ])

# --- HANDLERS ---
@dp.message(Command("start"))
async def start(msg: types.Message):
    if not await check_sub(msg.from_user.id):
        return await msg.answer(f"❌ Подпишись: {REQUIRED_CHANNEL_URL}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Подписаться", url=REQUIRED_CHANNEL_URL)]]))
    
    ok, vip = db_get_user(msg.from_user.id)
    if not ok:
        conn = sqlite3.connect(DB_NAME); conn.execute("INSERT OR IGNORE INTO whitelist (user_id) VALUES (?)", (msg.from_user.id,)); conn.commit(); conn.close()
        if ADMIN_ID: await bot.send_message(ADMIN_ID, f"Заявка: {msg.from_user.id}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅", callback_data=f"ap_{msg.from_user.id}")]]))
        return await msg.answer("🔒 Жди одобрения.")
    
    st = "VIP 👑" if vip else "Юзер 👤"
    await msg.answer(f"🔱 **Imperator v20.0**\nСтатус: {st}", reply_markup=kb_main(msg.from_user.id))

@dp.callback_query(F.data.startswith("ap_"))
async def ap(cb: types.CallbackQuery):
    u = int(cb.data.split("_")[1]); conn = sqlite3.connect(DB_NAME); conn.execute("UPDATE whitelist SET approved=1 WHERE user_id=?", (u,)); conn.commit(); conn.close()
    await bot.send_message(u, "✅ Доступ!")
    await cb.answer()

@dp.callback_query(F.data == "menu")
async def menu(cb: types.CallbackQuery): await cb.message.edit_text("Меню", reply_markup=kb_main(cb.from_user.id))

@dp.callback_query(F.data == "help")
async def help(cb: types.CallbackQuery): await cb.message.edit_text("1. Добавить аккаунт\n2. Ввести номер\n3. Ждать код (20 сек)\n4. Ввести в телефоне\n5. Нажать 'Я ввел код' за 120 сек.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙", callback_data="menu")]]))

@dp.callback_query(F.data == "stats")
async def stat(cb: types.CallbackQuery): await cb.answer(f"📱 Активных: {len(db_get_active())}\n{get_sys_status()}", show_alert=True)

@dp.callback_query(F.data == "settings")
async def sett(cb: types.CallbackQuery): await cb.message.edit_text(f"Режим: {CURRENT_MODE}", reply_markup=kb_set())

@dp.callback_query(F.data.startswith("set_"))
async def smode(cb: types.CallbackQuery):
    global CURRENT_MODE; CURRENT_MODE = cb.data.split("_")[1]
    await cb.message.edit_text(f"✅ {CURRENT_MODE}", reply_markup=kb_main(cb.from_user.id))

@dp.callback_query(F.data == "vip")
async def vip_s(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("ID юзера для VIP:"); await state.set_state(BotStates.waiting_vip_id)

@dp.message(BotStates.waiting_vip_id)
async def vip_f(msg: types.Message, state: FSMContext):
    try: db_set_vip(int(msg.text)); await msg.answer("✅ VIP выдан.")
    except: await msg.answer("Ошибка")
    await state.clear()

# --- ADD ACCOUNT (OLD SCHOOL LOGIC) ---
@dp.callback_query(F.data == "add_acc")
async def add_a(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("📞 Введи номер (цифры):"); await state.set_state(BotStates.waiting_phone)

@dp.message(BotStates.waiting_phone)
async def add_p(msg: types.Message, state: FSMContext):
    phone = "".join(filter(str.isdigit, msg.text))
    await state.clear()
    s = await msg.answer(f"🚀 Запуск для +{phone}...\n⏳ Ждем загрузку (Классический метод)...")
    
    async with BROWSER_SEMAPHORE:
        try:
            driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, phone)
            if not driver: return await s.edit_text("❌ Ошибка старта драйвера")
            
            ACTIVE_DRIVERS[phone] = {"driver": driver, "ua": ua, "res": res, "plat": plat, "tmp": tmp}
            await asyncio.to_thread(driver.get, "https://web.whatsapp.com")
            
            # === СТАРАЯ НАДЕЖНАЯ ЛОГИКА (БЕЗ СЛОЖНЫХ JS) ===
            wait = WebDriverWait(driver, 30) # Даем 30 сек на прогрузку
            
            # 1. Ждем и ищем кнопку "Связать по номеру"
            # Пробуем разные варианты локаторов
            try:
                link_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//span[contains(text(), 'Link with phone')] | //span[contains(text(), 'Связать с')]")))
                link_btn.click()
            except:
                # Если не нашли по тексту, пробуем JS клик как запасной вариант
                driver.execute_script("var b=document.querySelector('span[role=\"button\"]'); if(b) b.click();")
            
            # 2. Ждем поле ввода
            inp = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='text']")))
            inp.clear()
            # Медленный ввод для надежности
            for digit in f"+{phone}":
                inp.send_keys(digit)
                await asyncio.sleep(0.05)
            
            # 3. Жмем Enter или кнопку Далее
            await asyncio.sleep(0.5)
            inp.send_keys(Keys.ENTER)
            
            # 4. Ждем появления кода (просто пауза, так как код это Canvas/SVG)
            await asyncio.sleep(15)
            
            png = await asyncio.to_thread(driver.get_screenshot_as_png)
            await s.delete()
            await msg.answer_photo(BufferedInputFile(png, "code.png"), caption=f"✅ Код для +{phone}\n⏱ 120 сек", reply_markup=kb_code(phone))
            asyncio.create_task(kill_timer(phone, msg.chat.id, tmp))
            
        except Exception as e:
            await s.edit_text(f"❌ Ошибка входа: {e}")

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
        db_save(p, d['ua'], d['res'], d['plat'])
        try: d['driver'].quit()
        except: pass
        if d['tmp'] and os.path.exists(d['tmp']): shutil.rmtree(d['tmp'], ignore_errors=True)
        await cb.message.edit_text(f"✅ +{p} в базе!")
    else: await cb.message.edit_text("❌ Время вышло")

# --- HIVE MIND ---
async def worker(phone):
    driver = None; tmp = None
    try:
        targs = db_get_active(); t = random.choice([x for x in targs if x!=phone]) if len(targs)>1 else phone
        async with BROWSER_SEMAPHORE:
            driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, phone)
            if not driver: return
            
            await asyncio.to_thread(driver.get, f"https://web.whatsapp.com/send?phone={t}")
            wait = WebDriverWait(driver, 45)
            inp = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "footer div[contenteditable='true']")))
            
            txt = ai_engine.generate()
            for c in txt:
                inp.send_keys(c); await asyncio.sleep(random.uniform(0.05, 0.2))
            inp.send_keys(Keys.ENTER)
            
            conn=sqlite3.connect(DB_NAME); conn.execute("UPDATE accounts SET last_act=? WHERE phone=?",(datetime.now(),phone)); conn.commit(); conn.close()
            logger.info(f"✅ {phone}->{t}: {txt}")
            await asyncio.sleep(2)
    except: pass
    finally:
        if driver: 
            try: driver.quit()
            except: pass
        if tmp: shutil.rmtree(tmp, ignore_errors=True)

async def loop():
    while True:
        accs = db_get_targets()
        if not accs: await asyncio.sleep(30); continue
        for p, _ in accs:
            if p not in ACTIVE_DRIVERS: await worker(p); await asyncio.sleep(15)
        await asyncio.sleep(random.randint(*HEAT_MODES[CURRENT_MODE]))

async def main():
    cleanup_zombie(); db_init(); asyncio.create_task(loop())
    logger.info("🚀 LEGACY HYBRID STARTED"); await bot.delete_webhook(drop_pending_updates=True); await dp.start_polling(bot)

if __name__ == "__main__": asyncio.run(main())
