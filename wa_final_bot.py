import asyncio
import os
import logging
import sqlite3
import random
import re
import shutil
import psutil
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from faker import Faker

# --- SELENIUM ---
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.environ.get("BOT_TOKEN") 

# ЛИМИТЫ (Важно для 150 аккаунтов на BotHost)
BROWSER_SEMAPHORE = asyncio.Semaphore(3) # Максимум 3 браузера одновременно

DB_NAME = 'bot_database.db'
SESSIONS_DIR = "./sessions"
LOG_DIR = "./logs"
BAN_DIR = "./logs/bans"

CONFIG = {
    "mode": "MASS",     # MASS = Переписка между аккаунтами (прогрев)
    "speed": "NORMAL",  # Скорость
    "min_delay": 120,   # Мин задержка (сек)
    "max_delay": 400    # Макс задержка (сек)
}

ACTIVE_SESSIONS = {} 
fake = Faker('ru_RU')

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
logger = logging.getLogger("WA_UNLOCKED")

# --- БАЗА ДАННЫХ (WAL Mode - Быстро и надежно) ---
def init_db():
    with sqlite3.connect(DB_NAME, timeout=30) as conn:
        conn.execute("PRAGMA journal_mode=WAL;") # Важно для многопотока
        conn.execute('''CREATE TABLE IF NOT EXISTS accounts 
                        (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                         user_id INTEGER, 
                         phone_number TEXT UNIQUE, 
                         status TEXT DEFAULT 'pending', 
                         messages_sent INTEGER DEFAULT 0,
                         user_agent TEXT, resolution TEXT, platform TEXT,
                         ban_reason TEXT, 
                         profile_set INTEGER DEFAULT 0,
                         last_active TIMESTAMP)''')
        conn.commit()

def db_get_all_active():
    with sqlite3.connect(DB_NAME) as conn:
        return [row[0] for row in conn.execute("SELECT phone_number FROM accounts WHERE status = 'active'").fetchall()]

def db_get_acc(phone):
    with sqlite3.connect(DB_NAME) as conn:
        return conn.execute("SELECT * FROM accounts WHERE phone_number = ?", (phone,)).fetchone()

def db_update_status(phone, status, reason=None):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET status = ?, ban_reason = ?, last_active = ? WHERE phone_number = ?", 
                     (status, reason, datetime.now(), phone))

def db_set_profile_done(phone):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET profile_set = 1 WHERE phone_number = ?", (phone,))

def db_inc_msg(phone):
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("UPDATE accounts SET messages_sent = messages_sent + 1, last_active = ? WHERE phone_number = ?", 
                     (datetime.now(), phone))

def db_delete(phone):
    with sqlite3.connect(DB_NAME) as conn: conn.execute("DELETE FROM accounts WHERE phone_number = ?", (phone,))
    path = os.path.join(SESSIONS_DIR, str(phone))
    if os.path.exists(path): shutil.rmtree(path, ignore_errors=True)

# --- SYSTEM GUARD ---
def is_memory_safe():
    try:
        mem = psutil.virtual_memory().available / (1024 * 1024)
        if mem < 200:
            logger.warning(f"⚠️ LOW RAM: {mem:.1f}MB. Pause.")
            return False
        return True
    except: return True

async def zombie_killer():
    """Убивает зависшие процессы Chrome раз в 2 минуты"""
    while True:
        await asyncio.sleep(120)
        for proc in psutil.process_iter(['pid', 'name', 'create_time']):
            try:
                if 'chrome' in proc.info['name']:
                    # Если процесс живет дольше 30 минут - убиваем
                    if (datetime.now().timestamp() - proc.info['create_time']) > 1800:
                        proc.kill()
            except: pass

# --- SELENIUM UTILS ---
async def find_element_retry(driver, xpaths, timeout=10):
    wait = WebDriverWait(driver, timeout)
    for xp in xpaths:
        try: return wait.until(EC.presence_of_element_located((By.XPATH, xp)))
        except: continue
    return None

