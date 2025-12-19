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

# --- SELENIUM ---
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys

# ======================= КОНФИГУРАЦИЯ =======================
BOT_TOKEN = os.environ.get("BOT_TOKEN") 
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except:
    ADMIN_ID = 0

# Лимиты
BROWSER_SEMAPHORE = asyncio.Semaphore(3) # Макс 3 окна
DB_NAME = 'ultimate_farm.db'
SESSIONS_DIR = "./sessions"
ACTIVE_DRIVERS = {} 

# Логирование
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("WA_ULTIMATE")
fake = Faker('ru_RU')

# НАСТРОЙКИ СКОРОСТИ (В секундах)
SPEED_MODES = {
    "TURBO": (60, 180),    # 1-3 минуты
    "MEDIUM": (600, 1200), # 10-20 минут
    "SLOW": (1800, 3600)   # 30-60 минут
}
CURRENT_SPEED = "MEDIUM" # По умолчанию

# ======================= AI TEXT ENGINE =======================
class TextEngine:
    def get_appeal(self, phone):
        """Генератор жалоб для разбана"""
        intros = ["Hello Support,", "Dear WhatsApp Team,", "Здравствуйте,", "Приветствую поддержку,"]
        body = [
            f"My number {phone} is banned by mistake.", 
            "I lost access to my account, it says banned.", 
            "Мой номер заблокирован, я не нарушал правила.", 
            "Прошу разблокировать мой рабочий номер."
        ]
        ends = ["Please help.", "Fix this ASAP.", "Прошу разобраться.", "Жду ответа."]
        return f"{random.choice(intros)} {random.choice(body)} {random.choice(ends)}"

    def get_chat_msg(self):
        """Генератор сообщений для переписки"""
        msgs = [
            "Привет, ты тут?", "Надо созвониться", "Купил продукты", "Скинь отчет", 
            "Ok", "Meeting at 10", "Как дела?", "Не забудь про встречу", 
            "Дома буду поздно", "Перезвони мне", "Где документы?", "Скинь фотки"
        ]
        return random.choice(msgs)

ai_engine = TextEngine()

# ======================= DATABASE =======================
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

