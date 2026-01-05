import sys
import asyncio
import os
import logging
import random
import psutil
import shutil
import aiosqlite 
import time
from datetime import datetime, timedelta

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
from selenium.webdriver.common.action_chains import ActionChains

# ==========================================
# ⚙️ КОНФИГУРАЦИЯ v26.0 WARLORD
# ==========================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except:
    ADMIN_ID = 0

if not BOT_TOKEN:
    sys.exit("❌ FATAL: Нет токена! Проверь переменные окружения.")

DB_NAME = 'imperator_warlord_v26.db'
SESSIONS_DIR = os.path.abspath("./sessions")
TMP_BASE = os.path.abspath("./tmp_chrome_data")

# Лимит 2 браузера (для 10GB RAM)
BROWSER_SEMAPHORE = asyncio.Semaphore(2)

# Настройки Туториала (Лимиты)
MAX_MSGS_PER_HOUR = 15 # "В час отписывайте не более 15 мамонтам"
SPY_MODE_DURATION = 120 # Секунды сидения в шпионе (2 мин)

logging.basicConfig(level=logging.INFO, format='%(asctime)s | v26 | %(levelname)s | %(message)s')
logger = logging.getLogger("Imperator")
fake = Faker('ru_RU')

for d in [SESSIONS_DIR, TMP_BASE]:
    if not os.path.exists(d): os.makedirs(d)

ACTIVE_DRIVERS = {} 

DEVICES = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36", "res": "1920,1080", "plat": "Win32"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "res": "1440,900", "plat": "MacIntel"}
]

class BotStates(StatesGroup):
    waiting_phone_auto = State()
    waiting_phone_manual = State()

# ==========================================
# 🧠 AI ENGINE
# ==========================================
class DialogueAI:
    def __init__(self):
        self.greetings = ["Привет", "Ку", "Здарова", "Хай", "Салам", "Доброго"]
        self.questions = ["Как дела?", "Ты где?", "Скинь инфу", "На связи?", "Чего молчишь?", "Есть минутка?"]
        self.answers = ["Норм", "Работаю", "Ок", "Принял", "Скоро буду", "На месте", "Позже наберу"]
    
    def generate(self):
        # Имитация живого общения (не всегда вопросы)
        dice = random.random()
        if dice < 0.3: return random.choice(self.answers)
        if dice < 0.6: return f"{random.choice(self.greetings)}. {random.choice(self.questions)}"
        return fake.sentence(nb_words=random.randint(2, 5))

ai_engine = DialogueAI()

# ==========================================
# 🗄️ БАЗА ДАННЫХ (Async)
# ==========================================
async def db_init():
    async with aiosqlite.connect(DB_NAME) as db:
        # Схема с учетом статистики сообщений
        await db.execute("""CREATE TABLE IF NOT EXISTS accounts 
                            (phone TEXT PRIMARY KEY, status TEXT, ua TEXT, res TEXT, plat TEXT, 
                            last_act DATETIME, created_at DATETIME, ban_date DATETIME, 
                            msgs_hour INTEGER DEFAULT 0, last_msg_time DATETIME)""")
        
        await db.execute("""CREATE TABLE IF NOT EXISTS whitelist 
                            (user_id INTEGER PRIMARY KEY, approved INTEGER DEFAULT 0, username TEXT)""")
        
        await db.execute("CREATE INDEX IF NOT EXISTS idx_status ON accounts(status)")
        await db.commit()

async def db_get_active():
    async with aiosqlite.connect(DB_NAME) as db:
        # Берем только тех, кто не в бане и не в "отлеге" (status='active')
        async with db.execute("SELECT phone FROM accounts WHERE status='active'") as cursor:
            res = await cursor.fetchall()
            return [r[0] for r in res]

async def db_check_perm(user_id, username=""):
    if user_id == ADMIN_ID: return (1, 1) # Admin always VIP
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT approved FROM whitelist WHERE user_id=?", (user_id,)) as cursor:
            res = await cursor.fetchone()
            if res: return (res[0], 0)
            return (0, 0)

async def db_add_request(user_id, username):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO whitelist (user_id, approved, username) VALUES (?, 0, ?)", (user_id, username))
        await db.commit()

