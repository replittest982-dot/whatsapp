import asyncio
import os
import logging
import sqlite3
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

# --- SELENIUM IMPORTS ---
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException
from webdriver_manager.chrome import ChromeDriverManager
import time

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
# Если запускаете локально без Docker, вставьте токен сюда строкой
# BOT_TOKEN = "ВАШ_ТОКЕН"

if not BOT_TOKEN:
    exit("Error: BOT_TOKEN not found!")

# --- НАСТРОЙКА БД (SQLite) ---
def init_db():
    conn = sqlite3.connect('bot_database.db')
    cur = conn.cursor()
    # Создаем таблицу пользователей/аккаунтов
    cur.execute('''
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            phone_number TEXT,
            added_date TEXT
        )
    ''')
    conn.commit()
    conn.close()

def add_account_to_db(user_id, phone):
    conn = sqlite3.connect('bot_database.db')
    cur = conn.cursor()
    # Проверяем, нет ли уже такого номера
    cur.execute("SELECT * FROM accounts WHERE user_id = ? AND phone_number = ?", (user_id, phone))
    if not cur.fetchone():
        date_now = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        cur.execute("INSERT INTO accounts (user_id, phone_number, added_date) VALUES (?, ?, ?)", (user_id, phone, date_now))
        conn.commit()
    conn.close()

def get_user_accounts(user_id):
    conn = sqlite3.connect('bot_database.db')
    cur = conn.cursor()
    cur.execute("SELECT phone_number FROM accounts WHERE user_id = ?", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return [row[0] for row in rows]

def get_account_count(user_id):
    conn = sqlite3.connect('bot_database.db')
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM accounts WHERE user_id = ?", (user_id,))
    count = cur.fetchone()[0]
    conn.close()
    return count

# --- НАСТРОЙКА БОТА ---
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
driver = None

# Состояния для диалога (FSM)
class Form(StatesGroup):
    waiting_for_phone = State()

# --- ФУНКЦИИ SELENIUM ---
def start_chrome():
    global driver
    if driver is not None:
        return driver

    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    # Маскировка под обычный браузер
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36")
    
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    return driver

def quit_browser():
    global driver
    if driver:
        driver.quit()
        driver = None

def get_whatsapp_code(phone_number):
    """Логика получения 8-значного кода"""
    global driver
    if not driver:
        start_chrome()
    
    try:
        driver.get("https://web.whatsapp.com/")
        wait = WebDriverWait(driver, 45) # Увеличили ожидание загрузки

        # 1. Ждем кнопку "Link with phone number"
        try:
            link_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//span[contains(text(), 'Link with phone number')] | //div[contains(text(), 'Link with phone number')]")))
            link_btn.click()
        except Exception:
            # Если кнопки нет, возможно мы уже на странице ввода или QR
            pass

        time.sleep(2)

        # 2. Ввод номера
        phone_input = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@aria-label='Type your phone number.'] | //input[@type='text']")))
        # Очистка и ввод
        phone_input.clear()
        for char in phone_number:
            phone_input.send_keys(char)
            time.sleep(0.1) # Имитация ввода
        
        # 3. Кнопка NEXT
        next_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//div[text()='Next']")))
        next_btn.click()
        
        # 4. Получение кода
        code_container = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@aria-details='link-device-phone-number-code']")))
        code_text = code_container.text
        
        return code_text

    except Exception as e:
        return f"ERROR: {e}"

# --- КЛАВИАТУРЫ ---

def get_main_keyboard():
    # Главное меню (как на скрине 2)
    kb = [
        [InlineKeyboardButton(text="➕ Добавить", callback_data="add_account"), 
         InlineKeyboardButton(text="📞 Мои аккаунты", callback_data="my_accounts")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile"), 
         InlineKeyboardButton(text="ℹ️ Информация", callback_data="info")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings"), 
         InlineKeyboardButton(text="📩 Поддержка", callback_data="support")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_accounts_keyboard(accounts):
    # Список аккаунтов кнопками
    kb = []
    for phone in accounts:
        kb.append([InlineKeyboardButton(text=f"📱 {phone}", callback_data=f"acc_{phone}")])
    
    # Кнопки управления внизу списка
    kb.append([InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="add_account")])
    kb.append([InlineKeyboardButton(text="🔙 В меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def get_back_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 В меню", callback_data="main_menu")]])

# --- ОБРАБОТЧИКИ (HANDLERS) ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    user_name = message.from_user.first_name
    count = get_account_count(message.from_user.id)
    
    text = (
        f"🌟 **Привет, {user_name}!**\n"
        f"➡️ **WhatsApp Warmer** — бот для прогрева аккаунтов WhatsApp.\n\n"
        "Здесь можно управлять своими аккаунтами, следить за состоянием серверов и получить помощь.\n\n"
        f"✨ **Активных аккаунтов: {count}**\n"
        "Выберите действие из меню ниже:"
    )
    await message.answer(text, reply_markup=get_main_keyboard(), parse_mode="Markdown")

