import asyncio
import os
import logging
import sqlite3
import random
import shutil
import re
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from faker import Faker

# --- SELENIUM IMPORTS ---
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# !!! БЕРЕМ ID АДМИНА ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ !!!
# Если переменной нет, будет 0 (никто не админ)
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except:
    ADMIN_ID = 0

BROWSER_SEMAPHORE = asyncio.Semaphore(1) # Очередь браузеров (чтобы не убить RAM)
DB_NAME = 'bot_database.db'
SESSIONS_DIR = "/app/sessions"

ACTIVE_DRIVERS = {} # Для ручного управления (вход)
fake = Faker('ru_RU') # Генератор русского текста

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- БАЗА ДАННЫХ ---
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        # Таблица аккаунтов
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
        if status == 'active':
            # Если активируем, ставим время начала, если его нет
            conn.execute("UPDATE accounts SET status = ?, last_activity = ?, start_time = COALESCE(start_time, ?) WHERE phone_number = ?", 
                         (status, datetime.now(), datetime.now(), phone))
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

def db_get_active():
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT phone_number, user_id, start_time FROM accounts WHERE status = 'active'").fetchall()

def db_get_stats():
    with sqlite3.connect(DB_NAME) as conn:
        total = conn.execute("SELECT count(*) FROM accounts").fetchone()[0]
        active = conn.execute("SELECT count(*) FROM accounts WHERE status = 'active'").fetchone()[0]
        dead = conn.execute("SELECT count(*) FROM accounts WHERE status = 'dead'").fetchone()[0]
        return total, active, dead

def db_delete(phone):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("DELETE FROM accounts WHERE phone_number = ?", (phone,))
    try: shutil.rmtree(os.path.join(SESSIONS_DIR, phone))
    except: pass

def db_get_user_accounts(user_id):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT phone_number, status FROM accounts WHERE user_id = ?", (user_id,)).fetchall()

# --- БРАУЗЕР ---
def get_driver(phone_number=None):
    options = Options()
    
    # Пути
    CHROME_BINARIES = ["/usr/bin/google-chrome", "/opt/google/chrome/chrome"]
    found_path = next((p for p in CHROME_BINARIES if os.path.exists(p)), "/usr/bin/google-chrome")
    options.binary_location = found_path

    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage") 
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-setuid-sandbox")
    options.add_argument("--window-size=1366,768")
    options.add_argument("--ignore-certificate-errors")
    
    # MASK: Edge Linux + English
    EDGE_UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0'
    options.add_argument(f"user-agent={EDGE_UA}")
    options.add_argument("accept-language=en-US,en;q=0.9") 

    # СОХРАНЕНИЕ СЕССИИ
    if phone_number:
        profile_path = os.path.join(SESSIONS_DIR, phone_number)
        options.add_argument(f"--user-data-dir={profile_path}")

    service = Service(executable_path="/usr/local/bin/chromedriver")
    return webdriver.Chrome(service=service, options=options)

# --- ПРОЦЕСС ВХОДА ---
def run_login_attempt(user_id, phone_number):
    driver = None
    try:
        driver = get_driver(phone_number)
        ACTIVE_DRIVERS[user_id] = driver 
        
        driver.get("https://web.whatsapp.com/")
        wait = WebDriverWait(driver, 60)

        # 1. Проверка: может уже вошли?
        try:
            wait.until(EC.presence_of_element_located((By.XPATH, "//div[@id='pane-side']")))
            db_update_status(phone_number, 'active')
            return {"status": "ok", "type": "restored", "data": "Сессия жива!"}
        except: pass

        # 2. Жмем Link with phone
        try:
            time.sleep(3)
            btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//span[contains(text(), 'Link with phone number')] | //div[contains(text(), 'Link with phone number')]")))
            driver.execute_script("arguments[0].click();", btn)
        except: pass

        # 3. Ввод номера
        try:
            inp = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@aria-label='Type your phone number.'] | //input[@type='text']")))
            driver.execute_script("arguments[0].focus();", inp)
            driver.execute_script(f"arguments[0].value = '+{phone_number}';", inp)
            driver.execute_script("arguments[0].dispatchEvent(new Event('input', { bubbles: true }));", inp)
            time.sleep(0.5)
            driver.execute_script("arguments[0].blur();", inp) # Blur важен
            time.sleep(1)
            next_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//div[text()='Next']")))
            driver.execute_script("arguments[0].click();", next_btn)
        except Exception as e:
            # Если ошибка ввода, скорее всего сразу QR
            pass 

        # 4. Выдаем QR (так как код заблочен)
        time.sleep(3)
        screenshot = driver.get_screenshot_as_png()
        
        # Не закрываем драйвер, ждем сканирования
        return {"status": "ok", "type": "qr", "data": screenshot}

    except Exception as e:
        if user_id in ACTIVE_DRIVERS: del ACTIVE_DRIVERS[user_id]
        if driver: driver.quit()
        return {"status": "error", "data": str(e)}