def get_driver(phone):
    if not is_memory_safe(): return None
    
    path = os.path.join(SESSIONS_DIR, str(phone))
    if not os.path.exists(path): os.makedirs(path)

    acc = db_get_acc(phone)
    if acc and acc[5]:
        ua, res, plat = acc[5], acc[6], acc[7]
    else:
        # Генерация отпечатка
        ua = f"Mozilla/5.0 ({random.choice(['Windows NT 10.0; Win64; x64', 'Macintosh; Intel Mac OS X 10_15_7'])}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        res, plat = "1920,1080", "Win32"
        with sqlite3.connect(DB_NAME) as conn:
            conn.execute("UPDATE accounts SET user_agent=?, resolution=?, platform=? WHERE phone_number=?", (ua, res, plat, phone))

    opt = Options()
    opt.add_argument("--headless=new")
    opt.add_argument("--no-sandbox")
    opt.add_argument("--disable-dev-shm-usage")
    opt.add_argument(f"--window-size={res}")
    opt.add_argument("--lang=ru-KZ")
    opt.add_argument(f"user-agent={ua}")
    opt.add_argument(f"--user-data-dir={path}")
    opt.page_load_strategy = 'eager'
    
    # STEALTH
    opt.add_argument("--disable-blink-features=AutomationControlled")
    opt.add_experimental_option("excludeSwitches", ["enable-automation"])
    opt.add_experimental_option('useAutomationExtension', False)

    try:
        driver = webdriver.Chrome(options=opt)
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": f"Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}}); Object.defineProperty(navigator, 'platform', {{get: () => '{plat}'}});"
        })
        return driver
    except: return None

# --- UI UTILS ---
async def send_screen(driver, chat_id, caption=""):
    try:
        scr = driver.get_screenshot_as_png()
        await bot.send_photo(chat_id, BufferedInputFile(scr, "s.png"), caption=caption)
    except: pass

# --- BOT HANDLERS ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
class Form(StatesGroup): phone = State()

def kb_main():
    mode = "👤 Solo" if CONFIG['mode'] == "SOLO" else "👥 Mass"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить Аккаунт", callback_data="add")],
        [InlineKeyboardButton(text="📂 Ферма (Список)", callback_data="list")],
        [InlineKeyboardButton(text=f"Режим: {mode}", callback_data="toggle_mode")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats")]
    ])

# !!! САМОЕ ГЛАВНОЕ: УБРАНЫ ПРОВЕРКИ ADMIN_ID. РАБОТАЕТ ДЛЯ ВСЕХ !!!
@dp.message(Command("start"))
async def start(msg: types.Message):
    init_db()
    for d in [SESSIONS_DIR, LOG_DIR, BAN_DIR]:
        if not os.path.exists(d): os.makedirs(d)
    await msg.answer("🔥 **WA Farm Unlocked**\nДоступ открыт для всех. Жми кнопки.", reply_markup=kb_main())

@dp.callback_query(F.data == "add")
async def add_start(call: types.CallbackQuery, state: FSMContext):
    await call.message.edit_text("Введи номер телефона (только цифры, например 79001234567):")
    await state.set_state(Form.phone)

@dp.message(Form.phone)
async def add_phone(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("INSERT OR IGNORE INTO accounts (user_id, phone_number) VALUES (?, ?)", (msg.from_user.id, phone))
    
    await msg.answer(f"🚀 Запускаю браузер для **{phone}**...\nПодожди 10-15 сек, я пришлю скриншот.", reply_markup=None)
    # Запуск фонового процесса настройки
    asyncio.create_task(auth_session_start(msg.chat.id, phone))

async def auth_session_start(chat_id, phone):
    # Используем семафор, чтобы не убить сервер
    async with BROWSER_SEMAPHORE:
        driver = await asyncio.to_thread(get_driver, phone)
        if not driver: 
            await bot.send_message(chat_id, "❌ **Ошибка:** Не удалось открыть браузер (мало памяти).")
            return

        ACTIVE_SESSIONS[phone] = driver
        try:
            driver.get("https://web.whatsapp.com/")
            await asyncio.sleep(10) # Ждем загрузки страницы
            
            # Меню управления входом
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔗 Ввести номер (Авто)", callback_data=f"do_link_{phone}")],
                [InlineKeyboardButton(text="📷 Показать QR", callback_data=f"do_qr_{phone}")],
                [InlineKeyboardButton(text="🔄 Обновить скрин", callback_data=f"do_scr_{phone}")]
            ])
            
            # Шлем скриншот, чтобы ты видел, что происходит
            try:
                scr = driver.get_screenshot_as_png()
                await bot.send_photo(chat_id, BufferedInputFile(scr, "start.png"), 
                                   caption=f"✅ Браузер для {phone} открыт.\nВыбери метод входа:", reply_markup=kb)
            except:
                await bot.send_message(chat_id, "⚠️ Браузер открыт, но скрин не сделался. Попробуй нажать кнопки.", reply_markup=kb)
            
            # Ждем 5 минут, проверяя вход
            for _ in range(60):
                # Если появился список чатов
                if driver.find_elements(By.ID, "pane-side"):
                    db_update_status(phone, "active")
                    await bot.send_message(chat_id, f"✅ **{phone}** УСПЕШНО АВТОРИЗОВАН!\nАккаунт добавлен в базу и начнет работу.")
                    return
                await asyncio.sleep(5)
                
        except Exception as e:
            logger.error(f"Auth error: {e}")
            await bot.send_message(chat_id, "💥 Браузер вылетел.")
        finally:
            if phone in ACTIVE_SESSIONS: del ACTIVE_SESSIONS[phone]
            try: driver.quit()
            except: pass

