import asyncio
import os
import logging
import sqlite3
import random
import re
import shutil
import psutil
import traceback
from datetime import datetime
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
from selenium.common.exceptions import TimeoutException, WebDriverException

# --- КОНФИГ & НАСТРОЙКИ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN") 
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))

# Лимит потоков (BotHost PRO)
BROWSER_SEMAPHORE = asyncio.Semaphore(3)

DB_NAME = 'bot_database.db'
SESSIONS_DIR = "./sessions"
LOG_DIR = "./logs"

# Глобальные настройки (по умолчанию)
CONFIG = {
    "mode": "SOLO",    # SOLO или MASS
    "speed": "NORMAL", # TURBO, NORMAL, SLOW
    "min_delay": 120,
    "max_delay": 300
}

# Хранилище драйверов {phone: driver} - чтобы можно было делать "Чек"
ACTIVE_SESSIONS = {} 

fake = Faker('ru_RU')
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("TITANIUM")

# --- DATABASE ENGINE ---
def init_db():
    with sqlite3.connect(DB_NAME, timeout=10) as conn:
        # Добавили флаг profile_set (меняли ли мы имя/био)
        conn.execute('''CREATE TABLE IF NOT EXISTS accounts 
                        (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                         user_id INTEGER, 
                         phone_number TEXT UNIQUE, 
                         status TEXT DEFAULT 'pending', 
                         messages_sent INTEGER DEFAULT 0,
                         user_agent TEXT, resolution TEXT, platform TEXT,
                         ban_reason TEXT, 
                         profile_set INTEGER DEFAULT 0,
                         last_active TIMESTAMP)''')
        conn.commit()

def db_get_all_active():
    with sqlite3.connect(DB_NAME, timeout=10) as conn:
        return [row[0] for row in conn.execute("SELECT phone_number FROM accounts WHERE status = 'active'").fetchall()]

def db_get_acc(phone):
    with sqlite3.connect(DB_NAME, timeout=10) as conn:
        return conn.execute("SELECT * FROM accounts WHERE phone_number = ?", (phone,)).fetchone()

def db_update_status(phone, status, reason=None):
    with sqlite3.connect(DB_NAME, timeout=10) as conn:
        conn.execute("UPDATE accounts SET status = ?, ban_reason = ?, last_active = ? WHERE phone_number = ?", 
                     (status, reason, datetime.now(), phone))

def db_set_profile_done(phone):
    with sqlite3.connect(DB_NAME, timeout=10) as conn:
        conn.execute("UPDATE accounts SET profile_set = 1 WHERE phone_number = ?", (phone,))

def db_inc_msg(phone):
    with sqlite3.connect(DB_NAME, timeout=10) as conn:
        conn.execute("UPDATE accounts SET messages_sent = messages_sent + 1, last_active = ? WHERE phone_number = ?", 
                     (datetime.now(), phone))

def db_delete(phone):
    with sqlite3.connect(DB_NAME) as conn: conn.execute("DELETE FROM accounts WHERE phone_number = ?", (phone,))
    path = os.path.join(SESSIONS_DIR, str(phone))
    if os.path.exists(path): shutil.rmtree(path, ignore_errors=True)

# --- SYSTEM GUARD ---
def is_memory_safe():
    mem = psutil.virtual_memory().available / (1024 * 1024)
    if mem < 200:
        logger.warning(f"⚠️ LOW RAM: {mem:.1f}MB. Pause.")
        return False
    return True

async def zombie_killer():
    """Убивает зависшие процессы"""
    while True:
        await asyncio.sleep(120)
        for proc in psutil.process_iter(['pid', 'name', 'create_time']):
            try:
                if 'chrome' in proc.info['name']:
                    if (datetime.now().timestamp() - proc.info['create_time']) > 1500: # 25 мин
                        proc.kill()
            except: pass

# --- SELENIUM CORE ---
async def find_element_retry(driver, xpaths, timeout=10):
    wait = WebDriverWait(driver, timeout)
    for xp in xpaths:
        try: return wait.until(EC.presence_of_element_located((By.XPATH, xp)))
        except: continue
    return None

