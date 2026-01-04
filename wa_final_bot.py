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

# --- УСКОРЕНИЕ ---
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
# ⚙️ КОНФИГУРАЦИЯ v21.0 (MANUAL MODE)
# ==========================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except:
    ADMIN_ID = 0

if not BOT_TOKEN:
    sys.exit("❌ FATAL: Нет токена!")

REQUIRED_CHANNEL_ID = "@WhatsAppstatpro" 
REQUIRED_CHANNEL_URL = "https://t.me/WhatsAppstatpro"

INSTANCE_ID = int(os.getenv("INSTANCE_ID", 1))
TOTAL_INSTANCES = int(os.getenv("TOTAL_INSTANCES", 1))

# Лимит 2 браузера (ОПТИМИЗАЦИЯ RAM)
BROWSER_SEMAPHORE = asyncio.Semaphore(2)

DB_NAME = 'imperator_manual_v21.db'
SESSIONS_DIR = os.path.abspath("./sessions")
TMP_BASE = os.path.abspath("./tmp_chrome_data")

HEAT_MODES = {"TURBO": (15, 30), "MEDIUM": (60, 180), "SLOW": (300, 600)}
CURRENT_MODE = "MEDIUM"

logging.basicConfig(level=logging.INFO, format='%(asctime)s | MANUAL | %(levelname)s | %(message)s')
logger = logging.getLogger("Imperator")
fake = Faker('ru_RU')

for d in [SESSIONS_DIR, TMP_BASE]:
    if not os.path.exists(d): os.makedirs(d)

DEVICES = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36", "res": "1920,1080", "plat": "Win32"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "res": "1440,900", "plat": "MacIntel"}
]

# Глобальное хранилище активных драйверов
ACTIVE_DRIVERS = {}

class BotStates(StatesGroup):
    waiting_phone = State()
    waiting_vip_id = State()

# ==========================================
# 🧠 AI & UTILS
# ==========================================
class DialogueAI:
    def generate(self):
        greetings = ["Привет", "Ку", "Здарова", "Хай", "Салам"]
        questions = ["Как дела?", "Ты где?", "Скинь инфу", "На связи?", "Чего молчишь?"]
        answers = ["Норм", "Работаю", "Ок", "Принял", "Скоро буду", "На месте"]
        if random.random() < 0.2: return random.choice(answers)
        return f"{random.choice(greetings)}. {random.choice(questions)}"

ai_engine = DialogueAI()

def cleanup_zombie_sync():
    """Чистка на старте"""
    for p in psutil.process_iter(['name']):
        if p.info['name'] in ['chrome', 'chromedriver']:
            try: p.kill()
            except: pass
    if os.path.exists(TMP_BASE):
        try: shutil.rmtree(TMP_BASE)
        except: pass
        os.makedirs(TMP_BASE)

async def aggressive_cleanup_loop():
    """Фоновая очистка памяти"""
    while True:
        try:
            await asyncio.sleep(1800)
            mem = psutil.virtual_memory()
            if mem.available < 500 * 1024 * 1024: # Если меньше 500МБ свободно
                logger.warning("🧹 LOW RAM: Чистка...")
                for p in psutil.process_iter(['name']):
                    if p.info['name'] in ['chrome', 'chromedriver']:
                        try: p.kill()
                        except: pass
        except: pass

def get_sys_status():
    mem = psutil.virtual_memory()
    return f"RAM: {mem.available//1024//1024}MB | CPU: {psutil.cpu_percent()}%"

# ==========================================
# 🗄️ DATABASE
# ==========================================
async def db_init():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS accounts (phone TEXT PRIMARY KEY, status TEXT, ua TEXT, res TEXT, plat TEXT, last_act DATETIME, created_at DATETIME, ban_date DATETIME)")
        await db.execute("CREATE TABLE IF NOT EXISTS whitelist (user_id INTEGER PRIMARY KEY, approved INTEGER DEFAULT 0, is_unlimited INTEGER DEFAULT 0)")
        await db.commit()