async def db_approve(user_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE whitelist SET approved=1 WHERE user_id=?", (user_id,))
        await db.commit()

async def db_save(phone, ua, res, plat):
    async with aiosqlite.connect(DB_NAME) as db:
        now = datetime.now()
        await db.execute("""INSERT INTO accounts (phone, status, ua, res, plat, last_act, created_at, msgs_hour) 
                            VALUES (?, 'active', ?, ?, ?, ?, ?, 0) 
                            ON CONFLICT(phone) DO UPDATE SET status='active', last_act=excluded.last_act""", 
                         (phone, ua, res, plat, now, now))
        await db.commit()

async def db_set_sleep(phone, hours=24):
    """Ставит аккаунт на отлегу"""
    future = datetime.now() + timedelta(hours=hours)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE accounts SET last_act=? WHERE phone=?", (future, phone))
        await db.commit()

# ==========================================
# 🌐 SELENIUM (ANTI-DETECT 15+)
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
    
    # 🔥 ANTI-CRASH & STEALTH
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-images")
    options.add_argument("--blink-settings=imagesEnabled=false")
    options.add_argument("--memory-pressure-off")
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-breakpad")
    options.add_argument("--disable-component-update")
    options.add_argument("--disable-infobars")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)
    
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
    await asyncio.sleep(timeout)
    if phone in ACTIVE_DRIVERS:
        d = ACTIVE_DRIVERS.pop(phone)
        try: await asyncio.to_thread(d['driver'].quit)
        except: pass
        shutil.rmtree(d['tmp'], ignore_errors=True)
        try: await bot.send_message(chat_id, f"⏰ Таймер {timeout}с истек. Сессия +{phone} закрыта (память очищена).")
        except: pass

# ==========================================
# 🤖 BOT UI
# ==========================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

def kb_main(is_admin=False):
    btns = [
        [InlineKeyboardButton(text="🤖 АВТО ВХОД", callback_data="add_auto"), 
         InlineKeyboardButton(text="🎮 РУЧНОЙ ВХОД", callback_data="add_manual")],
        [InlineKeyboardButton(text="🕵️ ШПИОН РЕЖИМ", callback_data="spy_mode"),
         InlineKeyboardButton(text="🧹 ОЧИСТКА ТМП", callback_data="clean_tmp")],
        [InlineKeyboardButton(text="📊 ДАШБОРД", callback_data="dashboard")]
    ]
    if is_admin:
        btns.append([InlineKeyboardButton(text="⚙️ АДМИН ПАНЕЛЬ", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_manual_control(phone):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 ЧЕК", callback_data=f"man_1_{phone}"),
         InlineKeyboardButton(text="🔄 REFRESH", callback_data=f"man_r_{phone}")],
        [InlineKeyboardButton(text="🔗 ВХОД", callback_data=f"man_2_{phone}"),
         InlineKeyboardButton(text="⌨️ НОМЕР", callback_data=f"man_3_{phone}")],
        [InlineKeyboardButton(text="➡️ NEXT", callback_data=f"man_4_{phone}"),
         InlineKeyboardButton(text="↩️ ENTER", callback_data=f"man_e_{phone}")],
        [InlineKeyboardButton(text="✅ СОХРАНИТЬ", callback_data=f"man_5_{phone}"),
         InlineKeyboardButton(text="💤 ОТЛЕГА 24ч", callback_data=f"man_sleep_{phone}")],
        [InlineKeyboardButton(text="🗑 ОТМЕНА", callback_data=f"man_cancel_{phone}")]
    ])

