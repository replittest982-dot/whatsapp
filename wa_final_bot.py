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
from webdriver_manager.chrome import ChromeDriverManager
import time

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
# Разрешаем 1 поток браузера (безопасно)
BROWSER_SEMAPHORE = asyncio.Semaphore(1) 
DB_NAME = 'bot_database.db'

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

# --- ЛОГИКА БРАУЗЕРА ---
def get_driver():
    """Конфигурация Chrome для PRO-тарифа (2GB RAM)"""
    options = Options()
    
    # == ОБЯЗАТЕЛЬНЫЕ ФЛАГИ ДЛЯ DOCKER ==
    options.add_argument("--headless=new") 
    options.add_argument("--no-sandbox") 
    options.add_argument("--disable-dev-shm-usage") 
    options.add_argument("--disable-gpu")
    
    # Стандартный размер окна
    options.add_argument("--window-size=1920,1080")
    
    # Отключаем уведомления и инфобары
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-infobars")
    
    # User Agent (как обычный ПК)
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    
    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        return driver
    except Exception as e:
        logging.error(f"❌ Driver Init Error: {e}")
        raise e

def run_auth_process(phone_number):
    driver = None
    try:
        driver = get_driver()
        driver.set_page_load_timeout(60) # 60 сек на загрузку
        
        # Переход на сайт
        driver.get("https://web.whatsapp.com/")
        wait = WebDriverWait(driver, 45)

        # 1. Жмем "Link with phone number" (если есть)
        try:
            btn_xpath = "//span[contains(text(), 'Link with phone number')] | //div[contains(text(), 'Link with phone number')]"
            btn = wait.until(EC.presence_of_element_located((By.XPATH, btn_xpath)))
            driver.execute_script("arguments[0].click();", btn)
        except Exception: 
            pass 

        # 2. Вводим номер
        time.sleep(2)
        inp = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@aria-label='Type your phone number.'] | //input[@type='text']")))
        inp.clear()
        for ch in phone_number:
            inp.send_keys(ch)
            time.sleep(0.05)
        
        # 3. Жмем Next
        next_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//div[text()='Next']")))
        driver.execute_script("arguments[0].click();", next_btn)

        # 4. Ждем Код (или скриншот ошибки)
        try:
            code_el = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@aria-details='link-device-phone-number-code']")))
            time.sleep(1) 
            return {"status": "ok", "type": "code", "data": code_el.text}
        except Exception:
            # Делаем скриншот, если кода нет
            screenshot = driver.get_screenshot_as_png()
            return {"status": "ok", "type": "screenshot", "data": screenshot}

    except Exception as e:
        return {"status": "error", "data": str(e)}
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass

# --- КЛАВИАТУРЫ ---
def kb_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="add_acc")],
        [InlineKeyboardButton(text="📂 Мои номера", callback_data="list_acc"), 
         InlineKeyboardButton(text="👤 Профиль", callback_data="profile")]
    ])

def kb_back():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")]])

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
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("👋 **WhatsApp Manager**", reply_markup=kb_menu(), parse_mode="Markdown")

@dp.callback_query(F.data == "main_menu")
async def cb_menu(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.edit_text("👋 **Главное меню**", reply_markup=kb_menu(), parse_mode="Markdown")
    except:
        await call.message.answer("👋 **Главное меню**", reply_markup=kb_menu(), parse_mode="Markdown")

@dp.callback_query(F.data == "add_acc")
async def cb_add(call: types.CallbackQuery, state: FSMContext):
    if BROWSER_SEMAPHORE.locked():
        await call.answer("⚠️ Очередь занята. Ждите...", show_alert=True)
        return
    await call.message.edit_text("📞 **Введите номер** (7999...)", reply_markup=kb_back(), parse_mode="Markdown")
    await state.set_state(Form.wait_phone)

@dp.message(Form.wait_phone)
async def process_phone(message: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', message.text)
    if len(phone) == 11 and phone.startswith('8'): phone = '7' + phone[1:]
    elif len(phone) == 10: phone = '7' + phone

    if len(phone) < 10:
        await message.answer("❌ Неверный номер", reply_markup=kb_back())
        return

    msg = await message.answer(f"🚀 **Вхожу...**\nНомер: `+{phone}`", parse_mode="Markdown")
    
    async with BROWSER_SEMAPHORE:
        await bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id, 
                                  text=f"📲 **Запрос в WhatsApp...**\nЖдите ~30 сек...", parse_mode="Markdown")
        res = await asyncio.to_thread(run_auth_process, phone)

    if res['status'] == 'ok':
        if res['type'] == 'code':
            db_add(message.from_user.id, phone)
            await bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                      text=f"✅ **КОД:** `{res['data']}`", reply_markup=kb_back(), parse_mode="Markdown")
        elif res['type'] == 'screenshot':
            await msg.delete()
            await message.answer_photo(BufferedInputFile(res['data'], "err.png"), caption="⚠️ Кода нет. См. скрин.", reply_markup=kb_back())
    else:
        await bot.edit_message_text(chat_id=message.chat.id, message_id=msg.message_id,
                                  text=f"❌ Ошибка: {res['data']}", reply_markup=kb_back())
    await state.clear()

@dp.callback_query(F.data == "list_acc")
async def cb_list(call: types.CallbackQuery):
    accs = db_get(call.from_user.id)
    if not accs: await call.message.edit_text("📭 Пусто", reply_markup=kb_back())
    else: await call.message.edit_text(f"📂 **Номера ({len(accs)}):**", reply_markup=kb_acc_list(accs), parse_mode="Markdown")

@dp.callback_query(F.data == "profile")
async def cb_profile(call: types.CallbackQuery):
    accs = db_get(call.from_user.id)
    await call.message.edit_text(f"👤 **Профиль**\n📱 Номеров: {len(accs)}", reply_markup=kb_back(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("man_"))
async def cb_manage(call: types.CallbackQuery):
    acc_id = call.data.split("_")[1]
    await call.message.edit_text(f"⚙️ Аккаунт #{acc_id}", reply_markup=kb_manage(acc_id))

@dp.callback_query(F.data.startswith("del_"))
async def cb_del(call: types.CallbackQuery):
    db_delete(call.data.split("_")[1])
    await call.answer("Удалено")
    await cb_list(call)

# Обработчик мусора
@dp.message()
async def trash(msg: types.Message):
    await msg.answer("👇 Жми кнопки", reply_markup=kb_menu())

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
