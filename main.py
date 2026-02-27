import asyncio
import os
import logging
import sqlite3
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

# — SELENIUM —

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys

# — КОНФИГУРАЦИЯ —

BOT_TOKEN = os.environ.get(“BOT_TOKEN”)
try:
ADMIN_ID = int(os.environ.get(“ADMIN_ID”, 0))
except:
ADMIN_ID = 0

# =============================================

# НАСТРОЙКИ ФАРМА (МОЖНО МЕНЯТЬ ЧЕРЕЗ БОТА)

# =============================================

FARM_DELAY_MIN = 1        # мин. минут между сообщениями (по умолчанию)
FARM_DELAY_MAX = 3        # макс. минут между сообщениями (по умолчанию)
BROWSER_SEMAPHORE = asyncio.Semaphore(4)
DB_NAME = ‘bot_database.db’
SESSIONS_DIR = “/app/sessions”

ACTIVE_DRIVERS = {}
fake = Faker(‘ru_RU’)

# Хранение настроек задержки (в минутах) для каждого номера

FARM_SETTINGS = {}  # {phone: {“min”: 1, “max”: 3}}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(**name**)

# =============================================

# РАСШИРЕННАЯ БАЗА УСТРОЙСТВ

# =============================================

DEVICES = [
# — Windows Chrome —
{
“ua”: “Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36”,
“res”: “1920,1080”, “plat”: “Win32”, “vendor”: “Google Inc.”, “name”: “Chrome 124 / Win10”
},
{
“ua”: “Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36”,
“res”: “1920,1200”, “plat”: “Win32”, “vendor”: “Google Inc.”, “name”: “Chrome 123 / Win11”
},
# — Windows Edge —
{
“ua”: “Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0”,
“res”: “1920,1080”, “plat”: “Win32”, “vendor”: “Microsoft”, “name”: “Edge 124 / Win10”
},
{
“ua”: “Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.2365.92”,
“res”: “2560,1440”, “plat”: “Win32”, “vendor”: “Microsoft”, “name”: “Edge 122 / Win10 2K”
},
# — MacOS Chrome —
{
“ua”: “Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36”,
“res”: “1440,900”, “plat”: “MacIntel”, “vendor”: “Google Inc.”, “name”: “Chrome 124 / Mac14”
},
{
“ua”: “Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36”,
“res”: “2560,1600”, “plat”: “MacIntel”, “vendor”: “Google Inc.”, “name”: “Chrome 120 / Mac13 Retina”
},
# — MacOS Safari —
{
“ua”: “Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15”,
“res”: “1440,900”, “plat”: “MacIntel”, “vendor”: “Apple Computer, Inc.”, “name”: “Safari 17 / Mac14”
},
# — Linux Chrome —
{
“ua”: “Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36”,
“res”: “1366,768”, “plat”: “Linux x86_64”, “vendor”: “Google Inc.”, “name”: “Chrome 122 / Linux”
},
# — Android Chrome (мобильный) —
{
“ua”: “Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Mobile Safari/537.36”,
“res”: “412,915”, “plat”: “Linux armv8l”, “vendor”: “Google Inc.”, “name”: “Chrome Mobile / Samsung S21”
},
{
“ua”: “Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36”,
“res”: “393,851”, “plat”: “Linux armv8l”, “vendor”: “Google Inc.”, “name”: “Chrome Mobile / Pixel 8”
},
]

# Тексты для авто-сообщений самому себе

SELF_MESSAGES = [
“Не забыть купить: хлеб, молоко, яйца”,
“Идея: попробовать новый ресторан на Абая”,
“Позвонить маме вечером”,
“Сделать зарядку утром”,
“Оплатить интернет до конца недели”,
“Записать: пароль от нового сервиса”,
“Встреча с Азаматом в пятницу в 15:00”,
“Напомнить себе — купить подарок на день рождения”,
“Проверить почту сегодня”,
“Заказать такси заранее”,
]

SELF_BIOS = [
“Живу в моменте 🌙”,
“Алматы | Работа • Спорт • Кофе”,
“Просто хороший человек ☀️”,
“На связи не всегда, но отвечу”,
“Мечтаю о горах и тишине 🏔”,
“Путешествия — смысл жизни ✈️”,
“Не спешу, но двигаюсь вперёд”,
“Казахстан 🇰🇿 | IT • Спорт”,
“Тихий режим включён 🎧”,
“Сначала кофе, потом всё остальное ☕”,
]

