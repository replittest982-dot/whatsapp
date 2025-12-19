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

# --- SELENIUM ---
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
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

# Лимиты и пути
BROWSER_SEMAPHORE = asyncio.Semaphore(3)
DB_NAME = 'bot_database.db'
SESSIONS_DIR = "./sessions"
ACTIVE_DRIVERS = {} # Здесь живут активные браузеры

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("WA_FARM_CLEAN")

# --- БАЗА ДАННЫХ ---
def init_db():
    with sqlite3.connect(DB_NAME, timeout=10) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute('''CREATE TABLE IF NOT EXISTS accounts 
                        (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                         user_id INTEGER, phone_number TEXT UNIQUE, 
                         status TEXT DEFAULT 'pending', 
                         messages_sent INTEGER DEFAULT 0,
                         user_agent TEXT, resolution TEXT, platform TEXT,
                         ban_reason TEXT, last_active TIMESTAMP,
                         last_group_msg TIMESTAMP)''')

def db_get_active_phones():
    with sqlite3.connect(DB_NAME) as conn:
        return [row[0] for row in conn.execute("SELECT phone_number FROM accounts WHERE status = 'active'").fetchall()]

def db_update_status(phone, status):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET status = ? WHERE phone_number = ?", (status, phone))

# --- ZOMBIE KILLER ---
async def zombie_killer():
    """Чистит зависшие процессы Chrome"""
    while True:
        await asyncio.sleep(120)
        for proc in psutil.process_iter(['pid', 'name', 'create_time']):
            try:
                if 'chrome' in proc.info['name']:
                    # Если процесс живет дольше 40 минут - убиваем
                    if (datetime.now().timestamp() - proc.info['create_time']) > 2400:
                        proc.kill()
            except: pass

# --- DRIVER FACTORY ---
def get_driver_options(headless=True, user_data_dir=None):
    opt = Options()
    if headless:
        opt.add_argument("--headless=new")
    opt.add_argument("--no-sandbox")
    opt.add_argument("--disable-dev-shm-usage")
    opt.add_argument("--disable-gpu")
    opt.add_argument("--window-size=1920,1080")
    
    # Юзер-агент
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    opt.add_argument(f"user-agent={ua}")
    
    if user_data_dir:
        opt.add_argument(f"--user-data-dir={user_data_dir}")
    return opt

async def human_type(element, text):
    """Эмуляция набора текста"""
    for char in text:
        element.send_keys(char)
        await asyncio.sleep(random.uniform(0.05, 0.15))

# --- BOT SETUP ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class Form(StatesGroup):
    phone = State()         # Для логина
    unban_email = State()   # Почта для разбана
    unban_phone = State()   # Номер для разбана

# --- КЛАВИАТУРЫ ---
def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить Аккаунт", callback_data="add")],
        [InlineKeyboardButton(text="🚑 UNBAN CENTER", callback_data="unban_start")],
        [InlineKeyboardButton(text="📂 Список", callback_data="list")]
    ])

def kb_manual_control():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 ЧЕК", callback_data="check"),
         InlineKeyboardButton(text="✅ ГОТОВО / ВЫХОД", callback_data="done")],
        [InlineKeyboardButton(text="🔗 Log with phone number", callback_data="click_link_btn")],
        [InlineKeyboardButton(text="⌨️ Ввести номер", callback_data="type_phone_btn")],
        [InlineKeyboardButton(text="🔑 Получить КОД", callback_data="get_code_btn")],
        [InlineKeyboardButton(text="📨 ОТПРАВИТЬ ФОРМУ (UNBAN)", callback_data="submit_unban_btn")]
    ])

# --- HANDLERS ---

