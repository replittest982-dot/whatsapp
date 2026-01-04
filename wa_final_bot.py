import sys
import asyncio
import os
import logging
import random
import psutil
import shutil
import aiosqlite 
from datetime import datetime

# 1. 🔥 UVLOOP (Оптимизация ядра)
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
# ⚙️ КОНФИГУРАЦИЯ v24.0
# ==========================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))

if not BOT_TOKEN:
    sys.exit("❌ FATAL: Нет токена!")

DB_NAME = 'imperator_obelisk_v24.db'
SESSIONS_DIR = os.path.abspath("./sessions")
TMP_BASE = os.path.abspath("./tmp_chrome_data")

# Лимит 2 браузера (Оптимизация RAM)
BROWSER_SEMAPHORE = asyncio.Semaphore(2)

logging.basicConfig(level=logging.INFO, format='%(asctime)s | v24 | %(levelname)s | %(message)s')
logger = logging.getLogger("Imperator")
fake = Faker('ru_RU')

for d in [SESSIONS_DIR, TMP_BASE]:
    if not os.path.exists(d): os.makedirs(d)

ACTIVE_DRIVERS = {} # Глобальное хранилище

DEVICES = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36", "res": "1920,1080", "plat": "Win32"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "res": "1440,900", "plat": "MacIntel"}
]

class BotStates(StatesGroup):
    waiting_phone = State()

# ==========================================
# 🗄️ БАЗА ДАННЫХ (Async Fixes)
# ==========================================
async def db_init():
    async with aiosqlite.connect(DB_NAME) as db:
        # ✅ ФИКС: Добавили ban_date в схему
        await db.execute("""CREATE TABLE IF NOT EXISTS accounts 
                            (phone TEXT PRIMARY KEY, status TEXT, ua TEXT, res TEXT, plat TEXT, 
                            last_act DATETIME, created_at DATETIME, ban_date DATETIME)""")
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
        # ✅ ФИКС: Добавили NULL для ban_date
        await db.execute("INSERT INTO accounts VALUES (?, 'active', ?, ?, ?, ?, ?, NULL) ON CONFLICT(phone) DO UPDATE SET status='active', last_act=excluded.last_act", 
                         (phone, ua, res, plat, now, now))
        await db.commit()

# ==========================================
# 🌐 SELENIUM CORE
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
    
    # 🚨 CRITICAL FLAGS (ANTI-CRASH)
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-images")
    options.add_argument("--blink-settings=imagesEnabled=false")
    options.add_argument("--memory-pressure-off")
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
# 🤖 BOT & UI
# ==========================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ ДОБАВИТЬ", callback_data="add_acc")],
        [InlineKeyboardButton(text="📊 СТАТИСТИКА", callback_data="stats")]
    ])

def kb_manual_control(phone):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 1. ЧЕК", callback_data=f"man_1_{phone}")],
        [InlineKeyboardButton(text="🔗 2. ВХОД", callback_data=f"man_2_{phone}")],
        [InlineKeyboardButton(text="⌨️ 3. НОМЕР", callback_data=f"man_3_{phone}")],
        [InlineKeyboardButton(text="➡️ 4. NEXT", callback_data=f"man_4_{phone}")],
        [InlineKeyboardButton(text="✅ 5. СОХРАНИТЬ", callback_data=f"man_5_{phone}")],
        [InlineKeyboardButton(text="🗑 ОТМЕНА", callback_data=f"man_cancel_{phone}")]
    ])

async def kill_timer(phone, chat_id, timeout=300):
    """Убийца зомби-процессов"""
    await asyncio.sleep(timeout)
    if phone in ACTIVE_DRIVERS:
        d = ACTIVE_DRIVERS.pop(phone)
        try: await asyncio.to_thread(d['driver'].quit)
        except: pass
        shutil.rmtree(d['tmp'], ignore_errors=True)
        try: await bot.send_message(chat_id, f"⏰ Время вышло. Сессия +{phone} закрыта.")
        except: pass

# --- HANDLERS ---
@dp.message(Command("start"))
async def start(msg: types.Message):
    await db_init()
    # ✅ ФИКС: kb_main() вызов
    await msg.answer("🔱 **OBELISK v24**", reply_markup=kb_main())

@dp.message(Command("stats"))
async def stats_cmd(msg: types.Message):
    act = await db_get_active()
    await msg.answer(f"📊 Активных: {len(act)}\n💻 RAM: {get_sys_status()}\n🏎 Драйверов: {len(ACTIVE_DRIVERS)}")

@dp.callback_query(F.data == "stats")
async def stats_cb(cb: types.CallbackQuery):
    await stats_cmd(cb.message); await cb.answer()

