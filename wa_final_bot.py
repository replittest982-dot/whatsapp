import asyncio
import os
import logging
import sqlite3
import re
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.types import (
    BufferedInputFile, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# --- SELENIUM & DRIVER ---
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
from webdriver_manager.chrome import ChromeDriverManager
import time

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_ID")

# Очередь (чтобы сервер не упал от нагрузок)
BROWSER_SEMAPHORE = asyncio.Semaphore(1) 
DB_NAME = 'bot_database.db'

# --- БАЗА ДАННЫХ ---
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        cur = conn.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, reg_date TEXT)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS accounts (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, phone_number TEXT, added_date TEXT)''')
        conn.commit()

def db_add_account(user_id, phone):
    with sqlite3.connect(DB_NAME) as conn:
        cur = conn.cursor()
        if not cur.execute("SELECT * FROM accounts WHERE user_id = ? AND phone_number = ?", (user_id, phone)).fetchone():
            cur.execute("INSERT INTO accounts (user_id, phone_number, added_date) VALUES (?, ?, ?)", 
                        (user_id, phone, datetime.now().strftime("%Y-%m-%d %H:%M")))
            return True
    return False

def db_get_accounts(user_id):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT id, phone_number FROM accounts WHERE user_id = ?", (user_id,)).fetchall()

def db_delete_account(acc_id):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("DELETE FROM accounts WHERE id = ?", (acc_id,))

# --- БОТ И FSM ---
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
driver = None

class Form(StatesGroup):
    waiting_for_phone = State()

# --- УТИЛИТЫ ---
def clean_phone_number(raw_phone):
    """Умная очистка номера: убирает скобки, плюсы, пробелы. меняет 8 на 7."""
    # Оставляем только цифры
    digits = re.sub(r'\D', '', raw_phone)
    if not digits: return None
    
    # Если начинается с 8 и длина 11, меняем на 7
    if len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    # Если длина 10 (без кода страны), добавляем 7
    elif len(digits) == 10:
        digits = '7' + digits
        
    return digits

# --- SELENIUM LOGIC ---
def get_driver():
    global driver
    if driver:
        try:
            driver.title
            return driver
        except:
            driver = None
            
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36")
    
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    return driver

def selenium_login_flow(phone_number, status_callback=None):
    """Возвращает: {'status': 'ok/error', 'data': 'code/error_msg'}"""
    wd = get_driver()
    try:
        wd.get("https://web.whatsapp.com/")
        wait = WebDriverWait(wd, 30)
        
        # Клик по "Link with phone number"
        try:
            btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//span[contains(text(), 'Link with phone number')] | //div[contains(text(), 'Link with phone number')]")))
            btn.click()
        except: pass 
        
        time.sleep(1)
        
        # Ввод номера
        inp = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@aria-label='Type your phone number.'] | //input[@type='text']")))
        inp.clear()
        for ch in phone_number:
            inp.send_keys(ch)
            time.sleep(0.05)
            
        wait.until(EC.element_to_be_clickable((By.XPATH, "//div[text()='Next']"))).click()
        
        # Получение кода
        code_el = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@aria-details='link-device-phone-number-code']")))
        return {"status": "ok", "data": code_el.text}
        
    except Exception as e:
        return {"status": "error", "data": str(e)}

# --- UI (КЛАВИАТУРЫ) ---
def kb_main():
    # Главное меню (ВНИЗУ) - Как в Monkey Bot
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="➕ Добавить аккаунт"), KeyboardButton(text="📂 Мои номера")],
        [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="⚙️ Настройки")]
    ], resize_keyboard=True)

def kb_cancel():
    # Инлайн кнопка отмены
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_action")]])

def kb_accounts_list(accounts):
    kb = []
    for acc in accounts:
        # acc: (id, phone)
        kb.append([InlineKeyboardButton(text=f"📱 +{acc[1]}", callback_data=f"manage_{acc[0]}")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def kb_manage(acc_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del_{acc_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_list")]
    ])

# --- HANDLERS ---

@dp.message(Command("start"))
async def start(message: types.Message, state: FSMContext):
    await state.clear()
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR IGNORE INTO users (user_id, username, reg_date) VALUES (?, ?, ?)", 
                     (message.from_user.id, message.from_user.username, datetime.now().strftime("%Y-%m-%d")))
    
    await message.answer(
        f"👋 **Привет, {message.from_user.first_name}!**\n\n"
        "Я бот для управления WhatsApp аккаунтами.\n"
        "Жми кнопку внизу, чтобы добавить номер.",
        reply_markup=kb_main(),
        parse_mode="Markdown"
    )

# --- ДОБАВЛЕНИЕ АККАУНТА (СМАРТ ЛОГИКА) ---
@dp.message(F.text == "➕ Добавить аккаунт")
async def add_acc_start(message: types.Message, state: FSMContext):
    if BROWSER_SEMAPHORE.locked():
        await message.answer("⚠️ Очередь занята, подождите 10 сек...", reply_markup=kb_main())
        return

    await message.answer(
        "📞 **Отправьте номер телефона**\n\n"
        "Можно в любом формате:\n"
        "• `+7 999 123 45 67`\n"
        "• `89991234567`\n"
        "• `9991234567`",
        reply_markup=kb_cancel(),
        parse_mode="Markdown"
    )
    await state.set_state(Form.waiting_for_phone)

@dp.message(Form.waiting_for_phone)
async def process_phone(message: types.Message, state: FSMContext):
    raw_phone = message.text
    phone = clean_phone_number(raw_phone)
    
    if not phone or len(phone) < 10:
        await message.answer("❌ **Неверный формат номера!**\nПопробуйте еще раз или нажмите отмену.", reply_markup=kb_cancel(), parse_mode="Markdown")
        return

    # Живое обновление статуса
    status_msg = await message.answer(f"⏳ **Обработка номера: +{phone}**\n🔄 Запускаю безопасный браузер...", parse_mode="Markdown")
    
    async with BROWSER_SEMAPHORE:
        await status_msg.edit_text(f"⏳ **Обработка номера: +{phone}**\n🔄 Ввожу данные в WhatsApp...", parse_mode="Markdown")
        result = await asyncio.to_thread(selenium_login_flow, phone)

    if result['status'] == 'ok':
        db_add_account(message.from_user.id, phone)
        code = result['data']
        
        kb_result = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📷 Показать QR (если код не сработал)", callback_data="get_qr")],
            [InlineKeyboardButton(text="✅ Готово", callback_data="cancel_action")]
        ])
        
        await status_msg.delete()
        await message.answer(
            f"✅ **КОД ДЛЯ ВХОДА:**\n\n`{code}`\n\n"
            f"Вводите этот код в WhatsApp на телефоне.\nНомер: `+{phone}`",
            reply_markup=kb_result,
            parse_mode="Markdown"
        )
    else:
        await status_msg.edit_text(f"❌ **Ошибка WhatsApp:**\n{result['data']}", reply_markup=kb_main())
    
    await state.clear()

# --- ПРОФИЛЬ И СПИСКИ ---
@dp.message(F.text == "📂 Мои номера")
async def show_numbers(message: types.Message):
    accs = db_get_accounts(message.from_user.id)
    if not accs:
        await message.answer("📭 Список пуст.", reply_markup=kb_main())
    else:
        await message.answer(f"📱 **Ваши номера ({len(accs)}):**", reply_markup=kb_accounts_list(accs), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("manage_"))
async def manage_acc(call: types.CallbackQuery):
    acc_id = call.data.split("_")[1]
    await call.message.edit_text("⚙️ **Управление аккаунтом:**", reply_markup=kb_manage(acc_id), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("del_"))
async def delete_acc(call: types.CallbackQuery):
    acc_id = call.data.split("_")[1]
    db_delete_account(acc_id)
    await call.answer("🗑 Номер удален!")
    await call.message.edit_text("✅ Удалено.", reply_markup=None)

@dp.callback_query(F.data == "back_list")
async def back_to_list(call: types.CallbackQuery):
    await call.message.delete()
    await show_numbers(call.message)

@dp.callback_query(F.data == "cancel_action")
async def cancel_action(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.delete()
    await call.answer("Отменено")

# --- QR КОД ---
@dp.callback_query(F.data == "get_qr")
async def send_qr(call: types.CallbackQuery):
    global driver
    if not driver:
        await call.answer("Время сессии истекло, начните заново.", show_alert=True)
        return
    
    await call.answer("📸 Делаю скрин...")
    try:
        screen = await asyncio.to_thread(driver.get_screenshot_as_png)
        await call.message.answer_photo(BufferedInputFile(screen, "qr.png"), caption="Сканируйте QR, если нужно.")
    except:
        await call.answer("Ошибка скриншота", show_alert=True)

# --- ПРОФИЛЬ (Заглушка для красоты) ---
@dp.message(F.text == "👤 Профиль")
async def profile(message: types.Message):
    accs_count = len(db_get_accounts(message.from_user.id))
    text = (
        f"👤 **Ваш Профиль**\n"
        f"🆔 ID: `{message.from_user.id}`\n"
        f"📱 Подключено номеров: **{accs_count}**"
    )
    await message.answer(text, reply_markup=kb_main(), parse_mode="Markdown")

# --- ЗАПУСК ---
async def main():
    init_db()
    print("✅ БОТ ЗАПУЩЕН (MODE: ULTRA INLINE)")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        if driver: driver.quit()