def get_driver(phone):
    if not is_memory_safe(): return None
    
    path = os.path.join(SESSIONS_DIR, str(phone))
    if not os.path.exists(path): os.makedirs(path)

    # Получаем конфиг устройства
    acc = db_get_acc(phone)
    if acc and acc[5]:
        ua, res, plat = acc[5], acc[6], acc[7]
    else:
        # Генерируем новый
        ua = f"Mozilla/5.0 ({random.choice(['Windows NT 10.0; Win64; x64', 'Macintosh; Intel Mac OS X 10_15_7'])}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        res, plat = "1920,1080", "Win32"
        with sqlite3.connect(DB_NAME) as conn:
            conn.execute("UPDATE accounts SET user_agent=?, resolution=?, platform=? WHERE phone_number=?", (ua, res, plat, phone))

    opt = Options()
    opt.add_argument("--headless=new")
    opt.add_argument("--no-sandbox")
    opt.add_argument("--disable-dev-shm-usage")
    opt.add_argument(f"--window-size={res}")
    opt.add_argument("--lang=ru-KZ")
    opt.add_argument(f"user-agent={ua}")
    opt.add_argument(f"--user-data-dir={path}")
    opt.page_load_strategy = 'eager'
    opt.add_argument("--disable-blink-features=AutomationControlled")
    opt.add_experimental_option("excludeSwitches", ["enable-automation"])
    opt.add_experimental_option('useAutomationExtension', False)

    try:
        driver = webdriver.Chrome(options=opt)
        # Stealth Injections
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": f"Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}}); Object.defineProperty(navigator, 'platform', {{get: () => '{plat}'}});"
        })
        driver.execute_cdp_cmd("Emulation.setGeolocationOverride", {"latitude": 43.2389, "longitude": 76.8897, "accuracy": 50})
        return driver
    except Exception as e:
        logger.error(f"Driver Error: {e}")
        return None

# --- FARM LOGIC ---
async def human_type(element, text):
    for char in text:
        element.send_keys(char)
        await asyncio.sleep(random.uniform(0.05, 0.2))

async def task_change_profile(driver, phone):
    """Смена Имени и Био"""
    try:
        logger.info(f"🎭 {phone}: Changing Profile...")
        wait = WebDriverWait(driver, 15)
        
        # 1. Клик на свой профиль (верхний левый угол)
        profile_btn = await find_element_retry(driver, ["//header//img", "//div[@role='button'][@title='Profile']"], 10)
        if profile_btn:
            profile_btn.click()
            await asyncio.sleep(2)
            
            # 2. Меняем имя (Ищем карандаш)
            edits = driver.find_elements(By.XPATH, "//span[@data-icon='pencil']")
            if edits:
                # Имя обычно первое
                edits[0].click()
                await asyncio.sleep(1)
                inp = driver.switch_to.active_element
                inp.send_keys(Keys.CONTROL + "a" + Keys.BACKSPACE)
                await human_type(inp, fake.first_name())
                inp.send_keys(Keys.ENTER)
                await asyncio.sleep(2)
                
                # Био (если есть второй карандаш)
                if len(edits) > 1:
                    edits[1].click()
                    await asyncio.sleep(1)
                    inp2 = driver.switch_to.active_element
                    inp2.send_keys(Keys.CONTROL + "a" + Keys.BACKSPACE)
                    await human_type(inp2, fake.catch_phrase())
                    inp2.send_keys(Keys.ENTER)
            
            # Назад
            back = await find_element_retry(driver, ["//span[@data-icon='back']"], 5)
            if back: back.click()
            
            db_set_profile_done(phone)
            logger.info(f"✅ {phone}: Profile Updated!")
    except Exception as e:
        logger.error(f"Profile Change Err {phone}: {e}")