# --- ADD ACCOUNT FLOW ---
@dp.callback_query(F.data == "add_acc")
async def add_start(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("📞 Введи номер (только цифры):")
    await state.set_state(BotStates.waiting_phone)

@dp.message(BotStates.waiting_phone)
async def add_phone(msg: types.Message, state: FSMContext):
    if not msg.text.isdigit(): return await msg.answer("❌ Только цифры!")
    phone = msg.text
    await state.clear()
    
    status = await msg.answer(f"🚀 Запускаю Chrome для +{phone}...")
    
    async with BROWSER_SEMAPHORE:
        driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, phone)
        if not driver: return await status.edit_text("💥 Chrome Crash! Попробуй позже.")
        
        ACTIVE_DRIVERS[phone] = {"driver": driver, "ua": ua, "res": res, "plat": plat, "tmp": tmp}
        
        try:
            await asyncio.to_thread(driver.get, "https://web.whatsapp.com/?lang=en")
        except: pass 
        
        await status.edit_text(
            f"✅ **Пульт готов!**\n📱 +{phone}\n💻 {plat}\n\n👇 Жми кнопки по порядку:",
            reply_markup=kb_manual_control(phone)
        )
        asyncio.create_task(kill_timer(phone, msg.chat.id, 300))

# 🔥 MANUAL CONTROL (UPDATED)
@dp.callback_query(lambda c: c.data and c.data.startswith("man_"))
async def manual_control(cb: types.CallbackQuery):
    parts = cb.data.split("_")
    if len(parts) < 3: return await cb.answer("❌ Error data")
    
    action, phone = parts[1], parts[2]
    
    if phone not in ACTIVE_DRIVERS: 
        return await cb.answer("💥 Сессия мертва!", show_alert=True)
    
    d = ACTIVE_DRIVERS[phone]
    drv = d['driver']
    
    try:
        match action:
            case "1": # ЧЕК
                png = await asyncio.to_thread(drv.get_screenshot_as_png)
                await cb.message.answer_photo(BufferedInputFile(png, "screen.png"), caption="📸 Check")
                await cb.answer()
                
            case "2": # ВХОД
                drv.execute_script("document.querySelector('[data-testid=\"link-phone\"]').click() || document.querySelector('span[role=\"button\"][title*=\"Link\"]').click()")
                await cb.answer("✅ Clicked Link!")
                
            case "3": # НОМЕР
                try:
                    inp = drv.find_element(By.CSS_SELECTOR, "input[type='tel'], input[type='text']")
                    inp.clear()
                    for digit in f"+{phone}": 
                        inp.send_keys(digit); await asyncio.sleep(0.05)
                    await cb.answer("✅ Typed Number!")
                except: await cb.answer("❌ Input not found", show_alert=True)
                
            case "4": # NEXT
                try:
                    drv.find_element(By.XPATH, "//*[text()='Next' or @data-testid='next-button']").click()
                    await cb.answer("✅ Clicked Next!")
                    await asyncio.sleep(3)
                    png = await asyncio.to_thread(drv.get_screenshot_as_png)
                    await cb.message.answer_photo(BufferedInputFile(png, "code.png"), caption="📱 **ВОДІ КОД!**")
                except: await cb.answer("❌ Next btn not found", show_alert=True)
                
            case "5": # СОХРАНИТЬ (Fix applied)
                # ✅ ФИКС: Безопасное извлечение и сохранение
                session = ACTIVE_DRIVERS.pop(phone) 
                await db_save(phone, session['ua'], session['res'], session['plat'])
                
                try: await asyncio.to_thread(session['driver'].quit)
                except: pass
                shutil.rmtree(session['tmp'], ignore_errors=True)
                
                await cb.message.edit_text(f"🎉 **+{phone} СОХРАНЁН В СЕТКУ!**")
                
            case "cancel":
                d = ACTIVE_DRIVERS.pop(phone)
                try: await asyncio.to_thread(d['driver'].quit)
                except: pass
                shutil.rmtree(d['tmp'], ignore_errors=True)
                await cb.message.edit_text("🗑 Сессия удалена")
                
    except Exception as e:
        await cb.answer(f"Error: {str(e)[:50]}", show_alert=True)

# --- WORKER (Fix applied) ---
async def worker(phone):
    driver = None; tmp = None # ✅ ФИКС: Инициализация переменных
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
            inp = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "footer div[contenteditable='true']")))
            inp.send_keys(f"Hello {random.randint(1,999)}")
            inp.send_keys(Keys.ENTER)
            logger.info(f"✅ {phone} -> {t}")
            
    except Exception as e:
        logger.error(f"Worker error: {e}")
    finally: 
        # ✅ ФИКС: Гарантированная очистка
        if driver: 
            try: await asyncio.to_thread(driver.quit)
            except: pass
        if tmp and os.path.exists(tmp): 
            shutil.rmtree(tmp, ignore_errors=True)

async def loop():
    while True:
        accs = await db_get_active()
        for p in accs[:2]: # Макс 2 воркера
            if p not in ACTIVE_DRIVERS:
                asyncio.create_task(worker(p))
        await asyncio.sleep(120)

async def main():
    await db_init()
    asyncio.create_task(loop())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__": asyncio.run(main())