@dp.message(Command("start"))
async def start(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return 
    init_db()
    await msg.answer("🔥 **WA Farm: Clean Edition**\nПочта убрана. Код оптимизирован.", reply_markup=kb_main())

# ==========================================
# 1. ДОБАВЛЕНИЕ АККАУНТА (LOGIN)
# ==========================================
@dp.callback_query(F.data == "add")
async def add_flow(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID: return
    if call.from_user.id in ACTIVE_DRIVERS:
        try: ACTIVE_DRIVERS.pop(call.from_user.id).quit()
        except: pass
        
    await call.message.edit_text("Введи номер для входа (7XXXXXXXXXX):")
    await state.set_state(Form.phone)

@dp.message(Form.phone)
async def proc_phone(msg: types.Message, state: FSMContext):
    if msg.from_user.id != ADMIN_ID: return
    phone = re.sub(r'\D', '', msg.text)
    
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR IGNORE INTO accounts (user_id, phone_number) VALUES (?, ?)", (msg.from_user.id, phone))
    await state.update_data(phone=phone)
    
    await msg.answer("⏳ **Запускаю Chrome...**\nЖди сообщения 'Браузер готов'.", reply_markup=kb_manual_control())
    asyncio.create_task(bg_login_process(msg.from_user.id, phone))

async def bg_login_process(uid, phone):
    path = os.path.join(SESSIONS_DIR, str(phone))
    if not os.path.exists(path): os.makedirs(path)
    
    driver = None
    try:
        driver = await asyncio.to_thread(webdriver.Chrome, options=get_driver_options(user_data_dir=path))
        ACTIVE_DRIVERS[uid] = driver 
        
        await bot.send_message(uid, "✅ **Браузер готов!** Жми кнопки.")
        driver.get("https://web.whatsapp.com/")
        await asyncio.sleep(1200) # 20 минут висит открытым
        
    except Exception as e:
        await bot.send_message(uid, f"❌ Ошибка: {e}")
    finally:
        if driver:
            try: driver.quit()
            except: pass
        if uid in ACTIVE_DRIVERS: del ACTIVE_DRIVERS[uid]

# ==========================================
# 2. РАЗБАН (UNBAN CENTER) - БЕЗ ХАРДКОДА
# ==========================================
@dp.callback_query(F.data == "unban_start")
async def unban_step1(call: types.CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID: return
    # Чистим старый драйвер
    if call.from_user.id in ACTIVE_DRIVERS:
        try: ACTIVE_DRIVERS.pop(call.from_user.id).quit()
        except: pass

    await call.message.edit_text("📧 Введи **EMAIL**, который впишем в форму\n(Любой, на который придет ответ):")
    await state.set_state(Form.unban_email)

@dp.message(Form.unban_email)
async def unban_step2(msg: types.Message, state: FSMContext):
    email = msg.text.strip()
    await state.update_data(unban_email=email)
    await msg.answer("📞 Теперь введи **ЗАБАНЕННЫЙ НОМЕР** (7XXXXXXXXXX):")
    await state.set_state(Form.unban_phone)

@dp.message(Form.unban_phone)
async def unban_step3(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    data = await state.get_data()
    email = data.get("unban_email")
    
    await msg.answer(f"🚑 Открываю форму разбана...\nEmail: {email}\nPhone: {phone}\n\n**Жди, я напишу когда будет готово!**", reply_markup=kb_manual_control())
    asyncio.create_task(bg_unban_process(msg.from_user.id, phone, email))

async def bg_unban_process(uid, phone, email):
    driver = None
    try:
        # Чистый драйвер (Incognito style)
        driver = await asyncio.to_thread(webdriver.Chrome, options=get_driver_options(headless=True, user_data_dir=None))
        ACTIVE_DRIVERS[uid] = driver
        
        driver.get("https://www.whatsapp.com/contact/nsc")
        await asyncio.sleep(4) # Даем прогрузиться
        
        # ЗАПОЛНЕНИЕ ПОЛЕЙ
        try:
            driver.find_element(By.ID, "phone_number").send_keys(phone)
            driver.find_element(By.ID, "email").send_keys(email)
            driver.find_element(By.ID, "email_confirm").send_keys(email)
            
            # Выбор Android (иногда нужно кликнуть)
            try: driver.find_element(By.XPATH, "//input[@value='android']").click()
            except: pass

            appeals = [
                "Hello. Banned by mistake. Please unban.", 
                "Здравствуйте. Мой номер заблокирован ошибочно. Разбаньте.",
                "I lost access to my account. Please restore."
            ]
            msg_box = driver.find_element(By.ID, "message")
            msg_box.send_keys(random.choice(appeals))
            
            await bot.send_message(uid, "📝 **Всё заполнено!**\nЖми ЧЕК, проверяй Email, и потом 'ОТПРАВИТЬ ФОРМУ'.")
            
        except Exception as fill_err:
            await bot.send_message(uid, f"⚠️ Не все поля заполнились: {fill_err}. Проверь через ЧЕК.")

        # УДЕРЖАНИЕ СЕССИИ (15 минут)
        for _ in range(90):
            if uid not in ACTIVE_DRIVERS: break
            await asyncio.sleep(10)
            
    except Exception as e:
        await bot.send_message(uid, f"❌ Ошибка Unban: {e}")
    finally:
        if driver:
            try: driver.quit()
            except: pass
        if uid in ACTIVE_DRIVERS: del ACTIVE_DRIVERS[uid]

# ==========================================
# 3. КНОПКИ УПРАВЛЕНИЯ
# ==========================================

@dp.callback_query(F.data == "check")
async def check_screen(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Браузер закрыт (или грузится)", show_alert=True)
    
    try:
        scr = await asyncio.to_thread(driver.get_screenshot_as_png)
        # Добавляем время в подпись, чтобы ты видел, свежий ли скрин
        now_time = datetime.now().strftime("%H:%M:%S")
        await call.message.answer_photo(BufferedInputFile(scr, "s.png"), caption=f"🖥 Экран на {now_time}")
    except: await call.answer("Ошибка связи с браузером", show_alert=True)

@dp.callback_query(F.data == "click_link_btn")
async def btn_link(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Браузер закрыт", show_alert=True)
    try:
        xpaths = ["//span[contains(text(), 'Link with phone')]", "//a[contains(@href, 'link-device')]", "//span[contains(text(), 'Связать с номером')]"]
        for xp in xpaths:
            try: driver.find_element(By.XPATH, xp).click(); break
            except: continue
        await call.answer("Попытка нажатия выполнена")
    except: await call.answer("Не нашел кнопку")

@dp.callback_query(F.data == "type_phone_btn")
async def btn_type(call: types.CallbackQuery, state: FSMContext):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Браузер закрыт", show_alert=True)
    data = await state.get_data()
    phone = data.get("phone")
    if not phone: return await call.answer("Нет номера для ввода")
    
    try:
        inp = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.XPATH, "//input[@aria-label='Type your phone number.'] | //input[@type='text']")))
        driver.execute_script("arguments[0].value = '';", inp)
        inp.send_keys(Keys.CONTROL + "a" + Keys.BACKSPACE)
        for ch in phone: inp.send_keys(ch); await asyncio.sleep(0.05)
        inp.send_keys(Keys.ENTER)
        await call.answer(f"Ввел: {phone}")
    except: await call.answer("Ошибка ввода")

@dp.callback_query(F.data == "get_code_btn")
async def btn_code(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Браузер закрыт", show_alert=True)
    try:
        el = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.XPATH, "//div[@aria-details='link-device-phone-number-code']")))
        await call.message.answer(f"🔑 КОД: `{el.text}`", parse_mode="Markdown")
    except: 
        scr = driver.get_screenshot_as_png()
        await call.message.answer_photo(BufferedInputFile(scr, "err.png"), caption="Код не вижу")

@dp.callback_query(F.data == "submit_unban_btn")
async def btn_submit(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Браузер закрыт", show_alert=True)
    
    await call.message.answer("🚀 Отправляю форму...")
    try:
        # Ищем кнопку отправки агрессивно
        btn = driver.find_element(By.XPATH, "//button[contains(text(), 'Next Step') or contains(text(), 'Отправить') or contains(text(), 'Send')]")
        btn.click()
        
        await asyncio.sleep(3)
        scr = driver.get_screenshot_as_png()
        await call.message.answer_photo(BufferedInputFile(scr, "sent.png"), caption="✅ Кнопка нажата! Проверь результат.")
        
        # Закрываем, так как дело сделано
        driver.quit()
        if call.from_user.id in ACTIVE_DRIVERS: del ACTIVE_DRIVERS[call.from_user.id]
        
    except Exception as e:
        await call.message.answer(f"❌ Не смог нажать кнопку: {e}")

@dp.callback_query(F.data == "done")
async def done_action(call: types.CallbackQuery, state: FSMContext):
    uid = call.from_user.id
    if uid in ACTIVE_DRIVERS:
        ACTIVE_DRIVERS[uid].quit()
        del ACTIVE_DRIVERS[uid]
    
    data = await state.get_data()
    # Если это был логин нового номера, активируем его
    if data.get("phone") and not data.get("unban_email"):
        db_update_status(data.get("phone"), 'active')
        await call.message.edit_text(f"✅ Аккаунт {data.get('phone')} активирован.")
    else:
        await call.message.edit_text("✅ Работа завершена.")

@dp.callback_query(F.data == "list")
async def list_a(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    phones = db_get_active_phones()
    txt = "\n".join([f"🟢 {p}" for p in phones]) if phones else "Пусто"
    await call.message.edit_text(f"Активные аккаунты:\n{txt}", reply_markup=kb_main())

# --- ФОНОВЫЙ ФАРМ (ОЧЕНЬ ТИХИЙ) ---
async def farm_loop():
    asyncio.create_task(zombie_killer())
    while True:
        phones = db_get_active_phones()
        if phones:
            p = random.choice(phones)
            hour = datetime.now().hour
            # Днем - заход раз в 5-15 минут
            # Ночью - заход раз в 30-60 минут (с шансом 20%)
            if (hour >= 23 or hour < 7):
                if random.random() < 0.2:
                     asyncio.create_task(farm_bg(p))
            else:
                 asyncio.create_task(farm_bg(p))
        
        await asyncio.sleep(random.randint(300, 900))

async def farm_bg(phone):
    async with BROWSER_SEMAPHORE:
        path = os.path.join(SESSIONS_DIR, str(phone))
        if not os.path.exists(path): return
        try:
            driver = await asyncio.to_thread(webdriver.Chrome, options=get_driver_options(user_data_dir=path))
            driver.get("https://web.whatsapp.com/")
            await asyncio.sleep(40) # Просто онлайн 40 сек
            driver.quit()
        except: pass

async def main():
    init_db()
    asyncio.create_task(farm_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