async def task_send_message(driver, sender, target, is_solo):
    """Отправка сообщения"""
    try:
        driver.get(f"https://web.whatsapp.com/send?phone={target}")
        
        # Ищем поле ввода
        inp = await find_element_retry(driver, [
            "//div[@contenteditable='true'][@data-tab='10']",
            "//footer//div[@role='textbox']"
        ], 20)
        
        if inp:
            msg = fake.sentence() if not is_solo else f"Note: {fake.word()}"
            await human_type(inp, msg)
            await asyncio.sleep(1)
            inp.send_keys(Keys.ENTER)
            db_inc_msg(sender)
            logger.info(f"✉️ {sender} -> {target}: {msg}")
            return True
        else:
            logger.warning(f"❌ {sender}: Input not found")
            return False
    except Exception as e:
        logger.error(f"Send Err {sender}: {e}")
        return False

async def worker_cycle(phone, force_action=None):
    """Один полный цикл жизни аккаунта"""
    if not is_memory_safe(): return

    # Если это принудительная команда "чек" или "сообщение" - мы не используем семафор фермы
    # Если это обычный фарм - используем семафор
    context = BROWSER_SEMAPHORE if not force_action else asyncio.Semaphore(1)
    
    async with context:
        driver = await asyncio.to_thread(get_driver, phone)
        if not driver: return
        
        # Регистрируем для Live View
        ACTIVE_SESSIONS[phone] = driver
        
        try:
            driver.get("https://web.whatsapp.com/")
            
            # AUTO-LOGIN DETECT
            loaded = await find_element_retry(driver, ["//div[@id='pane-side']", "//div[@data-tab='3']"], 60)
            
            if not loaded:
                src = driver.page_source
                if "link with phone" in src.lower() or "qr code" in src.lower():
                     # Если мы не в режиме настройки, а в режиме фарма - значит вылетел
                    if not force_action:
                        db_update_status(phone, 'pending')
                        logger.warning(f"📉 {phone} logged out.")
                elif "account is not allowed" in src.lower():
                    db_update_status(phone, 'banned', 'PermBan')
                return

            # Если мы тут, значит мы залогинены
            db_update_status(phone, 'active')

            # --- FORCE ACTIONS (ЕСЛИ ПРОСИЛ ЮЗЕР) ---
            if force_action == "screenshot":
                # Просто держим браузер открытым пару секунд, скрин снимет хендлер
                await asyncio.sleep(5)
                return 
            
            if force_action == "msg":
                 await task_send_message(driver, phone, phone, True) # Шлем себе
                 return

            # --- ОБЫЧНЫЙ ФАРМ ---
            acc_data = db_get_acc(phone)
            # 1. Меняем профиль (если еще не меняли)
            if acc_data and acc_data[9] == 0:
                await task_change_profile(driver, phone)
            
            # 2. Выбор цели (Solo vs Mass)
            actives = db_get_all_active()
            mode = CONFIG['mode']
            
            target = phone # по дефолту Solo
            is_solo = True
            
            if mode == "MASS" and len(actives) > 1:
                t = random.choice(actives)
                if t != phone:
                    target = t
                    is_solo = False
            
            await task_send_message(driver, phone, target, is_solo)
            await asyncio.sleep(5)

        except Exception as e:
            logger.error(f"Worker Crash {phone}: {e}")
        finally:
            if phone in ACTIVE_SESSIONS: del ACTIVE_SESSIONS[phone]
            driver.quit()

async def farm_scheduler():
    """Главный планировщик"""
    asyncio.create_task(zombie_killer())
    logger.info("🚜 Titanium Farm Started")
    
    while True:
        phones = db_get_all_active()
        if phones:
            p = random.choice(phones)
            asyncio.create_task(worker_cycle(p))
        
        # Global Delay
        d_min, d_max = CONFIG['min_delay'], CONFIG['max_delay']
        await asyncio.sleep(random.randint(d_min, d_max))

# --- UI & HANDLERS ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
class Form(StatesGroup): phone = State()

