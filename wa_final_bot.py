import asyncio
import os
import logging
import sqlite3
import random
import re
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
from selenium.common.exceptions import TimeoutException

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except:
    ADMIN_ID = 0

# Семафор: только 1 браузер одновременно, чтобы не убить сервер
BROWSER_SEMAPHORE = asyncio.Semaphore(1)
DB_NAME = 'bot_database.db'
SESSIONS_DIR = "/app/sessions"

ACTIVE_DRIVERS = {} 
fake = Faker('ru_RU')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- БАЗА ДАННЫХ ---
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS accounts 
                        (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                         user_id INTEGER, 
                         phone_number TEXT UNIQUE, 
                         status TEXT DEFAULT 'pending', 
                         start_time TIMESTAMP,
                         last_activity TIMESTAMP)''')
        conn.commit()

def db_update_status(phone, status):
    with sqlite3.connect(DB_NAME) as conn:
        now = datetime.now()
        if status == 'active':
            conn.execute("UPDATE accounts SET status = ?, last_activity = ?, start_time = COALESCE(start_time, ?) WHERE phone_number = ?", 
                         (status, now, now, phone))
        else:
            conn.execute("UPDATE accounts SET status = ? WHERE phone_number = ?", (status, phone))

def db_add_pending(user_id, phone):
    with sqlite3.connect(DB_NAME) as conn:
        try:
            conn.execute("INSERT INTO accounts (user_id, phone_number, status, start_time) VALUES (?, ?, 'pending', ?)", 
                         (user_id, phone, datetime.now()))
        except sqlite3.IntegrityError:
            conn.execute("UPDATE accounts SET status = 'pending', start_time = ? WHERE phone_number = ?", 
                         (datetime.now(), phone))

def db_get_user_accounts(user_id):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT phone_number, status FROM accounts WHERE user_id = ?", (user_id,)).fetchall()

def db_get_stats():
    with sqlite3.connect(DB_NAME) as conn:
        total = conn.execute("SELECT count(*) FROM accounts").fetchone()[0]
        active = conn.execute("SELECT count(*) FROM accounts WHERE status = 'active'").fetchone()[0]
        dead = conn.execute("SELECT count(*) FROM accounts WHERE status = 'dead'").fetchone()[0]
        return total, active, dead

# --- ЛОГИКА БРАУЗЕРА ---
def get_driver(phone_number=None):
    options = Options()
    # Путь к Chrome (Docker/Linux)
    options.binary_location = "/usr/bin/google-chrome"
    
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage") 
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1366,768")
    
    # Маскировка под обычный Linux Desktop
    options.add_argument("user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    
    # Сохранение сессии (профиль)
    if phone_number:
        profile_path = os.path.join(SESSIONS_DIR, phone_number)
        options.add_argument(f"--user-data-dir={profile_path}")

    service = Service(executable_path="/usr/local/bin/chromedriver")
    return webdriver.Chrome(service=service, options=options)

# --- ИНТЕРФЕЙС ---
def kb_main(uid):
    kb = [[InlineKeyboardButton(text="➕ Добавить Аккаунт", callback_data="add")],
          [InlineKeyboardButton(text="📂 Мои Аккаунты", callback_data="list")]]
    if uid == ADMIN_ID:
        kb.append([InlineKeyboardButton(text="👑 Админ Панель", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def kb_auth_process():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 ЧЕК (QR / Статус)", callback_data="check_browser")],
        [InlineKeyboardButton(text="✅ Проверить вход", callback_data="check_scan")]
    ])

# --- БОТ ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
class Form(StatesGroup): wait_phone = State()

@dp.message(Command("start"))
async def start(msg: types.Message):
    await msg.answer("🤖 **WhatsApp Control Panel**", reply_markup=kb_main(msg.from_user.id), parse_mode="Markdown")

@dp.callback_query(F.data == "add")
async def add_start(call: types.CallbackQuery, state: FSMContext):
    await call.message.edit_text("📞 Введите номер телефона (только цифры, например 79001234567):")
    await state.set_state(Form.wait_phone)

@dp.message(Form.wait_phone)
async def process_phone(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    if len(phone) < 10:
        await msg.answer("❌ Номер слишком короткий.")
        return
    
    db_add_pending(msg.from_user.id, phone)
    await state.update_data(phone=phone)
    
    # Сразу даем пользователю кнопки, не ждем браузер
    await msg.answer(
        f"🚀 **Запуск процесса для {phone}...**\n\n"
        "1. Бот откроет WhatsApp.\n"
        "2. Введет номер.\n"
        "3. Если кода не будет — покажет QR.\n\n"
        "👉 **Жми кнопку 'ЧЕК' через 15-20 секунд!**", 
        reply_markup=kb_auth_process(), parse_mode="Markdown"
    )
    
    # Запускаем браузер в фоне
    asyncio.create_task(bg_login_task(msg.from_user.id, phone))

# --- ФОНОВАЯ ЗАДАЧА ВХОДА (САМОЕ ВАЖНОЕ) ---
async def bg_login_task(user_id, phone):
    async with BROWSER_SEMAPHORE:
        driver = None
        try:
            # 1. Запуск
            logger.info(f"Starting driver for {phone}")
            driver = await asyncio.to_thread(get_driver, phone)
            ACTIVE_DRIVERS[user_id] = driver
            driver.set_page_load_timeout(60)
            
            logger.info("Opening WA Web")
            driver.get("https://web.whatsapp.com/")
            
            # 2. Ждем кнопку "Link with phone number"
            # Если мы уже залогинены, этот этап пропустится, и пользователь увидит это через ЧЕК
            wait = WebDriverWait(driver, 20)
            try:
                # Ищем кнопку по тексту (это работает лучше всего)
                link_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//span[contains(text(), 'Link with phone number')] | //div[contains(text(), 'Link with phone number')]")))
                driver.execute_script("arguments[0].click();", link_btn)
                logger.info("Clicked 'Link with phone'")
                
                # 3. ВВОД НОМЕРА (ИСПРАВЛЕНИЕ КРАСНОЙ ОШИБКИ)
                # Ждем поле ввода
                inp = wait.until(EC.element_to_be_clickable((By.XPATH, "//input[@aria-label='Type your phone number.'] | //input[@type='text']")))
                
                # Кликаем, чтобы фокус точно был там
                inp.click()
                await asyncio.sleep(0.5)
                
                # ЧИСТИМ поле (на случай если там +7 уже стоит)
                inp.send_keys(Keys.CONTROL + "a")
                inp.send_keys(Keys.DELETE)
                
                # ПЕЧАТАЕМ номер (эмуляция клавиатуры)
                # WhatsApp валидирует только реальные нажатия
                full_phone = f"+{phone}"
                for char in full_phone:
                    inp.send_keys(char)
                    await asyncio.sleep(random.uniform(0.05, 0.2)) # Рандомная задержка как у человека
                
                logger.info("Phone typed")
                await asyncio.sleep(1)
                
                # Жмем ENTER (надежнее, чем искать кнопку Next)
                inp.send_keys(Keys.ENTER)
                logger.info("Enter pressed")
                
                # Теперь WhatsApp либо покажет код, либо вернет на QR.
                # Мы ничего не делаем, просто держим браузер открытым.
                # Пользователь увидит результат через кнопку "ЧЕК".
                
            except TimeoutException:
                # Если кнопка "Link with phone" не найдена, значит там сразу QR
                logger.info("Link button not found, assuming QR mode")
                pass
            except Exception as e:
                logger.error(f"Input error: {e}")

            # Держим браузер открытым 5 минут, чтобы юзер успел сосканировать
            # или ввести код (если он вдруг появится)
            await asyncio.sleep(300) 
            
        except Exception as e:
            logger.error(f"Global Background Error: {e}")
        finally:
            # Если пользователь не нажал "Проверить вход", браузер закроется сам через 5 мин
            if user_id in ACTIVE_DRIVERS:
                d = ACTIVE_DRIVERS.pop(user_id)
                try: d.quit()
                except: pass

@dp.callback_query(F.data == "check_browser")
async def check_browser(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver:
        await call.answer("⚠️ Браузер еще запускается или закрыт по таймауту.", show_alert=True)
        return
    
    await call.answer("📸 Получаю изображение...")
    try:
        # Делаем скриншот текущего состояния
        screen = await asyncio.to_thread(driver.get_screenshot_as_png)
        
        # Пытаемся найти 8-значный код (вдруг дали?)
        code_text = ""
        try:
            code_el = driver.find_element(By.XPATH, "//div[@aria-details='link-device-phone-number-code']")
            code_text = f"\n\n🔑 **КОД:** `{code_el.text}`"
        except: pass
        
        caption = "👀 **Текущий экран**"
        if code_text:
            caption += code_text
        else:
            caption += "\n\nСкорее всего нужен **QR-код**. Отсканируйте его!"

        await call.message.answer_photo(BufferedInputFile(screen, "status.png"), caption=caption, parse_mode="Markdown")
    except Exception as e:
        await call.answer(f"Ошибка получения скрина: {e}")

@dp.callback_query(F.data == "check_scan")
async def check_scan(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    phone = data.get("phone")
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    
    if not driver:
        await call.answer("Браузер закрыт.")
        return

    try:
        # Признак входа - панель чатов слева
        driver.find_element(By.XPATH, "//div[@id='pane-side'] | //span[@data-icon='chat']")
        
        db_update_status(phone, 'active')
        await call.message.edit_text(f"✅ **УСПЕХ!**\n\nАккаунт `{phone}` успешно привязан и сохранен в базе.", 
                                     reply_markup=kb_main(call.from_user.id), parse_mode="Markdown")
        
        # Закрываем драйвер, файлы профиля сохранятся на диске
        driver.quit()
        if call.from_user.id in ACTIVE_DRIVERS:
            del ACTIVE_DRIVERS[call.from_user.id]
        await state.clear()
        
    except:
        await call.answer("❌ Вход не обнаружен! Сначала отсканируйте QR.", show_alert=True)

@dp.callback_query(F.data == "list")
async def list_accs(call: types.CallbackQuery):
    accs = db_get_user_accounts(call.from_user.id)
    text = "📂 **Ваши аккаунты:**\n"
    if not accs:
        text += "Список пуст."
    else:
        for p, s in accs:
            icon = "🟢" if s == 'active' else "🔴"
            text += f"\n{icon} `{p}`"
    
    try: await call.message.edit_text(text, reply_markup=kb_main(call.from_user.id), parse_mode="Markdown")
    except: await call.answer()

@dp.callback_query(F.data == "admin")
async def admin_panel(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    t, a, d = db_get_stats()
    text = f"📊 **Статистика Фермы**\n\nВсего: {t}\nАктив: {a}\nСлет: {d}"
    try: await call.message.edit_text(text, reply_markup=kb_main(call.from_user.id), parse_mode="Markdown")
    except: await call.answer()

async def main():
    init_db()
    print("✅ BOT STARTED")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
