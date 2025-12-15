import asyncio
import os
import logging
import sqlite3
import re
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton
)
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

# ОЧЕРЕДЬ: Разрешаем только 1 браузер одновременно, чтобы избежать вылетов памяти
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

# --- ЛОГИКА БРАУЗЕРА (ANTI-CRASH) ---
def get_clean_driver():
    """Создает ультра-легкий Chrome для слабых серверов"""
    options = Options()
    # Самые важные флаги для Docker
    options.add_argument("--headless=new") 
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,720")
    
    # Отключаем всё лишнее для экономии памяти (RAM)
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-notifications")
    options.add_argument("--blink-settings=imagesEnabled=false") # Без картинок
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    
    # Инициализация
    try:
        service = Service(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        return driver
    except Exception as e:
        logging.error(f"Failed to start driver: {e}")
        raise e

def run_auth_process(phone_number):
    """
    Основной процесс входа. Возвращает статус и данные (код или скриншот).
    """
    driver = None
    try:
        driver = get_clean_driver()
        # Таймауты
        driver.set_page_load_timeout(60)
        driver.implicitly_wait(10)
        
        driver.get("https://web.whatsapp.com/")
        wait = WebDriverWait(driver, 50) # Ждем загрузки до 50 сек

        # 1. Нажимаем "Link with phone number" (используем JavaScript для надежности)
        try:
            btn_xpath = "//span[contains(text(), 'Link with phone number')] | //div[contains(text(), 'Link with phone number')]"
            btn = wait.until(EC.presence_of_element_located((By.XPATH, btn_xpath)))
            driver.execute_script("arguments[0].click();", btn)
        except Exception:
            pass # Если кнопки нет, возможно мы уже на форме ввода

        # 2. Вводим номер
        time.sleep(2)
        inp_xpath = "//input[@aria-label='Type your phone number.'] | //input[@type='text']"
        inp = wait.until(EC.presence_of_element_located((By.XPATH, inp_xpath)))
        inp.clear()
        for ch in phone_number:
            inp.send_keys(ch)
            time.sleep(0.05)
        
        # 3. Нажимаем Next (через JS)
        try:
            next_btn = wait.until(EC.element_to_be_clickable((By.XPATH, "//div[text()='Next']")))
            driver.execute_script("arguments[0].click();", next_btn)
        except Exception as e:
            return {"status": "error", "data": f"Кнопка Next не нажалась: {e}"}

        # 4. Ждем Код
        try:
            # Ищем блок с кодом
            code_el = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@aria-details='link-device-phone-number-code']")))
            time.sleep(1) # Даем тексту прогрузиться
            return {"status": "ok", "type": "code", "data": code_el.text}
        
        except Exception:
            # Если кода нет, делаем скриншот (вдруг там QR или капча)
            screenshot = driver.get_screenshot_as_png()
            return {"status": "ok", "type": "screenshot", "data": screenshot}

    except Exception as e:
        return {"status": "error", "data": str(e)}
    finally:
        # ОБЯЗАТЕЛЬНО убиваем процесс
        if driver:
            try:
                driver.quit()
            except:
                pass

# --- КЛАВИАТУРЫ (INLINE) ---
def kb_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="add_acc")],
        [InlineKeyboardButton(text="📂 Мои номера", callback_data="list_acc"), 
         InlineKeyboardButton(text="👤 Профиль", callback_data="profile")]
    ])

def kb_back():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад в меню", callback_data="main_menu")]])

def kb_acc_list(accounts):
    kb = []
    for acc in accounts:
        # acc: (id, phone)
        kb.append([InlineKeyboardButton(text=f"📱 +{acc[1]}", callback_data=f"man_{acc[0]}")])
    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def kb_manage(acc_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"del_{acc_id}")],
        [InlineKeyboardButton(text="🔙 К списку", callback_data="list_acc")]
    ])