def kb_main():
    mode_icon = "👤 Solo" if CONFIG['mode'] == "SOLO" else "👥 Mass"
    speed_icon = "🚗 Normal"
    if CONFIG['speed'] == "TURBO": speed_icon = "🚀 Turbo"
    elif CONFIG['speed'] == "SLOW": speed_icon = "🐢 Slow"
    
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить Аккаунт", callback_data="add")],
        [InlineKeyboardButton(text="📂 Мои Аккаунты (Live)", callback_data="list")],
        [InlineKeyboardButton(text=f"Режим: {mode_icon}", callback_data="toggle_mode"),
         InlineKeyboardButton(text=f"Скорость: {speed_icon}", callback_data="toggle_speed")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats")]
    ])

def kb_acc_actions(phone):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 ГЛЯНУТЬ (Скрин)", callback_data=f"view_{phone}")],
        [InlineKeyboardButton(text="⚡ ПНУТЬ (Написать)", callback_data=f"kick_{phone}")],
        [InlineKeyboardButton(text="🗑 УДАЛИТЬ", callback_data=f"del_{phone}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="list")]
    ])

@dp.message(Command("start"))
async def start(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    init_db()
    if not os.path.exists(SESSIONS_DIR): os.makedirs(SESSIONS_DIR)
    if not os.path.exists(LOG_DIR): os.makedirs(LOG_DIR)
    await msg.answer("💎 **WA Farm Architect: Titanium**", reply_markup=kb_main())

# --- SETTINGS TOGGLES ---
@dp.callback_query(F.data == "toggle_mode")
async def toggle_mode(call: types.CallbackQuery):
    CONFIG['mode'] = "MASS" if CONFIG['mode'] == "SOLO" else "SOLO"
    await call.message.edit_reply_markup(reply_markup=kb_main())

@dp.callback_query(F.data == "toggle_speed")
async def toggle_speed(call: types.CallbackQuery):
    s = CONFIG['speed']
    if s == "NORMAL": 
        CONFIG.update({"speed": "TURBO", "min_delay": 30, "max_delay": 90})
    elif s == "TURBO":
        CONFIG.update({"speed": "SLOW", "min_delay": 300, "max_delay": 600})
    else:
        CONFIG.update({"speed": "NORMAL", "min_delay": 120, "max_delay": 300})
    await call.message.edit_reply_markup(reply_markup=kb_main())

# --- ADD ACCOUNT (AUTO-DETECT LOGIC) ---
@dp.callback_query(F.data == "add")
async def add_start(call: types.CallbackQuery, state: FSMContext):
    await call.message.edit_text("Введи номер (7XXXXXXXXXX):")
    await state.set_state(Form.phone)

@dp.message(Form.phone)
async def add_phone(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR IGNORE INTO accounts (user_id, phone_number) VALUES (?, ?)", (msg.from_user.id, phone))
    
    await msg.answer(f"🚀 Запускаю настройку для {phone}...", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Вход по Номеру", callback_data=f"auth_link_{phone}")],
        [InlineKeyboardButton(text="📷 Показать QR", callback_data=f"auth_qr_{phone}")]
    ]))

async def auth_monitor(driver, phone, msg_to_edit):
    """Следит за входом и САМ завершает процесс"""
    for _ in range(60): # 5 минут ждем
        try:
            if driver.find_elements(By.ID, "pane-side"):
                db_update_status(phone, "active")
                try: await msg_to_edit.edit_text(f"✅ **УСПЕХ!**\n{phone} авторизован и добавлен в ферму.\nТеперь он сам будет менять имя и греться.")
                except: pass
                return True
        except: pass
        await asyncio.sleep(5)
    return False

@dp.callback_query(F.data.startswith("auth_"))
async def auth_flow(call: types.CallbackQuery):
    action, phone = call.data.split("_")[1], call.data.split("_")[2]
    await call.message.edit_text("⏳ Загружаю браузер... Жди.")
    
    # Запускаем в фоне, но держим ссылку
    asyncio.create_task(run_auth_process(call.message, phone, action))