# --- UI ДЕЙСТВИЯ (ВИЗУАЛЬНЫЙ ВХОД) ---
@dp.callback_query(F.data.startswith("do_scr_"))
async def refresh_screen(call: types.CallbackQuery):
    phone = call.data.split("_")[2]
    driver = ACTIVE_SESSIONS.get(phone)
    if driver:
        await send_screen(driver, call.message.chat.id, "Обновление экрана:")
        await call.answer()
    else:
        await call.answer("Браузер закрылся по тайм-ауту", show_alert=True)

@dp.callback_query(F.data.startswith("do_qr_"))
async def show_qr(call: types.CallbackQuery):
    phone = call.data.split("_")[2]
    driver = ACTIVE_SESSIONS.get(phone)
    if driver:
        await call.message.answer("Ищу QR код...")
        try:
            canvas = driver.find_element(By.TAG_NAME, "canvas")
            await send_screen(driver, call.message.chat.id, "Сканируй QR телефоном!")
        except:
            await send_screen(driver, call.message.chat.id, "QR не найден. WA мог сменить верстку или ты уже вошел.")
    else:
        await call.answer("Браузер закрыт")

@dp.callback_query(F.data.startswith("do_link_"))
async def do_link_number(call: types.CallbackQuery):
    phone = call.data.split("_")[2]
    driver = ACTIVE_SESSIONS.get(phone)
    if not driver: return await call.answer("Браузер закрыт")
    
    await call.answer("Пытаюсь нажать и ввести...")
    try:
        # 1. Жмем Link with phone
        btn = await find_element_retry(driver, ["//span[contains(text(), 'Link with phone')]", "//a[contains(@href, 'link-device')]", "//span[contains(text(), 'Связать с номером')]"], 5)
        if btn:
            btn.click()
            await asyncio.sleep(2)
        
        # 2. Ищем поле ввода
        inp = await find_element_retry(driver, ["//input[@aria-label='Type your phone number.']", "//input[@type='text']"], 5)
        if inp:
            # Чистим JS-ом
            driver.execute_script("arguments[0].value = '';", inp)
            inp.send_keys(Keys.CONTROL + "a" + Keys.BACKSPACE)
            # Вводим
            for ch in phone: inp.send_keys(ch); await asyncio.sleep(0.05)
            inp.send_keys(Keys.ENTER)
            
            # 3. Ждем код
            await asyncio.sleep(3)
            code_el = await find_element_retry(driver, ["//div[@aria-details='link-device-phone-number-code']"], 15)
            
            scr = driver.get_screenshot_as_png()
            txt = f"🔑 КОД: {code_el.text}" if code_el else "❌ Код не появился. Глянь скрин, мб ошибка."
            await bot.send_photo(call.message.chat.id, BufferedInputFile(scr, "code.png"), caption=txt)
        else:
            await send_screen(driver, call.message.chat.id, "Не нашел поле ввода! См. скрин.")
            
    except Exception as e:
        await call.message.answer(f"Ошибка: {e}")