async def db_get_active():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT phone FROM accounts WHERE status='active'") as cursor:
            res = await cursor.fetchall()
            return [r[0] for r in res]

async def db_save(phone, ua, res, plat):
    async with aiosqlite.connect(DB_NAME) as db:
        now = datetime.now()
        await db.execute("INSERT INTO accounts VALUES (?, 'active', ?, ?, ?, ?, ?, NULL) ON CONFLICT(phone) DO UPDATE SET status='active', last_act=excluded.last_act", (phone, ua, res, plat, now, now))
        await db.commit()

async def db_check_perm(user_id):
    if user_id == ADMIN_ID: return (1, 1)
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT approved, is_unlimited FROM whitelist WHERE user_id=?", (user_id,)) as cursor:
            res = await cursor.fetchone()
            return res if res else (0, 0)

# ==========================================
# 🌐 SELENIUM (MANUAL OPTIMIZED)
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
    
    # 🚨 CRITICAL FLAGS
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage") 
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-images") # Экономия памяти
    options.add_argument("--blink-settings=imagesEnabled=false")
    
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

# --- KEYBOARDS ---
def kb_main(uid):
    btns = [
        [InlineKeyboardButton(text="➕ ДОБАВИТЬ АККАУНТ", callback_data="add_acc")],
        [InlineKeyboardButton(text="📊 СТАТИСТИКА", callback_data="stats")],
        [InlineKeyboardButton(text="⚙️ НАСТРОЙКИ", callback_data="settings")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_manual_control(phone):
    """Тот самый пульт управления из скриншота"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 1. ЧЕК (Скрин)", callback_data=f"man_1_{phone}")],
        [InlineKeyboardButton(text="🔗 2. КЛИК 'ВХОД'", callback_data=f"man_2_{phone}")],
        [InlineKeyboardButton(text="⌨️ 3. ВВОД НОМЕРА", callback_data=f"man_3_{phone}")],
        [InlineKeyboardButton(text="➡️ 4. НАЖАТЬ 'ДАЛЕЕ'", callback_data=f"man_4_{phone}")],
        [InlineKeyboardButton(text="✅ 5. Я ВОШЕЛ (Сохранить)", callback_data=f"man_5_{phone}")],
        [InlineKeyboardButton(text="🗑 УДАЛИТЬ СЕССИЮ", callback_data=f"man_cancel_{phone}")]
    ])

# --- HANDLERS ---
@dp.message(Command("start"))
async def start(msg: types.Message):
    await db_init()
    # Простая проверка доступа
    ok, _ = await db_check_perm(msg.from_user.id)
    if not ok and msg.from_user.id != ADMIN_ID:
         # Авто-регистрация (можно убрать)
         async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("INSERT OR IGNORE INTO whitelist (user_id) VALUES (?)", (msg.from_user.id,))
            await db.commit()
    
    await msg.answer("🔱 **Imperator v21.0 MANUAL**", reply_markup=kb_main(msg.from_user.id))

@dp.callback_query(F.data == "stats")
async def stat(cb: types.CallbackQuery): 
    act = await db_get_active()
    await cb.answer(f"📱 Активных: {len(act)}\n{get_sys_status()}", show_alert=True)

# --- MANUAL ADD LOGIC ---
async def kill_session_timer(phone, delay=180):
    """Убивает браузер, если юзер забыл (Оптимизация памяти)"""
    await asyncio.sleep(delay)
    if phone in ACTIVE_DRIVERS:
        d = ACTIVE_DRIVERS.pop(phone, None)
        if d:
            try: await asyncio.to_thread(d['driver'].quit)
            except: pass
            if os.path.exists(d['tmp']): shutil.rmtree(d['tmp'], ignore_errors=True)

@dp.callback_query(F.data == "add_acc")
async def add_start(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("📞 Введи номер телефона (только цифры):")
    await state.set_state(BotStates.waiting_phone)

@dp.message(BotStates.waiting_phone)
async def add_phone_start(msg: types.Message, state: FSMContext):
    phone = "".join(filter(str.isdigit, msg.text))
    if not phone: return await msg.answer("❌ Номер некорректен.")
    await state.clear()
    
    status_msg = await msg.answer(f"🚀 Запускаю браузер для +{phone}...\n⏳ Жди загрузки...")
    
    async with BROWSER_SEMAPHORE: # Ждем слот
        driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, phone)
        
        if not driver:
            return await status_msg.edit_text("❌ Ошибка драйвера (Crash). Попробуй позже.")
        
        ACTIVE_DRIVERS[phone] = {"driver": driver, "ua": ua, "res": res, "plat": plat, "tmp": tmp}
        
        # Открываем WA на английском
        await asyncio.to_thread(driver.get, "https://web.whatsapp.com/?lang=en")
        asyncio.create_task(kill_session_timer(phone, 240)) # 4 минуты на всё
        
        await status_msg.edit_text(
            f"✅ **Браузер запущен!**\n📱 Номер: `{phone}`\n🖥 Plat: {plat}\n\n👇 **Используй кнопки по порядку:**",
            reply_markup=kb_manual_control(phone)
        )

# 1. ЧЕК (Скриншот)
@dp.callback_query(F.data.startswith("man_1_"))
async def m_check(cb: types.CallbackQuery):
    p = cb.data.split("_")[2]
    if p not in ACTIVE_DRIVERS: return await cb.answer("Сессия истекла", show_alert=True)
    
    try:
        png = await asyncio.to_thread(ACTIVE_DRIVERS[p]['driver'].get_screenshot_as_png)
        await cb.message.answer_photo(BufferedInputFile(png, "scr.png"), caption="📸 Текущий экран")
    except Exception as e:
        await cb.answer(f"Ошибка: {e}", show_alert=True)
    await cb.answer()

# 2. КЛИК 'ВХОД'
@dp.callback_query(F.data.startswith("man_2_"))
async def m_link(cb: types.CallbackQuery):
    p = cb.data.split("_")[2]
    if p not in ACTIVE_DRIVERS: return await cb.answer("Сессия истекла", show_alert=True)
    
    try:
        drv = ACTIVE_DRIVERS[p]['driver']
        # Пробуем JS клик (самый надежный)
        drv.execute_script("var b=document.querySelector('span[role=\"button\"]'); if(b && b.innerText.includes('Link')) b.click();")
        try:
            # Запасной вариант через XPath
            el = drv.find_element(By.XPATH, "//*[contains(text(), 'Link with phone')]")
            el.click()
        except: pass
        await cb.answer("✅ Клик отправлен. Жми ЧЕК, чтобы проверить.")
    except Exception as e:
        await cb.answer(f"Ошибка клика: {e}", show_alert=True)

# 3. ВВОД НОМЕРА
@dp.callback_query(F.data.startswith("man_3_"))
async def m_input(cb: types.CallbackQuery):
    p = cb.data.split("_")[2]
    if p not in ACTIVE_DRIVERS: return await cb.answer("Сессия истекла", show_alert=True)
    
    try:
        drv = ACTIVE_DRIVERS[p]['driver']
        inp = drv.find_element(By.CSS_SELECTOR, "input[type='text']")
        inp.click(); inp.clear()
        # Ввод по цифре
        for d in f"+{p}":
            inp.send_keys(d)
            await asyncio.sleep(0.05)
        await cb.answer("✅ Номер введен!")
    except Exception as e:
        await cb.answer(f"Не нашел поле ввода! Сделай ЧЕК.", show_alert=True)

# 4. НАЖАТЬ 'ДАЛЕЕ'
@dp.callback_query(F.data.startswith("man_4_"))
async def m_next(cb: types.CallbackQuery):
    p = cb.data.split("_")[2]
    if p not in ACTIVE_DRIVERS: return await cb.answer("Сессия истекла", show_alert=True)
    
    try:
        drv = ACTIVE_DRIVERS[p]['driver']
        try:
            btn = drv.find_element(By.XPATH, "//div[text()='Next']")
            btn.click()
        except:
            drv.find_element(By.CSS_SELECTOR, "input[type='text']").send_keys(Keys.ENTER)
        
        await cb.answer("✅ Нажал NEXT. Жди код и делай ЧЕК.")
        # Автоматически прислать скрин через 5 сек (удобство)
        await asyncio.sleep(5)
        png = await asyncio.to_thread(drv.get_screenshot_as_png)
        await cb.message.answer_photo(BufferedInputFile(png, "code.png"), caption="✅ Если видишь код - вводи в телефон!")
    except Exception as e:
        await cb.answer(f"Ошибка нажатия: {e}", show_alert=True)

# 5. Я ВОШЕЛ (Сохранить)
@dp.callback_query(F.data.startswith("man_5_"))
async def m_save(cb: types.CallbackQuery):
    p = cb.data.split("_")[2]
    d = ACTIVE_DRIVERS.pop(p, None)
    if d:
        await db_save(p, d['ua'], d['res'], d['plat'])
        try: await asyncio.to_thread(d['driver'].quit)
        except: pass
        if os.path.exists(d['tmp']): shutil.rmtree(d['tmp'], ignore_errors=True)
        await cb.message.edit_text(f"🎉 Аккаунт +{p} сохранен и добавлен в сетку!")
    else:
        await cb.answer("Сессия не найдена", show_alert=True)

# ОТМЕНА
@dp.callback_query(F.data.startswith("man_cancel_"))
async def m_cancel(cb: types.CallbackQuery):
    p = cb.data.split("_")[2]
    d = ACTIVE_DRIVERS.pop(p, None)
    if d:
        try: await asyncio.to_thread(d['driver'].quit)
        except: pass
        if os.path.exists(d['tmp']): shutil.rmtree(d['tmp'], ignore_errors=True)
    await cb.message.edit_text("❌ Сессия удалена.")

# ==========================================
# 🚜 HIVE MIND
# ==========================================
async def worker(phone):
    # Старая добрая логика грева
    driver = None; tmp = None
    try:
        targs = await db_get_active()
        if not targs: return
        t = random.choice([x for x in targs if x!=phone]) if len(targs)>1 else phone
        
        async with BROWSER_SEMAPHORE:
            driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, phone)
            if not driver: return
            
            try:
                driver.set_page_load_timeout(30)
                await asyncio.to_thread(driver.get, f"https://web.whatsapp.com/send?phone={t}")
            except TimeoutException: driver.execute_script("window.stop();")
            
            wait = WebDriverWait(driver, 40)
            try:
                inp = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "footer div[contenteditable='true']")))
                txt = ai_engine.generate()
                for c in txt:
                    inp.send_keys(c); await asyncio.sleep(0.1)
                inp.send_keys(Keys.ENTER)
                logger.info(f"✅ {phone}->{t}: {txt}")
                await asyncio.sleep(2)
            except: pass
    except: pass
    finally:
        if driver: 
            try: await asyncio.to_thread(driver.quit)
            except: pass
        if tmp and os.path.exists(tmp): shutil.rmtree(tmp, ignore_errors=True)

async def loop():
    while True:
        accs = await db_get_active()
        for p in accs:
            if p not in ACTIVE_DRIVERS: # Не мешаем ручному добавлению
                asyncio.create_task(worker(p))
                await asyncio.sleep(10)
        await asyncio.sleep(random.randint(60, 180))

async def main():
    cleanup_zombie_sync()
    await db_init()
    asyncio.create_task(loop())
    asyncio.create_task(aggressive_cleanup_loop())
    logger.info("🚀 LEGION v21.0 MANUAL STARTED")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__": asyncio.run(main())
