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

# --- SELENIUM & ACTION CHAINS ---
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains # Для мыши
from selenium.common.exceptions import TimeoutException, WebDriverException

# --- КОНФИГ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN") 
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))

# 3 потока = Безопасно для RAM BotHost.
# Для 150 аккаунтов цикл прохода займет время, но это и хорошо (меньше спама).
BROWSER_SEMAPHORE = asyncio.Semaphore(3)

DB_NAME = 'bot_database.db'
SESSIONS_DIR = "./sessions"
LOG_DIR = "./logs"
BAN_DIR = "./logs/bans"

CONFIG = {
    "mode": "MASS",     # Лучше сразу MASS для прогрева
    "speed": "NORMAL", 
    "min_delay": 180,   # Увеличил дефолтную задержку для безопасности
    "max_delay": 400
}

ACTIVE_SESSIONS = {} 
fake = Faker('ru_RU')

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("PLATINUM")

# --- DATABASE ENGINE (WAL MODE) ---
def init_db():
    with sqlite3.connect(DB_NAME, timeout=30) as conn:
        # Включаем WAL режим для скорости и надежности
        conn.execute("PRAGMA journal_mode=WAL;")
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
    with sqlite3.connect(DB_NAME) as conn:
        return [row[0] for row in conn.execute("SELECT phone_number FROM accounts WHERE status = 'active'").fetchall()]

def db_get_acc(phone):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT * FROM accounts WHERE phone_number = ?", (phone,)).fetchone()

def db_update_status(phone, status, reason=None):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET status = ?, ban_reason = ?, last_active = ? WHERE phone_number = ?", 
                     (status, reason, datetime.now(), phone))

def db_set_profile_done(phone):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET profile_set = 1 WHERE phone_number = ?", (phone,))

def db_inc_msg(phone):
    with sqlite3.connect(DB_NAME) as conn:
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
    while True:
        await asyncio.sleep(120)
        for proc in psutil.process_iter(['pid', 'name', 'create_time']):
            try:
                if 'chrome' in proc.info['name']:
                    # Если процесс висит больше 30 мин - убиваем
                    if (datetime.now().timestamp() - proc.info['create_time']) > 1800:
                        proc.kill()
            except: pass

# --- SELENIUM CORE ---
async def find_element_retry(driver, xpaths, timeout=15):
    wait = WebDriverWait(driver, timeout)
    for xp in xpaths:
        try: return wait.until(EC.presence_of_element_located((By.XPATH, xp)))
        except: continue
    return None

def get_driver(phone):
    if not is_memory_safe(): return None
    path = os.path.join(SESSIONS_DIR, str(phone))
    if not os.path.exists(path): os.makedirs(path)

    acc = db_get_acc(phone)
    if acc and acc[5]:
        ua, res, plat = acc[5], acc[6], acc[7]
    else:
        ua = f"Mozilla/5.0 ({random.choice(['Windows NT 10.0; Win64; x64', 'Macintosh; Intel Mac OS X 10_15_7'])}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
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
    
    # Anti-Detection
    opt.add_argument("--disable-blink-features=AutomationControlled")
    opt.add_experimental_option("excludeSwitches", ["enable-automation"])
    opt.add_experimental_option('useAutomationExtension', False)

    try:
        driver = webdriver.Chrome(options=opt)
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": f"Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}}); Object.defineProperty(navigator, 'platform', {{get: () => '{plat}'}});"
        })
        return driver
    except: return None

# --- HUMAN BEHAVIOR (NEW) ---
async def human_mouse_move(driver):
    """Эмуляция случайных движений мыши"""
    try:
        action = ActionChains(driver)
        for _ in range(random.randint(2, 5)):
            x_offset = random.randint(-50, 50)
            y_offset = random.randint(-50, 50)
            action.move_by_offset(x_offset, y_offset).perform()
            await asyncio.sleep(random.uniform(0.1, 0.3))
    except: pass

async def human_scroll(driver):
    """Случайный скролл чата"""
    try:
        driver.execute_script(f"window.scrollBy(0, {random.randint(100, 500)});")
        await asyncio.sleep(0.5)
    except: pass

async def human_type(element, text):
    """Печать с опечатками и разной скоростью"""
    for char in text:
        if random.random() < 0.02: # 2% шанс опечатки
            wrong_char = random.choice('йцукенгшщзхъфывапролджэ')
            element.send_keys(wrong_char)
            await asyncio.sleep(0.1)
            element.send_keys(Keys.BACKSPACE)
        element.send_keys(char)
        await asyncio.sleep(random.uniform(0.04, 0.15))

