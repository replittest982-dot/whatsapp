import asyncio
import os
import logging
import aiosqlite
import random
import re
import string
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
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys

# --- CONFIGURATION ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except Exception:
    ADMIN_ID = 0

BROWSER_SEMAPHORE = asyncio.Semaphore(3)  # Ограничение: максимум 3 одновременных браузера
DB_NAME = 'bot_database.db'
SESSIONS_DIR = "/app/sessions"

ACTIVE_DRIVERS = {}
fake = Faker('ru_RU')

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# --- DEVICES ---
DEVICES = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "res": "1280,800", "plat": "Win32", "vendor": "Google Inc."},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "res": "1280,800", "plat": "MacIntel", "vendor": "Google Inc."},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", "res": "1280,800", "plat": "Linux x86_64", "vendor": "Google Inc."},
]

SELF_MESSAGES = [
    "Не забыть купить: хлеб, молоко, яйца",
    "Идея: попробовать новый ресторан",
    "Позвонить маме вечером",
    "Сделать зарядку утром",
    "Оплатить интернет до конца недели",
    "Проверить почту сегодня",
    "Заказать такси заранее",
]

SELF_BIOS = [
    "Живу в моменте 🌙",
    "Работа • Спорт • Кофе",
    "Просто хороший человек ☀️",
    "На связи не всегда, но отвечу",
    "Тихий режим включён 🎧",
]

