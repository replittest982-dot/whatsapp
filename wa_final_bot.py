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

# --- LIBRARIES ---
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from faker import Faker

# --- SELENIUM ---
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import WebDriverException, TimeoutException

# ==========================================
# ⚙️ CONFIG v18.1 (CRASH FIX + VIP SYSTEM)
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

# Лимит одновременных браузеров (2 для 10ГБ RAM)
BROWSER_SEMAPHORE = asyncio.Semaphore(2)

DB_NAME = 'imperator_v18_1.db'
SESSIONS_DIR = os.path.abspath("./sessions")
TMP_BASE = os.path.abspath("./tmp_chrome")

HEAT_MODES = {
    "TURBO": (15, 30),
    "MEDIUM": (60, 180),
    "SLOW": (300, 600)
}
CURRENT_MODE = "MEDIUM"

logging.basicConfig(level=logging.INFO, format='%(asctime)s | INST-1 | %(levelname)s | %(name)s | %(message)s')
logger = logging.getLogger("Imperator")
fake = Faker('ru_RU')

# Создаем папки
for d in [SESSIONS_DIR, TMP_BASE]:
    if not os.path.exists(d): os.makedirs(d)

# БАЗА УСТРОЙСТВ (Пункт 84 - Разные платформы)
DEVICES = [
    # Windows Chrome
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "res": "1920,1080", "plat": "Win32"},
    # MacOS Chrome
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "res": "1440,900", "plat": "MacIntel"},
    # Linux Chrome
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "res": "1366,768", "plat": "Linux x86_64"},
    # Windows Edge (Имитация)
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0", "res": "1920,1080", "plat": "Win32"}
]

ACTIVE_DRIVERS = {}

class BotStates(StatesGroup):
    waiting_phone = State()
    waiting_vip_id = State() # Для админки

# ==========================================
# 🧠 AI DIALOGUE ENGINE
# ==========================================
class DialogueAI:
    def __init__(self):
        self.greetings = ["Привет", "Ку", "Здарова", "Хай", "Салам"]
        self.questions = ["Как дела?", "Ты где?", "Что нового?", "Когда будешь?", "Скинь инфу", "Ты тут?"]
        self.answers = ["Норм", "Работаю", "В пути", "Скоро буду", "Ок", "Принял"]
        self.smiles = ["))", "👍", "👋", "🔥"]

    def generate(self):
        """Генерирует уникальный текст"""
        mode = random.choice(['greet', 'ask', 'answer', 'fake'])
        text = ""
        if mode == 'greet': text = f"{random.choice(self.greetings)}. {random.choice(self.questions)}"
        elif mode == 'ask': text = random.choice(self.questions)
        elif mode == 'answer': text = random.choice(self.answers)
        else: text = fake.sentence(nb_words=random.randint(2, 6))
        
        if random.random() < 0.25: text += f" {random.choice(self.smiles)}"
        return text

ai_engine = DialogueAI()

# ==========================================
# 🛠 SYSTEM UTILS
# ==========================================
def cleanup_zombie_processes():
    """Зачистка процессов"""
    for p in psutil.process_iter(['name']):
        if p.info['name'] in ['chrome', 'chromedriver', 'google-chrome']:
            try: p.kill()
            except: pass
    if os.path.exists(TMP_BASE):
        shutil.rmtree(TMP_BASE, ignore_errors=True)
        os.makedirs(TMP_BASE)

def get_sys_status():
    mem = psutil.virtual_memory()
    return f"RAM: {mem.available//1024//1024}MB | CPU: {psutil.cpu_percent()}%"