# --- TASKS ---
async def task_change_profile(driver, phone):
    try:
        logger.info(f"🎭 {phone}: Настройка профиля...")
        await human_mouse_move(driver)
        
        profile_btn = await find_element_retry(driver, ["//header//img", "//div[@role='button'][@title='Profile']"], 10)
        if profile_btn:
            profile_btn.click()
            await asyncio.sleep(2)
            
            # Меняем Имя и Сведения
            edits = driver.find_elements(By.XPATH, "//span[@data-icon='pencil']")
            for i, edit_btn in enumerate(edits):
                if i > 1: break # Меняем только имя и инфо
                edit_btn.click()
                await asyncio.sleep(1)
                
                inp = driver.switch_to.active_element
                inp.send_keys(Keys.CONTROL + "a" + Keys.BACKSPACE)
                
                # Генерация данных
                text = fake.first_name() if i == 0 else fake.catch_phrase()
                await human_type(inp, text)
                inp.send_keys(Keys.ENTER)
                await asyncio.sleep(2)
            
            back = await find_element_retry(driver, ["//span[@data-icon='back']"], 5)
            if back: back.click()
            
            db_set_profile_done(phone)
            logger.info(f"✅ {phone}: Профиль обновлен!")
    except Exception as e:
        logger.error(f"Profile Change Err {phone}: {e}")

async def task_send_message(driver, sender, target, is_solo):
    try:
        driver.get(f"https://web.whatsapp.com/send?phone={target}")
        await human_mouse_move(driver)
        
        # Новый селектор (footer)
        inp = await find_element_retry(driver, [
            "//div[@contenteditable='true'][@data-tab='10']",
            "//footer//div[@role='textbox']",
            "//*[@id='main']//footer//div[contains(@class, 'selectable-text')]"
        ], 25)
        
        if inp:
            # Генерация осмысленного диалога (заглушка)
            if is_solo:
                msg = f"Заметка: {fake.word()} {random.randint(1,100)}"
            else:
                msg = fake.sentence()
                if "?" in msg: msg += f" {fake.first_name()}?" # Добавляем имя если вопрос

            await human_type(inp, msg)
            await asyncio.sleep(1)
            inp.send_keys(Keys.ENTER)
            
            db_inc_msg(sender)
            logger.info(f"✉️ {sender} -> {target}: Отправлено")
            return True
        else:
            return False
    except Exception as e:
        logger.error(f"Send Err {sender}: {e}")
        return False

# --- WORKER CYCLE ---
async def worker_cycle(phone, force_action=None):
    if not is_memory_safe(): return

    # NIGHT MODE CHECK (Только если это не принудительный пинок)
    if not force_action:
        hour = datetime.now().hour
        # С 23:00 до 07:00 спим с шансом 95%
        if (hour >= 23 or hour < 7) and random.random() < 0.95:
            return 

    context = BROWSER_SEMAPHORE if not force_action else asyncio.Semaphore(1)
    
    async with context:
        driver = await asyncio.to_thread(get_driver, phone)
        if not driver: return
        ACTIVE_SESSIONS[phone] = driver
        
        try:
            driver.get("https://web.whatsapp.com/")
            
            # Ждем прогрузки
            loaded = await find_element_retry(driver, ["//div[@id='pane-side']", "//div[@data-tab='3']"], 60)
            
            if not loaded:
                src = driver.page_source.lower()
                # Анализ бана
                if "account is not allowed" in src or "spam" in src:
                    # BAN AUTOPSY
                    ban_path = os.path.join(BAN_DIR, f"ban_{phone}_{int(datetime.now().timestamp())}.png")
                    driver.save_screenshot(ban_path)
                    db_update_status(phone, 'banned', 'PermBan')
                    logger.error(f"🚫 BAN {phone}. Скрин: {ban_path}")
                elif "link with phone" in src or "qr code" in src:
                    if not force_action:
                        db_update_status(phone, 'pending')
                        logger.warning(f"📉 {phone} Logout.")
                return

            db_update_status(phone, 'active')

            if force_action == "screenshot":
                await asyncio.sleep(3); return 
            if force_action == "msg":
                 await task_send_message(driver, phone, phone, True); return

            # ОБЫЧНАЯ РАБОТА
            acc_data = db_get_acc(phone)
            # 1. Профиль (один раз)
            if acc_data and acc_data[9] == 0:
                await task_change_profile(driver, phone)
            
            # 2. Выбор цели
            actives = db_get_all_active()
            mode = CONFIG['mode']
            target = phone 
            is_solo = True
            
            if mode == "MASS" and len(actives) > 1:
                # 80% шанс написать другому, 20% себе (естественность)
                if random.random() < 0.8:
                    candidates = [x for x in actives if x != phone]
                    if candidates:
                        target = random.choice(candidates)
                        is_solo = False
            
            await task_send_message(driver, phone, target, is_solo)
            
            # 3. Случайный скролл после сообщения (типа читаем)
            await human_scroll(driver)
            await asyncio.sleep(random.randint(5, 10))

        except Exception as e:
            logger.error(f"Worker Crash {phone}: {e}")
        finally:
            if phone in ACTIVE_SESSIONS: del ACTIVE_SESSIONS[phone]
            driver.quit()

