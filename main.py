import asyncio
import os
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import BufferedInputFile
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
from webdriver_manager.chrome import ChromeDriverManager
import time

# --- КОНФИГУРАЦИЯ (ЧТЕНИЕ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ) ---

# Получаем токен Telegram-бота
BOT_TOKEN = os.environ.get("TG_BOT_TOKEN") 
if not BOT_TOKEN:
    raise ValueError("Переменная TG_BOT_TOKEN не установлена!")

# Получаем ID администратора (для безопасности)
try:
    ADMIN_IDS = [int(os.environ.get("TG_ADMIN_ID"))] 
except (ValueError, TypeError):
    # Если TG_ADMIN_ID не установлен или не является числом, берем 0, но выводим предупреждение
    ADMIN_IDS = [0] 
    logging.warning("Переменная TG_ADMIN_ID не установлена или некорректна. Ботом сможет управлять только пользователь с ID 0 (что невозможно).")


# --- НАСТРОЙКА ЛОГИРОВАНИЯ ---
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
driver = None

# ... (Остальные функции Selenium start_chrome, quit_browser, check_login_status - остаются БЕЗ ИЗМЕНЕНИЙ) ...

def start_chrome():
    """Запускает браузер Chrome в фоновом режиме."""
    global driver
    if driver is not None:
        return driver

    options = Options()
    options.add_argument("--headless") # Режим без окна (для сервера)
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080") 
    
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    return driver

def quit_browser():
    """Закрывает браузер."""
    global driver
    if driver:
        driver.quit()
        driver = None

def get_link_code(phone_number):
    """
    Выполняет вход по номеру телефона и возвращает 8-значный код.
    (Этот код остается сложным, так как имитирует действия человека)
    """
    global driver
    if not driver:
        start_chrome()
    
    driver.get("https://web.whatsapp.com/")
    wait = WebDriverWait(driver, 30)
    
    try:
        print("1. Ожидание кнопки 'Link with phone number'...")
        # 1. Ждем появления кнопки "Link with phone number"
        link_button = wait.until(
            EC.element_to_be_clickable((By.XPATH, "//div[text()='Link with phone number'] | //button[contains(text(), 'Link with phone number')] | //*[text()='Link with phone number']"))
        )
        link_button.click()
        time.sleep(2)

        print("2. Ввод номера телефона...")
        # 2. Ввод номера
        phone_input = wait.until(
            EC.presence_of_element_located((By.XPATH, "//input[@placeholder='Phone number' or @type='tel']"))
        )
        phone_input.send_keys(phone_number)
        
        # 3. Нажатие кнопки "Next"
        next_button = wait.until(
            EC.element_to_be_clickable((By.XPATH, "//div[@role='button' and @title='Next'] | //button[contains(text(), 'Next')]"))
        )
        next_button.click()
        
        print("4. Ожидание 8-значного кода...")
        # 4. Ожидание и считывание 8-значного кода
        code_element = wait.until(
            EC.presence_of_element_located((By.XPATH, "//div[contains(@class, 'selectable-text') and string-length(text()) > 5]"))
        )
        
        return code_element.text
        
    except TimeoutException:
        print("Таймаут: Элементы не найдены. Возможно, уже авторизован.")
        return "ERROR: Timeout"
    except Exception as e:
        print(f"Общая ошибка в процессе входа: {e}")
        return f"ERROR: General error: {e}"

# --- ОБРАБОТЧИКИ TELEGRAM (Остаются БЕЗ ИЗМЕНЕНИЙ) ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer(
        "👋 Привет! Я твой пульт управления WhatsApp Userbot. Использую переменные окружения."
    )

@dp.message(Command("link"))
async def cmd_link(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ **Формат команды:** /link 7XXXXXXXXXX (номер без +)")
        return
        
    phone_number = args[1].strip().replace('+', '')
    await message.answer(f"⏳ Начинаю вход по номеру: **{phone_number}**...")
    
    result_code = await asyncio.to_thread(get_link_code, phone_number)
    
    if result_code and not result_code.startswith("ERROR"):
        await message.answer(
            f"✅ **КОД ДЛЯ ВХОДА:** `{result_code}`\n\n"
            "**Действие:** Откройте WhatsApp на телефоне, введите этот код в разделе 'Привязать устройство' -> 'Ссылка по номеру телефона'."
        )
    else:
        await message.answer(f"❌ **Ошибка входа:** {result_code}")

# ... (Остальные команды /screen, /status, /stop остаются без изменений) ...

@dp.message(Command("screen"))
async def cmd_screen(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    global driver
    if not driver:
        await message.answer("Браузер не запущен. Запустите его командой /link.")
        return

    screenshot = await asyncio.to_thread(driver.get_screenshot_as_png)
    photo_file = BufferedInputFile(screenshot, filename="debug_screen.png")
    await message.answer_photo(photo_file, caption="📸 Текущий экран браузера")

@dp.message(Command("status"))
async def cmd_status(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    is_logged_in = await asyncio.to_thread(check_login_status)
    if is_logged_in:
        await message.answer("✅ **Успешно авторизован!** Сессия активна.")
    else:
        await message.answer("❌ **Не авторизован.** Используйте /link.")

@dp.message(Command("stop"))
async def cmd_stop(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    quit_browser()
    await message.answer("🛑 Браузер закрыт.")
    
def check_login_status():
    global driver
    if not driver:
        return False
    try:
        # Ищем панель чатов (признак успешного входа)
        WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.ID, "pane-side"))
        )
        return True
    except:
        return False


# --- ЗАПУСК ---
async def main():
    print("Бот запущен. Читаю переменные окружения...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        logging.error(f"Критическая ошибка при запуске: {e}")
        quit_browser()
