import asyncio
import os
import logging
import sqlite3
import random
import re
import psutil
import shutil
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from faker import Faker

# --- SELENIUM & WEBDRIVER ---
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException, WebDriverException

# ======================= КОНФИГ =======================
BOT_TOKEN = os.environ.get("BOT_TOKEN") 
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except:
    ADMIN_ID = 0

# Лимиты
BROWSER_SEMAPHORE = asyncio.Semaphore(3) # Макс 3 окна
DB_NAME = 'fixed_farm.db'
SESSIONS_DIR = "./sessions"
ACTIVE_DRIVERS = {} 

# Логирование
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("WA_TESTER")
fake = Faker('ru_RU')

# Скорости фарма
SPEED_MODES = {
    "TURBO": (60, 180),    # 1-3 мин
    "MEDIUM": (600, 1200), # 10-20 мин
    "SLOW": (1800, 3600)   # 30-60 мин
}
CURRENT_SPEED = "MEDIUM"

# ======================= AI TEXT ENGINE =======================
class TextEngine:
    def get_appeal(self, phone):
        intros = ["Hello WhatsApp,", "Dear Support,", "Здравствуйте,", "Приветствую поддержку,"]
        body = [
            f"My number {phone} is banned.", 
            "I cannot access my account.", 
            "Мой номер заблокирован ошибочно.", 
            "Пишет, что аккаунт в бане, но я ничего не делал."
        ]
        context = ["I use it for work.", "It is my personal number.", "Мне нужен ватсап для работы.", "Я студент, мне нужна связь."]
        ends = ["Unban please.", "Help me.", "Прошу разблокировать.", "Жду ответа."]
        return f"{random.choice(intros)} {random.choice(body)} {random.choice(context)} {random.choice(ends)}"

    def get_chat_msg(self):
        msgs = ["Привет", "Как дела?", "Надо встретиться", "Скинь документы", "Ok", "Later", "Перезвони", "Доброе утро", "Ты где?"]
        return random.choice(msgs)

ai_engine = TextEngine()

