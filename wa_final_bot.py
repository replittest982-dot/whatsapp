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
from faker import Faker

# --- SELENIUM ---
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException

# ======================= КОНФИГУРАЦИЯ =======================
# 👇👇👇 ВПИШИ СВОИ ДАННЫЕ СЮДА 👇👇👇
BOT_TOKEN = "ТВОЙ_ТОКЕН"
ADMIN_ID = 123456789  # Твой ID цифрами

# Настройки для 2GB RAM
# Ставим 2 потока. Это безопасно для 2ГБ.
BROWSER_SEMAPHORE = asyncio.Semaphore(2) 
DB_NAME = 'optimized_farm.db'
SESSIONS_DIR = "./sessions"
ACTIVE_DRIVERS = {} 

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("WA_2GB_EDITION")
fake = Faker('ru_RU')

# ======================= ДВИЖОК BRAUSERA (ОПТИМИЗАЦИЯ) =======================
def get_driver(phone, headless=True):
    # Если свободно меньше 150МБ, тормозим, чтобы не упал сервер
    if psutil.virtual_memory().available < 150 * 1024 * 1024:
        logger.warning("⚠️ RAM заполнен. Ждем освобождения...")
        return None

    path = os.path.join(SESSIONS_DIR, str(phone)) if phone else None
    
    opt = Options()
    if headless:
        opt.add_argument("--headless=new")
    
    # === НАСТРОЙКИ ДЛЯ СКОРОСТИ И 2GB RAM ===
    opt.add_argument("--no-sandbox")
    opt.add_argument("--disable-dev-shm-usage")
    opt.add_argument("--disable-gpu")
    # 🔥 ГЛАВНОЕ: Отключаем картинки. Это убирает лаги на 80%
    opt.add_argument("--blink-settings=imagesEnabled=false") 
    opt.add_argument("--disable-extensions")
    opt.add_argument("--disable-software-rasterizer")
    opt.add_argument("--window-size=1280,720")
    
    # Маскировка, чтобы WA не палил
    opt.add_argument("--disable-blink-features=AutomationControlled")
    opt.add_experimental_option("excludeSwitches", ["enable-automation"])
    opt.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
    
    if path:
        if not os.path.exists(path): os.makedirs(path)
        opt.add_argument(f"--user-data-dir={path}")

    try:
        driver = webdriver.Chrome(options=opt)
        return driver
    except Exception as e:
        logger.error(f"❌ Ошибка запуска Chrome: {e}")
        return None