# --- DATABASE (ASYNC) ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS accounts 
                            (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                             user_id INTEGER, phone_number TEXT UNIQUE, 
                             status TEXT DEFAULT 'pending', 
                             messages_sent INTEGER DEFAULT 0,
                             user_agent TEXT, resolution TEXT, platform TEXT,
                             ban_reason TEXT, last_active TIMESTAMP,
                             farm_min INTEGER DEFAULT 1,
                             farm_max INTEGER DEFAULT 3)''')
        await db.commit()

async def db_get_acc(phone):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT * FROM accounts WHERE phone_number = ?", (phone,)) as cursor:
            return await cursor.fetchone()

async def db_get_active_phones():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT phone_number FROM accounts WHERE status = 'active'") as cursor:
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

async def db_update_status(phone, status, reason=None):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE accounts SET status = ?, ban_reason = ? WHERE phone_number = ?", (status, reason, phone))
        await db.commit()

async def db_inc_msg(phone):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE accounts SET messages_sent = messages_sent + 1, last_active = ? WHERE phone_number = ?", (datetime.now(), phone))
        await db.commit()

async def db_set_farm_delay(phone, min_m, max_m):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE accounts SET farm_min = ?, farm_max = ? WHERE phone_number = ?", (min_m, max_m, phone))
        await db.commit()

async def db_get_farm_delay(phone):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT farm_min, farm_max FROM accounts WHERE phone_number = ?", (phone,)) as cursor:
            row = await cursor.fetchone()
            if row:
                return row[0] or 1, row[1] or 3
            return 1, 3

async def db_get_stats():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT count(*) FROM accounts") as c: total = (await c.fetchone())[0]
        async with db.execute("SELECT count(*) FROM accounts WHERE status = 'active'") as c: active = (await c.fetchone())[0]
        async with db.execute("SELECT count(*) FROM accounts WHERE status = 'banned'") as c: banned = (await c.fetchone())[0]
        async with db.execute("SELECT sum(messages_sent) FROM accounts") as c: sent = (await c.fetchone())[0] or 0
    return total, active, banned, sent

# --- MEMORY CLEANUP & GUARD ---
def is_memory_critical():
    mem = psutil.virtual_memory()
    return (mem.available / 1024 / 1024) < 250  # Пауза если меньше 250MB RAM

def close_driver(driver):
    if not driver:
        return
    try:
        pid = driver.service.process.pid
        driver.quit()
        parent = psutil.Process(pid)
        for child in parent.children(recursive=True):
            child.kill()
        parent.kill()
    except Exception:
        pass

# --- DRIVER FACTORY (ULTRA LOW RAM) ---
def get_driver(phone, ua=None, res="1280,800", plat="Win32", vendor="Google Inc."):
    opt = Options()
    opt.binary_location = "/usr/bin/google-chrome"
    opt.add_argument("--headless=new")
    opt.add_argument("--no-sandbox")
    opt.add_argument("--disable-dev-shm-usage")
    opt.add_argument("--disable-gpu")
    opt.add_argument("--disable-software-rasterizer")
    opt.add_argument(f"--window-size={res}")
    
    # Максимальная экономия оперативной памяти (RAM)
    opt.add_argument("--blink-settings=imagesEnabled=false")  # Отключение картинок
    opt.add_argument("--disable-extensions")
    opt.add_argument("--disable-component-extensions-with-background-pages")
    opt.add_argument("--disable-background-networking")
    opt.add_argument("--disable-component-update")
    opt.add_argument("--disable-default-apps")
    opt.add_argument("--disable-sync")
    opt.add_argument("--disable-translate")
    opt.add_argument("--metrics-recording-only")
    opt.add_argument("--no-first-run")
    opt.add_argument("--safebrowsing-disable-auto-update")

    prefs = {
        "profile.managed_default_content_settings.images": 2,
        "profile.default_content_setting_values.notifications": 2,
        "profile.managed_default_content_settings.stylesheets": 2,
    }
    opt.add_experimental_option("prefs", prefs)

    # Stealth & Location
    opt.add_argument("--lang=ru-KZ,ru,kk")
    if ua:
        opt.add_argument(f"--user-agent={ua}")
    opt.add_argument("--disable-blink-features=AutomationControlled")
    opt.add_experimental_option("excludeSwitches", ["enable-automation"])
    opt.add_experimental_option('useAutomationExtension', False)
    
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    opt.add_argument(f"--user-data-dir={os.path.join(SESSIONS_DIR, str(phone))}")

    import glob
    _raw_path = ChromeDriverManager().install()
    if "THIRD_PARTY" in _raw_path or not _raw_path.endswith("chromedriver"):
        _base = os.path.dirname(_raw_path)
        _candidates = glob.glob(os.path.join(_base, "chromedriver")) or glob.glob(os.path.join(_base, "**", "chromedriver"), recursive=True)
        _raw_path = _candidates[0] if _candidates else _raw_path
    os.chmod(_raw_path, 0o755)

    driver = webdriver.Chrome(service=Service(_raw_path), options=opt)

    # Injection Anti-Detect + KZ Timezone
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": f"""
        Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}});
        Object.defineProperty(navigator, 'platform', {{get: () => '{plat}'}});
        Object.defineProperty(navigator, 'vendor', {{get: () => '{vendor}'}});
        Object.defineProperty(navigator, 'language', {{get: () => 'ru-KZ'}});
        Object.defineProperty(navigator, 'languages', {{get: () => ['ru-KZ', 'ru', 'kk', 'en']}});
        window.chrome = {{ runtime: {{}} }};
        """
    })

    driver.execute_cdp_cmd("Emulation.setGeolocationOverride", {"latitude": 43.2389, "longitude": 76.8897, "accuracy": 50})
    driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": "Asia/Almaty"})

    return driver

# --- HUMAN ACTIONS ---
async def human_type(element, text):
    for char in text:
        if random.random() < 0.03:
            element.send_keys(random.choice(string.ascii_lowercase))
            await asyncio.sleep(0.08)
            element.send_keys(Keys.BACKSPACE)
        element.send_keys(char)
        await asyncio.sleep(random.uniform(0.04, 0.12))

async def check_ban_status(driver, phone):
    try:
        page_text = driver.find_element(By.TAG_NAME, "body").text
        if "account is not allowed" in page_text or "spam" in page_text.lower():
            await db_update_status(phone, 'banned', 'PermBan')
            return True
        return False
    except Exception:
        return False

# --- KEYBOARDS ---
def kb_main(uid):
    kb = [
        [InlineKeyboardButton(text="➕ Добавить Аккаунт", callback_data="add")],
        [InlineKeyboardButton(text="📂 Мои Аккаунты", callback_data="list")],
        [InlineKeyboardButton(text="⚙️ Настройки фарма", callback_data="farm_settings_menu")],
    ]
    if uid == ADMIN_ID:
        kb.append([InlineKeyboardButton(text="👑 Админ Панель", callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def kb_auth():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 СКРИН", callback_data="check"),
         InlineKeyboardButton(text="✅ ГОТОВО", callback_data="done")],
        [InlineKeyboardButton(text="🔗 Вход по номеру", callback_data="force_link")],
        [InlineKeyboardButton(text="⌨️ Ввести номер", callback_data="force_type")],
    ])

def kb_admin():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="adm_refresh")],
        [InlineKeyboardButton(text="🗑 Очистить 'pending'", callback_data="adm_clean")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu")]
    ])

def kb_farm_settings(phone, mn, mx):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⏱ Мин: {mn} м [−]", callback_data=f"fd_min_dec_{phone}"),
         InlineKeyboardButton(text=f"[+]", callback_data=f"fd_min_inc_{phone}")],
        [InlineKeyboardButton(text=f"⏱ Макс: {mx} м [−]", callback_data=f"fd_max_dec_{phone}"),
         InlineKeyboardButton(text=f"[+]", callback_data=f"fd_max_inc_{phone}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="list")],
    ])

# --- BOT ROUTING ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class Form(StatesGroup):
    phone = State()

@dp.message(Command("start"))
async def start(msg: types.Message):
    await init_db()
    await msg.answer(
        "🏛 *WhatsApp Manager (Low-RAM Edition)*\n\n"
        "Оптимизирован для стабильной работы 1-5 аккаунтов.",
        reply_markup=kb_main(msg.from_user.id),
        parse_mode="Markdown"
    )

@dp.message(Command("admin"))
async def admin_cmd(msg: types.Message):
    if msg.from_user.id != ADMIN_ID: return
    await show_admin_panel(msg)

async def show_admin_panel(message_obj):
    tot, act, ban, sent = await db_get_stats()
    mem = psutil.virtual_memory()
    ram_usage = f"{mem.percent}% ({int(mem.available/1024/1024)}MB free)"
    txt = (
        f"👑 *АДМИН ПАНЕЛЬ*\n\n"
        f"📱 Всего: {tot} | 🟢 Актив: {act} | 🚫 Бан: {ban}\n"
        f"📨 Отправлено: {sent}\n"
        f"💾 Доступно RAM: {ram_usage}"
    )
    if isinstance(message_obj, types.CallbackQuery):
        await message_obj.message.edit_text(txt, reply_markup=kb_admin(), parse_mode="Markdown")
    else:
        await message_obj.answer(txt, reply_markup=kb_admin(), parse_mode="Markdown")

@dp.callback_query(F.data == "admin_panel")
async def admin_cb(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID: return await call.answer("Запрещено")
    await show_admin_panel(call)

@dp.callback_query(F.data == "adm_refresh")
async def adm_refresh(call: types.CallbackQuery):
    await show_admin_panel(call)

@dp.callback_query(F.data == "adm_clean")
async def adm_clean(call: types.CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM accounts WHERE status = 'pending'")
        await db.commit()
    await call.answer("Неактивные сессии очищены")
    await show_admin_panel(call)

@dp.callback_query(F.data == "menu")
async def back_menu(call: types.CallbackQuery):
    await call.message.edit_text("Главное меню", reply_markup=kb_main(call.from_user.id))

# --- FARM SETTINGS MENU ---
@dp.callback_query(F.data == "farm_settings_menu")
async def farm_settings_menu(call: types.CallbackQuery):
    phones = await db_get_active_phones()
    if not phones:
        return await call.answer("Нет активных аккаунтов")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📱 {p}", callback_data=f"farm_cfg_{p}")] for p in phones
    ] + [[InlineKeyboardButton(text="🔙 Назад", callback_data="menu")]])
    await call.message.edit_text("Выберите аккаунт:", reply_markup=kb)

@dp.callback_query(F.data.startswith("farm_cfg_"))
async def farm_cfg(call: types.CallbackQuery):
    phone = call.data.replace("farm_cfg_", "")
    mn, mx = await db_get_farm_delay(phone)
    await call.message.edit_text(
        f"⚙️ Задержка для `{phone}`: *{mn}–{mx} минут*",
        reply_markup=kb_farm_settings(phone, mn, mx),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("fd_min_inc_"))
async def fd_min_inc(call: types.CallbackQuery):
    phone = call.data.replace("fd_min_inc_", "")
    mn, mx = await db_get_farm_delay(phone)
    mn = min(mn + 1, mx)
    await db_set_farm_delay(phone, mn, mx)
    await call.message.edit_reply_markup(reply_markup=kb_farm_settings(phone, mn, mx))

@dp.callback_query(F.data.startswith("fd_min_dec_"))
async def fd_min_dec(call: types.CallbackQuery):
    phone = call.data.replace("fd_min_dec_", "")
    mn, mx = await db_get_farm_delay(phone)
    mn = max(1, mn - 1)
    await db_set_farm_delay(phone, mn, mx)
    await call.message.edit_reply_markup(reply_markup=kb_farm_settings(phone, mn, mx))

# --- ADD ACCOUNT FLOW ---
@dp.callback_query(F.data == "add")
async def add_flow(call: types.CallbackQuery, state: FSMContext):
    await call.message.edit_text("📞 Введите номер телефона (формат: 7XXXXXXXXXX):")
    await state.set_state(Form.phone)

@dp.message(Form.phone)
async def proc_phone(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    if len(phone) < 10:
        return await msg.answer("❌ Неверный номер.")
    
    dev = random.choice(DEVICES)
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO accounts (user_id, phone_number, user_agent, resolution, platform) VALUES (?, ?, ?, ?, ?)",
            (msg.from_user.id, phone, dev['ua'], dev['res'], dev['plat'])
        )
        await db.commit()

    await state.update_data(phone=phone)
    await msg.answer(
        f"🚀 Запускаю браузер для `{phone}`...\n\n"
        "1️⃣ Нажмите 🔗 *Вход по номеру*\n"
        "2️⃣ Нажмите ⌨️ *Ввести номер*\n"
        "3️⃣ Сделайте 📷 *СКРИН* для получения кода\n"
        "4️⃣ После входа нажмите ✅ *ГОТОВО*",
        reply_markup=kb_auth(),
        parse_mode="Markdown"
    )
    asyncio.create_task(bg_login_initial(msg.from_user.id, phone, dev))

async def bg_login_initial(uid, phone, dev):
    if uid in ACTIVE_DRIVERS:
        close_driver(ACTIVE_DRIVERS.pop(uid))
    try:
        driver = await asyncio.to_thread(get_driver, phone, dev['ua'], dev['res'], dev['plat'], dev['vendor'])
        ACTIVE_DRIVERS[uid] = driver
        driver.get("https://web.whatsapp.com/")
    except Exception as e:
        logger.error(f"Error starting driver for {phone}: {e}")

@dp.callback_query(F.data == "check")
async def check(call: types.CallbackQuery, state: FSMContext):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver:
        return await call.answer("Сессия не найдена. Нажмите 'Добавить' заново.")
    await call.answer("📷 Делаю скриншот...")
    try:
        scr = await asyncio.to_thread(driver.get_screenshot_as_png)
        await call.message.answer_photo(BufferedInputFile(scr, filename="screen.png"), caption="📱 Экран WhatsApp Web")
    except Exception as e:
        await call.message.answer(f"❌ Ошибка скрина: {e}")

@dp.callback_query(F.data == "force_link")
async def f_link(call: types.CallbackQuery):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    if not driver: return await call.answer("Сессия закрыта")
    await call.answer("Ищу кнопку...")
    try:
        xpaths = [
            "//span[contains(text(), 'Link with phone')]",
            "//span[contains(text(), 'Связать с номером')]",
            "//div[contains(text(), 'Link with phone')]",
            "//div[contains(text(), 'Связать с номером')]"
        ]
        for xp in xpaths:
            try:
                btn = driver.find_element(By.XPATH, xp)
                driver.execute_script("arguments[0].click();", btn)
                return await call.message.answer("✅ Нажато 'Вход по номеру'! Жмите ⌨️ Ввести номер.")
            except Exception:
                continue
        await call.message.answer("❌ Кнопка не найдена. Нажмите 📷 СКРИН.")
    except Exception as e:
        await call.message.answer(f"Ошибка: {e}")

@dp.callback_query(F.data == "force_type")
async def f_type(call: types.CallbackQuery, state: FSMContext):
    driver = ACTIVE_DRIVERS.get(call.from_user.id)
    data = await state.get_data()
    phone = data.get('phone', '')
    if not driver: return await call.answer("Сессия закрыта")

    await call.answer("Ввожу номер...")
    try:
        inp = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "input")))
        inp.send_keys(Keys.CONTROL + "a")
        inp.send_keys(Keys.BACKSPACE)
        for ch in f"+{phone}":
            inp.send_keys(ch)
            await asyncio.sleep(0.05)
        inp.send_keys(Keys.ENTER)
        await call.message.answer("✅ Номер введен! Нажмите 📷 СКРИН через 5 секунд.")
    except Exception as e:
        await call.message.answer(f"❌ Ошибка ввода: {e}")

@dp.callback_query(F.data == "done")
async def done(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    phone = data.get("phone")
    if not phone: return await call.answer("Нет номера")

    await db_update_status(phone, 'active')
    
    if call.from_user.id in ACTIVE_DRIVERS:
        drv = ACTIVE_DRIVERS.pop(call.from_user.id)
        await asyncio.to_thread(close_driver, drv)

    await call.message.answer(f"✅ Аккаунт `{phone}` успешно добавлен!", parse_mode="Markdown")
    asyncio.create_task(farm_solo_loop(phone))

@dp.callback_query(F.data == "list")
async def list_a(call: types.CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT phone_number, status, messages_sent, farm_min, farm_max FROM accounts") as cursor:
            all_d = await cursor.fetchall()

    if not all_d:
        return await call.message.answer("Аккаунтов нет", reply_markup=kb_main(call.from_user.id))

    txt = f"📊 *Аккаунты ({len(all_d)}):*\n\n"
    for p, s, m, mn, mx in all_d:
        icon = {"active": "🟢", "banned": "🚫", "pending": "🟡"}.get(s, "⚪")
        txt += f"{icon} `{p}` | 📨 {m} | ⏱ {mn}-{mx}м\n"

    await call.message.answer(txt, reply_markup=kb_main(call.from_user.id), parse_mode="Markdown")

# --- FARM ENGINE ---
async def send_self_message(driver, phone):
    try:
        wait = WebDriverWait(driver, 15)
        driver.get(f"https://web.whatsapp.com/send?phone={phone}&type=phone_number&app_absent=1")
        await asyncio.sleep(random.uniform(3, 5))

        inp = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@contenteditable='true'][@data-tab='10']")))
        text = random.choice(SELF_MESSAGES) if random.random() < 0.5 else fake.sentence()
        
        await human_type(inp, text)
        await asyncio.sleep(1)
        inp.send_keys(Keys.ENTER)

        await db_inc_msg(phone)
        logger.info(f"Сообщение отправлено: {phone}")
        return True
    except Exception as e:
        logger.error(f"Ошибка отправки {phone}: {e}")
        return False

async def farm_worker_solo(phone):
    while is_memory_critical():
        logger.warning("Мало RAM! Ожидание освобождения ресурсов...")
        await asyncio.sleep(30)

    async with BROWSER_SEMAPHORE:
        acc = await db_get_acc(phone)
        if not acc: return
        
        driver = None
        try:
            logger.info(f"Старт фарма: {phone}")
            driver = await asyncio.to_thread(get_driver, phone, acc[5], acc[6] or "1280,800", acc[7] or "Win32")
            driver.get("https://web.whatsapp.com/")

            wait = WebDriverWait(driver, 45)
            wait.until(EC.presence_of_element_located((By.ID, "pane-side")))

            if await check_ban_status(driver, phone):
                return

            await send_self_message(driver, phone)
            await asyncio.sleep(3)

        except Exception as e:
            logger.error(f"Ошибка цикла фарма {phone}: {e}")
        finally:
            if driver:
                await asyncio.to_thread(close_driver, driver)

async def farm_solo_loop(phone):
    logger.info(f"Цикл фарма запущен для {phone}")
    while True:
        acc = await db_get_acc(phone)
        if not acc or acc[3] != 'active':
            break

        mn, mx = await db_get_farm_delay(phone)
        await farm_worker_solo(phone)

        delay_sec = random.randint(mn * 60, mx * 60)
        logger.info(f"Сон {phone}: {delay_sec // 60} мин")
        await asyncio.sleep(delay_sec)

async def start_all_farm_loops():
    await asyncio.sleep(3)
    phones = await db_get_active_phones()
    for phone in phones:
        asyncio.create_task(farm_solo_loop(phone))
        await asyncio.sleep(5)

async def main():
    await init_db()
    asyncio.create_task(start_all_farm_loops())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