# 1. ОБРАБОТКА "ПРОФИЛЬ"
@dp.callback_query(F.data == "profile")
async def cb_profile(callback: types.CallbackQuery):
    user = callback.from_user
    count = get_account_count(user.id)
    # Пример даты регистрации (можно сделать реальную, если писать в БД при /start)
    reg_date = "12.12.2025" 
    
    text = (
        "Профиль 👑\n"
        f"👍 Username: @{user.username}\n"
        f"🔑 ID: `{user.id}`\n"
        f"💲 Оплаченных аккаунтов: {count}\n"
        f"📅 Дата регистрации: {reg_date}\n"
        "✨ Рефералов: 0 шт"
    )
    await callback.message.edit_text(text, reply_markup=get_back_keyboard(), parse_mode="Markdown")

# 2. ОБРАБОТКА "МОИ АККАУНТЫ"
@dp.callback_query(F.data == "my_accounts")
async def cb_my_accounts(callback: types.CallbackQuery):
    accounts = get_user_accounts(callback.from_user.id)
    if not accounts:
        await callback.message.edit_text("📭 У вас пока нет добавленных аккаунтов.", reply_markup=get_accounts_keyboard([]))
    else:
        await callback.message.edit_text(f"📱 Ваши аккаунты ({len(accounts)}):", reply_markup=get_accounts_keyboard(accounts))

# 3. ЛОГИКА ДОБАВЛЕНИЯ АККАУНТА (Вход)
@dp.callback_query(F.data == "add_account")
async def cb_add_account(callback: types.CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "📝 **Введите номер телефона** для привязки WhatsApp.\n"
        "Формат: `79991234567` (без +)\n\n"
        "👇 Отправьте номер сообщением.", 
        reply_markup=get_back_keyboard(),
        parse_mode="Markdown"
    )
    await state.set_state(Form.waiting_for_phone)

@dp.message(Form.waiting_for_phone)
async def process_phone(message: types.Message, state: FSMContext):
    phone = message.text.strip().replace('+', '').replace(' ', '')
    
    if not phone.isdigit() or len(phone) < 10:
        await message.answer("❌ Некорректный формат. Попробуйте снова (например, 79001234567).")
        return

    # Сохраняем в БД сразу (или после успеха - по желанию, сейчас сохраним сразу для UI)
    add_account_to_db(message.from_user.id, phone)
    
    msg = await message.answer(f"⏳ Запускаю браузер и ввожу номер `{phone}`...\nЭто займет около 10-20 секунд.", parse_mode="Markdown")
    
    # Запуск Selenium в потоке
    code_result = await asyncio.to_thread(get_whatsapp_code, phone)
    
    if "ERROR" in code_result:
        await msg.edit_text(f"❌ Ошибка: {code_result}\nПопробуйте позже или используйте QR.", reply_markup=get_back_keyboard())
        await state.clear()
    else:
        # Успех - отправляем КОД
        # Кнопка для QR кода
        kb_code = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📷 Вход через QR (Скриншот)", callback_data="show_qr")],
            [InlineKeyboardButton(text="🔙 В меню", callback_data="main_menu")]
        ])
        
        await msg.delete() # Удаляем сообщение о загрузке
        await message.answer(
            f"✅ **Ваш код для входа:**\n\n"
            f"`{code_result}`\n\n"
            "1. Откройте WhatsApp на телефоне\n"
            "2. Настройки -> Связанные устройства -> Привязка устройства\n"
            "3. Нажмите 'Привязка по номеру телефона' и введите этот код.",
            reply_markup=kb_code,
            parse_mode="Markdown"
        )
    await state.clear()

# 4. ПОКАЗАТЬ QR (СКРИНШОТ)
@dp.callback_query(F.data == "show_qr")
async def cb_show_qr(callback: types.CallbackQuery):
    global driver
    if not driver:
        await callback.answer("Браузер закрыт. Начните заново.", show_alert=True)
        return
    
    await callback.message.answer("📸 Делаю скриншот экрана...")
    try:
        screenshot = await asyncio.to_thread(driver.get_screenshot_as_png)
        photo = BufferedInputFile(screenshot, filename="screen.png")
        await callback.message.answer_photo(photo, caption="Вот текущий экран браузера.\nЕсли там QR код - сканируйте его.")
    except Exception as e:
        await callback.message.answer(f"Ошибка скриншота: {e}")

# 5. КНОПКА "В МЕНЮ"
@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await cmd_start(callback.message, state) # Возвращаем на старт

# 6. ЗАГЛУШКИ ДЛЯ ОСТАЛЬНЫХ КНОПОК
@dp.callback_query(F.data.in_({"info", "settings", "support"}))
async def cb_stub(callback: types.CallbackQuery):
    await callback.answer("🚧 Раздел в разработке", show_alert=True)

# --- ЗАПУСК ---
async def main():
    init_db() # Создаем БД при старте
    print("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        quit_browser()
    except Exception as e:
        print(f"Critial error: {e}")
        quit_browser()