async def farm_scheduler():
    asyncio.create_task(zombie_killer())
    logger.info("🚜 PLATINUM Farm Started")
    
    while True:
        phones = db_get_all_active()
        if phones:
            # Берем случайный (для 150 аккаунтов это норм распределение)
            p = random.choice(phones)
            asyncio.create_task(worker_cycle(p))
        
        d_min, d_max = CONFIG['min_delay'], CONFIG['max_delay']
        # Если ночь - задержка х3
        hour = datetime.now().hour
        if hour >= 23 or hour < 7:
            await asyncio.sleep(random.randint(d_min*3, d_max*3))
        else:
            await asyncio.sleep(random.randint(d_min, d_max))

# --- UI ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
class Form(StatesGroup): phone = State()

def kb_main():
    mode_icon = "👤 Solo" if CONFIG['mode'] == "SOLO" else "👥 Mass"
    speed_icon = "🚗 Norm"
    if CONFIG['speed'] == "TURBO": speed_icon = "🚀 Turbo"
    elif CONFIG['speed'] == "SLOW": speed_icon = "🐢 Slow"
    
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить", callback_data="add"),
         InlineKeyboardButton(text="📂 Список", callback_data="list")],
        [InlineKeyboardButton(text=f"Режим: {mode_icon}", callback_data="toggle_mode"),
         InlineKeyboardButton(text=f"Скорость: {speed_icon}", callback_data="toggle_speed")],
        [InlineKeyboardButton(text="📊 Стата / Ресурсы", callback_data="stats")]
    ])

def kb_acc(phone):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 СКРИН", callback_data=f"view_{phone}"),
         InlineKeyboardButton(text="⚡ ПНУТЬ", callback_data=f"kick_{phone}")],
        [InlineKeyboardButton(text="🗑 УДАЛИТЬ", callback_data=f"del_{phone}"),
         InlineKeyboardButton(text="🔙 Назад", callback_data="list")]
    ])

@dp.message(Command("start"))
async def start(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    init_db()
    for d in [SESSIONS_DIR, LOG_DIR, BAN_DIR]:
        if not os.path.exists(d): os.makedirs(d)
    await msg.answer("💎 **WA Farm PLATINUM (150+ Ready)**\nНочной режим, Human Mouse, Anti-Ban.", reply_markup=kb_main())

@dp.callback_query(F.data == "stats")
async def stats(call: types.CallbackQuery):
    mem = psutil.virtual_memory()
    phones = db_get_all_active()
    hour = datetime.now().hour
    is_night = (hour >= 23 or hour < 7)
    night_icon = "🌙 Ночь (Спим)" if is_night else "☀️ День (Работаем)"
    
    txt = (f"🖥 **BotHost Status:**\n"
           f"👥 Аккаунтов: {len(phones)}\n"
           f"🧠 RAM Free: {mem.available // 1024 // 1024} MB\n"
           f"🕒 Режим: {night_icon}\n"
           f"⚙️ Потоки: 3 (Safe)")
    await call.answer(txt, show_alert=True)

# --- SETTINGS ---
@dp.callback_query(F.data == "toggle_mode")
async def t_mode(call: types.CallbackQuery):
    CONFIG['mode'] = "MASS" if CONFIG['mode'] == "SOLO" else "SOLO"
    await call.message.edit_reply_markup(reply_markup=kb_main())

@dp.callback_query(F.data == "toggle_speed")
async def t_speed(call: types.CallbackQuery):
    s = CONFIG['speed']
    if s == "NORMAL": CONFIG.update({"speed": "TURBO", "min_delay": 60, "max_delay": 120})
    elif s == "TURBO": CONFIG.update({"speed": "SLOW", "min_delay": 400, "max_delay": 800})
    else: CONFIG.update({"speed": "NORMAL", "min_delay": 180, "max_delay": 400})
    await call.message.edit_reply_markup(reply_markup=kb_main())

# --- ADD ACCOUNT ---
@dp.callback_query(F.data == "add")
async def add_s(call: types.CallbackQuery, state: FSMContext):
    await call.message.edit_text("Введи номер (7XXXXXXXXXX):")
    await state.set_state(Form.phone)

@dp.message(Form.phone)
async def add_p(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR IGNORE INTO accounts (user_id, phone_number) VALUES (?, ?)", (msg.from_user.id, phone))
    await msg.answer(f"🚀 Выбери метод входа для {phone}:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Вход по коду", callback_data=f"auth_link_{phone}")],
        [InlineKeyboardButton(text="📷 QR Код", callback_data=f"auth_qr_{phone}")]
    ]))