# — DATABASE —

def init_db():
with sqlite3.connect(DB_NAME) as conn:
conn.execute(’’‘CREATE TABLE IF NOT EXISTS accounts
(id INTEGER PRIMARY KEY AUTOINCREMENT,
user_id INTEGER, phone_number TEXT UNIQUE,
status TEXT DEFAULT ‘pending’,
messages_sent INTEGER DEFAULT 0,
user_agent TEXT, resolution TEXT, platform TEXT,
ban_reason TEXT, last_active TIMESTAMP,
farm_min INTEGER DEFAULT 1,
farm_max INTEGER DEFAULT 3)’’’)
# Добавляем колонки если нет (миграция)
try:
conn.execute(“ALTER TABLE accounts ADD COLUMN farm_min INTEGER DEFAULT 1”)
except: pass
try:
conn.execute(“ALTER TABLE accounts ADD COLUMN farm_max INTEGER DEFAULT 3”)
except: pass
conn.commit()

def db_get_acc(phone):
with sqlite3.connect(DB_NAME) as conn:
return conn.execute(“SELECT * FROM accounts WHERE phone_number = ?”, (phone,)).fetchone()

def db_get_active_phones():
with sqlite3.connect(DB_NAME) as conn:
return [row[0] for row in conn.execute(“SELECT phone_number FROM accounts WHERE status = ‘active’”).fetchall()]

def db_update_status(phone, status, reason=None):
with sqlite3.connect(DB_NAME) as conn:
conn.execute(“UPDATE accounts SET status = ?, ban_reason = ? WHERE phone_number = ?”, (status, reason, phone))

def db_inc_msg(phone):
with sqlite3.connect(DB_NAME) as conn:
conn.execute(“UPDATE accounts SET messages_sent = messages_sent + 1, last_active = ? WHERE phone_number = ?”, (datetime.now(), phone))

def db_set_farm_delay(phone, min_m, max_m):
with sqlite3.connect(DB_NAME) as conn:
conn.execute(“UPDATE accounts SET farm_min = ?, farm_max = ? WHERE phone_number = ?”, (min_m, max_m, phone))

def db_get_farm_delay(phone):
with sqlite3.connect(DB_NAME) as conn:
row = conn.execute(“SELECT farm_min, farm_max FROM accounts WHERE phone_number = ?”, (phone,)).fetchone()
if row: return row[0] or 1, row[1] or 3
return 1, 3

def db_get_stats():
with sqlite3.connect(DB_NAME) as conn:
total = conn.execute(“SELECT count(*) FROM accounts”).fetchone()[0]
active = conn.execute(“SELECT count(*) FROM accounts WHERE status = ‘active’”).fetchone()[0]
banned = conn.execute(“SELECT count(*) FROM accounts WHERE status = ‘banned’”).fetchone()[0]
sent = conn.execute(“SELECT sum(messages_sent) FROM accounts”).fetchone()[0] or 0
return total, active, banned, sent

# — MEMORY GUARD —

def is_memory_critical():
mem = psutil.virtual_memory()
return (mem.available / 1024 / 1024) < 200

# — DRIVER FACTORY —

def get_driver(phone):
acc = db_get_acc(phone)
if acc and acc[5]:
ua, res, plat = acc[5], acc[6], acc[7]
vendor = “Google Inc.”  # fallback
# Попробуем найти vendor по ua
for d in DEVICES:
if d[‘ua’] == ua:
vendor = d.get(‘vendor’, ‘Google Inc.’)
break
else:
dev = random.choice(DEVICES)
ua, res, plat, vendor = dev[‘ua’], dev[‘res’], dev[‘plat’], dev.get(‘vendor’, ‘Google Inc.’)
with sqlite3.connect(DB_NAME) as conn:
conn.execute(“UPDATE accounts SET user_agent=?, resolution=?, platform=? WHERE phone_number=?”, (ua, res, plat, phone))

