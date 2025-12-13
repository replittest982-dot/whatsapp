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
    KeyboardButton,
    ReplyKeyboardRemove
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
from selenium.common.exceptions import TimeoutException, WebDriverException
from webdriver_manager.chrome import ChromeDriverManager
import time

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID = os.environ.get("ADMIN_ID") # Ваш ID для админки

# --- СИСТЕМА ОЧЕРЕДЕЙ (Anti-Crash) ---
# Selenium тяжелый. Ограничиваем одновременный запуск браузеров до 1.
# Остальные пользователи будут ждать в очереди.
BROWSER_SEMAPHORE = asyncio.Semaphore(1) 

# --- НАСТРОЙКА БД (SQLite) ---
DB_NAME = 'bot_database.db'

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        cur = conn.cursor()
        # Таблица пользователей
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                reg_date TEXT
            )
        ''')
        # Таблица аккаунтов
        cur.execute('''
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                phone_number TEXT,
                status TEXT DEFAULT 'active',
                added_date TEXT
            )
        ''')
        conn.commit()

def db_register_user(user: types.User):
    with sqlite3.connect(DB_NAME) as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user.id,))
        if not cur.fetchone():
            date_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur.execute(
                "INSERT INTO users (user_id, username, full_name, reg_date) VALUES (?, ?, ?, ?)",
                (user.id, user.username, user.full_name, date_now)
            )
            return True
    return False

def db_add_account(user_id, phone):
    with sqlite3.connect(DB_NAME) as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM accounts WHERE user_id = ? AND phone_number = ?", (user_id, phone))
        if not cur.fetchone():
            date_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur.execute("INSERT INTO accounts (user_id, phone_number, added_date) VALUES (?, ?, ?)", (user_id, phone, date_now))
            return True
    return False

def db_get_accounts(user_id):
    with sqlite3.connect(DB_NAME) as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, phone_number, status FROM accounts WHERE user_id = ?", (user_id,))
        return cur.fetchall()

def db_delete_account(acc_id, user_id):
    with sqlite3.connect(DB_NAME) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM accounts WHERE id = ? AND user_id = ?", (acc_id, user_id))
        conn.commit()

def db_get_stats():
    with sqlite3.connect(DB_NAME) as conn:
        cur = conn.cursor()
        users_count = cur.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        accs_count = cur.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        return users_count, accs_count

# --- НАСТРОЙКА БОТА ---
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
driver = None

# Состояния FSM
class Form(StatesGroup):
    waiting_for_phone = State()

# --- ФУНКЦИИ SELENIUM (Advanced) ---
def get_driver():
    """Создает и настраивает драйвер. Использует один глобальный инстанс."""
    global driver
    if driver is not None:
        try:
            # Проверка жив ли драйвер
            driver.title 
            return driver
        except WebDriverException:
            driver = None

    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--remote-debugging-port=9222")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36")
    
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    return driver

def logic_get_whatsapp_code(phone_number):
    """
    Сложная логика получения кода с обработкой ошибок.
    """
    wd = get_driver()
    try:
        wd.get("https://web.whatsapp.com/")
        wait = WebDriverWait(wd, 40) # 40 сек на загрузку страницы

        # 1. Поиск кнопки (с попытками)
        try:
            link_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//span[contains(text(), 'Link with phone number')] | //div[contains(text(), 'Link with phone number')]")))
            link_btn.click()
        except TimeoutException:
            # Возможно, мы уже на странице ввода
            pass

        time.sleep(2) # Небольшая пауза для анимации

        # 2. Ввод номера
        phone_input = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@aria-label='Type your phone number.'] | //input[@type='text']")))
        phone_input.clear()
        for char in phone_number:
            phone_input.send_keys(char)
            time.sleep(0.05) # Имитация человека
        
        # 3. Нажать Next
        next_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//div[text()='Next']")))
        next_btn.click()
        
        # 4. Получение кода
        code_element = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@aria-details='link-device-phone-number-code']")))
        return {"status": "success", "code": code_element.text}

    except TimeoutException:
        return {"status": "error", "msg": "Превышено время ожидания. WhatsApp долго грузится."}
    except Exception as e:
        return {"status": "error", "msg": f"Внутренняя ошибка: {str(e)}"}

# --- КЛАВИАТУРЫ (UI) ---

def kb_main_menu():
    """Главное меню (Reply) - как в Monkey Bot"""
    kb = [
        [KeyboardButton(text="➕ Добавить"), KeyboardButton(text="📞 Мои аккаунты")],
        [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="ℹ️ Информация")],
        [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="📩 Поддержка")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def kb_inline_back():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="delete_msg")]])

def kb_my_accounts(accounts):
    """Список аккаунтов с управлением"""
    kb = []
    for acc in accounts:
        # acc = (id, phone, status)
        status_icon = "🟢" if acc[2] == 'active' else "🔴"
        btn_text = f"{status_icon} {acc[1]}"
        # При нажатии можно показать меню управления конкретным аккаунтом
        kb.append([InlineKeyboardButton(text=btn_text, callback_data=f"manage_{acc[0]}")])
    
    kb.append([InlineKeyboardButton(text="➕ Добавить новый", callback_data="start_add_process")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def kb_account_manage(acc_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del_acc_{acc_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_list")]
    ])

# --- ОБРАБОТЧИКИ (HANDLERS) ---

@dp.message(Command("start"), StateFilter(None))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    # Регистрируем пользователя в БД
    is_new = db_register_user(message.from_user)
    
    users_total, _ = db_get_stats()
    welcome_text = (
        f"👋 **Привет, {message.from_user.first_name}!**\n\n"
        "🤖 Я — продвинутый бот для управления WhatsApp аккаунтами.\n"
        "Мои возможности:\n"
        "• Быстрый вход по коду\n"
        "• QR-код (скриншот)\n"
        "• Управление базой номеров\n\n"
        f"👥 Нас уже: **{users_total} пользователей**\n"
        "👇 Выберите действие в меню:"
    )
    await message.answer(welcome_text, reply_markup=kb_main_menu(), parse_mode="Markdown")

# --- СЕКЦИЯ: ПРОФИЛЬ ---
@dp.message(F.text == "👤 Профиль")
async def handle_profile(message: types.Message):
    accounts = db_get_accounts(message.from_user.id)
    reg_date = "Неизвестно" # В реальном проекте берем из БД
    
    text = (
        f"👤 **Профиль пользователя**\n"
        f"➖➖➖➖➖➖➖➖➖➖\n"
        f"🆔 ID: `{message.from_user.id}`\n"
        f"👤 Имя: {message.from_user.full_name}\n"
        f"📱 Аккаунтов: **{len(accounts)}**\n"
        f"➖➖➖➖➖➖➖➖➖➖"
    )
    await message.answer(text, reply_markup=kb_inline_back(), parse_mode="Markdown")

# --- СЕКЦИЯ: МОИ АККАУНТЫ ---
@dp.message(F.text == "📞 Мои аккаунты")
async def handle_my_accounts(message: types.Message):
    accounts = db_get_accounts(message.from_user.id)
    if not accounts:
        text = "📭 **У вас нет добавленных аккаунтов.**\nНажмите «Добавить», чтобы подключить."
        await message.answer(text, reply_markup=kb_inline_back(), parse_mode="Markdown")
    else:
        text = f"📂 **Ваши аккаунты ({len(accounts)}):**\nНажмите на аккаунт для управления."
        await message.answer(text, reply_markup=kb_my_accounts(accounts), parse_mode="Markdown")

# Управление аккаунтом (Inline)
@dp.callback_query(F.data.startswith("manage_"))
async def cb_manage_acc(callback: types.CallbackQuery):
    acc_id = callback.data.split("_")[1]
    await callback.message.edit_text(f"⚙️ Управление аккаунтом #{acc_id}", reply_markup=kb_account_manage(acc_id))

@dp.callback_query(F.data == "back_to_list")
async def cb_back_list(callback: types.CallbackQuery):
    await callback.message.delete()
    await handle_my_accounts(callback.message)

@dp.callback_query(F.data.startswith("del_acc_"))
async def cb_del_acc(callback: types.CallbackQuery):
    acc_id = callback.data.split("_")[2]
    db_delete_account(acc_id, callback.from_user.id)
    await callback.answer("✅ Аккаунт удален из базы", show_alert=True)
    await cb_back_list(callback)

# --- СЕКЦИЯ: ДОБАВЛЕНИЕ АККАУНТА (СЛОЖНАЯ ЛОГИКА) ---

@dp.message(F.text == "➕ Добавить")
@dp.callback_query(F.data == "start_add_process")
async def start_add(event: types.Message | types.CallbackQuery, state: FSMContext):
    msg_func = event.answer if isinstance(event, types.Message) else event.message.answer
    
    # Проверка очереди (Semaphore)
    if BROWSER_SEMAPHORE.locked():
        await msg_func("⚠️ **Сервер сейчас нагружен.**\nПожалуйста, подождите 10-20 секунд и попробуйте снова.", parse_mode="Markdown")
        return

    text = (
        "🚀 **Добавление нового аккаунта**\n\n"
        "Введите номер телефона, который привязан к WhatsApp.\n"
        "Формат: `79991234567` (только цифры)\n\n"
        "⚠️ *Держите телефон под рукой, нужно будет ввести код.*"
    )
    if isinstance(event, types.CallbackQuery):
        await event.message.answer(text, parse_mode="Markdown")
        await event.answer()
    else:
        await event.answer(text, parse_mode="Markdown")
        
    await state.set_state(Form.waiting_for_phone)

@dp.message(Form.waiting_for_phone)
async def process_phone(message: types.Message, state: FSMContext):
    phone = message.text.strip().replace('+', '').replace(' ', '').replace('-', '')
    
    if not phone.isdigit() or len(phone) < 7:
        await message.answer("❌ **Ошибка формата!**\nВведите только цифры. Пример: `79051234567`")
        return

    status_msg = await message.answer(f"⏳ **Встаю в очередь...**\nНомер: `{phone}`", parse_mode="Markdown")

    # Использование Семафора (Очередь)
    async with BROWSER_SEMAPHORE:
        await status_msg.edit_text(f"🔄 **Запускаю браузер...**\nПожалуйста, не закрывайте диалог.")
        
        # Запускаем тяжелую задачу в отдельном потоке
        result = await asyncio.to_thread(logic_get_whatsapp_code, phone)

    if result["status"] == "success":
        # Сохраняем в БД
        db_add_account(message.from_user.id, phone)
        
        # Кнопка для QR
        kb_qr = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📷 Показать QR код", callback_data="req_qr")],
            [InlineKeyboardButton(text="✅ Я ввел код", callback_data="delete_msg")]
        ])

        await status_msg.delete()
        await message.answer(
            f"✅ **Успешно! Ваш код:**\n\n"
            f"`{result['code']}`\n\n"
            "1️⃣ Зайдите в WhatsApp -> Настройки -> Связанные устройства.\n"
            "2️⃣ Нажмите «Привязка по номеру».\n"
            "3️⃣ Введите этот код.",
            reply_markup=kb_qr,
            parse_mode="Markdown"
        )
    else:
        await status_msg.edit_text(f"❌ **Ошибка:** {result['msg']}\nПопробуйте позже.")
    
    await state.clear()

# --- ФУНКЦИЯ QR КОДА ---
@dp.callback_query(F.data == "req_qr")
async def cb_show_qr(callback: types.CallbackQuery):
    global driver
    if not driver:
        await callback.answer("Сессия истекла. Попробуйте заново.", show_alert=True)
        return

    await callback.answer("📸 Делаю скриншот...")
    try:
        # Делаем скриншот в отдельном потоке, чтобы не блочить бота
        screenshot = await asyncio.to_thread(driver.get_screenshot_as_png)
        photo = BufferedInputFile(screenshot, filename="qrcode.png")
        await callback.message.answer_photo(photo, caption="📷 **Текущий экран:**\nСканируйте QR, если код не сработал.", parse_mode="Markdown")
    except Exception as e:
        await callback.message.answer(f"Не удалось сделать скриншот: {e}")

@dp.callback_query(F.data == "delete_msg")
async def cb_delete(callback: types.CallbackQuery):
    await callback.message.delete()

# --- АДМИН ПАНЕЛЬ ---
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    # Простейшая защита по ID (если переменная задана)
    if ADMIN_ID and str(message.from_user.id) != str(ADMIN_ID):
        return

    u_count, a_count = db_get_stats()
    text = (
        "🕵️‍♂️ **Админ-панель**\n\n"
        f"👥 Пользователей: `{u_count}`\n"
        f"📱 Аккаунтов: `{a_count}`\n"
        f"⚙️ Сервер: Работает"
    )
    await message.answer(text, parse_mode="Markdown")

# --- ОБРАБОТЧИКИ ЗАГЛУШЕК ---
@dp.message(F.text.in_({"ℹ️ Информация", "⚙️ Настройки", "📩 Поддержка"}))
async def handle_stub(message: types.Message):
    await message.answer("🛠 Этот раздел находится в разработке.", reply_markup=kb_inline_back())

# --- ЗАПУСК ---
async def main():
    init_db() # Инициализация БД
    print("✅ Бот запущен и готов к работе (v2.0 PRO)")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        if driver: driver.quit()
    except Exception as e:
        logging.error(f"Critical: {e}")
