import sys
import asyncio
import os
import logging
import random
import psutil
import shutil
import aiosqlite 
from datetime import datetime

# 🚀 1. UVLOOP (Turbo Core)
if sys.platform != 'win32':
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except ImportError: pass

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
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys

# ==========================================
# ⚙️ КОНФИГУРАЦИЯ v25.0 ULTIMATE
# ==========================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except:
    ADMIN_ID = 0

if not BOT_TOKEN:
    sys.exit("❌ FATAL: Нет токена!")

DB_NAME = 'imperator_ultimate_v25.db'
SESSIONS_DIR = os.path.abspath("./sessions")
TMP_BASE = os.path.abspath("./tmp_chrome_data")

# Лимит 2 браузера (Безопасно для 10GB RAM)
BROWSER_SEMAPHORE = asyncio.Semaphore(2)

# Настройки Hive Mind
HEAT_MODES = {
    "TURBO": (15, 30),
    "MEDIUM": (60, 180),
    "SLOW": (300, 600)
}
CURRENT_MODE = "MEDIUM"

logging.basicConfig(level=logging.INFO, format='%(asctime)s | v25 | %(levelname)s | %(message)s')
logger = logging.getLogger("Imperator")
fake = Faker('ru_RU')

for d in [SESSIONS_DIR, TMP_BASE]:
    if not os.path.exists(d): os.makedirs(d)

ACTIVE_DRIVERS = {} # Глобальный реестр драйверов

DEVICES = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36", "res": "1920,1080", "plat": "Win32"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "res": "1440,900", "plat": "MacIntel"}
]

class BotStates(StatesGroup):
    waiting_phone_auto = State()
    waiting_phone_manual = State()
    waiting_vip_id = State()

# ==========================================
# 🧠 AI ENGINE (v18 Logic)
# ==========================================
class DialogueAI:
    def __init__(self):
        self.greetings = ["Привет", "Ку", "Здарова", "Хай", "Салам"]
        self.questions = ["Как дела?", "Ты где?", "Скинь инфу", "На связи?", "Чего молчишь?"]
        self.answers = ["Норм", "Работаю", "Ок", "Принял", "Скоро буду", "На месте"]
    
    def generate(self):
        if random.random() < 0.2: return random.choice(self.answers)
        return f"{random.choice(self.greetings)}. {random.choice(self.questions)}"

ai_engine = DialogueAI()

# ==========================================
# 🗄️ БАЗА ДАННЫХ (Async aiosqlite)
# ==========================================
async def db_init():
    async with aiosqlite.connect(DB_NAME) as db:
        # ✅ SCHEMA: 8 полей + индексы
        await db.execute("""CREATE TABLE IF NOT EXISTS accounts 
                            (phone TEXT PRIMARY KEY, status TEXT, ua TEXT, res TEXT, plat TEXT, 
                            last_act DATETIME, created_at DATETIME, ban_date DATETIME)""")
        
        await db.execute("""CREATE TABLE IF NOT EXISTS whitelist 
                            (user_id INTEGER PRIMARY KEY, approved INTEGER DEFAULT 0, is_unlimited INTEGER DEFAULT 0)""")
        
        await db.execute("CREATE INDEX IF NOT EXISTS idx_status ON accounts(status)")
        await db.commit()

async def db_get_active():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT phone FROM accounts WHERE status='active'") as cursor:
            res = await cursor.fetchall()
            return [r[0] for r in res]

async def db_save(phone, ua, res, plat):
    async with aiosqlite.connect(DB_NAME) as db:
        now = datetime.now()
        # ✅ ban_date = NULL
        await db.execute("""INSERT INTO accounts VALUES (?, 'active', ?, ?, ?, ?, ?, NULL) 
                            ON CONFLICT(phone) DO UPDATE SET status='active', last_act=excluded.last_act""", 
                         (phone, ua, res, plat, now, now))
        await db.commit()

async def db_check_perm(user_id):
    if user_id == ADMIN_ID: return (1, 1)
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT approved, is_unlimited FROM whitelist WHERE user_id=?", (user_id,)) as cursor:
            res = await cursor.fetchone()
            return res if res else (0, 0)