# --- ПРОВЕРКА ПОСЛЕ СКАНА ---
def check_scan_status(user_id, phone):
    driver = ACTIVE_DRIVERS.get(user_id)
    if not driver: return False
    
    try:
        # Ждем загрузку чатов (признак успеха)
        WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.XPATH, "//div[@id='pane-side']")))
        
        # Успех -> сохраняем статус
        db_update_status(phone, 'active')
        
        # Закрываем, чтобы сохранить файлы сессии на диск
        driver.quit()
        del ACTIVE_DRIVERS[user_id]
        return True
    except:
        return False

# --- ФЕРМА ПРОГРЕВА ---
async def farm_loop():
    while True:
        try:
            # Интервал прогрева (рандом 2-5 минут)
            wait_time = random.randint(120, 300)
            await asyncio.sleep(wait_time)
            
            accounts = db_get_active()
            if len(accounts) < 2: continue

            # Выбираем пару
            sender = random.choice(accounts)
            receiver = random.choice(accounts)
            while sender == receiver: receiver = random.choice(accounts)
            
            s_phone, s_uid, s_time = sender
            r_phone, _, _ = receiver
            
            logger.info(f"🔥 FARM WORK: {s_phone} -> {r_phone}")

            async with BROWSER_SEMAPHORE:
                driver = await asyncio.to_thread(get_driver, s_phone)
                try:
                    driver.get(f"https://web.whatsapp.com/send?phone={r_phone}")
                    wait = WebDriverWait(driver, 45)
                    
                    # Проверка на СЛЕТ (Log out)
                    try:
                        if "Log out" in driver.page_source or "landing-title" in driver.page_source:
                            raise Exception("Logged out")
                    except: pass

                    # Ждем поле ввода
                    inp_xpath = "//div[@aria-placeholder='Type a message'] | //div[@contenteditable='true'][@data-tab='10']"
                    inp = wait.until(EC.presence_of_element_located((By.XPATH, inp_xpath)))
                    
                    # Генерируем текст
                    msg = fake.sentence(nb_words=random.randint(2, 8))
                    
                    # Пишем
                    driver.execute_script("arguments[0].focus();", inp)
                    driver.execute_script(f"document.execCommand('insertText', false, '{msg}');", inp)
                    time.sleep(1)
                    
                    # Enter
                    driver.execute_script("arguments[0].dispatchEvent(new KeyboardEvent('keydown', {bubbles: true, cancelable: true, keyCode: 13}));", inp)
                    
                    time.sleep(5) # Ждем ухода
                    logger.info(f"✅ Sent: {msg}")

                except Exception as e:
                    logger.error(f"❌ Dead Account: {s_phone} | {e}")
                    db_update_status(s_phone, 'dead')
                    
                    # Расчет времени жизни
                    try:
                        start_dt = datetime.strptime(s_time, "%Y-%m-%d %H:%M:%S.%f")
                        lived = datetime.now() - start_dt
                        # Пишем владельцу (или админу)
                        await bot.send_message(s_uid, f"☠️ **АККАУНТ СЛЕТЕЛ**\n📱 {s_phone}\n⏱ Прожил: {str(lived).split('.')[0]}")
                    except: pass
                
                finally:
                    driver.quit()

        except Exception as e:
            logger.error(f"Farm Loop Error: {e}")
            await asyncio.sleep(60)