```
opt = Options()
opt.binary_location = "/usr/bin/google-chrome"
opt.add_argument("--headless=new")
opt.add_argument("--no-sandbox")
opt.add_argument("--disable-dev-shm-usage")
opt.add_argument(f"--window-size={res}")
opt.add_argument("--hide-scrollbars")

# STEALTH + KZ
opt.add_argument("--lang=ru-KZ,ru,kk")
opt.add_argument(f"--user-agent={ua}")
opt.add_argument("--disable-blink-features=AutomationControlled")
opt.add_experimental_option("excludeSwitches", ["enable-automation"])
opt.add_experimental_option('useAutomationExtension', False)
opt.add_argument(f"--user-data-dir={os.path.join(SESSIONS_DIR, str(phone))}")

driver = webdriver.Chrome(service=Service("/usr/local/bin/chromedriver"), options=opt)

# JS INJECTION: Timezone Almaty + Anti-Detect + Vendor
tz_offset = -300  # UTC+5 Almaty
driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
    "source": f"""
    // Anti-webdriver
    Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}});
    
    // Platform spoofing
    Object.defineProperty(navigator, 'platform', {{get: () => '{plat}'}});
    
    // Vendor spoofing
    Object.defineProperty(navigator, 'vendor', {{get: () => '{vendor}'}});
    
    // Language KZ
    Object.defineProperty(navigator, 'language', {{get: () => 'ru-KZ'}});
    Object.defineProperty(navigator, 'languages', {{get: () => ['ru-KZ', 'ru', 'kk', 'en']}});
    
    // Timezone: Asia/Almaty (UTC+5)
    const origDateTimeFormat = Intl.DateTimeFormat;
    Intl.DateTimeFormat = function(locale, options) {{
        options = options || {{}};
        if (!options.timeZone) options.timeZone = 'Asia/Almaty';
        return new origDateTimeFormat(locale, options);
    }};
    Intl.DateTimeFormat.prototype = origDateTimeFormat.prototype;
    Intl.DateTimeFormat.supportedLocalesOf = origDateTimeFormat.supportedLocalesOf;
    
    // Date timezone
    const _toLocaleString = Date.prototype.toLocaleString;
    Date.prototype.toLocaleString = function(locale, options) {{
        return _toLocaleString.call(this, locale || 'ru-KZ', {{ timeZone: 'Asia/Almaty', ...options }});
    }};
    
    // Hide automation in chrome
    window.chrome = {{ runtime: {{}} }};
    
    // Screen resolution
    const [w, h] = '{res}'.split(',');
    Object.defineProperty(screen, 'width', {{get: () => parseInt(w)}});
    Object.defineProperty(screen, 'height', {{get: () => parseInt(h)}});
    Object.defineProperty(screen, 'availWidth', {{get: () => parseInt(w)}});
    Object.defineProperty(screen, 'availHeight', {{get: () => parseInt(h) - 40}});
    """
})

# GEO: Алматы
driver.execute_cdp_cmd("Emulation.setGeolocationOverride", {
    "latitude": 43.2389, "longitude": 76.8897, "accuracy": 50
})

# Timezone через CDP
driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {
    "timezoneId": "Asia/Almaty"
})

return driver
```

# — HUMAN ACTIONS —

async def human_type(element, text):
for char in text:
if random.random() < 0.04:
element.send_keys(random.choice(string.ascii_lowercase))
await asyncio.sleep(0.1)
element.send_keys(Keys.BACKSPACE)
element.send_keys(char)
await asyncio.sleep(random.uniform(0.04, 0.15))

async def check_ban_status(driver, phone):
try:
page_text = driver.find_element(By.TAG_NAME, “body”).text
if “account is not allowed” in page_text or “spam” in page_text.lower():
db_update_status(phone, ‘banned’, ‘PermBan’)
return “BAN”
return False
except:
return False

# — KEYBOARDS —

def kb_main(uid):
kb = [
[InlineKeyboardButton(text=“➕ Добавить Аккаунт”, callback_data=“add”)],
[InlineKeyboardButton(text=“📂 Мои Аккаунты”, callback_data=“list”)],
[InlineKeyboardButton(text=“⚙️ Настройки фарма”, callback_data=“farm_settings_menu”)],
]
if uid == ADMIN_ID:
kb.append([InlineKeyboardButton(text=“👑 Админ Панель”, callback_data=“admin_panel”)])
return InlineKeyboardMarkup(inline_keyboard=kb)

def kb_auth():
return InlineKeyboardMarkup(inline_keyboard=[
[InlineKeyboardButton(text=“📷 СКРИН (ПОЛНЫЙ)”, callback_data=“check”),
InlineKeyboardButton(text=“✅ ГОТОВО”, callback_data=“done”)],
[InlineKeyboardButton(text=“🔗 Вход по номеру (AUTO)”, callback_data=“force_link”)],
[InlineKeyboardButton(text=“⌨️ Ввести номер (AUTO)”, callback_data=“force_type”)],
])

