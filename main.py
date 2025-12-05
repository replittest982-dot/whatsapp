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
from selenium.common.exceptions import TimeoutException
from webdriver_manager.chrome import ChromeDriverManager
import time

# --- КОНФИГУРАЦИЯ (ЧТЕНИЕ ИЗ ПЕРЕМЕННЫХ ОКРУЖЕНИЯ) ---

# 1. Токен для Телеграм-бота (Пульт управления)
BOT_TOKEN = os.environ.get("BOT_TOKEN") 
if not BOT_TOKEN:
    raise ValueError("Переменная BOT_TOKEN не установлена!")

# 2. ID администратора (для безопасности)
try:
    ADMIN_IDS = [int(os.environ.get("ADMIN_ID"))] 
except (ValueError, TypeError):
    raise ValueError("Переменная ADMIN_ID не установлена или некорректна!")

# 3. Ключи Telegram API (на будущее, если понадобится Userbot)
API_ID = os.environ.get("API_ID") 
API_HASH = os.environ.get("API_HASH") 
# Примечание: В этом скрипте (для WhatsApp) эти переменные пока не используются,
# но они необходимы для любой дальнейшей работы с Telegram API.

# --- НАСТРОЙКА ЛОГИРОВАНИЯ ---
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
driver = None

# --- ФУНКЦИИ SELENIUM (WHATSAPP) ---

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
    
    # Автоматически находит и устанавливает драйвер
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    return driver

def quit_browser():
    """Закрывает браузер."""
    global driver
    if driver:
        driver.quit()
        driver = None

def check_login_status():
    """Проверяет, вошли мы в аккаунт или нет (ищет список чатов)."""
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


def get_link_code(phone_number):
    """
    Выполняет вход по номеру телефона и возвращает 8-значный код.
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
        return "ERROR: Timeout (Элемент не найден или страница не загрузилась)"
    except Exception as e:
        return f"ERROR: General error: {e}"

# --- ОБРАБОТЧИКИ TELEGRAM ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer(
        "👋 Привет! Я твой пульт управления WhatsApp Userbot. Все ключи взяты из переменных окружения."
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
    
    # Запускаем блокирующую задачу в отдельном потоке
    result_code = await asyncio.to_thread(get_link_code, phone_number)
    
    if result_code and not result_code.startswith("ERROR"):
        await message.answer(
            f"✅ **КОД ДЛЯ ВХОДА:** `{result_code}`\n\n"
            "**Действие:** Откройте WhatsApp на телефоне, введите этот код в разделе 'Привязать устройство' -> 'Ссылка по номеру телефона'."
        )
    else:
        await message.answer(f"❌ **Ошибка входа:** {result_code}")

@dp.message(Command("screen"))
async def cmd_screen(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    global driver
    if not driver:
        await message.answer("Браузер не запущен. Запустите его командой /link.")
        return

    # Делаем скриншот
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