# ==========================================
# 🌐 SELENIUM (ANTI-CRASH v24)
# ==========================================
def get_sys_status():
    mem = psutil.virtual_memory()
    return f"RAM: {mem.available//1024//1024}MB | CPU: {psutil.cpu_percent()}%"

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
    
    # 🔥 15+ ANTI-CRASH FLAGS
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage") # Самое важное для Docker
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-images")
    options.add_argument("--blink-settings=imagesEnabled=false")
    options.add_argument("--memory-pressure-off")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-breakpad")
    options.add_argument("--disable-component-update")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    
    # ⚠️ --single-process УБРАН, так как он крашит Chrome 143+
    
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

async def kill_timer(phone, chat_id, timeout=300):
    """Очистка сессии по таймеру"""
    await asyncio.sleep(timeout)
    if phone in ACTIVE_DRIVERS:
        d = ACTIVE_DRIVERS.pop(phone)
        try: await asyncio.to_thread(d['driver'].quit)
        except: pass
        shutil.rmtree(d['tmp'], ignore_errors=True)
        try: await bot.send_message(chat_id, f"⏰ Таймер {timeout}с истек. Сессия +{phone} сброшена.")
        except: pass

# ==========================================
# 🤖 BOT UI & HANDLERS
# ==========================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- KEYBOARDS ---
def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 АВТО ДОБАВЛЕНИЕ", callback_data="add_auto"), 
         InlineKeyboardButton(text="🎮 РУЧНОЙ РЕЖИМ", callback_data="add_manual")],
        [InlineKeyboardButton(text="📊 СТАТИСТИКА", callback_data="stats"),
         InlineKeyboardButton(text="⚙️ НАСТРОЙКИ", callback_data="settings")],
        [InlineKeyboardButton(text="👑 VIP / WHITELIST", callback_data="vip")]
    ])

