import asyncio
import os
import io
import logging
from aiogram import Bot, Dispatcher, types, F
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

# --- КОНФИГУРАЦИЯ ---
# Вставьте сюда токен, полученный от @BotFather
BOT_TOKEN = "ВАШ_ТОКЕН_ТЕЛЕГРАМ_БОТА" 
ADMIN_IDS = [123456789] # Ваш личный ID

# --- НАСТРОЙКА ЛОГИРОВАНИЯ ---
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
driver = None

# --- СЕЛЕНИУМ ФУНКЦИИ ---

def start_chrome():
    """Запускает браузер Chrome в фоновом режиме."""
    global driver
    if driver is not None:
        return driver

    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080") 
    
    # Автоматическая установка драйвера
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
    """
    global driver
    if not driver:
        start_chrome()
    
    driver.get("https://web.whatsapp.com/")
    wait = WebDriverWait(driver, 30)
    
    try:
        print("1. Ожидание загрузки страницы...")
        # 1. Ждем появления кнопки или ссылки "Link with phone number" (сложный селектор)
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
        # Вводим номер (WhatsApp Web требует номер без плюса и с кодом страны)
        phone_input.send_keys(phone_number)
        
        # 3. Нажатие кнопки "Next"
        next_button = wait.until(
            EC.element_to_be_clickable((By.XPATH, "//div[@role='button' and @title='Next'] | //button[contains(text(), 'Next')]"))
        )
        next_button.click()
        
        print("3. Ожидание 8-значного кода...")
        # 4. Ожидание и считывание 8-значного кода
        code_element = wait.until(
            EC.presence_of_element_located((By.XPATH, "//div[contains(@class, 'selectable-text') and string-length(text()) > 5]"))
        )
        
        return code_element.text
        
    except TimeoutException:
        print("Таймаут: QR-код или поле ввода не найдено. Возможно, уже авторизован.")
        return "ERROR: Timeout"
    except NoSuchElementException:
        print("Элемент не найден. Возможно, изменился интерфейс WhatsApp Web.")
        return "ERROR: Element not found"
    except Exception as e:
        print(f"Общая ошибка в процессе входа: {e}")
        return f"ERROR: General error: {e}"

# --- ОБРАБОТЧИКИ TELEGRAM ---

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
            "**Действие:** Откройте WhatsApp на телефоне, перейдите в *Настройки -> Связанные устройства -> Привязка устройства* и выберите *Ссылка по номеру телефона*. Введите этот код."
        )
    else:
        await message.answer(f"❌ **Ошибка входа:** {result_code}")


@dp.message(Command("screen"))
async def cmd_screen(message: types.Message):
    """Отладочная команда: присылает скриншот того, что сейчас видит бот."""
    if message.from_user.id not in ADMIN_IDS:
        return
    global driver
    if not driver:
        await message.answer("Браузер не запущен. Запустите его командой /link.")
        return

    # Делаем скриншот всей страницы
    screenshot = await asyncio.to_thread(driver.get_screenshot_as_png)
    photo_file = BufferedInputFile(screenshot, filename="debug_screen.png")
    await message.answer_photo(photo_file, caption="📸 Текущий экран браузера")


# (Оставьте команды /start, /status, /stop из предыдущего кода,
# чтобы иметь полный контроль над браузером)

# --- ЗАПУСК ---
async def main():
    print("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"Критическая ошибка: {e}")
        quit_browser()
