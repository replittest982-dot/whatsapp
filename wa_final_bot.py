import asyncio
import os
import logging
import sqlite3
import random
import re
import string
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
# Айди админа (число!). Если их несколько, можно сделать список.
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except:
    ADMIN_ID = 0

# МАКСИМУМ 4 БРАУЗЕРА ОДНОВРЕМЕННО (Чтобы сервер жил)
BROWSER_SEMAPHORE = asyncio.Semaphore(4)
DB_NAME = 'bot_database.db'
SESSIONS_DIR = "/app/sessions"

ACTIVE_DRIVERS = {} 
fake = Faker('ru_RU')

# Скорость фарма (сек)
FARM_DELAY_MIN = 40
FARM_DELAY_MAX = 120

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

def db_get_acc(phone):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT * FROM accounts WHERE phone_number = ?", (phone,)).fetchone()

def db_get_active_phones():
    with sqlite3.connect(DB_NAME) as conn:
        return [row[0] for row in conn.execute("SELECT phone_number FROM accounts WHERE status = 'active'").fetchall()]

def db_update_status(phone, status, reason=None):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET status = ?, ban_reason = ? WHERE phone_number = ?", (status, reason, phone))

def db_inc_msg(phone):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET messages_sent = messages_sent + 1, last_active = ? WHERE phone_number = ?", (datetime.now(), phone))

def db_get_stats():
    with sqlite3.connect(DB_NAME) as conn:
        total = conn.execute("SELECT count(*) FROM accounts").fetchone()[0]
        active = conn.execute("SELECT count(*) FROM accounts WHERE status = 'active'").fetchone()[0]
        banned = conn.execute("SELECT count(*) FROM accounts WHERE status = 'banned'").fetchone()[0]
        sent = conn.execute("SELECT sum(messages_sent) FROM accounts").fetchone()[0] or 0
    return total, active, banned, sent

# --- MEMORY GUARD ---
def is_memory_critical():
    """Если памяти меньше 200МБ - тормозим"""
    mem = psutil.virtual_memory()
    if (mem.available / 1024 / 1024) < 200: return True
    return False

# --- DRIVER FACTORY ---
def get_driver(phone):
    acc = db_get_acc(phone)
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
    
    # JS INJECTION (Timezone Almaty + Anti-Detect)
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
    
    # GEO INJECTION
    driver.execute_cdp_cmd("Emulation.setGeolocationOverride", {
        "latitude": 43.2389, "longitude": 76.8897, "accuracy": 100
    })
    
    return driver

# --- HUMAN ACTIONS ---
async def human_type(element, text):
    for char in text:
        if random.random() < 0.04:
            element.send_keys(random.choice(string.ascii_lowercase))
            await asyncio.sleep(0.1)
            element.send_keys(Keys.BACKSPACE)
        element.send_keys(char)
        await asyncio.sleep(random.uniform(0.04, 0.12))

async def check_ban_status(driver, phone):
    try:
        # Если видим QR, но статус Active -> Слет
        if "WhatsApp Web" in driver.title and len(driver.find_elements(By.XPATH, "//canvas")) > 0:
            logger.warning(f"QR DETECTED: {phone}")
            return "QR"
        
        # Проверка текста бана (может отличаться)
        page_text = driver.find_element(By.TAG_NAME, "body").text
        if "account is not allowed" in page_text or "spam" in page_text.lower():
            logger.error(f"BAN DETECTED: {phone}")
            db_update_status(phone, 'banned', 'PermBan')
            return "BAN"
        return False
    except: return False

# --- KEYBOARDS ---
def kb_main(uid):
    kb = [[InlineKeyboardButton(text="➕ Добавить Аккаунт", callback_data="add")],
          [InlineKeyboardButton(text="📂 Мои Аккаунты", callback_data="list")]]
    if uid == ADMIN_ID:
        kb.append([InlineKeyboardButton(text="👑 Админ Панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def kb_auth():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 ЧЕК", callback_data="check"),
         InlineKeyboardButton(text="✅ ГОТОВО", callback_data="done")],
        [InlineKeyboardButton(text="🔗 Вход по номеру (AUTO)", callback_data="force_link")],
        [InlineKeyboardButton(text="⌨️ Ввести номер (AUTO)", callback_data="force_type")]
    ])