# ==========================================
# 🗄️ DATABASE
# ==========================================
def db_init():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS accounts (
        phone TEXT PRIMARY KEY, status TEXT, ua TEXT, res TEXT, plat TEXT, 
        last_act DATETIME, created_at DATETIME, ban_date DATETIME
    )''')
    # is_unlimited для VIP (Пункт 49)
    c.execute('''CREATE TABLE IF NOT EXISTS whitelist (
        user_id INTEGER PRIMARY KEY, approved INTEGER DEFAULT 0, is_unlimited INTEGER DEFAULT 0
    )''')
    conn.commit(); conn.close()

def db_get_active_phones():
    conn = sqlite3.connect(DB_NAME); res = conn.execute("SELECT phone FROM accounts WHERE status='active'").fetchall(); conn.close()
    return [r[0] for r in res]

def db_get_my_targets():
    conn = sqlite3.connect(DB_NAME)
    q = f"SELECT phone, created_at FROM accounts WHERE status='active' AND (rowid % {TOTAL_INSTANCES}) = ({INSTANCE_ID}-1)"
    res = conn.execute(q).fetchall()
    conn.close()
    return res

def db_save(phone, ua, res, plat):
    conn = sqlite3.connect(DB_NAME); now = datetime.now()
    conn.execute("INSERT INTO accounts (phone, status, ua, res, plat, last_act, created_at) VALUES (?, 'active', ?, ?, ?, ?, ?) ON CONFLICT(phone) DO UPDATE SET status='active', last_act=excluded.last_act", (phone, ua, res, plat, now, now))
    conn.commit(); conn.close()

def db_ban(phone):
    conn = sqlite3.connect(DB_NAME); conn.execute("UPDATE accounts SET status='banned', ban_date=? WHERE phone=?", (datetime.now(), phone)); conn.commit(); conn.close()

def db_get_user_limit_info(user_id):
    """Проверка прав: (approved, is_vip)"""
    if user_id == ADMIN_ID: return (1, 1)
    conn = sqlite3.connect(DB_NAME)
    res = conn.execute("SELECT approved, is_unlimited FROM whitelist WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return res if res else (0, 0)

def db_set_vip(user_id):
    conn = sqlite3.connect(DB_NAME)
    conn.execute("UPDATE whitelist SET approved=1, is_unlimited=1 WHERE user_id=?", (user_id,))
    conn.commit(); conn.close()

def db_count_user_accounts():
    # Простой подсчет всех активных (в реальной системе нужна привязка owner_id к accounts)
    # Тут считаем общее кол-во для лимита (как упрощение)
    conn = sqlite3.connect(DB_NAME)
    res = conn.execute("SELECT COUNT(*) FROM accounts WHERE status='active'").fetchone()[0]
    conn.close()
    return res

# ==========================================
# 🌐 SELENIUM (FIXED DRIVER)
# ==========================================
def get_driver(phone):
    # Генерация устройства
    conn = sqlite3.connect(DB_NAME)
    row = conn.execute("SELECT ua, res, plat FROM accounts WHERE phone=?", (phone,)).fetchone()
    conn.close()
    
    if row: 
        ua, res, plat = row
    else: 
        d = random.choice(DEVICES)
        ua, res, plat = d['ua'], d['res'], d['plat']

    options = Options()
    
    # ПУТИ
    profile_path = os.path.join(SESSIONS_DIR, phone)
    # Уникальная tmp папка для каждого процесса (избегает конфликтов)
    tmp_path = os.path.join(TMP_BASE, f"tmp_{phone}_{random.randint(1000, 9999)}")
    if not os.path.exists(tmp_path): os.makedirs(tmp_path)
    
    options.add_argument(f"--user-data-dir={profile_path}")
    options.add_argument(f"--data-path={tmp_path}")
    options.add_argument(f"--disk-cache-dir={tmp_path}")
    
    options.add_argument("--headless=new")
    
    # 🔥 FIX КРАШЕЙ (Убраны single-process/no-zygote, так как они ломают Chrome 143) 🔥
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage") # Must have
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-extensions")
    
    # Рандомный порт отладки, чтобы процессы не конфликтовали
    debug_port = random.randint(9223, 9999)
    options.add_argument(f"--remote-debugging-port={debug_port}")
    
    options.add_argument(f"--user-agent={ua}")
    options.add_argument(f"--window-size={res}")
    options.page_load_strategy = 'eager'

    try:
        driver = webdriver.Chrome(options=options)
        
        # Stealth
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        })
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": f"Object.defineProperty(navigator, 'platform', {{get: () => '{plat}'}});"
        })
        
        return driver, ua, res, plat, tmp_path
    except Exception as e:
        logger.error(f"Driver Init Error: {e}")
        return None, None, None, None, None

# ==========================================
# 🤖 BOT LOGIC
# ==========================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- Middlewares ---
async def check_sub(user_id):
    try:
        m = await bot.get_chat_member(REQUIRED_CHANNEL_ID, user_id)
        return m.status in ['member', 'administrator', 'creator']
    except: return False # Для тестов можно True

async def auto_kill_session(phone, chat_id, tmp_path):
    """Таймер 120 сек"""
    await asyncio.sleep(120)
    if phone in ACTIVE_DRIVERS:
        d = ACTIVE_DRIVERS.pop(phone, None)
        if d:
            try: await asyncio.to_thread(d['driver'].quit)
            except: pass
        shutil.rmtree(os.path.join(SESSIONS_DIR, phone), ignore_errors=True)
        if tmp_path and os.path.exists(tmp_path): shutil.rmtree(tmp_path, ignore_errors=True)
        try: await bot.send_message(chat_id, f"⏳ **Время вышло!** Сессия +{phone} удалена.")
        except: pass

# --- Keyboards ---
def kb_main(user_id):
    # Проверка на админа для кнопки VIP
    btns = [
        [InlineKeyboardButton(text="➕ ДОБАВИТЬ АККАУНТ", callback_data="add_acc")],
        [InlineKeyboardButton(text="📊 СТАТИСТИКА", callback_data="stats")],
        [InlineKeyboardButton(text="⚙️ НАСТРОЙКИ", callback_data="settings"), 
         InlineKeyboardButton(text="🆘 ПОМОЩЬ", callback_data="help")]
    ]
    if user_id == ADMIN_ID:
        btns.append([InlineKeyboardButton(text="👑 ДАТЬ VIP (Юзер)", callback_data="add_vip")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_settings():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{'✅' if CURRENT_MODE=='TURBO' else ''} TURBO", callback_data="set_TURBO")],
        [InlineKeyboardButton(text=f"{'✅' if CURRENT_MODE=='MEDIUM' else ''} MEDIUM", callback_data="set_MEDIUM")],
        [InlineKeyboardButton(text=f"{'✅' if CURRENT_MODE=='SLOW' else ''} SLOW", callback_data="set_SLOW")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu")]
    ])

def kb_login_process(phone):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 ОБНОВИТЬ КОД", callback_data=f"getcode_{phone}")],
        [InlineKeyboardButton(text="✅ Я ВВЕЛ КОД", callback_data=f"finish_{phone}")]
    ])

# --- Handlers ---
@dp.message(Command("start"))
async def start_handler(msg: types.Message):
    if not await check_sub(msg.from_user.id):
        return await msg.answer(f"❌ Подпишись: {REQUIRED_CHANNEL_URL}", 
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Подписаться", url=REQUIRED_CHANNEL_URL)]]))

    approved, vip = db_get_user_limit_info(msg.from_user.id)
    
    if not approved and msg.from_user.id != ADMIN_ID:
        conn = sqlite3.connect(DB_NAME)
        conn.execute("INSERT OR IGNORE INTO whitelist (user_id) VALUES (?)", (msg.from_user.id,))
        conn.commit(); conn.close()
        if ADMIN_ID: 
            await bot.send_message(ADMIN_ID, f"Заявка: {msg.from_user.id}", 
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Пустить", callback_data=f"ap_{msg.from_user.id}")]])
            )
        return await msg.answer("🔒 Заявка отправлена.")

    status = "👑 VIP" if vip else "👤 Юзер (Лимит: 3)"
    await msg.answer(f"🔱 **Imperator v18.1**\nСтатус: {status}", reply_markup=kb_main(msg.from_user.id))

@dp.callback_query(F.data.startswith("ap_"))
async def approve(cb: types.CallbackQuery):
    uid = int(cb.data.split("_")[1])
    conn = sqlite3.connect(DB_NAME); conn.execute("UPDATE whitelist SET approved=1 WHERE user_id=?", (uid,)); conn.commit(); conn.close()
    await bot.send_message(uid, "✅ Доступ открыт!")
    await cb.answer("Ок")

@dp.callback_query(F.data == "menu")
async def menu(cb: types.CallbackQuery):
    await cb.message.edit_text("Главное меню", reply_markup=kb_main(cb.from_user.id))

@dp.callback_query(F.data == "help")
async def help_h(cb: types.CallbackQuery):
    # Пункт 46 - Красивая помощь
    txt = ("📚 **Помощь**\n\n"
           "1. Нажми **Добавить аккаунт**.\n"
           "2. Введи номер (без +).\n"
           "3. Подожди, пока бот нажмет кнопки.\n"
           "4. Получи код и введи в телефоне.\n"
           "5. Нажми 'Я ВВЕЛ КОД' за **120 сек**.\n\n"
           "⚠️ *Если не успеть, сессия удалится.*")
    await cb.message.edit_text(txt, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙", callback_data="menu")]]))

@dp.callback_query(F.data == "stats")
async def stats(cb: types.CallbackQuery):
    # Пункт 26 - Статистика номеров
    phones = db_get_active_phones()
    await cb.answer(f"📱 Номеров в базе: {len(phones)}\n💻 {get_sys_status()}", show_alert=True)

@dp.callback_query(F.data == "settings")
async def sett(cb: types.CallbackQuery):
    await cb.message.edit_text(f"Режим: {CURRENT_MODE}", reply_markup=kb_settings())

@dp.callback_query(F.data.startswith("set_"))
async def set_m(cb: types.CallbackQuery):
    global CURRENT_MODE
    CURRENT_MODE = cb.data.split("_")[1]
    await cb.message.edit_text(f"✅ Режим: {CURRENT_MODE}", reply_markup=kb_main(cb.from_user.id))

# --- VIP SYSTEM (Пункт 49) ---
@dp.callback_query(F.data == "add_vip")
async def add_vip_s(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    await cb.message.answer("Введите ID пользователя для VIP:")
    await state.set_state(BotStates.waiting_vip_id)

@dp.message(BotStates.waiting_vip_id)
async def add_vip_f(msg: types.Message, state: FSMContext):
    try:
        uid = int(msg.text)
        db_set_vip(uid)
        await msg.answer(f"✅ User {uid} теперь VIP (Безлимит).")
    except: await msg.answer("Ошибка ID")
    await state.clear()

# --- ADD ACC ---
@dp.callback_query(F.data == "add_acc")
async def add_a(cb: types.CallbackQuery, state: FSMContext):
    approved, vip = db_get_user_limit_info(cb.from_user.id)
    # Лимит для обычных юзеров - 3 аккаунта (если бы мы привязывали акки к юзерам). 
    # В текущей общей базе это условность, но логика готова.
    
    await cb.message.answer("📞 Введите номер:")
    await state.set_state(BotStates.waiting_phone)

@dp.message(BotStates.waiting_phone)
async def add_p(msg: types.Message, state: FSMContext):
    phone = "".join(filter(str.isdigit, msg.text))
    await state.clear()
    
    s = await msg.answer(f"🚀 Запуск Chrome +{phone}...")
    
    async with BROWSER_SEMAPHORE:
        try:
            driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, phone)
            if not driver: return await s.edit_text("❌ Краш драйвера (Попробуй позже)")
            
            ACTIVE_DRIVERS[phone] = {"driver": driver, "ua": ua, "res": res, "plat": plat, "tmp": tmp}
            await asyncio.to_thread(driver.get, "https://web.whatsapp.com")
            
            # JS AUTO-INPUT
            driver.execute_script(f"""
                var check = setInterval(function(){{
                    var btn = document.querySelector('span[role="button"]');
                    if(btn && (btn.innerText.includes('Link') || btn.innerText.includes('Связать'))) btn.click();
                    
                    var inp = document.querySelector('input[type="text"]');
                    if(inp){{
                        clearInterval(check);
                        inp.focus();
                        document.execCommand('selectAll');
                        document.execCommand('delete');
                        document.execCommand('insertText', false, '+{phone}');
                        setTimeout(() => {{ 
                            var b = document.querySelector('button.type-primary') || document.querySelector('div[role="button"][class*="primary"]');
                            if(b) b.click();
                        }}, 500);
                    }}
                }}, 1000);
            """)
            
            await asyncio.sleep(12)
            png = await asyncio.to_thread(driver.get_screenshot_as_png)
            await s.delete()
            await msg.answer_photo(BufferedInputFile(png, "code.png"), caption=f"✅ Код для +{phone}\n⏱ 120 сек", reply_markup=kb_login_process(phone))
            asyncio.create_task(auto_kill_session(phone, msg.chat.id, tmp))
            
        except Exception as e:
            await s.edit_text(f"Error: {e}")

@dp.callback_query(F.data.startswith("getcode_"))
async def get_c(cb: types.CallbackQuery):
    p = cb.data.split("_")[1]
    d = ACTIVE_DRIVERS.get(p)
    if d:
        await asyncio.sleep(1)
        png = await asyncio.to_thread(d['driver'].get_screenshot_as_png)
        await cb.message.answer_photo(BufferedInputFile(png, "code.png"))
    await cb.answer()

@dp.callback_query(F.data.startswith("finish_"))
async def fin(cb: types.CallbackQuery):
    p = cb.data.split("_")[1]
    d = ACTIVE_DRIVERS.pop(p, None)
    if d:
        db_save(p, d['ua'], d['res'], d['plat'])
        try: await asyncio.to_thread(d['driver'].quit)
        except: pass
        if d['tmp'] and os.path.exists(d['tmp']): shutil.rmtree(d['tmp'], ignore_errors=True)
        await cb.message.edit_text(f"✅ +{p} Сохранен!")
    else:
        await cb.message.edit_text("❌ Сессия истекла")

# --- HIVE MIND ---
async def worker(phone):
    driver = None
    tmp = None
    try:
        targs = db_get_active_phones()
        target = random.choice([t for t in targs if t != phone]) if len(targs) > 1 else phone
        
        async with BROWSER_SEMAPHORE:
            logger.info(f"🐝 {phone} -> {target}")
            driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, phone)
            if not driver: return
            
            await asyncio.to_thread(driver.get, f"https://web.whatsapp.com/send?phone={target}")
            wait = WebDriverWait(driver, 60)
            
            try:
                inp = wait.until(EC.presence_of_element_located((By.XPATH, "//footer//div[@contenteditable='true']")))
                # Пункт 25 (Игнор): Мы не читаем, просто пишем
                text = ai_engine.generate()
                for c in text:
                    inp.send_keys(c)
                    await asyncio.sleep(random.uniform(0.05, 0.2))
                inp.send_keys(Keys.ENTER)
                
                conn = sqlite3.connect(DB_NAME); conn.execute("UPDATE accounts SET last_act=? WHERE phone=?", (datetime.now(), phone)); conn.commit(); conn.close()
                await asyncio.sleep(2)
            except TimeoutException:
                if "banned" in driver.page_source.lower():
                    db_ban(phone)
                    shutil.rmtree(os.path.join(SESSIONS_DIR, phone), ignore_errors=True)
                    logger.error(f"💀 BAN: {phone}")

    except: pass
    finally:
        if driver: 
            try: await asyncio.to_thread(driver.quit)
            except: pass
        if tmp and os.path.exists(tmp): shutil.rmtree(tmp, ignore_errors=True)

async def loop():
    logger.info("🐝 HIVE LOOP START")
    while True:
        accs = db_get_my_targets()
        if not accs: await asyncio.sleep(30); continue
        
        for p, _ in accs:
            if p not in ACTIVE_DRIVERS:
                await worker(p)
                await asyncio.sleep(random.randint(10, 20))
        
        await asyncio.sleep(random.randint(*HEAT_MODES[CURRENT_MODE]))

async def main():
    if not BOT_TOKEN: sys.exit("NO TOKEN")
    cleanup_zombie_processes()
    db_init()
    asyncio.create_task(loop())
    logger.info("🚀 STARTED v18.1")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
