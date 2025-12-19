import asyncio
import os
import logging
import sqlite3
import random
import re
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
DB_NAME = 'titan_db.db'
SESSIONS_DIR = "./sessions"
ACTIVE_DRIVERS = {} # Здесь живут браузеры ручного управления

# Логирование
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("WA_TITAN")

fake = Faker('ru_RU')

# ======================= ГЕНЕРАТОР ТЕКСТОВ (AI LITE) =======================
class TextEngine:
    """Генерирует тексты для разбана и переписки"""
    def get_appeal(self, phone):
        intros = ["Hello Support,", "Dear WhatsApp Team,", "Здравствуйте,", "Приветствую поддержку,"]
        problems = ["My number was banned by mistake.", "I lost access to my account.", "Мой номер заблокирован ошибочно.", "Пишет что аккаунт в бане."]
        reasons = ["I use it for work.", "I need to contact my family.", "Я курьер, мне нужен ватсап.", "Это мой личный номер."]
        ends = ["Please unban.", "Fix this ASAP.", "Прошу разблокировать.", "Жду помощи."]
        
        text = f"{random.choice(intros)} {random.choice(problems)} {random.choice(reasons)} {random.choice(ends)}"
        # Добавляем тех. шум для уникальности
        if random.random() < 0.5: text += f"\n\nRef: {random.randint(1000,9999)}"
        return text

    def get_chat_msg(self):
        msgs = [fake.sentence(), "Привет, ты тут?", "Надо созвониться", "Купил продукты", "Скинь отчет", "Ok", "Meeting at 10", "Как дела?"]
        return random.choice(msgs)

ai_engine = TextEngine()

# ======================= БАЗА ДАННЫХ =======================
def init_db():
    with sqlite3.connect(DB_NAME, timeout=10) as conn:
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

# ======================= ДРАЙВЕР И УТИЛИТЫ =======================
async def zombie_killer():
    """Чистильщик памяти"""
    while True:
        await asyncio.sleep(120)
        for proc in psutil.process_iter(['pid', 'name', 'create_time']):
            try:
                if 'chrome' in proc.info['name']:
                    if (datetime.now().timestamp() - proc.info['create_time']) > 2000:
                        proc.kill()
            except: pass

def get_driver(phone, headless=True):
    if psutil.virtual_memory().available < 200 * 1024 * 1024:
        logger.warning("⚠️ Low RAM")
        return None

    path = os.path.join(SESSIONS_DIR, str(phone)) if phone else None
    
    opt = Options()
    if headless: opt.add_argument("--headless=new")
    opt.add_argument("--no-sandbox")
    opt.add_argument("--disable-dev-shm-usage")
    opt.add_argument("--window-size=1920,1080")
    opt.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    
    if path:
        if not os.path.exists(path): os.makedirs(path)
        opt.add_argument(f"--user-data-dir={path}")
    
    return webdriver.Chrome(options=opt)

async def human_type(element, text):
    for char in text:
        element.send_keys(char)
        await asyncio.sleep(random.uniform(0.05, 0.15))

# ======================= BOT SETUP =======================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class Form(StatesGroup):
    phone = State()         # Для входа
    unban_email = State()   # Для разбана
    unban_phone = State()   # Для разбана

# --- МЕНЮШКИ ---
def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ ВХОД (LOGIN)", callback_data="add"),
         InlineKeyboardButton(text="🚑 РАЗБАН (UNBAN)", callback_data="unban_start")],
        [InlineKeyboardButton(text="📂 Список Аккаунтов", callback_data="list")]
    ])

def kb_login_manual():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 ЧЕК", callback_data="check"),
         InlineKeyboardButton(text="✅ ГОТОВО / ВЫХОД", callback_data="done")],
        [InlineKeyboardButton(text="🔗 1. Нажать Ссылку", callback_data="btn_click_link")],
        [InlineKeyboardButton(text="⌨️ 2. Ввести Номер", callback_data="btn_type_num")],
        [InlineKeyboardButton(text="🔑 3. Получить КОД", callback_data="btn_get_code")]
    ])

def kb_unban_manual():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 ЧЕК ФОРМЫ", callback_data="check"),
         InlineKeyboardButton(text="❌ ЗАКРЫТЬ", callback_data="done")],
        [InlineKeyboardButton(text="🚀 ОТПРАВИТЬ (SEND)", callback_data="btn_submit_unban")]
    ])

# ======================= ЛОГИКА БОТА =======================