def kb_admin():
return InlineKeyboardMarkup(inline_keyboard=[
[InlineKeyboardButton(text=“🔄 Обновить статусы”, callback_data=“adm_refresh”)],
[InlineKeyboardButton(text=“🗑 Очистить ‘pending’”, callback_data=“adm_clean”)],
[InlineKeyboardButton(text=“🔙 Назад”, callback_data=“menu”)]
])

def kb_farm_settings(phone):
mn, mx = db_get_farm_delay(phone)
return InlineKeyboardMarkup(inline_keyboard=[
[InlineKeyboardButton(text=f”⏱ Мин: {mn} мин  [−]”, callback_data=f”fd_min_dec_{phone}”),
InlineKeyboardButton(text=f”[+]”, callback_data=f”fd_min_inc_{phone}”)],
[InlineKeyboardButton(text=f”⏱ Макс: {mx} мин  [−]”, callback_data=f”fd_max_dec_{phone}”),
InlineKeyboardButton(text=f”[+]”, callback_data=f”fd_max_inc_{phone}”)],
[InlineKeyboardButton(text=“🔙 Назад”, callback_data=“list”)],
])

# — BOT —

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

class Form(StatesGroup):
phone = State()
farm_settings_phone = State()

@dp.message(Command(“start”))
async def start(msg: types.Message):
init_db()
await msg.answer(
“🏛 *WhatsApp Imperator v17.0*\n\n”
“✅ Маскировка под Алматы (IP, Timezone, GEO)\n”
“✅ 10 типов устройств (Edge, Chrome, Safari, Mobile)\n”
“✅ Авто-смена bio и сообщения самому себе\n”
“✅ Настройка задержки (мин/макс минут)\n\n”
“Жми кнопку ниже:”,
reply_markup=kb_main(msg.from_user.id),
parse_mode=“Markdown”
)

@dp.message(Command(“admin”))
async def admin_cmd(msg: types.Message):
if msg.from_user.id != ADMIN_ID: return
await show_admin_panel(msg)

async def show_admin_panel(message_obj):
tot, act, ban, sent = db_get_stats()
mem = psutil.virtual_memory()
ram_usage = f”{mem.percent}% ({int(mem.available/1024/1024)}MB free)”
txt = (
f”👑 *АДМИН ПАНЕЛЬ*\n\n”
f”📱 Всего аккаунтов: {tot}\n”
f”🟢 Активных: {act}\n”
f”🚫 В бане: {ban}\n”
f”📨 Отправлено: {sent}\n”
f”💾 RAM: {ram_usage}”
)
if isinstance(message_obj, types.CallbackQuery):
await message_obj.message.edit_text(txt, reply_markup=kb_admin(), parse_mode=“Markdown”)
else:
await message_obj.answer(txt, reply_markup=kb_admin(), parse_mode=“Markdown”)

@dp.callback_query(F.data == “admin_panel”)
async def admin_cb(call: types.CallbackQuery):
if call.from_user.id != ADMIN_ID: return await call.answer(“Доступ запрещен”)
await show_admin_panel(call)

@dp.callback_query(F.data == “adm_refresh”)
async def adm_refresh(call: types.CallbackQuery):
await show_admin_panel(call)

@dp.callback_query(F.data == “adm_clean”)
async def adm_clean(call: types.CallbackQuery):
with sqlite3.connect(DB_NAME) as conn:
conn.execute(“DELETE FROM accounts WHERE status = ‘pending’”)
await call.answer(“Мусор удален”)
await show_admin_panel(call)

@dp.callback_query(F.data == “menu”)
async def back_menu(call: types.CallbackQuery):
await call.message.edit_text(“Главное меню”, reply_markup=kb_main(call.from_user.id))

# — НАСТРОЙКИ ФАРМА —

@dp.callback_query(F.data == “farm_settings_menu”)
async def farm_settings_menu(call: types.CallbackQuery):
phones = db_get_active_phones()
if not phones:
return await call.answer(“Нет активных аккаунтов”)
kb = InlineKeyboardMarkup(inline_keyboard=[
[InlineKeyboardButton(text=f”📱 {p}”, callback_data=f”farm_cfg_{p}”)]
for p in phones
] + [[InlineKeyboardButton(text=“🔙 Назад”, callback_data=“menu”)]])
await call.message.edit_text(“Выбери аккаунт для настройки задержки:”, reply_markup=kb)

