import os
import asyncio
import sqlite3
import random
import logging
from datetime import datetime

# Библиотеки Selenium
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException, TimeoutException

# Библиотеки Aiogram
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile

# --- КОНФИГУРАЦИЯ ---
INSTANCE_ID = os.getenv("INSTANCE_ID", "1") 
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВАШ_ТОКЕН")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

# ЛИМИТЫ: 1 поток на инстанс (стабильность превыше всего)
BROWSER_SEMAPHORE = asyncio.Semaphore(1) 
SESSION_DIR = "./sessions"
DB_PATH = "imperator_v16.db"

# ЛОГИРОВАНИЕ
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger(f"Inst_{INSTANCE_ID}")

if not os.path.exists(SESSION_DIR):
    os.makedirs(SESSION_DIR)

# --- FSM ---
class AddAccount(StatesGroup):
    waiting_for_phone = State()
    browser_active = State()

# --- БАЗА ДАННЫХ ---
def db_init():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            phone_number TEXT UNIQUE,
            status TEXT DEFAULT 'pending',
            messages_sent INTEGER DEFAULT 0,
            last_active DATETIME
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS whitelist (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            approved BOOLEAN DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def is_approved(user_id):
    if user_id == ADMIN_ID: return True
    conn = sqlite3.connect(DB_PATH)
    res = conn.execute("SELECT approved FROM whitelist WHERE user_id = ?", (user_id,)).fetchone()
    conn.close()
    return res and res[0] == 1

def add_user_request(user_id, username):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO whitelist (user_id, username, approved) VALUES (?, ?, 0)", (user_id, username))
    conn.commit()
    conn.close()

def approve_user_db(user_id, status):
    conn = sqlite3.connect(DB_PATH)
    if status:
        conn.execute("UPDATE whitelist SET approved = 1 WHERE user_id = ?", (user_id,))
    else:
        conn.execute("DELETE FROM whitelist WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

# --- SELENIUM CORE (FIXED) ---
def get_driver(phone):
    options = Options()
    user_data = os.path.join(os.getcwd(), "sessions", f"inst_{INSTANCE_ID}", phone)
    
    options.add_argument(f"--user-data-dir={user_data}")
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    
    # ВАЖНО: Ставим FullHD разрешение, чтобы скрин был нормальным
    options.add_argument("--window-size=1920,1080")
    
    options.add_argument("--lang=en-US") # Лучше EN, кнопки стабильнее
    options.page_load_strategy = 'eager'
    
    driver = webdriver.Chrome(options=options)
    
    # Маскировка (Алматы)
    try:
        driver.execute_cdp_cmd("Emulation.setGeolocationOverride", {
            "latitude": 43.2389, "longitude": 76.8897, "accuracy": 100
        })
    except: pass
    
    return driver

# Кэш драйверов
active_drivers = {}

# --- TELEGRAM BOT ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- KEYBOARDS ---
def get_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Новый Аккаунт", callback_data="add_acc")],
        [InlineKeyboardButton(text="🔄 Проверить статус", callback_data="status")]
    ])

def get_control_kb(phone):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 ЧЕК (Экран)", callback_data=f"check_{phone}")],
        [InlineKeyboardButton(text="🔗 Вход по ссылке", callback_data=f"link_{phone}")],
        [InlineKeyboardButton(text="⌨️ Ввести номер", callback_data=f"type_{phone}")],
        [InlineKeyboardButton(text="✅ ГОТОВО (В базу)", callback_data=f"ready_{phone}")]
    ])

# --- HANDLERS ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    if is_approved(message.from_user.id):
        await message.answer(f"🤖 **Imperator v16.2 | Inst #{INSTANCE_ID}**\nРежим: 1920x1080 | Fix: Input", reply_markup=get_main_kb())
    else:
        add_user_request(message.from_user.id, message.from_user.username)
        await message.answer("🔒 Запрос отправлен администратору.")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Принять", callback_data=f"ap_{message.from_user.id}"),
             InlineKeyboardButton(text="Откл", callback_data=f"rj_{message.from_user.id}")]
        ])
        await bot.send_message(ADMIN_ID, f"Req: {message.from_user.id}", reply_markup=kb)

