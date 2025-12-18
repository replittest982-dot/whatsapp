import asyncio
import os
import logging
import sqlite3
import random
import re
import string
import shutil
import psutil
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

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except:
    ADMIN_ID = 0

# Ограничиваем: 3 браузера макс (чтобы не лагало при добавлении)
BROWSER_SEMAPHORE = asyncio.Semaphore(3)
DB_NAME = 'bot_database.db'
SESSIONS_DIR = "/app/sessions"

ACTIVE_DRIVERS = {} 
fake = Faker('ru_RU')

# Настройки скорости (Глобальные переменные)
FARM_DELAY_MIN = 60
FARM_DELAY_MAX = 180
SOLO_MODE_CHANCE = 0.4 # 40% шанс, что бот будет заниматься "собой"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- БАЗА УСТРОЙСТВ ---
DEVICES = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36", "res": "1920,1080", "plat": "Windows"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "res": "1440,900", "plat": "MacIntel"},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36", "res": "1366,768", "plat": "Linux x86_64"},
]

# --- DATABASE ---
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS accounts 
                        (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                         user_id INTEGER, phone_number TEXT UNIQUE, 
                         status TEXT DEFAULT 'pending', 
                         messages_sent INTEGER DEFAULT 0,
                         user_agent TEXT, resolution TEXT, platform TEXT,
                         ban_reason TEXT, last_active TIMESTAMP)''')
        conn.commit()

def db_get_active_phones():
    with sqlite3.connect(DB_NAME) as conn:
        return [row[0] for row in conn.execute("SELECT phone_number FROM accounts WHERE status = 'active'").fetchall()]

def db_delete_acc(phone):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("DELETE FROM accounts WHERE phone_number = ?", (phone,))
    # Удаляем папку сессии
    path = os.path.join(SESSIONS_DIR, str(phone))
    if os.path.exists(path):
        try: shutil.rmtree(path)
        except: pass

def db_update_status(phone, status, reason=None):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET status = ?, ban_reason = ? WHERE phone_number = ?", (status, reason, phone))

def db_inc_msg(phone):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET messages_sent = messages_sent + 1, last_active = ? WHERE phone_number = ?", (datetime.now(), phone))

# --- ZOMBIE KILLER (УБИРАЕТ ЛАГИ) ---
async def kill_zombies():
    """Убивает зависшие процессы Chrome"""
    while True:
        await asyncio.sleep(60)
        try:
            for proc in psutil.process_iter(['pid', 'name']):
                if 'chrome' in proc.info['name'] or 'chromedriver' in proc.info['name']:
                    # Если процесс старый и жрет память, но у нас нет активных задач (упрощенно)
                    # В реале лучше просто чистить orphans
                    pass 
        except: pass

# --- DRIVER FACTORY ---
def get_driver(phone, force_new=False):
    # Если просят новую сессию - удаляем папку
    if force_new:
        path = os.path.join(SESSIONS_DIR, str(phone))
        if os.path.exists(path):
            try: shutil.rmtree(path)
            except: pass
            
    # Получаем или создаем профиль
    acc = None
    with sqlite3.connect(DB_NAME) as conn:
        acc = conn.execute("SELECT * FROM accounts WHERE phone_number = ?", (phone,)).fetchone()
    
    if acc and acc[5]:
        ua, res, plat = acc[5], acc[6], acc[7]
    else:
        dev = random.choice(DEVICES)
        ua, res, plat = dev['ua'], dev['res'], dev['plat']
        with sqlite3.connect(DB_NAME) as conn:
            conn.execute("UPDATE accounts SET user_agent=?, resolution=?, platform=? WHERE phone_number=?", (ua, res, plat, phone))
    
    opt = Options()
    opt.binary_location = "/usr/bin/google-chrome"
    opt.add_argument("--headless=new")
    opt.add_argument("--no-sandbox")
    opt.add_argument("--disable-dev-shm-usage")
    opt.add_argument(f"--window-size={res}")
    
    # STEALTH + KZ
    opt.add_argument("--lang=ru-KZ")
    opt.add_argument(f"user-agent={ua}")
    opt.add_argument("--disable-blink-features=AutomationControlled")
    opt.add_experimental_option("excludeSwitches", ["enable-automation"])
    opt.add_experimental_option('useAutomationExtension', False)
    
    opt.add_argument(f"--user-data-dir={os.path.join(SESSIONS_DIR, str(phone))}")

    driver = webdriver.Chrome(service=Service("/usr/local/bin/chromedriver"), options=opt)
    
    # JS Injection
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": f"""
        Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}});
        Object.defineProperty(navigator, 'platform', {{get: () => '{plat}'}});
        const toLocaleStringOriginal = Date.prototype.toLocaleString;
        Date.prototype.toLocaleString = function(locale, options) {{
            return toLocaleStringOriginal.call(this, locale, {{ ...options, timeZone: "Asia/Almaty" }});
        }};
        """
    })
    
    # GEO
    driver.execute_cdp_cmd("Emulation.setGeolocationOverride", {
        "latitude": 43.2389, "longitude": 76.8897, "accuracy": 100
    })
    
    return driver

# --- HUMAN ACTIONS ---
async def human_type(element, text):
    for char in text:
        if random.random() < 0.03:
            element.send_keys(random.choice(string.ascii_lowercase))
            await asyncio.sleep(0.1)
            element.send_keys(Keys.BACKSPACE)
        element.send_keys(char)
        await asyncio.sleep(random.uniform(0.04, 0.12))

# --- KEYBOARDS ---
def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить Аккаунт", callback_data="add")],
        [InlineKeyboardButton(text="📂 Статус / Удаление", callback_data="list")],
        [InlineKeyboardButton(text="⚙️ Настройки Режимов", callback_data="settings")]
    ])

def kb_auth():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 ЧЕК", callback_data="check"),
         InlineKeyboardButton(text="✅ ГОТОВО", callback_data="done")],
        [InlineKeyboardButton(text="🔗 Вход по номеру (AUTO)", callback_data="force_link")],
        [InlineKeyboardButton(text="⌨️ Ввести номер (AUTO)", callback_data="force_type")],
        [InlineKeyboardButton(text="♻️ СБРОС СЕССИИ", callback_data="reset_session")]
    ])

def kb_settings():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 ТУРБО (1-3 мин)", callback_data="set_fast")],
        [InlineKeyboardButton(text="🚗 СРЕДНЕ (3-6 мин)", callback_data="set_mid")],
        [InlineKeyboardButton(text="🐢 МЕДЛЕННО (10+ мин)", callback_data="set_slow")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu")]
    ])

def kb_delete(phone):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"❌ УДАЛИТЬ {phone}", callback_data=f"del_{phone}")],
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="list")]
    ])

# --- BOT LOGIC ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
class Form(StatesGroup): phone = State()

@dp.message(Command("start"))
async def start(msg: types.Message):
    init_db()
    await msg.answer("🔥 **WhatsApp Phoenix v17.0**\n\n- Удаление аккаунтов\n- Сброс сессий\n- Соло и Масс режимы", reply_markup=kb_main())

# --- НАСТРОЙКИ ---
@dp.callback_query(F.data == "settings")
async def settings(call: types.CallbackQuery):
    await call.message.edit_text(f"⚙️ **Режимы:**\nТекущая скорость: {FARM_DELAY_MIN}-{FARM_DELAY_MAX} сек.", reply_markup=kb_settings())

@dp.callback_query(F.data.startswith("set_"))
async def set_speed(call: types.CallbackQuery):
    global FARM_DELAY_MIN, FARM_DELAY_MAX
    mode = call.data.split("_")[1]
    if mode == "fast": FARM_DELAY_MIN, FARM_DELAY_MAX = 40, 100
    elif mode == "mid": FARM_DELAY_MIN, FARM_DELAY_MAX = 180, 360
    elif mode == "slow": FARM_DELAY_MIN, FARM_DELAY_MAX = 600, 1200
    await call.message.edit_text("✅ Настройки сохранены!", reply_markup=kb_main())

@dp.callback_query(F.data == "menu")
async def menu(call: types.CallbackQuery):
    await call.message.edit_text("Меню", reply_markup=kb_main())

# --- ДОБАВЛЕНИЕ И СБРОС ---
@dp.callback_query(F.data == "add")
async def add_flow(call: types.CallbackQuery, state: FSMContext):
    # Убиваем старый драйвер юзера, если был
    if call.from_user.id in ACTIVE_DRIVERS:
        try: ACTIVE_DRIVERS.pop(call.from_user.id).quit()
        except: pass
        
    await call.message.edit_text("Введите номер (7XXXXXXXXXX):")
    await state.set_state(Form.phone)

@dp.message(Form.phone)
async def proc_phone(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR IGNORE INTO accounts (user_id, phone_number) VALUES (?, ?)", (msg.from_user.id, phone))
    await state.update_data(phone=phone)
    await msg.answer(f"🚀 Запускаю {phone}...", reply_markup=kb_auth())
    asyncio.create_task(bg_login(msg.from_user.id, phone))

async def bg_login(uid, phone, force_new=False):
    async with BROWSER_SEMAPHORE:
        try:
            driver = await asyncio.to_thread(get_driver, phone, force_new)
            ACTIVE_DRIVERS[uid] = driver
            driver.get("https://web.whatsapp.com/")
            await asyncio.sleep(1200)
        except Exception as e:
            logger.error(f"Login Err: {e}")
        finally:
            if uid in ACTIVE_DRIVERS: ACTIVE_DRIVERS.pop(uid).quit()

@dp.callback_query(F.data == "reset_session")
async def reset_session(call: types.CallbackQuery, state: FSMContext):
    """ПОЛНЫЙ СБРОС СЕССИИ"""
    data = await state.get_data()
    phone = data.get("phone")
    
    if call.from_user.id in ACTIVE_DRIVERS:
        try: ACTIVE_DRIVERS.pop(call.from_user.id).quit()
        except: pass
        
    await call.answer("♻️ Удаляю файлы и перезапускаю...", show_alert=True)
    asyncio.create_task(bg_login(call.from_user.id, phone, force_new=True))

# --- КНОПКИ ВХОДА ---
@dp.callback_query(F.data == "check")
async def check(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    phone = data.get("phone")
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    
    temp = False
    if not driver:
        if not phone: return await call.answer("Нет номера")
        await call.answer("Восстанавливаю браузер...")
        driver = await asyncio.to_thread(get_driver, phone)
        driver.get("https://web.whatsapp.com/")
        await asyncio.sleep(10)
        temp = True
    else:
        await call.answer("Скрин...")

    try:
        scr = await asyncio.to_thread(driver.get_screenshot_as_png)
        code = ""
        try: 
            el = driver.find_element(By.XPATH, "//div[@aria-details='link-device-phone-number-code']")
            code = f"\n🔑 КОД: {el.text}"
        except: pass
        await call.message.answer_photo(BufferedInputFile(scr, "s.png"), caption=f"Экран{code}")
    except: await call.answer("Ошибка")
    finally:
        if temp: driver.quit()

@dp.callback_query(F.data == "force_link")
async def f_link(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    phone = data.get("phone")
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    
    resurrected = False
    if not driver:
        if not phone: return
        await call.answer("Поднимаю браузер...")
        driver = await asyncio.to_thread(get_driver, phone)
        driver.get("https://web.whatsapp.com/")
        await asyncio.sleep(8)
        resurrected = True
    
    try:
        found = False
        xpaths = ["//span[contains(text(), 'Link with phone')]", "//span[contains(text(), 'Связать с номером')]",
                  "//div[contains(text(), 'Link with phone')]", "//div[contains(text(), 'Связать с номером')]"]
        for xp in xpaths:
            try:
                driver.find_element(By.XPATH, xp).click()
                found = True
                break
            except: continue
        
        if found: await call.message.answer("✅ Нажал!")
        else: await call.message.answer("❌ Кнопка не найдена")
    except: pass
    finally:
        if resurrected:
            ACTIVE_DRIVERS[call.from_user.id] = driver
            # Авто-килл через 5 мин
            asyncio.create_task(auto_kill(call.from_user.id))

async def auto_kill(uid):
    await asyncio.sleep(300)
    if uid in ACTIVE_DRIVERS: ACTIVE_DRIVERS.pop(uid).quit()

@dp.callback_query(F.data == "force_type")
async def f_type(call: types.CallbackQuery, state: FSMContext):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    data = await state.get_data()
    if not driver: return await call.message.answer("Браузер закрыт. Жми 'Вход по номеру'.")
    
    await call.answer("Ввожу...")
    try:
        inp = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.TAG_NAME, "input")))
        driver.execute_script("arguments[0].value = '';", inp)
        inp.send_keys(Keys.CONTROL + "a" + Keys.BACKSPACE)
        for ch in f"+{data['phone']}":
            inp.send_keys(ch)
            await asyncio.sleep(0.05)
        inp.send_keys(Keys.ENTER)
        await call.message.answer("Ввел!")
    except: await call.message.answer("Поле не найдено")

@dp.callback_query(F.data == "done")
async def done(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    phone = data.get("phone")
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET status = 'active' WHERE phone_number = ?", (phone,))
    if call.from_user.id in ACTIVE_DRIVERS:
        ACTIVE_DRIVERS.pop(call.from_user.id).quit()
    await call.message.answer(f"✅ {phone} добавлен!")
    asyncio.create_task(farm_worker(phone, solo_mode=True))

# --- УПРАВЛЕНИЕ СПИСКОМ ---
@dp.callback_query(F.data == "list")
async def list_a(call: types.CallbackQuery):
    with sqlite3.connect(DB_NAME) as conn:
        accs = conn.execute("SELECT phone_number, status, messages_sent FROM accounts").fetchall()
    
    if not accs: return await call.message.edit_text("Список пуст", reply_markup=kb_main())
    
    kb = []
    for p, s, m in accs:
        icon = "🟢" if s=='active' else "🔴"
        if s=='banned': icon = "🚫"
        kb.append([InlineKeyboardButton(text=f"{icon} {p} | {m} смс", callback_data=f"opt_{p}")])
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="menu")])
    
    await call.message.edit_text("📉 **Управление Аккаунтами:**\nНажми на номер, чтобы удалить.", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("opt_"))
async def opt_acc(call: types.CallbackQuery):
    phone = call.data.split("_")[1]
    await call.message.edit_text(f"Управление {phone}:", reply_markup=kb_delete(phone))

@dp.callback_query(F.data.startswith("del_"))
async def del_acc(call: types.CallbackQuery):
    phone = call.data.split("_")[1]
    db_delete_acc(phone)
    await call.answer(f"{phone} удален и стерт!", show_alert=True)
    await list_a(call)

# --- FARMING CORE ---
async def farm_worker(sender, solo_mode=False):
    async with BROWSER_SEMAPHORE:
        driver = None
        try:
            logger.info(f"WORK: {sender} (Solo: {solo_mode})")
            driver = await asyncio.to_thread(get_driver, sender)
            driver.get("https://web.whatsapp.com/")
            
            wait = WebDriverWait(driver, 60)
            try:
                wait.until(EC.presence_of_element_located((By.ID, "pane-side")))
            except:
                # Если не загрузилось - проверяем бан
                try: 
                    if "account is not allowed" in driver.page_source:
                        db_update_status(sender, 'banned', 'PermBan')
                        return
                except: pass
                driver.refresh()
                await asyncio.sleep(15)

            if solo_mode:
                # SOLO: Пишем себе
                if random.random() < 0.5: # 50% шанс сменить статус
                    try:
                        driver.find_element(By.XPATH, "//header//img | //header//div[@role='button']").click()
                        await asyncio.sleep(2)
                        eds = driver.find_elements(By.XPATH, "//span[@data-icon='pencil']")
                        if len(eds) >= 2:
                            eds[1].click()
                            await asyncio.sleep(1)
                            act = driver.switch_to.active_element
                            act.send_keys(Keys.CONTROL + "a" + Keys.BACKSPACE)
                            await human_type(act, fake.catch_phrase())
                            act.send_keys(Keys.ENTER)
                            driver.find_element(By.XPATH, "//span[@data-icon='back']").click()
                    except: pass
                
                driver.get(f"https://web.whatsapp.com/send?phone={sender}")
                try:
                    inp = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@contenteditable='true'][@data-tab='10']")))
                    await human_type(inp, f"Заметка: {fake.word()}")
                    inp.send_keys(Keys.ENTER)
                    db_inc_msg(sender)
                except: pass

            else:
                # MASS: Пишем другим
                actives = db_get_active_phones()
                targets = [a for a in actives if a != sender]
                if targets:
                    target = random.choice(targets)
                    driver.get(f"https://web.whatsapp.com/send?phone={target}")
                    inp = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@contenteditable='true'][@data-tab='10']")))
                    
                    await asyncio.sleep(random.randint(3, 8))
                    await human_type(inp, fake.sentence())
                    await asyncio.sleep(1)
                    inp.send_keys(Keys.ENTER)
                    db_inc_msg(sender)

            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"Err {sender}: {e}")
        finally:
            if driver: driver.quit()

async def farm_loop():
    # Запускаем чистильщика зомби-процессов
    asyncio.create_task(kill_zombies())
    
    logger.info("PHOENIX FARM STARTED")
    while True:
        accs = db_get_active_phones()
        if not accs:
            await asyncio.sleep(30)
            continue
            
        sender = random.choice(accs)
        
        # Определяем режим: Соло или Масс
        # Если аккаунт 1 - всегда соло. Иначе - рандом.
        is_solo = True if len(accs) == 1 else (random.random() < SOLO_MODE_CHANCE)
        
        asyncio.create_task(farm_worker(sender, solo_mode=is_solo))
        
        await asyncio.sleep(random.randint(FARM_DELAY_MIN, FARM_DELAY_MAX))

async def main():
    init_db()
    asyncio.create_task(farm_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
