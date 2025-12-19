import asyncio
import os
import logging
import sqlite3
import random
import re
import psutil
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# --- SELENIUM ---
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys

# ======================= КОНФИГУРАЦИЯ =======================
BOT_TOKEN = os.environ.get("BOT_TOKEN") # Или вставь токен сюда в кавычках
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except:
    ADMIN_ID = 0

# Настройки производительности
BROWSER_SEMAPHORE = asyncio.Semaphore(2) # Макс 2 браузера одновременно (бережем память)
DB_NAME = 'bot_database.db'
SESSIONS_DIR = "./sessions"
ACTIVE_DRIVERS = {} 

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("WA_GOD_MODE")

# ======================= SMART TEXT ENGINE (AI-LITE) =======================
class SmartTextGenerator:
    """Генерирует уникальные тексты жалоб, не нагружая процессор"""
    def __init__(self):
        self.intros = [
            "Здравствуйте, команда поддержки.", "Hello WhatsApp Support.", "Доброго времени суток.", "Dear Support Team,",
            "Приветствую.", "Hi there,", "Уважаемая поддержка!", "Greetings,"
        ]
        self.problems = [
            "Мой номер был заблокирован без причины.", "My phone number has been banned by mistake.",
            "Я потерял доступ к аккаунту, пишет что бан.", "I suddenly lost access to my WhatsApp.",
            "Случилась ошибка, мой номер в блоке.", "It seems my account is banned for no reason.",
            "Меня забанили, но я ничего не нарушал.", "I was banned but I followed all terms."
        ]
        self.contexts = [
            "Я использую ватсап для общения с семьей.", "I use this app to talk to my parents.",
            "У меня там рабочие чаты, это срочно.", "I have important work chats there.",
            "Я пожилой человек, мне нужна связь.", "I need this account for my school project.",
            "Я только что купил эту симку.", "I just bought this SIM card recently."
        ]
        self.pleas = [
            "Пожалуйста, разберитесь и разбаньте.", "Please review and unban me ASAP.",
            "Прошу восстановить доступ.", "Kindly restore my account.",
            "Исправьте эту ошибку, пожалуйста.", "Please fix this error immediately.",
            "Очень жду вашего ответа.", "Looking forward to your quick response."
        ]
        self.devices = ["Android", "iPhone 14", "Samsung S23", "Xiaomi Redmi", "Pixel 7"]

    def generate(self, phone):
        # Собираем конструктор
        text = f"{random.choice(self.intros)} {random.choice(self.problems)} {random.choice(self.contexts)} {random.choice(self.pleas)}"
        # Добавляем "шум" (случайные пробелы или тех. данные), чтобы хеш текста был уникальным
        if random.random() < 0.5:
            text += f"\n\nDevice: {random.choice(self.devices)}\nPhone: {phone}"
        return text

text_engine = SmartTextGenerator()

# ======================= БАЗА ДАННЫХ =======================
def init_db():
    with sqlite3.connect(DB_NAME, timeout=10) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
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

# ======================= ДРАЙВЕР & УТИЛИТЫ =======================
async def zombie_killer():
    """Санитар леса: убивает зависшие процессы"""
    while True:
        await asyncio.sleep(60)
        for proc in psutil.process_iter(['pid', 'name', 'create_time']):
            try:
                if 'chrome' in proc.info['name']:
                    # Если процесс живет дольше 30 минут - расстрел
                    if (datetime.now().timestamp() - proc.info['create_time']) > 1800:
                        proc.kill()
            except: pass

def get_driver_options(headless=True, user_data_dir=None):
    opt = Options()
    if headless:
        opt.add_argument("--headless=new")
    opt.add_argument("--no-sandbox")
    opt.add_argument("--disable-dev-shm-usage")
    opt.add_argument("--disable-gpu")
    opt.add_argument("--window-size=1280,720") # Меньше разрешение = меньше памяти
    opt.add_argument("--remote-allow-origins=*")
    
    # Ротация User-Agent (простая)
    agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ]
    opt.add_argument(f"user-agent={random.choice(agents)}")
    
    if user_data_dir:
        opt.add_argument(f"--user-data-dir={user_data_dir}")
    return opt