@dp.callback_query(F.data.startswith("ap_"))
async def approve(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    uid = int(cb.data.split("_")[1])
    approve_user_db(uid, True)
    await cb.message.edit_text(f"Approved {uid}")

@dp.callback_query(F.data.startswith("rj_"))
async def reject(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    uid = int(cb.data.split("_")[1])
    approve_user_db(uid, False)
    await cb.message.edit_text(f"Rejected {uid}")

@dp.callback_query(F.data == "add_acc")
async def add_acc_start(cb: CallbackQuery, state: FSMContext):
    if not is_approved(cb.from_user.id): return
    await cb.message.answer("Введите номер (только цифры):")
    await state.set_state(AddAccount.waiting_for_phone)

@dp.message(AddAccount.waiting_for_phone)
async def process_phone(msg: Message, state: FSMContext):
    phone = msg.text.strip().replace("+", "")
    await state.update_data(phone=phone)
    m = await msg.answer("⏳ Запускаю браузер (FullHD)...")
    
    try:
        async with BROWSER_SEMAPHORE:
            driver = await asyncio.to_thread(get_driver, phone)
            active_drivers[phone] = driver
            await asyncio.to_thread(driver.get, "https://web.whatsapp.com")
            
        await m.edit_text(f"✅ Браузер готов: {phone}", reply_markup=get_control_kb(phone))
        await state.set_state(AddAccount.browser_active)
    except Exception as e:
        await m.edit_text(f"Error: {str(e)[:100]}")

# --- FIXED FUNCTIONS ---

@dp.callback_query(F.data.startswith("check_"))
async def check_screen(cb: CallbackQuery):
    phone = cb.data.split("_")[1]
    driver = active_drivers.get(phone)
    if not driver: return await cb.answer("Нет драйвера", show_alert=True)
    
    try:
        # Скриншот теперь будет большим (1920x1080)
        png = await asyncio.to_thread(driver.get_screenshot_as_png)
        await cb.message.answer_photo(BufferedInputFile(png, "s.png"), caption=f"Status: {phone}")
        await cb.answer()
    except Exception as e:
        # ОБРЕЗАЕМ ОШИБКУ, ЧТОБЫ БОТ НЕ ПАДАЛ
        await cb.answer(f"Err: {str(e)[:50]}", show_alert=True)

@dp.callback_query(F.data.startswith("link_"))
async def click_link_btn(cb: CallbackQuery):
    phone = cb.data.split("_")[1]
    driver = active_drivers.get(phone)
    if not driver: return
    
    try:
        # Ищем по всем возможным вариантам текста
        xpaths = [
            "//*[contains(text(), 'Log in with phone number')]", 
            "//*[contains(text(), 'Link with phone number')]",
            "//*[contains(text(), 'Связать с номером телефона')]",
            "//span[@role='button']"
        ]
        found = False
        for xp in xpaths:
            try:
                els = driver.find_elements(By.XPATH, xp)
                for el in els:
                    if el.is_displayed():
                        driver.execute_script("arguments[0].click();", el)
                        found = True
                        break
                if found: break
            except: continue
            
        if found:
            await cb.answer("✅ Кнопка нажата!", show_alert=True)
        else:
            await cb.answer("❌ Кнопка не найдена (обнови скрин)", show_alert=True)
            
    except Exception as e:
        await cb.answer(f"Err: {str(e)[:50]}", show_alert=True)

@dp.callback_query(F.data.startswith("type_"))
async def type_number_nuclear(cb: CallbackQuery):
    phone = cb.data.split("_")[1]
    driver = active_drivers.get(phone)
    if not driver: return
    
    try:
        # "ЯДЕРНЫЙ" МЕТОД ВВОДА
        # 1. Находим поле любым способом
        # 2. Используем execCommand - это эмулирует клавиатуру, а не просто меняет переменную
        js_code = f"""
            var input = document.querySelector('input[aria-label="Type your phone number."]') || 
                        document.querySelector('input[type="text"]');
            
            if (input) {{
                input.focus();
                // Очистка
                input.value = '';
                // Эмуляция печати
                document.execCommand('insertText', false, '{phone}');
                // Принудительные события React
                input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                return true;
            }}
            return false;
        """
        success = driver.execute_script(js_code)
        
        if success:
            await cb.answer("✅ Номер введен (Эмуляция клавиатуры)", show_alert=True)
            # Пытаемся нажать NEXT
            await asyncio.sleep(0.5)
            driver.execute_script("""
                var btns = document.querySelectorAll('[role="button"]');
                btns.forEach(b => {
                    if(b.innerText.includes("Next") || b.innerText.includes("Далее")) b.click();
                });
            """)
        else:
            await cb.answer("❌ Поле ввода не найдено!", show_alert=True)

    except Exception as e:
        await cb.answer(f"Err: {str(e)[:50]}", show_alert=True)

@dp.callback_query(F.data.startswith("ready_"))
async def save_acc(cb: CallbackQuery, state: FSMContext):
    phone = cb.data.split("_")[1]
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO accounts (phone_number, status, last_active) VALUES (?, 'active', ?)", 
                 (phone, datetime.now()))
    conn.commit()
    conn.close()
    
    if phone in active_drivers:
        d = active_drivers.pop(phone)
        try: d.quit()
        except: pass
        
    await cb.message.answer(f"📁 {phone} сохранен в базу!")
    await state.clear()

# --- FARM LOOP ---
async def farm_loop():
    while True:
        await asyncio.sleep(30)
        try:
            conn = sqlite3.connect(DB_PATH)
            # Берем случайный активный аккаунт
            target = conn.execute("SELECT phone_number FROM accounts WHERE status='active' ORDER BY RANDOM() LIMIT 1").fetchone()
            conn.close()
            
            if target and target[0] not in active_drivers:
                async with BROWSER_SEMAPHORE:
                    await run_farm(target[0])
        except Exception as e:
            logger.error(f"Loop: {e}")

async def run_farm(phone):
    driver = None
    try:
        driver = await asyncio.to_thread(get_driver, phone)
        await asyncio.to_thread(driver.get, "https://web.whatsapp.com")
        await asyncio.sleep(20) # Activity time
        
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE accounts SET last_active=? WHERE phone_number=?", (datetime.now(), phone))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Farm {phone}: {e}")
    finally:
        if driver:
            try: await asyncio.to_thread(driver.quit)
            except: pass

async def main():
    db_init()
    asyncio.create_task(farm_loop())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except: pass