# ======================= STEALTH DRIVER (NO PROXY) =======================
def get_driver(phone, headless=True):
    # Проверка памяти
    if psutil.virtual_memory().available < 150 * 1024 * 1024:
        logger.warning("⚠️ Low RAM. Skip.")
        return None

    path = os.path.join(SESSIONS_DIR, str(phone)) if phone else None
    
    opt = Options()
    if headless: opt.add_argument("--headless=new")
    
    # Флаги против детекта
    opt.add_argument("--no-sandbox")
    opt.add_argument("--disable-dev-shm-usage")
    opt.add_argument("--disable-gpu")
    opt.add_argument("--window-size=1920,1080")
    opt.add_argument("--disable-blink-features=AutomationControlled")
    opt.add_experimental_option("excludeSwitches", ["enable-automation"])
    opt.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    
    if path:
        if not os.path.exists(path): os.makedirs(path)
        opt.add_argument(f"--user-data-dir={path}")
    
    driver = webdriver.Chrome(options=opt)

    # 🎭 HARDWARE MASKING (Глубокая маскировка железа)
    # Это позволяет работать без прокси, меняя отпечатки WebGL и Audio
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": """
            // 1. Скрываем WebDriver
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            
            // 2. Подменяем видеокарту (WebGL)
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) return 'Google Inc. (NVIDIA)';
                if (parameter === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Ti Direct3D11 vs_5_0 ps_5_0, D3D11)';
                return getParameter(parameter);
            };

            // 3. Подменяем Аудио контекст (шум)
            const originalGetChannelData = AudioBuffer.prototype.getChannelData;
            AudioBuffer.prototype.getChannelData = function(channel) {
                const results = originalGetChannelData.apply(this, arguments);
                for (let i = 0; i < results.length; i++) {
                    results[i] = results[i] + 0.0000001; // Микро-шум
                }
                return results;
            }
        """
    })
    
    return driver

async def human_type(element, text):
    """Печать с опечатками (человечность)"""
    for char in text:
        element.send_keys(char)
        # Иногда делаем паузу, будто думаем
        if random.random() < 0.1: await asyncio.sleep(0.5)
        await asyncio.sleep(random.uniform(0.05, 0.15))

# ======================= BOT SETUP =======================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class Form(StatesGroup):
    phone = State()
    unban_email = State()
    unban_phone = State()

# --- МЕНЮ ---
def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ ВХОД (LOGIN)", callback_data="add"),
         InlineKeyboardButton(text="🚑 РАЗБАН (UNBAN)", callback_data="unban_start")],
        [InlineKeyboardButton(text=f"⚡️ РЕЖИМ: {CURRENT_SPEED}", callback_data="change_speed")],
        [InlineKeyboardButton(text="📂 Список Активных", callback_data="list")]
    ])

def kb_speed():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 TURBO (1-3 мин)", callback_data="set_speed_TURBO")],
        [InlineKeyboardButton(text="🚗 MEDIUM (10-20 мин)", callback_data="set_speed_MEDIUM")],
        [InlineKeyboardButton(text="🐢 SLOW (30-60 мин)", callback_data="set_speed_SLOW")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu")]
    ])

def kb_manual_auth():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 ЧЕК ЭКРАНА", callback_data="check"),
         InlineKeyboardButton(text="✅ ГОТОВО", callback_data="done")],
        [InlineKeyboardButton(text="1️⃣ Log with phone number", callback_data="btn_link")],
        [InlineKeyboardButton(text="2️⃣ Ввести номер", callback_data="btn_type")],
        [InlineKeyboardButton(text="3️⃣ Получить КОД", callback_data="btn_code")]
    ])

# ======================= HANDLERS =======================

@dp.message(Command("start"))
async def start(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return 
    init_db()
    await msg.answer("🤖 **WA FARM ULTIMATE**\n\nСистема готова к работе.", reply_markup=kb_main())

# --- СМЕНА СКОРОСТИ ---
@dp.callback_query(F.data == "change_speed")
async def speed_menu(call: types.CallbackQuery):
    await call.message.edit_text(f"Текущая скорость: **{CURRENT_SPEED}**\nВыберите режим:", reply_markup=kb_speed())

@dp.callback_query(F.data.startswith("set_speed_"))
async def set_speed(call: types.CallbackQuery):
    global CURRENT_SPEED
    mode = call.data.split("_")[-1]
    CURRENT_SPEED = mode
    await call.message.edit_text(f"✅ Скорость установлена: **{mode}**", reply_markup=kb_main())

# --- ВХОД (LOGIN) ---
@dp.callback_query(F.data == "add")
async def add_start(call: types.CallbackQuery, state: FSMContext):
    # Очистка старой сессии
    if call.from_user.id in ACTIVE_DRIVERS:
        try: ACTIVE_DRIVERS[call.from_user.id].quit()
        except: pass
        del ACTIVE_DRIVERS[call.from_user.id]

    await call.message.edit_text("📱 Введи номер телефона (7XXXXXXXXXX):")
    await state.set_state(Form.phone)

@dp.message(Form.phone)
async def add_phone(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    # Сохраняем номер в базу и в память (чтобы потом ввести его кнопкой)
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR IGNORE INTO accounts (user_id, phone_number) VALUES (?, ?)", (msg.from_user.id, phone))
    await state.update_data(phone=phone)
    
    await msg.answer(f"⏳ Запускаю браузер для **{phone}**...", reply_markup=kb_manual_auth())
    asyncio.create_task(bg_session(msg.from_user.id, phone))

async def bg_session(uid, phone):
    try:
        driver = await asyncio.to_thread(get_driver, phone, headless=False) # False = видно окно (если есть GUI), или используй True
        if not driver: return
        ACTIVE_DRIVERS[uid] = driver
        
        driver.get("https://web.whatsapp.com/")
        
        # Держим сессию 15 минут
        for _ in range(90):
            if uid not in ACTIVE_DRIVERS: break
            await asyncio.sleep(10)
    except Exception as e:
        logger.error(f"Session Error: {e}")
    finally:
        if uid in ACTIVE_DRIVERS:
            try: ACTIVE_DRIVERS[uid].quit()
            except: pass
            del ACTIVE_DRIVERS[uid]

# --- КНОПКИ ВХОДА ---
@dp.callback_query(F.data == "btn_link")
async def btn_link(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Нет браузера")
    try:
        xp = "//span[contains(text(), 'Link with phone')] | //span[contains(text(), 'Связать с номером')]"
        driver.find_element(By.XPATH, xp).click()
        await call.answer("Нажал!")
    except: await call.answer("Кнопка не найдена")

@dp.callback_query(F.data == "btn_type")
async def btn_type(call: types.CallbackQuery, state: FSMContext):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Нет браузера")
    
    # Берем номер из памяти, который ввел юзер
    data = await state.get_data()
    phone = data.get("phone")
    if not phone: return await call.answer("Ошибка: Номер потерян")

    try:
        inp = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.XPATH, "//input[@aria-label='Type your phone number.'] | //input[@type='text']")))
        # Очистка и ввод
        driver.execute_script("arguments[0].value = '';", inp)
        inp.send_keys(Keys.CONTROL + "a" + Keys.BACKSPACE)
        for ch in phone: 
            inp.send_keys(ch)
            await asyncio.sleep(0.05)
        inp.send_keys(Keys.ENTER)
        await call.answer(f"Ввел номер: {phone}")
    except: await call.answer("Поле ввода не найдено")

@dp.callback_query(F.data == "btn_code")
async def btn_code(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Нет браузера")
    try:
        el = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.XPATH, "//div[@aria-details='link-device-phone-number-code']")))
        await call.message.answer(f"🔑 **КОД:** `{el.text}`", parse_mode="Markdown")
    except: 
        scr = await asyncio.to_thread(driver.get_screenshot_as_png)
        await call.message.answer_photo(BufferedInputFile(scr, "err.png"), caption="Код не вижу")

@dp.callback_query(F.data == "done")
async def btn_done(call: types.CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    if uid in ACTIVE_DRIVERS:
        ACTIVE_DRIVERS[uid].quit()
        del ACTIVE_DRIVERS[uid]
    
    data = await state.get_data()
    if data.get("phone"):
        db_update_status(data.get("phone"), 'active')
        await call.message.edit_text("✅ Аккаунт добавлен в ферму!")
    else:
        await call.message.edit_text("Завершено.")

@dp.callback_query(F.data == "check")
async def check_scr(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return
    try:
        scr = await asyncio.to_thread(driver.get_screenshot_as_png)
        await call.message.answer_photo(BufferedInputFile(scr, "s.png"))
    except: pass

@dp.callback_query(F.data == "list")
async def list_accs(call: types.CallbackQuery):
    phones = db_get_active_phones()
    txt = "\n".join([f"🟢 {p}" for p in phones]) if phones else "Пусто"
    await call.message.edit_text(f"Активные аккаунты:\n{txt}", reply_markup=kb_main())

@dp.callback_query(F.data == "menu")
async def back_menu(call: types.CallbackQuery):
    await call.message.edit_text("Главное меню", reply_markup=kb_main())

# --- РАЗБАН (UNBAN) ---
@dp.callback_query(F.data == "unban_start")
async def unban_s1(call: types.CallbackQuery, state: FSMContext):
    await call.message.edit_text("📧 Введи EMAIL:")
    await state.set_state(Form.unban_email)

@dp.message(Form.unban_email)
async def unban_s2(msg: types.Message, state: FSMContext):
    await state.update_data(unban_email=msg.text.strip())
    await msg.answer("📞 Введи ЗАБАНЕННЫЙ НОМЕР:")
    await state.set_state(Form.unban_phone)

@dp.message(Form.unban_phone)
async def unban_s3(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    data = await state.get_data()
    
    await msg.answer("🚀 Запускаю процесс разбана...")
    asyncio.create_task(bg_unban(msg.from_user.id, phone, data.get("unban_email")))

async def bg_unban(uid, phone, email):
    driver = await asyncio.to_thread(get_driver, None) # Без профиля
    if not driver: return
    try:
        driver.get("https://www.whatsapp.com/contact/nsc")
        await asyncio.sleep(2)
        
        # Заполнение
        driver.find_element(By.ID, "phone_number").send_keys(phone)
        driver.find_element(By.ID, "email").send_keys(email)
        driver.find_element(By.ID, "email_confirm").send_keys(email)
        try: driver.find_element(By.XPATH, "//input[@value='android']").click()
        except: pass
        
        text = ai_engine.get_appeal(phone)
        driver.find_element(By.ID, "message").send_keys(text)
        
        # Клик "Далее"
        driver.find_element(By.XPATH, "//button[contains(text(), 'Next') or contains(text(), 'Send')]").click()
        await asyncio.sleep(3)
        
        # Скриншот
        scr = await asyncio.to_thread(driver.get_screenshot_as_png)
        await bot.send_photo(uid, BufferedInputFile(scr, "done.png"), caption="✅ Форма отправлена (или ждет подтверждения)")
    except Exception as e:
        await bot.send_message(uid, f"Ошибка: {e}")
    finally:
        driver.quit()

# ======================= ФАРМ ЦИКЛ (СЕТЬ + СОЛО) =======================
async def farm_loop():
    logger.info("🚜 FARM ENGINE STARTED")
    
    while True:
        phones = db_get_active_phones()
        if phones:
            p = random.choice(phones)
            
            # --- ЛОГИКА СЕТИ ---
            # Если есть другие боты, пишем им. Если нет - пишем себе.
            target = p
            mode = "SOLO (Self)"
            
            others = [x for x in phones if x != p]
            if others:
                target = random.choice(others)
                mode = f"NETWORK -> {target}"
            
            asyncio.create_task(farm_worker(p, target, mode))
            
            # ЗАДЕРЖКА ПО ВЫБРАННОМУ РЕЖИМУ
            min_t, max_t = SPEED_MODES[CURRENT_SPEED]
            delay = random.randint(min_t, max_t)
            logger.info(f"💤 Жду {delay} сек ({CURRENT_SPEED})")
            await asyncio.sleep(delay)
        else:
            await asyncio.sleep(30)

async def farm_worker(sender, target, mode):
    async with BROWSER_SEMAPHORE:
        driver = await asyncio.to_thread(get_driver, sender)
        if not driver: return
        try:
            logger.info(f"🚜 Work: {sender} | Mode: {mode}")
            driver.get("https://web.whatsapp.com/")
            
            try: WebDriverWait(driver, 40).until(EC.presence_of_element_located((By.ID, "pane-side")))
            except: 
                logger.warning(f"❌ {sender} не загрузился (или бан).")
                driver.quit(); return

            # Идем в чат
            driver.get(f"https://web.whatsapp.com/send?phone={target}")
            
            # Ждем поле ввода
            inp = WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.XPATH, "//div[@contenteditable='true'][@data-tab='10']")))
            
            # Печатаем
            msg = ai_engine.get_chat_msg()
            await human_type(inp, msg)
            inp.send_keys(Keys.ENTER)
            
            db_inc_msg(sender)
            await asyncio.sleep(10)
            
        except Exception as e:
            logger.error(f"Farm Fail: {e}")
        finally:
            driver.quit()

# ======================= MAIN =======================
async def main():
    init_db()
    asyncio.create_task(farm_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