async def auth_mon(driver, phone, msg_to_edit):
    for _ in range(60): 
        try:
            if driver.find_elements(By.ID, "pane-side"):
                db_update_status(phone, "active")
                try: await msg_to_edit.edit_text(f"✅ **{phone}** В СТРОЮ!\nСкоро сменит имя и начнет работу.")
                except: pass
                return
        except: pass
        await asyncio.sleep(5)

@dp.callback_query(F.data.startswith("auth_"))
async def auth_f(call: types.CallbackQuery):
    action, phone = call.data.split("_")[1], call.data.split("_")[2]
    await call.message.edit_text("⏳ Загрузка... (Не закрывай)")
    asyncio.create_task(run_auth(call.message, phone, action))

async def run_auth(message, phone, action):
    async with BROWSER_SEMAPHORE:
        driver = await asyncio.to_thread(get_driver, phone)
        if not driver: return
        ACTIVE_SESSIONS[phone] = driver 
        try:
            driver.get("https://web.whatsapp.com/")
            wait = WebDriverWait(driver, 30)
            
            if action == "link":
                try:
                    # FIX: Ищем кнопку Link агрессивно
                    btn = await find_element_retry(driver, ["//span[contains(text(), 'Link with phone')]", "//a[contains(@href, 'link-device')]", "//div[@role='button']//div[contains(text(), 'Link')]"])
                    if btn: 
                        btn.click()
                        inp = await find_element_retry(driver, ["//input[@aria-label='Type your phone number.']", "//input[@type='text']"])
                        if inp:
                            driver.execute_script("arguments[0].value = '';", inp)
                            inp.send_keys(Keys.CONTROL + "a" + Keys.BACKSPACE)
                            for ch in phone: inp.send_keys(ch); await asyncio.sleep(0.05)
                            inp.send_keys(Keys.ENTER)
                            
                            code = await find_element_retry(driver, ["//div[@aria-details='link-device-phone-number-code']"], 20)
                            if code: await message.edit_text(f"🔑 КОД: `{code.text}`\nВводи! Я жду...", parse_mode="Markdown")
                except: pass
            elif action == "qr":
                await asyncio.sleep(5)
                scr = driver.get_screenshot_as_png()
                await message.answer_photo(BufferedInputFile(scr, "qr.png"), caption="📷 Сканируй QR!")

            await auth_mon(driver, phone, message)
        finally:
            if phone in ACTIVE_SESSIONS: del ACTIVE_SESSIONS[phone]
            driver.quit()

# --- LIST & ACTIONS ---
@dp.callback_query(F.data == "list")
async def list_a(call: types.CallbackQuery):
    with sqlite3.connect(DB_NAME) as conn:
        rows = conn.execute("SELECT phone_number, status, messages_sent FROM accounts").fetchall()
    if not rows: return await call.message.edit_text("Пусто", reply_markup=kb_main())
    
    kb = []
    for p, s, m in rows:
        icon = "🟢" if s == 'active' else "🔴"
        if s == 'banned': icon = "🚫"
        kb.append([InlineKeyboardButton(text=f"{icon} {p} ({m})", callback_data=f"opt_{p}")])
    kb.append([InlineKeyboardButton(text="🔙 Меню", callback_data="menu")])
    await call.message.edit_text("Список:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data == "menu")
async def menu(call: types.CallbackQuery):
    await call.message.edit_text("Главное меню", reply_markup=kb_main())

@dp.callback_query(F.data.startswith("opt_"))
async def opt(call: types.CallbackQuery):
    phone = call.data.split("_")[1]
    await call.message.edit_text(f"Настройки {phone}", reply_markup=kb_acc(phone))

@dp.callback_query(F.data.startswith("view_"))
async def view(call: types.CallbackQuery):
    phone = call.data.split("_")[1]
    await call.answer("📸 ...")
    if phone in ACTIVE_SESSIONS:
        try:
            scr = ACTIVE_SESSIONS[phone].get_screenshot_as_png()
            await call.message.answer_photo(BufferedInputFile(scr, "live.png"))
            return
        except: pass
    asyncio.create_task(worker_cycle(phone, force_action="screenshot"))

@dp.callback_query(F.data.startswith("kick_"))
async def kick(call: types.CallbackQuery):
    phone = call.data.split("_")[1]
    await call.answer("⚡ Пнул!")
    asyncio.create_task(worker_cycle(phone, force_action="msg"))

@dp.callback_query(F.data.startswith("del_"))
async def dele(call: types.CallbackQuery):
    phone = call.data.split("_")[1]
    db_delete(phone)
    await call.answer("Удален!", show_alert=True)
    await list_a(call)

async def main():
    init_db()
    asyncio.create_task(farm_scheduler())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