def kb_manual_control(phone):
    """Пульт v24"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 1. ЧЕК", callback_data=f"man_1_{phone}")],
        [InlineKeyboardButton(text="🔗 2. ВХОД", callback_data=f"man_2_{phone}")],
        [InlineKeyboardButton(text="⌨️ 3. НОМЕР", callback_data=f"man_3_{phone}")],
        [InlineKeyboardButton(text="➡️ 4. NEXT", callback_data=f"man_4_{phone}")],
        [InlineKeyboardButton(text="✅ 5. СОХРАНИТЬ", callback_data=f"man_5_{phone}")],
        [InlineKeyboardButton(text="🗑 ОТМЕНА", callback_data=f"man_cancel_{phone}")]
    ])

# --- START & MENU ---
@dp.message(Command("start"))
async def start(msg: types.Message):
    await db_init()
    ok, vip = await db_check_perm(msg.from_user.id)
    if not ok:
        # Авто-заявка
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("INSERT OR IGNORE INTO whitelist (user_id) VALUES (?)", (msg.from_user.id,))
            await db.commit()
        if ADMIN_ID: await bot.send_message(ADMIN_ID, f"Заявка: {msg.from_user.id}")
        return await msg.answer("🔒 Доступ ограничен. Жди одобрения.")
    
    st = "👑 VIP" if vip else "👤 User"
    await msg.answer(f"🔱 **IMPERATOR v25.0 ULTIMATE**\nСтатус: {st}", reply_markup=kb_main())

@dp.callback_query(F.data == "stats")
async def stats(cb: types.CallbackQuery):
    act = await db_get_active()
    await cb.answer(f"📱 Активных: {len(act)}\n{get_sys_status()}\nDrivers: {len(ACTIVE_DRIVERS)}", show_alert=True)

# --- 1. АВТО РЕЖИМ (v18 Logic) ---
@dp.callback_query(F.data == "add_auto")
async def auto_start(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("🤖 **АВТО-РЕЖИМ**\nВведи номер (только цифры):")
    await state.set_state(BotStates.waiting_phone_auto)

@dp.message(BotStates.waiting_phone_auto)
async def auto_flow(msg: types.Message, state: FSMContext):
    phone = "".join(filter(str.isdigit, msg.text))
    await state.clear()
    s = await msg.answer(f"🚀 [AUTO] Запуск +{phone}...")

    async with BROWSER_SEMAPHORE:
        driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, phone)
        if not driver: return await s.edit_text("💥 Chrome Crash.")
        
        ACTIVE_DRIVERS[phone] = {"driver": driver, "ua": ua, "res": res, "plat": plat, "tmp": tmp}
        
        try:
            # 1. Open
            await asyncio.to_thread(driver.get, "https://web.whatsapp.com/?lang=en")
            wait = WebDriverWait(driver, 45)
            
            # 2. Click Link (Auto)
            await s.edit_text("⏳ Ищу кнопку входа...")
            try:
                wait.until(EC.element_to_be_clickable((By.XPATH, "//*[contains(text(), 'Link with phone')]"))).click()
            except:
                driver.execute_script("document.querySelector('[data-testid=\"link-phone\"]').click()")

            # 3. Input & Next (Auto)
            await s.edit_text("⏳ Ввожу номер...")
            inp = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='text']")))
            inp.clear()
            for d in f"+{phone}": inp.send_keys(d); await asyncio.sleep(0.1)
            
            try: driver.find_element(By.XPATH, "//div[text()='Next']").click()
            except: inp.send_keys(Keys.ENTER)

            # 4. Get Code
            await s.edit_text("⏳ Жду код (15с)...")
            await asyncio.sleep(15)
            png = await asyncio.to_thread(driver.get_screenshot_as_png)
            
            # Переход в ручной режим для сохранения (чтобы ввести код)
            await s.delete()
            await msg.answer_photo(
                BufferedInputFile(png, "code.png"),
                caption=f"✅ **КОД ПОЛУЧЕН!**\nВведи код в телефоне и нажми 'СОХРАНИТЬ' на пульте.",
                reply_markup=kb_manual_control(phone) # Отдаем пульт для финала
            )
            asyncio.create_task(kill_timer(phone, msg.chat.id, 120)) # 120 сек на ввод
            
        except Exception as e:
            await s.edit_text(f"❌ Ошибка авто: {e}")
            if phone in ACTIVE_DRIVERS:
                d = ACTIVE_DRIVERS.pop(phone)
                try: await asyncio.to_thread(d['driver'].quit)
                except: pass
                shutil.rmtree(d['tmp'], ignore_errors=True)

# --- 2. РУЧНОЙ РЕЖИМ (v24 Logic) ---
@dp.callback_query(F.data == "add_manual")
async def manual_start(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("🎮 **РУЧНОЙ РЕЖИМ**\nВведи номер (только цифры):")
    await state.set_state(BotStates.waiting_phone_manual)

@dp.message(BotStates.waiting_phone_manual)
async def manual_flow(msg: types.Message, state: FSMContext):
    phone = "".join(filter(str.isdigit, msg.text))
    await state.clear()
    s = await msg.answer(f"🚀 [MANUAL] Запуск +{phone}...")

    async with BROWSER_SEMAPHORE:
        driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, phone)
        if not driver: return await s.edit_text("💥 Chrome Crash.")
        
        ACTIVE_DRIVERS[phone] = {"driver": driver, "ua": ua, "res": res, "plat": plat, "tmp": tmp}
        
        try: await asyncio.to_thread(driver.get, "https://web.whatsapp.com/?lang=en")
        except: pass

        await s.edit_text(
            f"✅ **ПУЛЬТ ГОТОВ**\n📱 +{phone}\nИспользуй кнопки по шагам:",
            reply_markup=kb_manual_control(phone)
        )
        asyncio.create_task(kill_timer(phone, msg.chat.id, 300)) # 5 мин на всё

# --- 🔥 ЕДИНЫЙ КОНТРОЛЛЕР (v24 Unified Handler) ---
@dp.callback_query(lambda c: c.data and c.data.startswith("man_"))
async def manual_control_handler(cb: types.CallbackQuery):
    parts = cb.data.split("_")
    action, phone = parts[1], parts[2]
    
    if phone not in ACTIVE_DRIVERS: 
        return await cb.answer("❌ Сессия мертва (таймаут)", show_alert=True)
    
    d = ACTIVE_DRIVERS[phone]
    drv = d['driver']
    
    try:
        match action:
            case "1": # ЧЕК
                png = await asyncio.to_thread(drv.get_screenshot_as_png)
                await cb.message.answer_photo(BufferedInputFile(png, "screen.png"), caption="📸")
                await cb.answer()
            
            case "2": # ВХОД
                drv.execute_script("document.querySelector('[data-testid=\"link-phone\"]').click() || document.querySelector('span[role=\"button\"][title*=\"Link\"]').click()")
                await cb.answer("✅ Click Link")
            
            case "3": # НОМЕР
                try:
                    inp = drv.find_element(By.CSS_SELECTOR, "input[type='text']")
                    inp.clear()
                    for x in f"+{phone}": inp.send_keys(x); await asyncio.sleep(0.05)
                    await cb.answer("✅ Typed")
                except: await cb.answer("❌ Поле не найдено", show_alert=True)
            
            case "4": # NEXT
                try:
                    drv.find_element(By.XPATH, "//*[text()='Next']").click()
                    await cb.answer("✅ Next Clicked")
                    # Авто-скрин через 3 сек
                    await asyncio.sleep(3)
                    png = await asyncio.to_thread(drv.get_screenshot_as_png)
                    await cb.message.answer_photo(BufferedInputFile(png, "code.png"), caption="✅ **КОД**")
                except: await cb.answer("❌ Кнопка Next не найдена", show_alert=True)
            
            case "5": # СОХРАНИТЬ
                session = ACTIVE_DRIVERS.pop(phone) # Safe pop
                await db_save(phone, session['ua'], session['res'], session['plat'])
                
                try: await asyncio.to_thread(session['driver'].quit)
                except: pass
                shutil.rmtree(session['tmp'], ignore_errors=True)
                
                await cb.message.edit_text(f"🎉 **+{phone} СОХРАНЕН В СЕТЬ!**")
            
            case "cancel":
                session = ACTIVE_DRIVERS.pop(phone)
                try: await asyncio.to_thread(session['driver'].quit)
                except: pass
                shutil.rmtree(session['tmp'], ignore_errors=True)
                await cb.message.edit_text("🗑 Отмена.")

    except Exception as e:
        await cb.answer(f"Err: {str(e)[:50]}", show_alert=True)

# ==========================================
# 🚜 HIVE MIND WORKER (v18 Logic + Safe Cleanup)
# ==========================================
async def worker(phone):
    driver = None; tmp = None
    try:
        targs = await db_get_active()
        if not targs or len(targs) < 2: return
        t = random.choice([x for x in targs if x!=phone])
        
        async with BROWSER_SEMAPHORE:
            driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, phone)
            if not driver: return
            
            try:
                driver.set_page_load_timeout(30)
                await asyncio.to_thread(driver.get, f"https://web.whatsapp.com/send?phone={t}")
            except: driver.execute_script("window.stop();")
            
            wait = WebDriverWait(driver, 40)
            # Ждем поле ввода
            inp = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "footer div[contenteditable='true']")))
            
            text = ai_engine.generate()
            for c in text: inp.send_keys(c); await asyncio.sleep(0.05)
            inp.send_keys(Keys.ENTER)
            
            # Update Last Act
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("UPDATE accounts SET last_act=? WHERE phone=?", (datetime.now(), phone))
                await db.commit()
            
            logger.info(f"✅ {phone} -> {t}: {text}")
            await asyncio.sleep(2)
            
    except Exception as e:
        logger.error(f"Worker {phone} error: {e}")
    finally:
        # ✅ MEMORY LEAK FIX
        if driver: 
            try: await asyncio.to_thread(driver.quit)
            except: pass
        if tmp and os.path.exists(tmp): 
            shutil.rmtree(tmp, ignore_errors=True)

async def loop():
    while True:
        accs = await db_get_active()
        for p in accs:
            if p not in ACTIVE_DRIVERS: # Не трогаем тех, кто сейчас добавляется
                asyncio.create_task(worker(p))
                await asyncio.sleep(random.randint(10, 30)) # Разброс запуска
        
        await asyncio.sleep(random.randint(*HEAT_MODES[CURRENT_MODE]))

async def main():
    await db_init()
    asyncio.create_task(loop())
    logger.info("🚀 LEGION v25.0 ULTIMATE STARTED")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__": asyncio.run(main())