@dp.message(Command("start"))
async def start(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return 
    init_db()
    await msg.answer("🤖 **TITAN WA BOT**\nВсе системы в норме.\n\nФарм работает в фоне.", reply_markup=kb_main())

# --- 1. МОДУЛЬ ВХОДА (LOGIN) ---
@dp.callback_query(F.data == "add")
async def add_flow(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID: return
    if call.from_user.id in ACTIVE_DRIVERS:
        try: ACTIVE_DRIVERS[call.from_user.id].quit()
        except: pass
        del ACTIVE_DRIVERS[call.from_user.id]

    await call.message.edit_text("📱 Введи номер (7XXXXXXXXXX):")
    await state.set_state(Form.phone)

@dp.message(Form.phone)
async def proc_phone(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR IGNORE INTO accounts (user_id, phone_number) VALUES (?, ?)", (msg.from_user.id, phone))
    
    await state.update_data(phone=phone)
    await msg.answer(f"⏳ Открываю браузер для **{phone}**...", reply_markup=kb_login_manual())
    asyncio.create_task(bg_manual_session(msg.from_user.id, phone, "LOGIN"))

# --- 2. МОДУЛЬ РАЗБАНА (UNBAN) ---
@dp.callback_query(F.data == "unban_start")
async def unban_s1(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID: return
    if call.from_user.id in ACTIVE_DRIVERS:
        try: ACTIVE_DRIVERS[call.from_user.id].quit()
        except: pass
        del ACTIVE_DRIVERS[call.from_user.id]

    await call.message.edit_text("📧 Введи **EMAIL** (для ответа):")
    await state.set_state(Form.unban_email)

@dp.message(Form.unban_email)
async def unban_s2(msg: types.Message, state: FSMContext):
    await state.update_data(unban_email=msg.text.strip())
    await msg.answer("📞 Введи **ЗАБАНЕННЫЙ НОМЕР**:")
    await state.set_state(Form.unban_phone)

@dp.message(Form.unban_phone)
async def unban_s3(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    data = await state.get_data()
    email = data.get("unban_email")
    
    await msg.answer(f"🚑 Захожу на сайт разбана...\nEmail: {email}\nPhone: {phone}\n\nЖди команду 'ЧЕК'.", reply_markup=kb_unban_manual())
    asyncio.create_task(bg_manual_session(msg.from_user.id, phone, "UNBAN", email))

# --- ФОНОВЫЕ СЕССИИ ДЛЯ РУЧНОГО УПРАВЛЕНИЯ ---
async def bg_manual_session(uid, phone, mode, email=None):
    driver = None
    try:
        if mode == "LOGIN":
            driver = await asyncio.to_thread(get_driver, phone, headless=False) # Можно True, если хост слабый
            if not driver: 
                await bot.send_message(uid, "❌ Ошибка драйвера")
                return
            ACTIVE_DRIVERS[uid] = driver
            driver.get("https://web.whatsapp.com/")
            
        elif mode == "UNBAN":
            # Чистый драйвер
            driver = await asyncio.to_thread(get_driver, None, headless=True)
            if not driver: return
            ACTIVE_DRIVERS[uid] = driver
            driver.get("https://www.whatsapp.com/contact/nsc")
            await asyncio.sleep(3)
            
            # Автозаполнение
            driver.find_element(By.ID, "phone_number").send_keys(phone)
            driver.find_element(By.ID, "email").send_keys(email)
            driver.find_element(By.ID, "email_confirm").send_keys(email)
            try: driver.find_element(By.XPATH, "//input[@value='android']").click()
            except: pass
            
            msg_box = driver.find_element(By.ID, "message")
            text = ai_engine.get_appeal(phone)
            await human_type(msg_box, text)
            
            await bot.send_message(uid, "📝 Форма заполнена! Жми ЧЕК, потом ОТПРАВИТЬ.")

        # Удержание сессии 15 минут
        for _ in range(90):
            if uid not in ACTIVE_DRIVERS: break
            await asyncio.sleep(10)

    except Exception as e:
        logger.error(f"Manual Session Error: {e}")
        await bot.send_message(uid, f"⚠️ Ошибка: {e}")
    finally:
        if driver:
            try: driver.quit()
            except: pass
        if uid in ACTIVE_DRIVERS: del ACTIVE_DRIVERS[uid]

# ======================= КНОПКИ УПРАВЛЕНИЯ =======================

@dp.callback_query(F.data == "check")
async def btn_check(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Браузер закрыт", show_alert=True)
    try:
        scr = await asyncio.to_thread(driver.get_screenshot_as_png)
        await call.message.answer_photo(BufferedInputFile(scr, "s.png"), caption=f"🖥 Экран: {datetime.now().strftime('%H:%M:%S')}")
    except: await call.answer("Ошибка скрина")

# --- LOGIN BUTTONS ---
@dp.callback_query(F.data == "btn_click_link")
async def btn_link(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return
    try:
        xpaths = ["//span[contains(text(), 'Link with phone')]", "//a[contains(@href, 'link-device')]", "//span[contains(text(), 'Связать с номером')]"]
        for xp in xpaths:
            try: driver.find_element(By.XPATH, xp).click(); break
            except: continue
        await call.answer("Клик!")
    except: await call.answer("Кнопка не найдена")

@dp.callback_query(F.data == "btn_type_num")
async def btn_type(call: types.CallbackQuery, state: FSMContext):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return
    data = await state.get_data()
    phone = data.get("phone")
    try:
        inp = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.XPATH, "//input[@aria-label='Type your phone number.'] | //input[@type='text']")))
        driver.execute_script("arguments[0].value = '';", inp)
        inp.send_keys(Keys.CONTROL + "a" + Keys.BACKSPACE)
        for ch in phone: inp.send_keys(ch); await asyncio.sleep(0.05)
        inp.send_keys(Keys.ENTER)
        await call.answer(f"Ввел {phone}")
    except: await call.answer("Поле ввода не найдено")

@dp.callback_query(F.data == "btn_get_code")
async def btn_code(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return
    try:
        el = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.XPATH, "//div[@aria-details='link-device-phone-number-code']")))
        await call.message.answer(f"🔑 КОД: `{el.text}`", parse_mode="Markdown")
    except: 
        scr = await asyncio.to_thread(driver.get_screenshot_as_png)
        await call.message.answer_photo(BufferedInputFile(scr, "err.png"), caption="Код не вижу")

# --- UNBAN BUTTONS ---
@dp.callback_query(F.data == "btn_submit_unban")
async def btn_sub(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return
    await call.message.answer("🚀 Жму отправку...")
    try:
        btn = driver.find_element(By.XPATH, "//button[contains(text(), 'Next') or contains(text(), 'Send') or contains(text(), 'Отправить')]")
        btn.click()
        await asyncio.sleep(5)
        scr = await asyncio.to_thread(driver.get_screenshot_as_png)
        await call.message.answer_photo(BufferedInputFile(scr, "done.png"), caption="✅ Результат")
        driver.quit()
        if call.from_user.id in ACTIVE_DRIVERS: del ACTIVE_DRIVERS[call.from_user.id]
    except Exception as e: await call.message.answer(f"Ошибка: {e}")

@dp.callback_query(F.data == "done")
async def btn_done(call: types.CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    if uid in ACTIVE_DRIVERS:
        ACTIVE_DRIVERS[uid].quit()
        del ACTIVE_DRIVERS[uid]
    
    data = await state.get_data()
    if data.get("phone") and not data.get("unban_email"):
        db_update_status(data.get("phone"), 'active')
        await call.message.edit_text("✅ Аккаунт активирован!")
    else:
        await call.message.edit_text("Завершено.")

@dp.callback_query(F.data == "list")
async def list_accs(call: types.CallbackQuery):
    phones = db_get_active_phones()
    txt = "\n".join([f"🟢 {p}" for p in phones]) if phones else "Пусто"
    await call.message.edit_text(f"Список:\n{txt}", reply_markup=kb_main())

# ======================= ФАРМЕР (ФОН) =======================
async def farm_loop():
    logger.info("🚜 FARM ACTIVE")
    asyncio.create_task(zombie_killer())
    
    while True:
        phones = db_get_active_phones()
        if phones:
            p = random.choice(phones)
            
            # Логика: 70% Соло, 30% Сеть
            mode = "SOLO"
            target = p # Себе
            
            if len(phones) > 1 and random.random() < 0.3:
                others = [x for x in phones if x != p]
                if others:
                    target = random.choice(others)
                    mode = "NETWORK"
            
            asyncio.create_task(farm_worker(p, target, mode))
            await asyncio.sleep(random.randint(300, 900)) # 5-15 мин задержка
        else:
            await asyncio.sleep(60)

async def farm_worker(sender, target, mode):
    async with BROWSER_SEMAPHORE:
        driver = await asyncio.to_thread(get_driver, sender)
        if not driver: return
        try:
            driver.get("https://web.whatsapp.com/")
            try:
                WebDriverWait(driver, 60).until(EC.presence_of_element_located((By.ID, "pane-side")))
            except:
                driver.quit(); return

            # Переход в чат
            if mode == "SOLO":
                driver.get(f"https://web.whatsapp.com/send?phone={sender}") # Себе
            else:
                driver.get(f"https://web.whatsapp.com/send?phone={target}") # Другу

            # Пишем текст
            inp = WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.XPATH, "//div[@contenteditable='true'][@data-tab='10']")))
            text = ai_engine.get_chat_msg()
            await human_type(inp, text)
            inp.send_keys(Keys.ENTER)
            
            db_inc_msg(sender)
            logger.info(f"✅ {sender} -> {target} ({text})")
            await asyncio.sleep(10)
            
        except Exception as e:
            logger.error(f"Farm Error: {e}")
        finally:
            driver.quit()

# ======================= ЗАПУСК =======================
async def main():
    init_db()
    asyncio.create_task(farm_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