# ======================= БОТ И БАЗА =======================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class Form(StatesGroup):
    phone = State()
    unban_email = State()
    unban_phone = State()

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS accounts 
                        (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                         phone_number TEXT UNIQUE, 
                         status TEXT DEFAULT 'pending', 
                         messages_sent INTEGER DEFAULT 0)''')

def db_get_active():
    with sqlite3.connect(DB_NAME) as conn:
        return [row[0] for row in conn.execute("SELECT phone_number FROM accounts WHERE status='active'").fetchall()]

def db_update(phone, status):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET status=? WHERE phone_number=?", (status, phone))

# Меню
def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ ВХОД (LOGIN)", callback_data="add"),
         InlineKeyboardButton(text="🚑 РАЗБАН", callback_data="unban_start")],
        [InlineKeyboardButton(text="📂 Активные", callback_data="list")]
    ])

def kb_manual():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 ЧЕК", callback_data="check"),
         InlineKeyboardButton(text="✅ ГОТОВО", callback_data="done")],
        [InlineKeyboardButton(text="1. Нажать Ссылку", callback_data="btn_link")],
        [InlineKeyboardButton(text="2. Ввести Номер", callback_data="btn_type")],
        [InlineKeyboardButton(text="3. Получить КОД", callback_data="btn_code")]
    ])

# ======================= ЛОГИКА БОТА =======================

@dp.message(Command("start"))
async def start(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    init_db()
    mem = psutil.virtual_memory().available // 1024 // 1024
    await msg.answer(f"🚀 **Бот запущен!**\nСвободно RAM: {mem} MB\nОптимизация: ВКЛ (без картинок)", reply_markup=kb_main())

# --- ВХОД ---
@dp.callback_query(F.data == "add")
async def add_start(call: types.CallbackQuery, state: FSMContext):
    # Закрываем старые окна, чтобы не жрать память
    uid = call.from_user.id
    if uid in ACTIVE_DRIVERS:
        try: ACTIVE_DRIVERS[uid].quit()
        except: pass
        del ACTIVE_DRIVERS[uid]

    await call.message.edit_text("📱 Введи номер (7XXXXXXXXXX):")
    await state.set_state(Form.phone)

@dp.message(Form.phone)
async def add_phone(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR IGNORE INTO accounts (phone_number) VALUES (?)", (phone,))
    await state.update_data(phone=phone)
    
    await msg.answer(f"⏳ Открываю браузер для **{phone}**...", reply_markup=kb_manual())
    asyncio.create_task(bg_manual_login(msg.from_user.id, phone))

async def bg_manual_login(uid, phone):
    driver = await asyncio.to_thread(get_driver, phone, headless=True) # На сервере headless=True работает быстрее
    if not driver:
        await bot.send_message(uid, "❌ Не хватило памяти для Chrome.")
        return
        
    ACTIVE_DRIVERS[uid] = driver
    try:
        driver.get("https://web.whatsapp.com/")
        # Держим сессию 10 минут
        for _ in range(60):
            if uid not in ACTIVE_DRIVERS: break
            await asyncio.sleep(10)
    except: pass
    finally:
        if uid in ACTIVE_DRIVERS:
            try: ACTIVE_DRIVERS[uid].quit()
            except: pass
            del ACTIVE_DRIVERS[uid]

# --- КНОПКИ ВХОДА (ОПТИМИЗИРОВАННЫЕ) ---
@dp.callback_query(F.data == "check")
async def check_scr(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Браузер закрыт")
    
    await call.answer("📸 Делаю скрин...")
    try:
        # Делаем скрин в отдельном потоке, чтобы бот не завис
        scr = await asyncio.to_thread(driver.get_screenshot_as_png)
        await call.message.answer_photo(BufferedInputFile(scr, "s.png"), caption="Экран")
    except: 
        await call.message.answer("Ошибка скриншота (возможно, страница грузится)")

@dp.callback_query(F.data == "btn_link")
async def btn_link(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return
    try:
        # Ищем кнопку по тексту
        xp = "//span[contains(text(), 'Link with phone')] | //span[contains(text(), 'Связать с номером')]"
        el = WebDriverWait(driver, 5).until(EC.element_to_be_clickable((By.XPATH, xp)))
        el.click()
        await call.answer("✅ Нажал!")
    except: await call.answer("❌ Кнопка не найдена")

@dp.callback_query(F.data == "btn_type")
async def btn_type(call: types.CallbackQuery, state: FSMContext):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    data = await state.get_data()
    phone = data.get("phone")
    if not driver or not phone: return await call.answer("Ошибка данных")
    
    try:
        # МГНОВЕННЫЙ ВВОД ЧЕРЕЗ JS (Без лагов)
        driver.execute_script(f"""
            var input = document.querySelector('input[aria-label="Type your phone number."]') || document.querySelector('input[type="text"]');
            if (input) {{
                input.value = "{phone}";
                input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                input.dispatchEvent(new Event('change', {{ bubbles: true }}));
            }}
        """)
        await asyncio.sleep(0.5)
        # Жмем Enter
        actions = webdriver.ActionChains(driver)
        actions.send_keys(Keys.ENTER).perform()
        
        await call.answer(f"🚀 Вставил {phone}")
    except Exception as e:
        await call.message.answer(f"Ошибка ввода: {e}")

@dp.callback_query(F.data == "btn_code")
async def btn_code(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return
    try:
        el = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.XPATH, "//div[@aria-details='link-device-phone-number-code']")))
        await call.message.answer(f"🔑 КОД: `{el.text}`", parse_mode="Markdown")
    except: 
        scr = await asyncio.to_thread(driver.get_screenshot_as_png)
        await call.message.answer_photo(BufferedInputFile(scr, "err.png"), caption="Код не вижу")

@dp.callback_query(F.data == "done")
async def done(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("phone"):
        db_update(data.get("phone"), 'active')
        await call.message.edit_text("✅ Сохранено!")
    
    uid = call.from_user.id
    if uid in ACTIVE_DRIVERS:
        ACTIVE_DRIVERS[uid].quit()
        del ACTIVE_DRIVERS[uid]

@dp.callback_query(F.data == "list")
async def list_a(call: types.CallbackQuery):
    phones = db_get_active()
    txt = "\n".join(phones) if phones else "Пусто"
    await call.message.edit_text(f"Активные:\n{txt}", reply_markup=kb_main())

# --- РАЗБАН ---
@dp.callback_query(F.data == "unban_start")
async def unban_s1(call: types.CallbackQuery, state: FSMContext):
    await call.message.edit_text("📧 Введи EMAIL:")
    await state.set_state(Form.unban_email)

@dp.message(Form.unban_email)
async def unban_s2(msg: types.Message, state: FSMContext):
    await state.update_data(unban_email=msg.text.strip())
    await msg.answer("📞 Введи НОМЕР:")
    await state.set_state(Form.unban_phone)

@dp.message(Form.unban_phone)
async def unban_s3(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    data = await state.get_data()
    await msg.answer("🚀 Работаю...")
    asyncio.create_task(bg_unban(msg.from_user.id, phone, data.get("unban_email")))

async def bg_unban(uid, phone, email):
    driver = await asyncio.to_thread(get_driver, None)
    if not driver: return
    try:
        driver.get("https://www.whatsapp.com/contact/nsc")
        wait = WebDriverWait(driver, 15)
        
        # Поиск полей (Устойчивый)
        ph = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@id='phone_number'] | //input[@type='tel']")))
        ph.send_keys(phone)
        
        driver.find_element(By.ID, "email").send_keys(email)
        driver.find_element(By.ID, "email_confirm").send_keys(email)
        try: driver.find_element(By.XPATH, "//input[@value='android']").click()
        except: pass
        
        driver.find_element(By.ID, "message").send_keys(f"Hello, my number {phone} banned by mistake. Please fix. I use it for work.")
        
        driver.find_element(By.XPATH, "//button[contains(text(), 'Next') or contains(text(), 'Send')]").click()
        
        await asyncio.sleep(3)
        scr = await asyncio.to_thread(driver.get_screenshot_as_png)
        await bot.send_photo(uid, BufferedInputFile(scr, "res.png"), caption="Готово")
    except Exception as e:
        await bot.send_message(uid, f"Ошибка: {e}")
    finally:
        driver.quit()

# --- ФАРМ (ФОН) ---
async def farm_loop():
    while True:
        phones = db_get_active()
        if phones:
            p = random.choice(phones)
            asyncio.create_task(farm_worker(p))
            # Пауза 5-15 минут, чтобы не грузить сервер
            await asyncio.sleep(random.randint(300, 900)) 
        else:
            await asyncio.sleep(60)

async def farm_worker(phone):
    # Ограничиваем количество браузеров через семафор (макс 2 для 2ГБ)
    async with BROWSER_SEMAPHORE:
        driver = await asyncio.to_thread(get_driver, phone)
        if not driver: return
        try:
            driver.get("https://web.whatsapp.com/")
            await asyncio.sleep(40) # Просто онлайн
        except: pass
        finally: driver.quit()

# ЗАПУСК
async def main():
    init_db()
    asyncio.create_task(farm_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