@dp.callback_query(F.data.startswith(“farm_cfg_”))
async def farm_cfg(call: types.CallbackQuery):
phone = call.data.replace(“farm_cfg_”, “”)
mn, mx = db_get_farm_delay(phone)
await call.message.edit_text(
f”⚙️ Настройка задержки для `{phone}`\n\n”
f”Текущий диапазон: *{mn}–{mx} минут*\n\n”
f”Бот будет отправлять 1 сообщение каждые {mn}–{mx} мин.”,
reply_markup=kb_farm_settings(phone),
parse_mode=“Markdown”
)

@dp.callback_query(F.data.startswith(“fd_min_inc_”))
async def fd_min_inc(call: types.CallbackQuery):
phone = call.data.replace(“fd_min_inc_”, “”)
mn, mx = db_get_farm_delay(phone)
mn = min(mn + 1, mx)
db_set_farm_delay(phone, mn, mx)
await call.message.edit_reply_markup(reply_markup=kb_farm_settings(phone))
await call.answer(f”Мин: {mn}”)

@dp.callback_query(F.data.startswith(“fd_min_dec_”))
async def fd_min_dec(call: types.CallbackQuery):
phone = call.data.replace(“fd_min_dec_”, “”)
mn, mx = db_get_farm_delay(phone)
mn = max(1, mn - 1)
db_set_farm_delay(phone, mn, mx)
await call.message.edit_reply_markup(reply_markup=kb_farm_settings(phone))
await call.answer(f”Мин: {mn}”)

@dp.callback_query(F.data.startswith(“fd_max_inc_”))
async def fd_max_inc(call: types.CallbackQuery):
phone = call.data.replace(“fd_max_inc_”, “”)
mn, mx = db_get_farm_delay(phone)
mx = min(mx + 1, 120)
db_set_farm_delay(phone, mn, mx)
await call.message.edit_reply_markup(reply_markup=kb_farm_settings(phone))
await call.answer(f”Макс: {mx}”)

@dp.callback_query(F.data.startswith(“fd_max_dec_”))
async def fd_max_dec(call: types.CallbackQuery):
phone = call.data.replace(“fd_max_dec_”, “”)
mn, mx = db_get_farm_delay(phone)
mx = max(mn, mx - 1)
db_set_farm_delay(phone, mn, mx)
await call.message.edit_reply_markup(reply_markup=kb_farm_settings(phone))
await call.answer(f”Макс: {mx}”)

# — ADD ACCOUNT FLOW —

@dp.callback_query(F.data == “add”)
async def add_flow(call: types.CallbackQuery, state: FSMContext):
await call.message.edit_text(“📞 Введите номер телефона (формат: 7XXXXXXXXXX):”)
await state.set_state(Form.phone)

@dp.message(Form.phone)
async def proc_phone(msg: types.Message, state: FSMContext):
phone = re.sub(r’\D’, ‘’, msg.text)
if len(phone) < 10:
return await msg.answer(“❌ Неверный номер. Попробуй ещё раз:”)
with sqlite3.connect(DB_NAME) as conn:
conn.execute(“INSERT OR IGNORE INTO accounts (user_id, phone_number) VALUES (?, ?)”, (msg.from_user.id, phone))
await state.update_data(phone=phone)
await msg.answer(
f”🚀 Запускаю браузер для `{phone}`…\n\n”
“1️⃣ Жди 10–15 сек\n”
“2️⃣ Нажми 📷 СКРИН — увидишь QR или поле ввода\n”
“3️⃣ Используй кнопки AUTO для авто-входа\n”
“4️⃣ Когда вошёл — жми ✅ ГОТОВО”,
reply_markup=kb_auth(),
parse_mode=“Markdown”
)
asyncio.create_task(bg_login_initial(msg.from_user.id, phone))