# --- FARM WORKER (PLATINUM LOGIC) ---
async def worker_cycle(phone, force_action=None):
    if not is_memory_safe(): return
    
    # Ночной режим (Спим с 23 до 07, если не пнули принудительно)
    if not force_action:
        h = datetime.now().hour
        if (h >= 23 or h < 7) and random.random() < 0.95: return

    context = BROWSER_SEMAPHORE if not force_action else asyncio.Semaphore(1)
    
    async with context:
        driver = await asyncio.to_thread(get_driver, phone)
        if not driver: return
        ACTIVE_SESSIONS[phone] = driver
        
        try:
            driver.get("https://web.whatsapp.com/")
            
            # Ждем чаты
            loaded = await find_element_retry(driver, ["//div[@id='pane-side']"], 60)
            
            if not loaded:
                src = driver.page_source.lower()
                if "account is not allowed" in src:
                    db_update_status(phone, 'banned', 'PermBan')
                    driver.save_screenshot(os.path.join(BAN_DIR, f"ban_{phone}.png"))
                elif "link with phone" in src:
                    if not force_action: db_update_status(phone, 'pending')
                return

            db_update_status(phone, 'active')

            # Обработка Force Actions
            if force_action == "screenshot":
                await asyncio.sleep(2); return 
            if force_action == "msg":
                 await send_msg_selenium(driver, phone, phone, "Check")
                 return

            # Фарм Логика
            acc = db_get_acc(phone)
            # Если профиль не настроен - настраиваем
            if acc[9] == 0: await change_profile(driver, phone)
            
            actives = db_get_all_active()
            target = phone
            is_solo = True
            
            # Если режим MASS и есть другие акки - пишем им
            if CONFIG['mode'] == "MASS" and len(actives) > 1:
                if random.random() < 0.8: # 80% шанс писать другому
                    cand = [x for x in actives if x != phone]
                    if cand: target = random.choice(cand); is_solo = False
            
            await send_msg_selenium(driver, phone, target, "Solo" if is_solo else "Mass")
            await asyncio.sleep(random.randint(5, 10))

        except Exception as e:
            logger.error(f"Worker err {phone}: {e}")
        finally:
            if phone in ACTIVE_SESSIONS: del ACTIVE_SESSIONS[phone]
            try: driver.quit()
            except: pass

async def send_msg_selenium(driver, sender, target, mode):
    try:
        driver.get(f"https://web.whatsapp.com/send?phone={target}")
        inp = await find_element_retry(driver, ["//div[@contenteditable='true'][@data-tab='10']", "//footer//div[@role='textbox']"], 30)
        if inp:
            txt = fake.sentence()
            for ch in txt: inp.send_keys(ch); await asyncio.sleep(0.05)
            inp.send_keys(Keys.ENTER)
            db_inc_msg(sender)
    except: pass

async def change_profile(driver, phone):
    try:
        # Клик по аватарке
        driver.find_element(By.XPATH, "//header//img").click()
        await asyncio.sleep(2)
        # Клик по карандашу имени
        driver.find_element(By.XPATH, "//span[@data-icon='pencil']").click()
        act = driver.switch_to.active_element
        act.send_keys(Keys.CONTROL + "a" + Keys.BACKSPACE)
        act.send_keys(fake.first_name())
        act.send_keys(Keys.ENTER)
        db_set_profile_done(phone)
    except: pass

async def farm_scheduler():
    asyncio.create_task(zombie_killer())
    while True:
        phones = db_get_all_active()
        if phones:
            p = random.choice(phones)
            asyncio.create_task(worker_cycle(p))
        
        # Задержка между запусками
        d = random.randint(CONFIG['min_delay'], CONFIG['max_delay'])
        if datetime.now().hour >= 23: d *= 3 # Ночью реже
        await asyncio.sleep(d)

# --- MENUS ---
@dp.callback_query(F.data == "list")
async def list_accs(call: types.CallbackQuery):
    accs = db_get_all_active()
    txt = f"Активных: {len(accs)}\n" + "\n".join([f"🟢 {a}" for a in accs[:10]])
    if len(accs) > 10: txt += "\n..."
    if not accs: txt = "Нет активных"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="menu")]])
    await call.message.edit_text(txt, reply_markup=kb)

@dp.callback_query(F.data == "menu")
async def menu_back(call: types.CallbackQuery):
    await call.message.edit_text("Меню:", reply_markup=kb_main())

@dp.callback_query(F.data == "toggle_mode")
async def tog_mode(call: types.CallbackQuery):
    CONFIG['mode'] = "MASS" if CONFIG['mode'] == "SOLO" else "SOLO"
    await call.message.edit_reply_markup(reply_markup=kb_main())

@dp.callback_query(F.data == "stats")
async def show_stats(call: types.CallbackQuery):
    m = psutil.virtual_memory()
    await call.answer(f"RAM: {m.available//1024//1024}MB\nАккаунтов: {len(db_get_all_active())}", show_alert=True)

async def main():
    init_db()
    asyncio.create_task(farm_scheduler())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