# ==========================================
# 🛂 AUTH & START
# ==========================================
@dp.message(Command("start"))
async def start(msg: types.Message):
    await db_init()
    user_id = msg.from_user.id
    username = msg.from_user.username or "Unknown"
    
    is_approved, _ = await db_check_perm(user_id, username)
    
    if is_approved:
        await msg.answer("🔱 **IMPERATOR v26.0 WARLORD**\nДоступ разрешен.", reply_markup=kb_main(user_id==ADMIN_ID))
    else:
        # Логика для чужаков (Fix: теперь бот отвечает)
        await db_add_request(user_id, username)
        if ADMIN_ID:
            await bot.send_message(ADMIN_ID, f"👤 **Новая заявка:**\nID: `{user_id}`\nUser: @{username}", 
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_{user_id}")]]))
        await msg.answer("🔒 **Доступ закрыт.**\nВаша заявка отправлена администратору.\nОжидайте одобрения.")

@dp.callback_query(F.data.startswith("approve_"))
async def approve_user(cb: types.CallbackQuery):
    uid = int(cb.data.split("_")[1])
    await db_approve(uid)
    await bot.send_message(uid, "✅ **Администратор одобрил ваш доступ!**\nЖмите /start")
    await cb.answer("Одобрено!")

# ==========================================
# 📊 DASHBOARD & TOOLS
# ==========================================
@dp.callback_query(F.data == "dashboard")
async def show_dash(cb: types.CallbackQuery):
    act = await db_get_active()
    ram = psutil.virtual_memory().percent
    cpu = psutil.cpu_percent()
    
    # Текстовый график
    bar_ram = "█" * int(ram / 10) + "░" * (10 - int(ram / 10))
    bar_cpu = "█" * int(cpu / 10) + "░" * (10 - int(cpu / 10))
    
    text = (
        f"📊 **DASHBOARD v26**\n"
        f"📱 Активных аккаунтов: `{len(act)}`\n"
        f"🏎 Драйверов в памяти: `{len(ACTIVE_DRIVERS)}`\n\n"
        f"🧠 **RAM:** {ram}%  [{bar_ram}]\n"
        f"⚙️ **CPU:** {cpu}%  [{bar_cpu}]\n\n"
        f"🛡 **Статус:** Hive Mind работает"
    )
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="menu")]]))

@dp.callback_query(F.data == "clean_tmp")
async def clean_tmp(cb: types.CallbackQuery):
    if os.path.exists(TMP_BASE):
        shutil.rmtree(TMP_BASE)
        os.makedirs(TMP_BASE)
    await cb.answer("🧹 Временные файлы очищены!", show_alert=True)

@dp.callback_query(F.data == "menu")
async def back_menu(cb: types.CallbackQuery):
    await cb.message.edit_text("Главное меню", reply_markup=kb_main(cb.from_user.id==ADMIN_ID))

# ==========================================
# 🔥 ADD ACCOUNTS (AUTO / MANUAL)
# ==========================================
@dp.callback_query(F.data == "add_auto")
async def auto_start(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("🤖 **АВТО-РЕЖИМ**\nВведи номер (цифры):")
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
            await asyncio.to_thread(driver.get, "https://web.whatsapp.com/?lang=en")
            wait = WebDriverWait(driver, 45)
            
            # Click Link
            try:
                wait.until(EC.element_to_be_clickable((By.XPATH, "//*[contains(text(), 'Link with phone')]"))).click()
            except:
                driver.execute_script("document.querySelector('[data-testid=\"link-phone\"]').click()")

            # Input
            await s.edit_text("⏳ Ввожу номер...")
            inp = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='text']")))
            inp.clear()
            for d in f"+{phone}": inp.send_keys(d); await asyncio.sleep(0.1)
            inp.send_keys(Keys.ENTER)

            # Wait Code
            await s.edit_text("⏳ Получаю код...")
            await asyncio.sleep(12)
            png = await asyncio.to_thread(driver.get_screenshot_as_png)
            
            await s.delete()
            await msg.answer_photo(
                BufferedInputFile(png, "code.png"),
                caption=f"✅ **КОД ПОЛУЧЕН!**\nВведи код в телефоне и нажми 'СОХРАНИТЬ'.",
                reply_markup=kb_manual_control(phone)
            )
            asyncio.create_task(kill_timer(phone, msg.chat.id, 120))
            
        except Exception as e:
            await s.edit_text(f"❌ Авто-сбой: {e}. Пробуй ручной.")
            if phone in ACTIVE_DRIVERS:
                d = ACTIVE_DRIVERS.pop(phone)
                try: await asyncio.to_thread(d['driver'].quit)
                except: pass
                shutil.rmtree(d['tmp'], ignore_errors=True)