# --- БОТ ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
class Form(StatesGroup): wait_phone = State()

def kb_main(uid):
    kb = [[InlineKeyboardButton(text="➕ Добавить Аккаунт", callback_data="add")],
          [InlineKeyboardButton(text="📂 Мои Аккаунты", callback_data="list")]]
    if uid == ADMIN_ID:
        kb.append([InlineKeyboardButton(text="👑 Админ Панель", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def kb_check():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я отсканировал QR", callback_data="check_scan")],
        [InlineKeyboardButton(text="🔄 Новый QR", callback_data="refresh_qr")]
    ])

@dp.message(Command("start"))
async def start(msg: types.Message):
    await msg.answer(f"🤖 **WhatsApp Farm v2.0**\nВаш ID: `{msg.from_user.id}`", 
                     reply_markup=kb_main(msg.from_user.id), parse_mode="Markdown")

@dp.callback_query(F.data == "admin")
async def admin_panel(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    total, active, dead = db_get_stats()
    await call.message.edit_text(f"📊 **СТАТИСТИКА**\n\nВсего: {total}\n🟢 В работе: {active}\n🔴 Слетело: {dead}", 
                                 reply_markup=kb_main(call.from_user.id), parse_mode="Markdown")

@dp.callback_query(F.data == "add")
async def add_start(call: types.CallbackQuery, state: FSMContext):
    if BROWSER_SEMAPHORE.locked():
        await call.answer("⚠️ Очередь занята, ждите...", show_alert=True)
        return
    await call.message.edit_text("📞 Введите номер (79XXXXXXXXX):")
    await state.set_state(Form.wait_phone)

@dp.message(Form.wait_phone)
async def add_process(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    db_add_pending(msg.from_user.id, phone)
    await state.update_data(phone=phone)
    
    status = await msg.answer("🚀 Запуск браузера... (Нидерланды)")
    
    async with BROWSER_SEMAPHORE:
        res = await asyncio.to_thread(run_login_attempt, msg.from_user.id, phone)
    
    try: await status.delete()
    except: pass

    if res['status'] == 'ok' and res['type'] == 'qr':
        await msg.answer_photo(BufferedInputFile(res['data'], "qr.png"), 
                               caption="📱 **СКАНЕРУЙТЕ QR!**\n\nКак только отсканируете в телефоне — нажмите кнопку ниже.", 
                               reply_markup=kb_check(), parse_mode="Markdown")
    elif res['type'] == 'restored':
        await msg.answer("✅ Этот аккаунт уже в базе и активен!")
        await state.clear()
    else:
        await msg.answer(f"Ошибка: {res['data']}")
        await state.clear()

@dp.callback_query(F.data == "check_scan")
async def check_scan_handler(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    phone = data.get("phone")
    if not phone: 
        await call.message.answer("Ошибка контекста.")
        return

    is_logged = await asyncio.to_thread(check_scan_status, call.from_user.id, phone)
    
    if is_logged:
        await call.message.edit_text(f"✅ **АККАУНТ {phone} ДОБАВЛЕН!**\n\nТеперь он в ферме прогрева. Бот будет сам общаться.")
        await state.clear()
    else:
        await call.answer("❌ Вход не обнаружен. Попробуйте еще раз или обновите QR.", show_alert=True)

@dp.callback_query(F.data == "list")
async def my_accs(call: types.CallbackQuery):
    accs = db_get_user_accounts(call.from_user.id)
    text = "📂 **Ваши аккаунты:**\n"
    for p, s in accs:
        status = "🟢 Активен" if s == 'active' else ("🔴 Слетел" if s == 'dead' else "🟡 Ждет")
        text += f"\n📱 `{p}` — {status}"
    await call.message.edit_text(text, reply_markup=kb_main(call.from_user.id), parse_mode="Markdown")

async def main():
    init_db()
    print("✅ FARM STARTED")
    asyncio.create_task(farm_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