async def human_type(element, text):
    for char in text:
        element.send_keys(char)
        await asyncio.sleep(random.uniform(0.03, 0.1))

# ======================= BOT SETUP =======================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class Form(StatesGroup):
    phone = State()
    unban_email = State()
    unban_phone = State()

def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить Аккаунт (Login)", callback_data="add")],
        [InlineKeyboardButton(text="🚑 UNBAN CENTER (Разбан)", callback_data="unban_start")],
        [InlineKeyboardButton(text="📂 Список Активных", callback_data="list")]
    ])

def kb_manual_control():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 ЧЕК ЭКРАНА", callback_data="check"),
         InlineKeyboardButton(text="❌ ЗАКРЫТЬ", callback_data="done")],
        [InlineKeyboardButton(text="🔗 Нажать 'Link with phone'", callback_data="click_link_btn")],
        [InlineKeyboardButton(text="⌨️ Ввести номер", callback_data="type_phone_btn")],
        [InlineKeyboardButton(text="🔑 Получить КОД", callback_data="get_code_btn")],
        [InlineKeyboardButton(text="🚀 ОТПРАВИТЬ ЖАЛОБУ (SEND)", callback_data="submit_unban_btn")]
    ])

# ======================= HANDLERS: START & MENU =======================
@dp.message(Command("start"))
async def start(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return 
    init_db()
    # Проверка ресурсов
    mem = psutil.virtual_memory()
    await msg.answer(f"🤖 **WA GOD MODE ACTIVATED**\n\n💾 RAM Free: {mem.available // 1024 // 1024} MB\n🧠 AI Engine: Ready", reply_markup=kb_main())

# ======================= MODULE 1: LOGIN FLOW =======================
@dp.callback_query(F.data == "add")
async def add_flow(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID: return
    # Kill previous
    if call.from_user.id in ACTIVE_DRIVERS:
        try: ACTIVE_DRIVERS.pop(call.from_user.id).quit()
        except: pass
        
    await call.message.edit_text("📱 Введи номер для входа (7999...):")
    await state.set_state(Form.phone)

@dp.message(Form.phone)
async def proc_phone(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR IGNORE INTO accounts (user_id, phone_number) VALUES (?, ?)", (msg.from_user.id, phone))
    await state.update_data(phone=phone)
    
    await msg.answer("⏳ **Загрузка профиля...**\nЖди команду 'Готово'.", reply_markup=kb_manual_control())
    asyncio.create_task(bg_login_process(msg.from_user.id, phone))

async def bg_login_process(uid, phone):
    path = os.path.join(SESSIONS_DIR, str(phone))
    if not os.path.exists(path): os.makedirs(path)
    
    driver = None
    try:
        # Используем семафор, чтобы не убить сервер
        async with BROWSER_SEMAPHORE:
            driver = await asyncio.to_thread(webdriver.Chrome, options=get_driver_options(user_data_dir=path))
            ACTIVE_DRIVERS[uid] = driver
            
            await bot.send_message(uid, "✅ **Браузер открыт!**\nМожешь запрашивать код.")
            driver.get("https://web.whatsapp.com/")
            
            # Держим сессию 15 минут для настройки
            for _ in range(90): 
                if uid not in ACTIVE_DRIVERS: break
                await asyncio.sleep(10)
                
    except Exception as e:
        await bot.send_message(uid, f"❌ Crash: {e}")
    finally:
        if driver: 
            try: driver.quit()
            except: pass
        if uid in ACTIVE_DRIVERS: del ACTIVE_DRIVERS[uid]

# ======================= MODULE 2: UNBAN CENTER (AI) =======================
@dp.callback_query(F.data == "unban_start")
async def unban_step1(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID: return
    # Kill previous
    if call.from_user.id in ACTIVE_DRIVERS:
        try: ACTIVE_DRIVERS.pop(call.from_user.id).quit()
        except: pass

    await call.message.edit_text("📧 Введи **EMAIL** для ответа (любой):")
    await state.set_state(Form.unban_email)

@dp.message(Form.unban_email)
async def unban_step2(msg: types.Message, state: FSMContext):
    await state.update_data(unban_email=msg.text.strip())
    await msg.answer("📞 Введи **ЗАБАНЕННЫЙ НОМЕР** (7999...):")
    await state.set_state(Form.unban_phone)

@dp.message(Form.unban_phone)
async def unban_step3(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    data = await state.get_data()
    email = data.get("unban_email")
    
    # Генерация текста
    ai_text = text_engine.generate(phone)
    
    await msg.answer(f"🚑 **Генерация жалобы...**\n\n📝 Текст AI:\n_{ai_text}_\n\nЗапускаю браузер...", parse_mode="Markdown", reply_markup=kb_manual_control())
    asyncio.create_task(bg_unban_process(msg.from_user.id, phone, email, ai_text))

async def bg_unban_process(uid, phone, email, text):
    driver = None
    try:
        async with BROWSER_SEMAPHORE:
            # Запускаем в инкогнито (без профиля)
            driver = await asyncio.to_thread(webdriver.Chrome, options=get_driver_options(headless=True, user_data_dir=None))
            ACTIVE_DRIVERS[uid] = driver
            
            driver.get("https://www.whatsapp.com/contact/nsc")
            await asyncio.sleep(5)
            
            # --- АВТОЗАПОЛНЕНИЕ ---
            try:
                # Номер
                driver.find_element(By.ID, "phone_number").send_keys(phone)
                # Почта
                driver.find_element(By.ID, "email").send_keys(email)
                driver.find_element(By.ID, "email_confirm").send_keys(email)
                
                # Выбор платформы (Android)
                try: driver.find_element(By.XPATH, "//input[@value='android']").click()
                except: pass
                
                # Текст (Печатаем как человек)
                msg_box = driver.find_element(By.ID, "message")
                await human_type(msg_box, text)
                
                await bot.send_message(uid, "🤖 **AI всё заполнил!**\n1. Жми '📷 ЧЕК'\n2. Если ок, жми '🚀 ОТПРАВИТЬ'")
                
            except Exception as e:
                await bot.send_message(uid, f"⚠️ Ошибка заполнения: {e}\nПопробуй вручную.")

            # Ждем команды пользователя (20 минут макс)
            for _ in range(120):
                if uid not in ACTIVE_DRIVERS: break
                await asyncio.sleep(10)

    except Exception as e:
        await bot.send_message(uid, f"❌ Unban Crash: {e}")
    finally:
        if driver:
            try: driver.quit()
            except: pass
        if uid in ACTIVE_DRIVERS: del ACTIVE_DRIVERS[uid]

# ======================= CONTROL PANEL BUTTONS =======================

@dp.callback_query(F.data == "check")
async def btn_check(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Браузер закрыт/грузится", show_alert=True)
    
    try:
        # Делаем скрин
        scr = await asyncio.to_thread(driver.get_screenshot_as_png)
        ts = datetime.now().strftime("%H:%M:%S")
        await call.message.answer_photo(BufferedInputFile(scr, "s.png"), caption=f"🖥 Экран в {ts}")
    except: await call.answer("Ошибка скрина", show_alert=True)

@dp.callback_query(F.data == "submit_unban_btn")
async def btn_submit(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Браузер закрыт", show_alert=True)
    
    await call.message.answer("🚀 Нажимаю 'Next Step' / 'Send'...")
    try:
        # Универсальный поиск кнопки отправки (на разных языках)
        xpath = "//button[contains(text(), 'Next') or contains(text(), 'Далее') or contains(text(), 'Send') or contains(text(), 'Отправить')]"
        btn = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.XPATH, xpath)))
        btn.click()
        
        # Ждем результата
        await asyncio.sleep(5)
        scr = await asyncio.to_thread(driver.get_screenshot_as_png)
        await call.message.answer_photo(BufferedInputFile(scr, "done.png"), caption="✅ **Результат нажатия**\nЕсли видишь галочку или 'Sent' - успех!")
        
        # Завершаем работу
        driver.quit()
        if call.from_user.id in ACTIVE_DRIVERS: del ACTIVE_DRIVERS[call.from_user.id]
        
    except Exception as e:
        await call.message.answer(f"❌ Не нашел кнопку отправки: {e}")

@dp.callback_query(F.data == "click_link_btn")
async def btn_link(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Нет браузера", show_alert=True)
    try:
        xp = "//span[contains(text(), 'Link with phone')]"
        driver.find_element(By.XPATH, xp).click()
        await call.answer("Клик!")
    except: await call.answer("Кнопка не найдена")

@dp.callback_query(F.data == "type_phone_btn")
async def btn_type(call: types.CallbackQuery, state: FSMContext):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Нет браузера", show_alert=True)
    data = await state.get_data()
    phone = data.get("phone")
    if not phone: return await call.answer("Нет номера")
    
    try:
        # Поиск поля ввода
        inp = driver.find_element(By.XPATH, "//input[@aria-label='Type your phone number.'] | //input[@type='text']")
        inp.send_keys(Keys.CONTROL + "a" + Keys.BACKSPACE)
        for ch in phone: 
            inp.send_keys(ch)
            await asyncio.sleep(0.05)
        inp.send_keys(Keys.ENTER)
        await call.answer(f"Ввел {phone}")
    except: await call.answer("Ошибка ввода")

@dp.callback_query(F.data == "get_code_btn")
async def btn_code(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Нет браузера", show_alert=True)
    try:
        el = driver.find_element(By.XPATH, "//div[@aria-details='link-device-phone-number-code']")
        await call.message.answer(f"🔑 КОД: `{el.text}`", parse_mode="Markdown")
    except: 
        scr = await asyncio.to_thread(driver.get_screenshot_as_png)
        await call.message.answer_photo(BufferedInputFile(scr, "err.png"), caption="Код не вижу. Посмотри скрин.")

@dp.callback_query(F.data == "done")
async def btn_done(call: types.CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    if uid in ACTIVE_DRIVERS:
        ACTIVE_DRIVERS[uid].quit()
        del ACTIVE_DRIVERS[uid]
    
    data = await state.get_data()
    if data.get("phone") and not data.get("unban_email"):
        db_update_status(data.get("phone"), 'active')
        await call.message.edit_text("✅ Аккаунт сохранен в базу!")
    else:
        await call.message.edit_text("👋 Работа завершена.")

@dp.callback_query(F.data == "list")
async def list_active(call: types.CallbackQuery):
    phones = db_get_active_phones()
    txt = "\n".join([f"🟢 {p}" for p in phones]) if phones else "Список пуст"
    await call.message.edit_text(f"📋 **Активные сессии:**\n{txt}", reply_markup=kb_main())

# ======================= ФОНОВЫЙ ПРОГРЕВ (ФАРМ) =======================
async def farm_loop():
    logger.info("🚜 Farm Loop Started")
    asyncio.create_task(zombie_killer()) # Запуск защиты от зависаний
    
    while True:
        phones = db_get_active_phones()
        if phones:
            p = random.choice(phones)
            
            # Логика День/Ночь
            hour = datetime.now().hour
            is_night = (hour >= 23 or hour < 7)
            
            # Ночью шанс запуска всего 10%, Днем 100%
            if not is_night or (is_night and random.random() < 0.1):
                asyncio.create_task(farm_single_worker(p))
            
            # Пауза между аккаунтами (чтобы не грузить хост)
            # Днем 5-10 минут, Ночью 20-40 минут
            delay = random.randint(300, 600) if not is_night else random.randint(1200, 2400)
            await asyncio.sleep(delay)
        else:
            await asyncio.sleep(60)

async def farm_single_worker(phone):
    """Тихий заход в сеть на 30 секунд"""
    # Проверка памяти перед запуском
    if psutil.virtual_memory().available < 300 * 1024 * 1024:
        logger.warning("⚠️ Low RAM, skipping farm cycle")
        return

    async with BROWSER_SEMAPHORE: # Ждет очереди, если занято
        path = os.path.join(SESSIONS_DIR, str(phone))
        if not os.path.exists(path): return
        
        try:
            logger.info(f"🚜 Farming: {phone}")
            driver = await asyncio.to_thread(webdriver.Chrome, options=get_driver_options(user_data_dir=path))
            driver.get("https://web.whatsapp.com/")
            await asyncio.sleep(random.randint(30, 60)) # Просто висит онлайн
            driver.quit()
        except: pass

# ======================= ЗАПУСК =======================
async def main():
    init_db()
    asyncio.create_task(farm_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