@dp.callback_query(F.data == "add_manual")
async def manual_start(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("🎮 **РУЧНОЙ РЕЖИМ**\nВведи номер:")
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
        await asyncio.to_thread(driver.get, "https://web.whatsapp.com/?lang=en")

        await s.edit_text(
            f"✅ **ПУЛЬТ ГОТОВ**\n📱 +{phone}\nИспользуй кнопки:",
            reply_markup=kb_manual_control(phone)
        )
        asyncio.create_task(kill_timer(phone, msg.chat.id, 300))

# 🔥 УЛУЧШЕННЫЙ ПУЛЬТ (Unified Handler)
@dp.callback_query(lambda c: c.data and c.data.startswith("man_"))
async def manual_control_handler(cb: types.CallbackQuery):
    parts = cb.data.split("_")
    action, phone = parts[1], parts[2]
    
    if phone not in ACTIVE_DRIVERS: return await cb.answer("❌ Сессия закрыта", show_alert=True)
    d = ACTIVE_DRIVERS[phone]; drv = d['driver']
    
    try:
        if action == "1": # ЧЕК
            png = await asyncio.to_thread(drv.get_screenshot_as_png)
            await cb.message.answer_photo(BufferedInputFile(png, "s.png"), caption="📸")
            await cb.answer()
            
        elif action == "2": # ВХОД
            drv.execute_script("document.querySelector('[data-testid=\"link-phone\"]').click() || document.querySelector('span[role=\"button\"][title*=\"Link\"]').click()")
            await cb.answer("✅ Клик!")
            
        elif action == "3": # НОМЕР
            try:
                inp = drv.find_element(By.CSS_SELECTOR, "input[type='text']")
                inp.clear()
                for x in f"+{phone}": inp.send_keys(x); await asyncio.sleep(0.05)
                await cb.answer("✅ Номер введен")
            except: await cb.answer("❌ Нет поля", show_alert=True)
            
        elif action == "4": # NEXT
            try:
                drv.find_element(By.XPATH, "//*[text()='Next']").click()
                await cb.answer("✅ Next")
                await asyncio.sleep(3)
                png = await asyncio.to_thread(drv.get_screenshot_as_png)
                await cb.message.answer_photo(BufferedInputFile(png, "c.png"), caption="✅ **КОД**")
            except: await cb.answer("❌ Кнопки нет", show_alert=True)
            
        elif action == "e": # ENTER (New)
            actions = ActionChains(drv)
            actions.send_keys(Keys.ENTER).perform()
            await cb.answer("↩️ Enter нажат")
            
        elif action == "r": # REFRESH (New)
            await asyncio.to_thread(drv.refresh)
            await cb.answer("🔄 Страница обновлена")
            
        elif action == "5": # СОХРАНИТЬ
            s = ACTIVE_DRIVERS.pop(phone)
            await db_save(phone, s['ua'], s['res'], s['plat'])
            try: await asyncio.to_thread(s['driver'].quit)
            except: pass
            shutil.rmtree(s['tmp'], ignore_errors=True)
            await cb.message.edit_text(f"🎉 **+{phone} ДОБАВЛЕН!**")
            
        elif action == "sleep": # ОТЛЕГА (New)
            s = ACTIVE_DRIVERS.pop(phone)
            await db_save(phone, s['ua'], s['res'], s['plat'])
            await db_set_sleep(phone, 24) # 24 часа отлеги
            try: await asyncio.to_thread(s['driver'].quit)
            except: pass
            shutil.rmtree(s['tmp'], ignore_errors=True)
            await cb.message.edit_text(f"💤 **+{phone} ОТПРАВЛЕН В ОТЛЕГУ (24ч)**")
            
        elif action == "cancel":
            s = ACTIVE_DRIVERS.pop(phone)
            try: await asyncio.to_thread(s['driver'].quit)
            except: pass
            shutil.rmtree(s['tmp'], ignore_errors=True)
            await cb.message.edit_text("🗑 Отменено.")

    except Exception as e:
        await cb.answer(f"Error: {str(e)[:50]}", show_alert=True)

# ==========================================
# 🕵️ SPY MODE (ПРОГРЕВ БЕЗ СООБЩЕНИЙ)
# ==========================================
@dp.callback_query(F.data == "spy_mode")
async def spy_start(cb: types.CallbackQuery):
    await cb.answer("🕵️ Запускаю ШПИОНА...", show_alert=True)
    asyncio.create_task(worker_spy())

async def worker_spy():
    """Заходит на аккаунты, скроллит, читает, но не пишет"""
    targs = await db_get_active()
    if not targs: return
    
    phone = random.choice(targs)
    logger.info(f"🕵️ SPY MODE: {phone}")
    
    driver = None; tmp = None
    try:
        async with BROWSER_SEMAPHORE:
            driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, phone)
            if not driver: return
            
            await asyncio.to_thread(driver.get, "https://web.whatsapp.com/?lang=en")
            wait = WebDriverWait(driver, 60)
            
            # Ждем загрузку списка чатов
            try:
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "div[aria-label='Chat list']")))
                logger.info(f"🕵️ {phone}: Logged in (Spying...)")
                
                # Имитация чтения (Random Clicks)
                for _ in range(5):
                    try:
                        # Находим случайный чат и кликаем
                        chats = driver.find_elements(By.CSS_SELECTOR, "div[role='listitem']")
                        if chats:
                            random.choice(chats).click()
                            await asyncio.sleep(random.randint(5, 10))
                    except: pass
                
            except TimeoutException:
                logger.warning(f"🕵️ {phone}: Timeout or Banned?")
            
    except Exception as e:
        logger.error(f"Spy error: {e}")
    finally:
        if driver: 
            try: await asyncio.to_thread(driver.quit)
            except: pass
        if tmp and os.path.exists(tmp): shutil.rmtree(tmp, ignore_errors=True)