# --- ОБРАБОТЧИКИ (HANDLERS) ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 **WhatsApp Manager**\n\nУправление через кнопки ниже 👇", 
        reply_markup=kb_menu(), 
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "main_menu")
async def cb_menu(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.edit_text("👋 **Главное меню**", reply_markup=kb_menu(), parse_mode="Markdown")
    except:
        await call.message.answer("👋 **Главное меню**", reply_markup=kb_menu(), parse_mode="Markdown")

# --- ДОБАВЛЕНИЕ ---
@dp.callback_query(F.data == "add_acc")
async def cb_add(call: types.CallbackQuery, state: FSMContext):
    # Проверка очереди
    if BROWSER_SEMAPHORE.locked():
        await call.answer("⚠️ Сервер занят (другой пользователь входит). Подождите 15 сек!", show_alert=True)
        return

    await call.message.edit_text(
        "📞 **Введите номер телефона**\n"
        "Формат любой: `+7 999...` или `8999...`\n\n"
        "👇 Отправьте цифры сообщением.", 
        reply_markup=kb_back(),
        parse_mode="Markdown"
    )
    await state.set_state(Form.wait_phone)

@dp.message(Form.wait_phone)
async def process_phone(message: types.Message, state: FSMContext):
    # Очистка номера
    raw = message.text
    digits = re.sub(r'\D', '', raw)
    
    # Логика 8 -> 7
    if len(digits) == 11 and digits.startswith('8'):
        phone = '7' + digits[1:]
    elif len(digits) == 10:
        phone = '7' + digits
    else:
        phone = digits

    if len(phone) < 10:
        await message.answer("❌ Неверный формат. Попробуйте еще раз.", reply_markup=kb_back())
        return

    # Сообщение о запуске
    status_msg = await message.answer(f"🚀 **Запуск...**\nНомер: `+{phone}`\n⏳ Ждите...", parse_mode="Markdown")
    
    # Вход в Semaphore (Блокировка других юзеров)
    async with BROWSER_SEMAPHORE:
        await bot.edit_message_text(
            chat_id=message.chat.id, 
            message_id=status_msg.message_id, 
            text=f"📲 **Ввожу данные в WhatsApp...**\nНомер: `+{phone}`\n⏳ Это займет 20-40 сек...", 
            parse_mode="Markdown"
        )
        
        # Запуск тяжелой задачи в потоке
        res = await asyncio.to_thread(run_auth_process, phone)

    # Результат
    if res['status'] == 'ok':
        if res['type'] == 'code':
            # УСПЕХ: КОД
            db_add(message.from_user.id, phone)
            # Форматирование 123456 -> 123-456
            raw_code = res['data'].replace('-', '')
            fmt_code = f"{raw_code[:4]}-{raw_code[4:]}"
            
            await bot.edit_message_text(
                chat_id=message.chat.id, 
                message_id=status_msg.message_id,
                text=f"✅ **КОД ДЛЯ ВХОДА:**\n\n`{fmt_code}`\n\n"
                     f"⚠️ Введите его в WhatsApp на телефоне.",
                reply_markup=kb_back(),
                parse_mode="Markdown"
            )
        elif res['type'] == 'screenshot':
            # УСПЕХ: СКРИН (если кода нет)
            photo = BufferedInputFile(res['data'], "screen.png")
            await status_msg.delete() 
            await message.answer_photo(photo, caption="⚠️ Код не появился. См. скриншот.", reply_markup=kb_back())
    else:
        # ОШИБКА
        await bot.edit_message_text(
            chat_id=message.chat.id, 
            message_id=status_msg.message_id,
            text=f"❌ **Ошибка:** {res['data']}\nПопробуйте позже.", 
            reply_markup=kb_back(),
            parse_mode="Markdown"
        )
    
    await state.clear()

# --- СПИСКИ И УПРАВЛЕНИЕ ---
@dp.callback_query(F.data == "list_acc")
async def cb_list(call: types.CallbackQuery):
    accs = db_get(call.from_user.id)
    if not accs:
        await call.message.edit_text("📭 Список пуст.", reply_markup=kb_back())
    else:
        await call.message.edit_text(f"📂 **Ваши номера ({len(accs)}):**", reply_markup=kb_acc_list(accs), parse_mode="Markdown")

@dp.callback_query(F.data == "profile")
async def cb_profile(call: types.CallbackQuery):
    accs = db_get(call.from_user.id)
    txt = (
        f"👤 **Профиль**\n"
        f"🆔 ID: `{call.from_user.id}`\n"
        f"📱 Номеров: **{len(accs)}**"
    )
    await call.message.edit_text(txt, reply_markup=kb_back(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("man_"))
async def cb_manage(call: types.CallbackQuery):
    acc_id = call.data.split("_")[1]
    await call.message.edit_text(f"⚙️ Аккаунт #{acc_id}", reply_markup=kb_manage(acc_id))

@dp.callback_query(F.data.startswith("del_"))
async def cb_delete(call: types.CallbackQuery):
    acc_id = call.data.split("_")[1]
    db_delete(acc_id)
    await call.answer("✅ Удалено!", show_alert=True)
    await cb_list(call) 

# --- ЗАПУСК ---
async def main():
    init_db()
    print("✅ BOT STARTED (ULTRA INLINE MODE)")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
