import asyncio
import os
import logging
import sqlite3
import re
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
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
BROWSER_SEMAPHORE = asyncio.Semaphore(1) 
DB_NAME = 'bot_database.db'

# Глобальный словарь для кнопки "ЧЕК"
ACTIVE_DRIVERS = {}

# --- БАЗА ДАННЫХ ---
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS accounts 
                        (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, phone_number TEXT, added_date TEXT)''')
        conn.commit()

def db_add(user_id, phone):
    with sqlite3.connect(DB_NAME) as conn:
        if not conn.execute("SELECT id FROM accounts WHERE user_id = ? AND phone_number = ?", (user_id, phone)).fetchone():
            conn.execute("INSERT INTO accounts (user_id, phone_number, added_date) VALUES (?, ?, ?)", 
                         (user_id, phone, datetime.now().strftime("%Y-%m-%d")))

def db_get(user_id):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT id, phone_number FROM accounts WHERE user_id = ?", (user_id,)).fetchall()

def db_delete(acc_id):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("DELETE FROM accounts WHERE id = ?", (acc_id,))

# --- БОТ ---
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class Form(StatesGroup):
    wait_phone = State()

# --- ЛОГИКА БРАУЗЕРА (EDGE + NETHERLANDS MASK) ---
def get_driver():
    options = Options()
    
    # 1. Путь к Chrome
    CHROME_BINARIES = ["/usr/bin/google-chrome", "/opt/google/chrome/chrome"]
    found_path = next((p for p in CHROME_BINARIES if os.path.exists(p)), "/usr/bin/google-chrome")
    options.binary_location = found_path

    # 2. Флаги (Стабильность + Анти-детект)
    options.add_argument("--headless=new") 
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage") 
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-setuid-sandbox")
    options.add_argument("--window-size=1366,768") # Стандартный размер ноута
    options.add_argument("--ignore-certificate-errors")
    
    # User-Agent: Microsoft Edge (Linux) - как у конкурентов
    EDGE_UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0'
    options.add_argument(f"user-agent={EDGE_UA}")

    # Язык: Английский (Международный), чтобы не палить RU локаль
    options.add_argument("accept-language=en-US,en;q=0.9") 
    
    # 3. Драйвер
    service = Service(executable_path="/usr/local/bin/chromedriver")
    
    try:
        driver = webdriver.Chrome(service=service, options=options)
        return driver
    except Exception as e:
        logging.error(f"❌ Driver Init Error: {e}")
        raise e

def run_auth_process(user_id, phone_number):
    driver = None
    try:
        driver = get_driver()
        ACTIVE_DRIVERS[user_id] = driver 
        
        driver.set_page_load_timeout(60)
        driver.get("https://web.whatsapp.com/")
        wait = WebDriverWait(driver, 60)

        # === ШАГ 1: "Link with phone number" ===
        try:
            # Ищем чуть дольше, чтобы страница точно прогрузилась
            time.sleep(5) 
            btn_xpath = "//span[contains(text(), 'Link with phone number')] | //div[contains(text(), 'Link with phone number')]"
            btn = wait.until(EC.element_to_be_clickable((By.XPATH, btn_xpath)))
            driver.execute_script("arguments[0].click();", btn)
            time.sleep(2) 
        except Exception as e:
            pass 

        # === ШАГ 2: ВВОД НОМЕРА (SUPER SAFE MODE) ===
        try:
            inp_xpath = "//input[@aria-label='Type your phone number.'] | //input[@type='text']"
            inp = wait.until(EC.presence_of_element_located((By.XPATH, inp_xpath)))
            
            # 1. Фокус
            driver.execute_script("arguments[0].focus();", inp)
            time.sleep(0.5)

            # 2. Ввод JS
            driver.execute_script(f"arguments[0].value = '+{phone_number}';", inp)
            driver.execute_script("arguments[0].dispatchEvent(new Event('input', { bubbles: true }));", inp)
            time.sleep(0.2)
            driver.execute_script("arguments[0].dispatchEvent(new Event('change', { bubbles: true }));", inp)
            
            # 3. УБИРАЕМ ФОКУС (BLUR) - Важно для валидации!
            driver.execute_script("arguments[0].blur();", inp)
            time.sleep(2) 

            # 4. Жмем NEXT
            next_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//div[text()='Next']")))
            driver.execute_script("arguments[0].click();", next_btn)
            
        except Exception as e:
             return {"status": "error", "data": f"Ввод: {e}"}

        # === ШАГ 3: АКТИВНЫЙ ПОИСК КОДА (БЕЗ ОЖИДАНИЯ) ===
        # Мы будем проверять наличие кода в цикле в течение 60 секунд.
        # Как только найдем - сразу возвращаем.
        
        start_time = time.time()
        code_xpath = "//div[@aria-details='link-device-phone-number-code'] | //span[contains(@aria-label, 'link code')]"
        
        while time.time() - start_time < 60:
            try:
                # Пытаемся найти элемент БЫСТРО (таймаут 1 сек)
                code_el = driver.find_element(By.XPATH, code_xpath)
                if code_el and code_el.text.strip():
                    return {"status": "ok", "type": "code", "data": code_el.text}
            except:
                # Если не нашли, ждем немного и повторяем
                pass
            
            time.sleep(1) # Проверка каждую секунду

        # Если цикл закончился, а кода нет -> Скриншот (значит QR или ошибка)
        screenshot = driver.get_screenshot_as_png()
        return {"status": "ok", "type": "screenshot", "data": screenshot}

    except Exception as e:
        return {"status": "error", "data": str(e)}
    finally:
        # Чистим
        if user_id in ACTIVE_DRIVERS:
            del ACTIVE_DRIVERS[user_id]
        if driver:
            try: driver.quit()
            except: pass

# --- ИНТЕРФЕЙС ---
def kb_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="add_acc")],
        [InlineKeyboardButton(text="📂 Мои номера", callback_data="list_acc"), 
         InlineKeyboardButton(text="👤 Профиль", callback_data="profile")]
    ])

def kb_back():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]])

def kb_process():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 ЧЕК (Скриншот)", callback_data="check_browser")]
    ])

def kb_acc_list(accounts):
    kb = []
    for acc in accounts:
        kb.append([InlineKeyboardButton(text=f"📱 +{acc[1]}", callback_data=f"man_{acc[0]}")])
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def kb_manage(acc_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del_{acc_id}")],
        [InlineKeyboardButton(text="🔙 К списку", callback_data="list_acc")]
    ])

# --- ХЕНДЛЕРЫ ---
@dp.message(Command("start"))
async def start(msg: types.Message, state: FSMContext):
    await state.clear()
    await msg.answer("👋 **WhatsApp Manager**\n🇳🇱 Mode: Netherlands/Edge", reply_markup=kb_menu(), parse_mode="Markdown")

@dp.callback_query(F.data == "main_menu")
async def menu(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.delete()
    await call.message.answer("👋 **Главное меню**", reply_markup=kb_menu(), parse_mode="Markdown")

@dp.callback_query(F.data == "add_acc")
async def add(call: types.CallbackQuery, state: FSMContext):
    if BROWSER_SEMAPHORE.locked():
        await call.answer("⚠️ Очередь занята! Ждите.", show_alert=True)
        return
    await call.message.edit_text("📞 **Введите номер телефона** (79XXXXXXXXX)", reply_markup=kb_back(), parse_mode="Markdown")
    await state.set_state(Form.wait_phone)

@dp.callback_query(F.data == "check_browser")
async def check(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver:
        await call.answer("⚠️ Браузер не активен.", show_alert=True)
        return
    await call.answer("📸 Снимаю...")
    try:
        screen = await asyncio.to_thread(driver.get_screenshot_as_png)
        await call.message.answer_photo(BufferedInputFile(screen, "status.png"), caption="👀 Текущий экран")
    except Exception as e:
        await call.answer(f"Ошибка: {e}", show_alert=True)

@dp.message(Form.wait_phone)
async def process(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    if len(phone) == 11 and phone.startswith('8'): phone = '7' + phone[1:]
    elif len(phone) == 10: phone = '7' + phone
    
    if len(phone) < 10:
        await msg.answer("❌ Неверный номер", reply_markup=kb_back())
        return

    status = await msg.answer(
        f"🚀 **Запуск...**\n🌍 Server: NL Mode\n📱 Номер: `+{phone}`\n\n⏳ Ждем код (до 60 сек)...", 
        reply_markup=kb_process(), 
        parse_mode="Markdown"
    )

    async with BROWSER_SEMAPHORE:
        res = await asyncio.to_thread(run_auth_process, msg.from_user.id, phone)

    try: await status.delete()
    except: pass

    if res['status'] == 'ok':
        if res['type'] == 'code':
            db_add(msg.from_user.id, phone)
            code = res['data'].replace("-", "")
            await msg.answer(f"✅ **КОД ПОЛУЧЕН:**\n\n`{code}`", reply_markup=kb_back(), parse_mode="Markdown")
        elif res['type'] == 'screenshot':
            await msg.answer_photo(BufferedInputFile(res['data'], "err.png"), caption="⚠️ Код не пришел (см. скрин). Скорее всего нужен QR.", reply_markup=kb_back())
    else:
        await msg.answer(f"❌ Ошибка: {res['data']}", reply_markup=kb_back())
    
    await state.clear()

@dp.callback_query(F.data == "list_acc")
async def list_acc(call: types.CallbackQuery):
    accs = db_get(call.from_user.id)
    if not accs: await call.message.edit_text("📭 Пусто", reply_markup=kb_back())
    else: await call.message.edit_text(f"📂 **Номера ({len(accs)}):**", reply_markup=kb_acc_list(accs), parse_mode="Markdown")

@dp.callback_query(F.data == "profile")
async def profile(call: types.CallbackQuery):
    accs = db_get(call.from_user.id)
    await call.message.edit_text(f"👤 **Профиль**\n📱 Номеров: {len(accs)}", reply_markup=kb_back(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("man_"))
async def manage(call: types.CallbackQuery):
    acc_id = call.data.split("_")[1]
    await call.message.edit_text(f"⚙️ Аккаунт #{acc_id}", reply_markup=kb_manage(acc_id))

@dp.callback_query(F.data.startswith("del_"))
async def delete(call: types.CallbackQuery):
    db_delete(call.data.split("_")[1])
    await call.answer("Удалено")
    await list_acc(call)

async def main():
    init_db()
    print("✅ BOT STARTED (NETHERLANDS MODE)")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