# ==========================================
# 🚜 HIVE MIND (WORKER WITH LIMITS)
# ==========================================
async def worker_hive(phone):
    """Грев с учетом лимитов (15 в час)"""
    # Проверка лимитов (упрощенная)
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute("SELECT msgs_hour, last_msg_time FROM accounts WHERE phone=?", (phone,))
        row = await cursor.fetchone()
        if row:
            cnt, last_t = row[0], row[1]
            # Сброс счетчика если прошел час
            if last_t:
                lt = datetime.strptime(last_t.split(".")[0], "%Y-%m-%d %H:%M:%S")
                if datetime.now() - lt > timedelta(hours=1):
                    cnt = 0
            
            if cnt >= MAX_MSGS_PER_HOUR:
                logger.info(f"⚠️ {phone}: Limit reached ({cnt}/15). Skip.")
                return # Лимит исчерпан
    
    # Стандартная логика отправки
    targs = await db_get_active()
    if len(targs) < 2: return
    target = random.choice([t for t in targs if t!=phone])
    
    driver = None; tmp = None
    try:
        async with BROWSER_SEMAPHORE:
            driver, ua, res, plat, tmp = await asyncio.to_thread(get_driver, phone)
            if not driver: return
            
            try:
                driver.set_page_load_timeout(40)
                await asyncio.to_thread(driver.get, f"https://web.whatsapp.com/send?phone={target}")
            except: driver.execute_script("window.stop();")
            
            wait = WebDriverWait(driver, 50)
            inp = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "footer div[contenteditable='true']")))
            
            msg_text = ai_engine.generate()
            for c in msg_text: inp.send_keys(c); await asyncio.sleep(0.05)
            inp.send_keys(Keys.ENTER)
            
            # Обновляем статистику
            async with aiosqlite.connect(DB_NAME) as db:
                await db.execute("""UPDATE accounts SET msgs_hour = msgs_hour + 1, last_msg_time = ?, last_act = ? 
                                    WHERE phone=?""", (datetime.now(), datetime.now(), phone))
                await db.commit()
            
            logger.info(f"✅ {phone} -> {target}: {msg_text}")
            await asyncio.sleep(5)
            
    except Exception as e:
        logger.error(f"Hive error: {e}")
    finally:
        if driver: 
            try: await asyncio.to_thread(driver.quit)
            except: pass
        if tmp and os.path.exists(tmp): shutil.rmtree(tmp, ignore_errors=True)

async def main_loop():
    while True:
        phones = await db_get_active()
        # Случайный выбор: либо Hive Mind (общение), либо Шпион (прогрев)
        for p in phones:
            if p not in ACTIVE_DRIVERS:
                if random.random() < 0.7:
                    asyncio.create_task(worker_hive(p))
                else:
                    asyncio.create_task(worker_spy()) # Иногда запускаем шпиона
                
                await asyncio.sleep(random.randint(20, 60))
        
        await asyncio.sleep(120)

async def main():
    await db_init()
    asyncio.create_task(main_loop())
    logger.info("🚀 IMPERATOR v26.0 WARLORD STARTED")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__": asyncio.run(main())