async def bg_login_initial(uid, phone):
async with BROWSER_SEMAPHORE:
try:
driver = await asyncio.to_thread(get_driver, phone)
ACTIVE_DRIVERS[uid] = driver
driver.get(“https://web.whatsapp.com/”)
await asyncio.sleep(900)
except Exception as e:
logger.error(f”bg_login error: {e}”)
finally:
if uid in ACTIVE_DRIVERS:
try: ACTIVE_DRIVERS.pop(uid).quit()
except: pass

# — СКРИН (ПОЛНЫЙ ЭКРАН) —

@dp.callback_query(F.data == “check”)
async def check(call: types.CallbackQuery, state: FSMContext):
data = await state.get_data()
phone = data.get(“phone”)
driver = ACTIVE_DRIVERS.get(call.from_user.id)

```
temp_driver = False
if not driver:
    if not phone: return await call.answer("Сначала введи номер")
    if is_memory_critical(): return await call.answer("⚠️ Сервер перегружен, подожди...")
    await call.answer("♻️ Восстанавливаю браузер...")
    try:
        driver = await asyncio.to_thread(get_driver, phone)
        driver.get("https://web.whatsapp.com/")
        await asyncio.sleep(10)
        temp_driver = True
    except:
        return await call.answer("❌ Ошибка запуска браузера")
else:
    await call.answer("📷 Делаю скрин...")

try:
    # Полноэкранный скрин страницы (не только viewport)
    scr = await asyncio.to_thread(driver.get_screenshot_as_png)

    # Ищем код привязки
    code_text = ""
    try:
        # Код из блока привязки по номеру
        el = driver.find_element(By.XPATH, "//div[@aria-details='link-device-phone-number-code']")
        code_text = f"\n🔑 *КОД: {el.text}*"
    except:
        pass

    # Статус страницы
    page_info = ""
    try:
        title = driver.title
        page_info = f"\n🌐 {title}"
    except:
        pass

    caption = f"📱 Экран WhatsApp Web{page_info}{code_text}"
    await call.message.answer_photo(
        BufferedInputFile(scr, filename="whatsapp_screen.png"),
        caption=caption,
        parse_mode="Markdown"
    )
except Exception as e:
    await call.message.answer(f"❌ Ошибка скрина: {e}")
finally:
    if temp_driver:
        try: driver.quit()
        except: pass
```

# — FORCE LINK —

@dp.callback_query(F.data == “force_link”)
async def f_link(call: types.CallbackQuery, state: FSMContext):
data = await state.get_data()
phone = data.get(“phone”)
driver = ACTIVE_DRIVERS.get(call.from_user.id)

```
resurrected = False
if not driver:
    if not phone: return await call.answer("Нет номера")
    await call.answer("♻️ Запускаю браузер...")
    try:
        driver = await asyncio.to_thread(get_driver, phone)
        driver.get("https://web.whatsapp.com/")
        await asyncio.sleep(8)
        resurrected = True
    except:
        return await call.answer("❌ Ошибка запуска")
else:
    await call.answer("🔍 Ищу кнопку...")

try:
    xpaths = [
        "//span[contains(text(), 'Link with phone')]",
        "//span[contains(text(), 'Связать с номером')]",
        "//span[contains(text(), 'Link with phone number')]",
        "//div[contains(text(), 'Link with phone')]",
        "//div[contains(text(), 'Связать с номером')]",
        "//button[contains(., 'phone')]",
    ]
    found = False
    for xp in xpaths:
        try:
            btn = driver.find_element(By.XPATH, xp)
            driver.execute_script("arguments[0].scrollIntoView(true);", btn)
            driver.execute_script("arguments[0].click();", btn)
            found = True
            break
        except:
            continue

    if found:
        await call.message.answer("✅ Нажал кнопку 'Вход по номеру'!\nТеперь жми ⌨️ Ввести номер.")
    else:
        await call.message.answer("❌ Кнопка не найдена. Сделай 📷 СКРИН и посмотри что на экране.")
except Exception as e:
    await call.message.answer(f"Ошибка: {e}")
finally:
    if resurrected:
        ACTIVE_DRIVERS[call.from_user.id] = driver
        asyncio.create_task(auto_close(call.from_user.id, driver))
```

async def auto_close(uid, driver):
await asyncio.sleep(300)
try: driver.quit()
except: pass
if uid in ACTIVE_DRIVERS:
try: del ACTIVE_DRIVERS[uid]
except: pass

# — FORCE TYPE —

@dp.callback_query(F.data == “force_type”)
async def f_type(call: types.CallbackQuery, state: FSMContext):
driver = ACTIVE_DRIVERS.get(call.from_user.id)
data = await state.get_data()

```
if not driver:
    return await call.message.answer("⚠️ Браузер закрыт. Сначала нажми 🔗 Вход по номеру.")

await call.answer("⌨️ Печатаю номер...")
try:
    # Ждём поле ввода
    inp = WebDriverWait(driver, 12).until(
        EC.presence_of_element_located((By.TAG_NAME, "input"))
    )
    driver.execute_script("arguments[0].value = '';", inp)
    inp.send_keys(Keys.CONTROL + "a")
    inp.send_keys(Keys.BACKSPACE)
    await asyncio.sleep(0.5)

    phone = data.get('phone', '')
    for ch in f"+{phone}":
        inp.send_keys(ch)
        await asyncio.sleep(random.uniform(0.05, 0.12))

    await asyncio.sleep(0.5)
    inp.send_keys(Keys.ENTER)
    await call.message.answer(f"✅ Ввёл `+{phone}`!\nЖди 3–5 сек и жми 📷 СКРИН — там будет код.", parse_mode="Markdown")
except Exception as e:
    await call.message.answer(f"❌ Не нашёл поле ввода.\nОшибка: {e}\n\nСделай 📷 СКРИН.")
```

# — DONE —

@dp.callback_query(F.data == “done”)
async def done(call: types.CallbackQuery, state: FSMContext):
data = await state.get_data()
phone = data.get(“phone”)
if not phone:
return await call.answer(“Нет номера в сессии”)

```
with sqlite3.connect(DB_NAME) as conn:
    conn.execute("UPDATE accounts SET status = 'active' WHERE phone_number = ?", (phone,))

if call.from_user.id in ACTIVE_DRIVERS:
    try: ACTIVE_DRIVERS.pop(call.from_user.id).quit()
    except: pass

await call.message.answer(
    f"✅ Аккаунт `{phone}` добавлен и активен!\n\n"
    f"Фарм запущен автоматически 🚀",
    parse_mode="Markdown"
)
asyncio.create_task(farm_solo_loop(phone))
```

# — LIST —

@dp.callback_query(F.data == “list”)
async def list_a(call: types.CallbackQuery):
with sqlite3.connect(DB_NAME) as conn:
all_d = conn.execute(“SELECT phone_number, status, messages_sent, farm_min, farm_max FROM accounts”).fetchall()

```
if not all_d:
    return await call.message.answer("Аккаунтов нет", reply_markup=kb_main(call.from_user.id))

txt = f"📊 *Аккаунты ({len(all_d)}):*\n\n"
for p, s, m, mn, mx in all_d:
    icon = {"active": "🟢", "banned": "🚫", "pending": "🟡"}.get(s, "⚪")
    txt += f"{icon} `{p}` | 📨{m} | ⏱{mn}-{mx}м\n"

await call.message.answer(txt, reply_markup=kb_main(call.from_user.id), parse_mode="Markdown")
```

# =============================================

# FARM ENGINE — ТОЛЬКО САМОМУ СЕБЕ

# =============================================

async def change_bio(driver, phone):
“”“Меняем статус/bio аккаунта”””
try:
wait = WebDriverWait(driver, 10)
# Открываем профиль
try:
profile_btn = driver.find_element(By.XPATH, “//header//img[@role=‘button’] | //header//div[@data-icon=‘menu’]”)
driver.execute_script(“arguments[0].click();”, profile_btn)
except:
# Через меню
try:
menu = driver.find_element(By.XPATH, “//div[@data-icon=‘menu’] | //span[@data-icon=‘menu’]”)
driver.execute_script(“arguments[0].click();”, menu)
await asyncio.sleep(1)
settings = driver.find_element(By.XPATH, “//div[contains(text(),‘Settings’)] | //div[contains(text(),‘Настройки’)]”)
settings.click()
except:
return False

```
    await asyncio.sleep(2)

    # Ищем кнопку редактировать bio/about
    edit_btns = driver.find_elements(By.XPATH, "//span[@data-icon='pencil']")
    if len(edit_btns) >= 2:
        edit_btns[1].click()
        await asyncio.sleep(1.5)
        act = driver.switch_to.active_element
        act.send_keys(Keys.CONTROL + "a")
        act.send_keys(Keys.BACKSPACE)
        new_bio = random.choice(SELF_BIOS)
        await human_type(act, new_bio)
        await asyncio.sleep(0.5)
        act.send_keys(Keys.ENTER)
        await asyncio.sleep(1)
        # Возвращаемся
        try:
            back = driver.find_element(By.XPATH, "//span[@data-icon='back'] | //span[@data-icon='arrow-back']")
            back.click()
        except:
            pass
        logger.info(f"BIO changed for {phone}: {new_bio}")
        return True
except Exception as e:
    logger.warning(f"Bio change error {phone}: {e}")
return False
```

async def send_self_message(driver, phone):
“”“Отправляем сообщение самому себе”””
try:
wait = WebDriverWait(driver, 20)
driver.get(f”https://web.whatsapp.com/send?phone={phone}&type=phone_number&app_absent=1”)
await asyncio.sleep(random.uniform(3, 6))

```
    inp = wait.until(EC.presence_of_element_located(
        (By.XPATH, "//div[@contenteditable='true'][@data-tab='10']")
    ))

    # Выбираем случайное сообщение или генерируем
    r = random.random()
    if r < 0.4:
        text = random.choice(SELF_MESSAGES)
    elif r < 0.7:
        text = f"Заметка {datetime.now().strftime('%d.%m')}: {fake.sentence()}"
    else:
        text = fake.sentence()

    # Имитируем человека: пауза перед вводом
    await asyncio.sleep(random.uniform(1, 3))
    await human_type(inp, text)
    await asyncio.sleep(random.uniform(0.5, 1.5))
    inp.send_keys(Keys.ENTER)

    db_inc_msg(phone)
    logger.info(f"SELF MSG sent: {phone} -> '{text}'")
    return True
except Exception as e:
    logger.error(f"self_msg error {phone}: {e}")
    return False
```

async def farm_worker_solo(phone):
“”“Один цикл фарма: биография + сообщение себе”””
while is_memory_critical():
await asyncio.sleep(15)

```
async with BROWSER_SEMAPHORE:
    driver = None
    try:
        logger.info(f"FARM START: {phone}")
        driver = await asyncio.to_thread(get_driver, phone)
        driver.get("https://web.whatsapp.com/")

        wait = WebDriverWait(driver, 60)
        try:
            wait.until(EC.presence_of_element_located((By.ID, "pane-side")))
        except:
            # Проверяем бан/QR
            status = await check_ban_status(driver, phone)
            if status:
                return
            # Пробуем обновить
            driver.refresh()
            await asyncio.sleep(15)
            try:
                wait.until(EC.presence_of_element_located((By.ID, "pane-side")))
            except:
                return

        # Проверка бана
        ban = await check_ban_status(driver, phone)
        if ban:
            return

        # 40% шанс сменить bio
        if random.random() < 0.4:
            await change_bio(driver, phone)
            await asyncio.sleep(random.uniform(2, 5))

        # Отправляем себе сообщение
        await send_self_message(driver, phone)

        await asyncio.sleep(3)

    except Exception as e:
        logger.error(f"FARM ERR {phone}: {e}")
    finally:
        if driver:
            try: driver.quit()
            except: pass
```

async def farm_solo_loop(phone):
“”“Бесконечный цикл фарма для одного аккаунта”””
logger.info(f”🌱 SOLO LOOP started: {phone}”)
while True:
acc = db_get_acc(phone)
if not acc or acc[3] != ‘active’:
logger.info(f”Account {phone} not active, stopping loop”)
break

```
    mn, mx = db_get_farm_delay(phone)
    await farm_worker_solo(phone)

    # Задержка в минутах
    delay_sec = random.randint(mn * 60, mx * 60)
    logger.info(f"SLEEP {phone}: {delay_sec}s ({delay_sec//60}m)")
    await asyncio.sleep(delay_sec)
```

async def start_all_farm_loops():
“”“При старте бота запускаем фарм для всех активных аккаунтов”””
await asyncio.sleep(5)  # Даём боту запуститься
phones = db_get_active_phones()
for phone in phones:
asyncio.create_task(farm_solo_loop(phone))
await asyncio.sleep(random.randint(5, 15))  # Рассредоточенный старт
logger.info(f”🔥 Started {len(phones)} farm loops”)

async def main():
init_db()
asyncio.create_task(start_all_farm_loops())
await dp.start_polling(bot)

if **name** == “**main**”:
asyncio.run(main())