async def run_auth_process(message, phone, action):
    async with BROWSER_SEMAPHORE:
        driver = await asyncio.to_thread(get_driver, phone)
        if not driver: return
        ACTIVE_SESSIONS[phone] = driver # Для скринов
        
        try:
            driver.get("https://web.whatsapp.com/")
            wait = WebDriverWait(driver, 30)
            
            if action == "link":
                # Логика входа по номеру
                try:
                    btn = await find_element_retry(driver, ["//span[contains(text(), 'Link with phone')]", "//a[contains(@href, 'link-device')]"])
                    if btn: btn.click()
                    
                    inp = await find_element_retry(driver, ["//input[@aria-label='Type your phone number.']", "//input[@type='text']"])
                    if inp:
                        driver.execute_script("arguments[0].value = '';", inp)
                        inp.send_keys(Keys.CONTROL + "a" + Keys.BACKSPACE)
                        for ch in phone: inp.send_keys(ch); await asyncio.sleep(0.05)
                        inp.send_keys(Keys.ENTER)
                        
                        code_el = await find_element_retry(driver, ["//div[@aria-details='link-device-phone-number-code']"], 20)
                        if code_el:
                            await message.edit_text(f"🔑 **КОД:** `{code_el.text}`\nВводи в телефон! Я жду авто-входа...", parse_mode="Markdown")
                except Exception as e:
                    logger.error(f"Auth Link Err: {e}")

            elif action == "qr":
                await asyncio.sleep(5)
                scr = driver.get_screenshot_as_png()
                await message.answer_photo(BufferedInputFile(scr, "qr.png"), caption="📷 Сканируй QR! Я жду авто-входа...")

            # Запускаем авто-детект входа
            await auth_monitor(driver, phone, message)

        finally:
            if phone in ACTIVE_SESSIONS: del ACTIVE_SESSIONS[phone]
            driver.quit()

# --- LIST & ACTIONS ---
@dp.callback_query(F.data == "list")
async def list_accs(call: types.CallbackQuery):
    with sqlite3.connect(DB_NAME) as conn:
        rows = conn.execute("SELECT phone_number, status, messages_sent FROM accounts").fetchall()
    
    if not rows: return await call.message.edit_text("Список пуст", reply_markup=kb_main())
    
    kb = []
    for p, s, m in rows:
        icon = "🟢" if s == 'active' else "🔴"
        if s == 'banned': icon = "🚫"
        kb.append([InlineKeyboardButton(text=f"{icon} {p} ({m})", callback_data=f"act_{p}")])
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="menu")])
    
    await call.message.edit_text("📂 Выбери аккаунт для управления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data == "menu")
async def menu_back(call: types.CallbackQuery):
    await call.message.edit_text("Главное меню", reply_markup=kb_main())

@dp.callback_query(F.data.startswith("act_"))
async def acc_options(call: types.CallbackQuery):
    phone = call.data.split("_")[1]
    await call.message.edit_text(f"⚙️ Управление {phone}", reply_markup=kb_acc_actions(phone))

@dp.callback_query(F.data.startswith("view_"))
async def view_screen(call: types.CallbackQuery):
    phone = call.data.split("_")[1]
    await call.answer("📸 Делаю снимок...")
    
    # Если драйвер уже активен (работает воркер)
    if phone in ACTIVE_SESSIONS:
        try:
            scr = ACTIVE_SESSIONS[phone].get_screenshot_as_png()
            await call.message.answer_photo(BufferedInputFile(scr, "live.png"), caption=f"Live View: {phone}")
            return
        except: pass
    
    # Если спит - запускаем быстрый чек (придется ждать)
    await call.message.answer("⚠️ Аккаунт спит. Бужу для скрина (10-15 сек)...")
    asyncio.create_task(worker_cycle(phone, force_action="screenshot"))

@dp.callback_query(F.data.startswith("kick_"))
async def kick_worker(call: types.CallbackQuery):
    phone = call.data.split("_")[1]
    await call.answer("⚡ Задача отправлена!")
    asyncio.create_task(worker_cycle(phone, force_action="msg"))

@dp.callback_query(F.data.startswith("del_"))
async def delete_acc(call: types.CallbackQuery):
    phone = call.data.split("_")[1]
    db_delete(phone)
    await call.answer("🗑 Удалено!", show_alert=True)
    await list_accs(call)

async def main():
    init_db()
    asyncio.create_task(farm_scheduler())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