# ======================= БАЗА ДАННЫХ =======================
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS accounts 
                        (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                         user_id INTEGER, phone_number TEXT UNIQUE, 
                         status TEXT DEFAULT 'pending', 
                         messages_sent INTEGER DEFAULT 0,
                         last_active TIMESTAMP)''')

def db_get_active_phones():
    with sqlite3.connect(DB_NAME) as conn:
        return [row[0] for row in conn.execute("SELECT phone_number FROM accounts WHERE status = 'active'").fetchall()]

def db_update_status(phone, status):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET status = ? WHERE phone_number = ?", (status, phone))

def db_inc_msg(phone):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET messages_sent = messages_sent + 1, last_active = ? WHERE phone_number = ?", (datetime.now(), phone))

# ======================= DRIVER (FIXED & STEALTH) =======================
def get_driver(phone, headless=True):
    # 1. ПРОВЕРКА ПАМЯТИ (ОЧЕНЬ ВАЖНО)
    # Если памяти меньше 100 МБ, мы даже не пытаемся запустить хром, чтобы не крашнуть сервер
    if psutil.virtual_memory().available < 100 * 1024 * 1024:
        logger.warning("⚠️ CRITICAL RAM LOW. Skip launch.")
        return None

    path = os.path.join(SESSIONS_DIR, str(phone)) if phone else None
    
    opt = Options()
    if headless: 
        opt.add_argument("--headless=new")
    
    # 2. ФЛАГИ СТАБИЛЬНОСТИ (ЧТОБЫ НЕ КРАШИЛОСЬ)
    opt.add_argument("--no-sandbox") 
    opt.add_argument("--disable-dev-shm-usage") 
    opt.add_argument("--disable-gpu")
    opt.add_argument("--window-size=1280,720") # Меньше разрешение = меньше нагрузка
    opt.add_argument("--disable-extensions")
    opt.add_argument("--disable-infobars")
    
    # Отключаем загрузку картинок (Экономит 50% RAM)
    opt.add_argument("--blink-settings=imagesEnabled=false")
    
    opt.page_load_strategy = 'eager' # Не ждем полной загрузки тяжелых скриптов
    
    # Маскировка
    opt.add_argument("--disable-blink-features=AutomationControlled")
    opt.add_experimental_option("excludeSwitches", ["enable-automation"])
    opt.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    
    if path:
        if not os.path.exists(path): os.makedirs(path)
        opt.add_argument(f"--user-data-dir={path}")

    try:
        driver = webdriver.Chrome(options=opt)
        
        # JS Инъекции (Маскировка железа)
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                const getParameter = WebGLRenderingContext.prototype.getParameter;
                WebGLRenderingContext.prototype.getParameter = function(parameter) {
                    if (parameter === 37445) return 'Google Inc. (NVIDIA)';
                    if (parameter === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)';
                    return getParameter(parameter);
                };
            """
        })
        return driver
    except Exception as e:
        logger.error(f"❌ Driver Crash: {e}")
        return None

async def human_type(element, text):
    for char in text:
        element.send_keys(char)
        await asyncio.sleep(random.uniform(0.05, 0.15))

# ======================= BOT SETUP =======================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class Form(StatesGroup):
    phone = State()
    unban_email = State()
    unban_phone = State()

def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ ВХОД (LOGIN)", callback_data="add"),
         InlineKeyboardButton(text="🚑 РАЗБАН (UNBAN)", callback_data="unban_start")],
        [InlineKeyboardButton(text=f"⚡️ РЕЖИМ: {CURRENT_SPEED}", callback_data="change_speed")],
        [InlineKeyboardButton(text="📂 Активные", callback_data="list")]
    ])

def kb_speed():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 TURBO", callback_data="set_speed_TURBO"),
         InlineKeyboardButton(text="🚗 MEDIUM", callback_data="set_speed_MEDIUM")],
        [InlineKeyboardButton(text="🐢 SLOW", callback_data="set_speed_SLOW"),
         InlineKeyboardButton(text="🔙 Назад", callback_data="menu")]
    ])

def kb_manual():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 ЧЕК", callback_data="check"),
         InlineKeyboardButton(text="✅ ГОТОВО", callback_data="done")],
        [InlineKeyboardButton(text="1. Нажать Ссылку", callback_data="btn_link")],
        [InlineKeyboardButton(text="2. Ввести Номер", callback_data="btn_type")],
        [InlineKeyboardButton(text="3. Получить КОД", callback_data="btn_code")]
    ])

# ======================= HANDLERS =======================
@dp.message(Command("start"))
async def start(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return 
    init_db()
    await msg.answer("🛠 **WA REPAIR BOT**\nВсе ошибки пофикшены.", reply_markup=kb_main())

# --- SPEED ---
@dp.callback_query(F.data == "change_speed")
async def sp_menu(call: types.CallbackQuery):
    await call.message.edit_text("Выбери скорость фарма:", reply_markup=kb_speed())

@dp.callback_query(F.data.startswith("set_speed_"))
async def sp_set(call: types.CallbackQuery):
    global CURRENT_SPEED
    CURRENT_SPEED = call.data.split("_")[-1]
    await call.message.edit_text(f"✅ Установлено: {CURRENT_SPEED}", reply_markup=kb_main())

@dp.callback_query(F.data == "menu")
async def back(call: types.CallbackQuery):
    await call.message.edit_text("Меню", reply_markup=kb_main())

# --- LOGIN FLOW ---
@dp.callback_query(F.data == "add")
async def add_s1(call: types.CallbackQuery, state: FSMContext):
    # Очистка старой сессии
    if call.from_user.id in ACTIVE_DRIVERS:
        try: ACTIVE_DRIVERS[call.from_user.id].quit()
        except: pass
        del ACTIVE_DRIVERS[call.from_user.id]

    await call.message.edit_text("Введи номер (7XXXXXXXXXX):")
    await state.set_state(Form.phone)

@dp.message(Form.phone)
async def add_s2(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR IGNORE INTO accounts (user_id, phone_number) VALUES (?, ?)", (msg.from_user.id, phone))
    await state.update_data(phone=phone)
    
    await msg.answer(f"⏳ Открываю браузер для **{phone}**...", reply_markup=kb_manual())
    asyncio.create_task(bg_manual_hold(msg.from_user.id, phone))

async def bg_manual_hold(uid, phone):
    try:
        # headless=False если хочешь видеть, но на сервере используй True
        driver = await asyncio.to_thread(get_driver, phone, headless=True)
        if not driver:
            await bot.send_message(uid, "❌ Ошибка памяти. Не смог открыть хром.")
            return
            
        ACTIVE_DRIVERS[uid] = driver
        driver.get("https://web.whatsapp.com/")
        
        # Держим 15 минут
        for _ in range(90):
            if uid not in ACTIVE_DRIVERS: break
            await asyncio.sleep(10)
    except Exception as e:
        logger.error(f"Login Hold Err: {e}")
    finally:
        if uid in ACTIVE_DRIVERS:
            try: ACTIVE_DRIVERS[uid].quit()
            except: pass
            del ACTIVE_DRIVERS[uid]

# --- LOGIN BUTTONS ---
@dp.callback_query(F.data == "check")
async def check_scr(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Браузер закрыт")
    try:
        scr = await asyncio.to_thread(driver.get_screenshot_as_png)
        await call.message.answer_photo(BufferedInputFile(scr, "s.png"))
    except: await call.answer("Ошибка фото")

@dp.callback_query(F.data == "btn_link")
async def btn_link(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return
    try:
        # Пробуем несколько вариантов кнопки
        xpaths = [
            "//span[contains(text(), 'Link with phone')]", 
            "//span[contains(text(), 'Связать с номером')]",
            "//a[contains(@href, 'link-device')]"
        ]
        found = False
        for xp in xpaths:
            try: 
                driver.find_element(By.XPATH, xp).click()
                found = True
                break
            except: continue
        
        if found: await call.answer("✅ Нажал!")
        else: await call.answer("❌ Кнопка не найдена (проверь чек)")
    except: await call.answer("Ошибка клика")

@dp.callback_query(F.data == "btn_type")
async def btn_type(call: types.CallbackQuery, state: FSMContext):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    data = await state.get_data()
    phone = data.get("phone")
    if not driver or not phone: return
    
    try:
        # Явное ожидание поля
        inp = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.XPATH, "//input[@aria-label='Type your phone number.'] | //input[@type='text']")))
        
        # Жесткая очистка
        driver.execute_script("arguments[0].value = '';", inp)
        inp.send_keys(Keys.CONTROL + "a" + Keys.BACKSPACE)
        
        # Ввод
        for ch in phone: 
            inp.send_keys(ch)
            await asyncio.sleep(0.05)
        inp.send_keys(Keys.ENTER)
        await call.answer(f"Ввел: {phone}")
    except: await call.answer("❌ Поле ввода не найдено")

@dp.callback_query(F.data == "btn_code")
async def btn_code(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return
    try:
        el = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.XPATH, "//div[@aria-details='link-device-phone-number-code']")))
        await call.message.answer(f"🔑 **КОД:** `{el.text}`", parse_mode="Markdown")
    except: 
        scr = await asyncio.to_thread(driver.get_screenshot_as_png)
        await call.message.answer_photo(BufferedInputFile(scr, "err.png"), caption="❌ Код не найден")

@dp.callback_query(F.data == "done")
async def done_login(call: types.CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    if uid in ACTIVE_DRIVERS:
        ACTIVE_DRIVERS[uid].quit()
        del ACTIVE_DRIVERS[uid]
    
    data = await state.get_data()
    if data.get("phone"):
        db_update_status(data.get("phone"), 'active')
        await call.message.edit_text("✅ Аккаунт готов к работе!")

@dp.callback_query(F.data == "list")
async def list_a(call: types.CallbackQuery):
    phones = db_get_active_phones()
    txt = "\n".join([f"🟢 {p}" for p in phones]) if phones else "Пусто"
    await call.message.edit_text(f"Список:\n{txt}", reply_markup=kb_main())

# --- UNBAN LOGIC (FIXED) ---
@dp.callback_query(F.data == "unban_start")
async def un_s1(call: types.CallbackQuery, state: FSMContext):
    await call.message.edit_text("📧 Введи EMAIL:")
    await state.set_state(Form.unban_email)

@dp.message(Form.unban_email)
async def un_s2(msg: types.Message, state: FSMContext):
    await state.update_data(unban_email=msg.text.strip())
    await msg.answer("📞 Введи ЗАБАНЕННЫЙ НОМЕР:")
    await state.set_state(Form.unban_phone)

@dp.message(Form.unban_phone)
async def un_s3(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    data = await state.get_data()
    await msg.answer("🚀 Пробую разбанить...")
    asyncio.create_task(bg_unban_process(msg.from_user.id, phone, data.get("unban_email")))

async def bg_unban_process(uid, phone, email):
    driver = await asyncio.to_thread(get_driver, None) # Без профиля
    if not driver: return
    try:
        driver.get("https://www.whatsapp.com/contact/nsc")
        
        # --- ФИКС ОШИБКИ "No Such Element" ---
        wait = WebDriverWait(driver, 20) # Ждем 20 секунд появления формы
        
        try:
            # Ищем любое поле, похожее на ввод телефона
            ph_field = wait.until(EC.presence_of_element_located((
                By.XPATH, "//input[@id='phone_number'] | //input[@type='tel'] | //input[contains(@placeholder, 'Phone')]"
            )))
            ph_field.send_keys(phone)
            
            driver.find_element(By.ID, "email").send_keys(email)
            driver.find_element(By.ID, "email_confirm").send_keys(email)
            
            # Андроид
            try: driver.find_element(By.XPATH, "//input[@value='android']").click()
            except: pass
            
            text = ai_engine.get_appeal(phone)
            driver.find_element(By.ID, "message").send_keys(text)
            
            # Кнопка отправки
            btn = driver.find_element(By.XPATH, "//button[contains(text(), 'Next') or contains(text(), 'Send')]")
            btn.click()
            
            await asyncio.sleep(5)
            scr = await asyncio.to_thread(driver.get_screenshot_as_png)
            await bot.send_photo(uid, BufferedInputFile(scr, "done.png"), caption="✅ Данные отправлены!")
            
        except TimeoutException:
            # Если время вышло, значит там капча или Cloudflare
            scr = await asyncio.to_thread(driver.get_screenshot_as_png)
            await bot.send_photo(uid, BufferedInputFile(scr, "fail.png"), caption="❌ Не вижу форму (см. скрин). Возможно IP в блоке.")
            
    except Exception as e:
        await bot.send_message(uid, f"Ошибка: {e}")
    finally:
        driver.quit()

# --- FARM LOOP (FIXED) ---
async def farm_loop():
    logger.info("🚜 FARM STARTED")
    while True:
        phones = db_get_active_phones()
        if phones:
            p = random.choice(phones)
            
            # Выбор: Себе или Другу
            target = p
            mode = "SOLO"
            others = [x for x in phones if x != p]
            if others and random.random() < 0.3:
                target = random.choice(others)
                mode = "NETWORK"
            
            asyncio.create_task(farm_worker(p, target, mode))
            
            # Задержка
            t_min, t_max = SPEED_MODES[CURRENT_SPEED]
            await asyncio.sleep(random.randint(t_min, t_max))
        else:
            await asyncio.sleep(30)

async def farm_worker(sender, target, mode):
    async with BROWSER_SEMAPHORE:
        driver = await asyncio.to_thread(get_driver, sender)
        if not driver: return
        try:
            driver.get("https://web.whatsapp.com/")
            
            # Ждем прогрузки (ФИКС ЗАВИСАНИЙ)
            try:
                WebDriverWait(driver, 60).until(EC.presence_of_element_located((By.ID, "pane-side")))
            except:
                logger.warning(f"{sender} не прогрузился. Выход.")
                driver.quit()
                return

            # Пишем
            driver.get(f"https://web.whatsapp.com/send?phone={target}")
            
            inp = WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.XPATH, "//div[@contenteditable='true'][@data-tab='10']")))
            
            text = ai_engine.get_chat_msg()
            await human_type(inp, text)
            inp.send_keys(Keys.ENTER)
            
            db_inc_msg(sender)
            logger.info(f"✅ MSG: {sender} -> {target}")
            await asyncio.sleep(10)
            
        except Exception as e:
            logger.error(f"Farm Err: {e}")
        finally:
            driver.quit()

# ======================= MAIN =======================
async def main():
    init_db()
    asyncio.create_task(farm_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