def kb_admin():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить статусы", callback_data="adm_refresh")],
        [InlineKeyboardButton(text="🗑 Очистить 'pending'", callback_data="adm_clean")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu")]
    ])

# --- BOT LOGIC ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
class Form(StatesGroup): phone = State()

@dp.message(Command("start"))
async def start(msg: types.Message):
    init_db()
    await msg.answer("🏛 **WhatsApp Imperator v16.0**\n\n- 4 Потока\n- KZ Маскировка\n- Авто-восстановление сессий\n\nЖми кнопку ниже:", reply_markup=kb_main(msg.from_user.id))

@dp.message(Command("admin"))
async def admin_cmd(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    await show_admin_panel(msg)

async def show_admin_panel(message_obj):
    tot, act, ban, sent = db_get_stats()
    mem = psutil.virtual_memory()
    ram_usage = f"{mem.percent}% ({int(mem.available/1024/1024)}MB free)"
    
    txt = (f"👑 **АДМИН ПАНЕЛЬ**\n\n"
           f"📱 Всего аккаунтов: {tot}\n"
           f"🟢 Активных: {act}\n"
           f"🚫 В бане: {ban}\n"
           f"📨 Отправлено: {sent}\n"
           f"💾 RAM Сервера: {ram_usage}")
    
    if isinstance(message_obj, types.CallbackQuery):
        await message_obj.message.edit_text(txt, reply_markup=kb_admin())
    else:
        await message_obj.answer(txt, reply_markup=kb_admin())

@dp.callback_query(F.data == "admin_panel")
async def admin_cb(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID: return await call.answer("Доступ запрещен")
    await show_admin_panel(call)

@dp.callback_query(F.data == "adm_refresh")
async def adm_refresh(call: types.CallbackQuery):
    await show_admin_panel(call)

@dp.callback_query(F.data == "adm_clean")
async def adm_clean(call: types.CallbackQuery):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("DELETE FROM accounts WHERE status = 'pending'")
    await call.answer("Мусор удален")
    await show_admin_panel(call)

@dp.callback_query(F.data == "menu")
async def back_menu(call: types.CallbackQuery):
    await call.message.edit_text("Главное меню", reply_markup=kb_main(call.from_user.id))

# --- ADD ACCOUNT FLOW ---
@dp.callback_query(F.data == "add")
async def add_flow(call: types.CallbackQuery, state: FSMContext):
    await call.message.edit_text("📞 Введите номер (7XXXXXXXXXX):")
    await state.set_state(Form.phone)

@dp.message(Form.phone)
async def proc_phone(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR IGNORE INTO accounts (user_id, phone_number) VALUES (?, ?)", (msg.from_user.id, phone))
    await state.update_data(phone=phone)
    await msg.answer(f"🚀 Запускаю браузер для {phone}...\n\n1. Жди 10-15 сек\n2. Если 'Браузер закрыт' — жми кнопки, я сам открою.", reply_markup=kb_auth())
    asyncio.create_task(bg_login_initial(msg.from_user.id, phone))

async def bg_login_initial(uid, phone):
    # Пытаемся запустить браузер для первичного входа
    async with BROWSER_SEMAPHORE:
        try:
            driver = await asyncio.to_thread(get_driver, phone)
            ACTIVE_DRIVERS[uid] = driver # Сохраняем в память для кнопок
            driver.get("https://web.whatsapp.com/")
            await asyncio.sleep(900) # Держим 15 минут
        except: pass
        finally:
            if uid in ACTIVE_DRIVERS: ACTIVE_DRIVERS.pop(uid).quit()

# --- SMART BUTTONS (AUTO-RESURRECT) ---
@dp.callback_query(F.data == "check")
async def check(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    phone = data.get("phone")
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    
    # Если браузер вылетел - ВОСКРЕШАЕМ на 15 секунд для скрина
    temp_driver = False
    if not driver:
        if not phone: return await call.answer("Сначала введи номер")
        if is_memory_critical(): return await call.answer("Сервер перегружен, жди...")
        
        await call.answer("♻️ Восстанавливаю сессию...")
        try:
            driver = await asyncio.to_thread(get_driver, phone)
            driver.get("https://web.whatsapp.com/")
            await asyncio.sleep(8)
            temp_driver = True
        except: return await call.answer("Ошибка запуска")
    else:
        await call.answer("Делаю скрин...")

    try:
        scr = await asyncio.to_thread(driver.get_screenshot_as_png)
        code = ""
        try: 
            el = driver.find_element(By.XPATH, "//div[@aria-details='link-device-phone-number-code']")
            code = f"\n🔑 КОД: {el.text}"
        except: pass
        
        await call.message.answer_photo(BufferedInputFile(scr, "s.png"), caption=f"Экран{code}")
    except: await call.answer("Ошибка скрина")
    finally:
        if temp_driver: driver.quit()

@dp.callback_query(F.data == "force_link")
async def f_link(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    phone = data.get("phone")
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    
    # ВОСКРЕШЕНИЕ БРАУЗЕРА
    resurrected = False
    if not driver:
        if not phone: return
        await call.answer("♻️ Запускаю браузер для нажатия...")
        driver = await asyncio.to_thread(get_driver, phone)
        driver.get("https://web.whatsapp.com/")
        await asyncio.sleep(7)
        resurrected = True
    else:
        await call.answer("Ищу кнопку...")
    
    try:
        xpaths = ["//span[contains(text(), 'Link with phone')]", "//span[contains(text(), 'Связать с номером')]",
                  "//div[contains(text(), 'Link with phone')]", "//div[contains(text(), 'Связать с номером')]"]
        found = False
        for xp in xpaths:
            try:
                btn = driver.find_element(By.XPATH, xp)
                driver.execute_script("arguments[0].click();", btn)
                found = True
                break
            except: continue
        
        if found: await call.message.answer("✅ Нажал! Жми 'Ввести номер'.")
        else: await call.message.answer("❌ Кнопка не найдена (попробуй ЧЕК)")

    except Exception as e: await call.message.answer(f"Ошибка: {e}")
    finally:
        # Если мы воскресили браузер, сохраняем его в ACTIVE_DRIVERS, чтобы следующая кнопка сработала
        if resurrected:
            ACTIVE_DRIVERS[call.from_user.id] = driver
            # Запускаем таймер на авто-закрытие через 5 минут, чтобы не висел вечно
            asyncio.create_task(auto_close(call.from_user.id, driver))

async def auto_close(uid, driver):
    await asyncio.sleep(300)
    try: driver.quit()
    except: pass
    if uid in ACTIVE_DRIVERS: del ACTIVE_DRIVERS[uid]

@dp.callback_query(F.data == "force_type")
async def f_type(call: types.CallbackQuery, state: FSMContext):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    data = await state.get_data()
    
    if not driver: return await call.message.answer("⚠️ Браузер закрыт. Сначала нажми 'Вход по номеру', он запустится.")
    
    await call.answer("Печатаю...")
    try:
        # Умное ожидание поля
        inp = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "input")))
        driver.execute_script("arguments[0].value = '';", inp)
        inp.send_keys(Keys.CONTROL + "a" + Keys.BACKSPACE)
        for ch in f"+{data['phone']}":
            inp.send_keys(ch)
            await asyncio.sleep(0.05)
        inp.send_keys(Keys.ENTER)
        await call.message.answer(f"✅ Ввел +{data['phone']}! Жми ЧЕК.")
    except: 
        await call.message.answer("❌ Не нашел поле ввода. Проверь экран.")

@dp.callback_query(F.data == "done")
async def done(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    phone = data.get("phone")
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET status = 'active' WHERE phone_number = ?", (phone,))
    
    # Закрываем окно регистрации, передаем в ферму
    if call.from_user.id in ACTIVE_DRIVERS:
        ACTIVE_DRIVERS.pop(call.from_user.id).quit()
    
    await call.message.answer(f"✅ {phone} сохранен в базу!")
    # Моментальный пинок
    asyncio.create_task(farm_worker(phone, solo_mode=True))

@dp.callback_query(F.data == "list")
async def list_a(call: types.CallbackQuery):
    accs = db_get_active_phones()
    with sqlite3.connect(DB_NAME) as conn:
        all_d = conn.execute("SELECT phone_number, status, messages_sent FROM accounts").fetchall()
    
    txt = f"📊 **Аккаунты ({len(all_d)}):**\n"
    for p, s, m in all_d:
        icon = "🟢" if s=='active' else "🔴"
        if s=='banned': icon = "🚫"
        txt += f"\n{icon} `{p}` | {m}"
    await call.message.answer(txt, reply_markup=kb_main(call.from_user.id), parse_mode="Markdown")

# --- FARM ENGINE (4 THREADS) ---
async def farm_worker(sender, solo_mode=False):
    # Ждем память
    while is_memory_critical(): await asyncio.sleep(10)
    
    async with BROWSER_SEMAPHORE:
        driver = None
        try:
            logger.info(f"WORK: {sender}")
            driver = await asyncio.to_thread(get_driver, sender)
            driver.get("https://web.whatsapp.com/")
            
            wait = WebDriverWait(driver, 60)
            try:
                wait.until(EC.presence_of_element_located((By.ID, "pane-side")))
            except:
                status = await check_ban_status(driver, sender)
                if status: return # Stop if ban/qr
                driver.refresh()
                await asyncio.sleep(15)

            if solo_mode:
                # SOLO: Change Bio + Write to Self
                if random.random() < 0.4:
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
                
                # Write Self
                driver.get(f"https://web.whatsapp.com/send?phone={sender}")
                try:
                    inp = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@contenteditable='true'][@data-tab='10']")))
                    await human_type(inp, f"Заметка: {fake.date()}")
                    inp.send_keys(Keys.ENTER)
                    db_inc_msg(sender)
                except: pass

            else:
                # PAIR: Write Other
                actives = db_get_active_phones()
                targets = [a for a in actives if a != sender]
                if targets:
                    target = random.choice(targets)
                    driver.get(f"https://web.whatsapp.com/send?phone={target}")
                    inp = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@contenteditable='true'][@data-tab='10']")))
                    
                    await asyncio.sleep(random.randint(2, 6))
                    await human_type(inp, fake.sentence())
                    await asyncio.sleep(1)
                    inp.send_keys(Keys.ENTER)
                    db_inc_msg(sender)

            await asyncio.sleep(5)
        except Exception as e:
            logger.error(f"ERR {sender}: {e}")
        finally:
            if driver: driver.quit()

async def farm_loop():
    logger.info("🔥 IMPERATOR FARM STARTED")
    while True:
        accs = db_get_active_phones()
        if not accs:
            await asyncio.sleep(30)
            continue
            
        # Выбираем рандомного бойца
        sender = random.choice(accs)
        
        # 50% Соло (смена био, заметки) - безопаснее
        is_solo = random.random() < 0.5
        
        # Запускаем в фоне (очередь регулируется семафором)
        asyncio.create_task(farm_worker(sender, solo_mode=is_solo))
        
        # Пауза между запусками потоков
        await asyncio.sleep(random.randint(FARM_DELAY_MIN, FARM_DELAY_MAX))

async def main():
    init_db()
    asyncio.create_task(farm_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
