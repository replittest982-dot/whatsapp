“””
⚡ IMPERATOR v17 — ULTRA SLIM

- Авто-вход (QR или номер, сам определяет)
- Авто-фарм (пишет себе)
- No-detect + Human typing
- Авто-смена имени/био
- 8GB RAM оптимизация (1 браузер, headless)
  “””

import asyncio, os, logging, sqlite3, random, time
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramBadRequest
from faker import Faker
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# ── CONFIG ──────────────────────────────────────────────

BOT_TOKEN  = os.environ.get(“BOT_TOKEN”, “”)
ADMIN_ID   = int(os.environ.get(“ADMIN_ID”, 0))
DB         = “imp17.db”
SESS_DIR   = os.path.join(os.getcwd(), “sessions”)
os.makedirs(SESS_DIR, exist_ok=True)

FARM_MIN   = 5 * 60    # мин. пауза между сообщениями (сек)
FARM_MAX   = 15 * 60   # макс. пауза

FAKE_NAMES = [“Алексей”, “Максим”, “Иван”, “Дмитрий”, “Сергей”,
“Николай”, “Артём”, “Владимир”, “Андрей”, “Роман”]
FAKE_BIOS  = [“Всё хорошо 🌿”, “На связи”, “Работаю 💼”,
“Не беспокоить”, “Живу и радуюсь ☀️”]

DEVICES = [
{“ua”: “Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36”, “w”: 1920, “h”: 1080, “plat”: “Win32”},
{“ua”: “Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36”, “w”: 1440, “h”: 900,  “plat”: “MacIntel”},
{“ua”: “Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36”, “w”: 1366, “h”: 768,  “plat”: “Linux x86_64”},
]

logging.basicConfig(level=logging.INFO, format=”%(asctime)s [%(levelname)s] %(message)s”)
log = logging.getLogger(**name**)
fake = Faker(“ru_RU”)

# ── DB ───────────────────────────────────────────────────

def db_init():
with sqlite3.connect(DB) as c:
c.execute(””“CREATE TABLE IF NOT EXISTS accounts (
phone TEXT PRIMARY KEY,
ua TEXT, res TEXT, plat TEXT,
status TEXT DEFAULT ‘active’,
last_active DATETIME
)”””)

def db_save(phone, ua, res, plat):
with sqlite3.connect(DB) as c:
c.execute(“INSERT OR REPLACE INTO accounts VALUES (?,?,?,?,‘active’,?)”,
(phone, ua, res, plat, datetime.now()))

def db_get(phone):
with sqlite3.connect(DB) as c:
return c.execute(“SELECT ua,res,plat FROM accounts WHERE phone=?”, (phone,)).fetchone()

def db_all_active():
with sqlite3.connect(DB) as c:
return [r[0] for r in c.execute(“SELECT phone FROM accounts WHERE status=‘active’”)]

def db_touch(phone):
with sqlite3.connect(DB) as c:
c.execute(“UPDATE accounts SET last_active=? WHERE phone=?”, (datetime.now(), phone))

# ── DRIVER ───────────────────────────────────────────────

def make_driver(phone: str):
cfg = db_get(phone)
if cfg:
ua, res, plat = cfg
w, h = map(int, res.split(”,”))
else:
dev  = random.choice(DEVICES)
ua, w, h, plat = dev[“ua”], dev[“w”], dev[“h”], dev[“plat”]
db_save(phone, ua, f”{w},{h}”, plat)

```
opt = Options()
opt.add_argument(f"--user-agent={ua}")
opt.add_argument(f"--window-size={w},{h}")
opt.add_argument("--headless=new")
opt.add_argument("--no-sandbox")
opt.add_argument("--disable-dev-shm-usage")
opt.add_argument("--disable-gpu")
opt.add_argument("--disable-extensions")
opt.add_argument("--blink-settings=imagesEnabled=false")
opt.add_argument(f"--user-data-dir={os.path.join(SESS_DIR, phone)}")
opt.page_load_strategy = "eager"
# Снижаем потребление памяти
opt.add_argument("--js-flags=--max-old-space-size=256")
opt.add_argument("--renderer-process-limit=1")

driver = webdriver.Chrome(options=opt)
driver.set_page_load_timeout(45)

# Anti-detect инъекции
driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": f"""
    Object.defineProperty(navigator,'webdriver',{{get:()=>undefined}});
    Object.defineProperty(navigator,'platform',{{get:()=>'{plat}'}});
    window.chrome={{runtime:{{}}}};
    Object.defineProperty(navigator,'plugins',{{get:()=>[1,2,3]}});
"""})
driver.execute_cdp_cmd("Emulation.setGeolocationOverride",
                       {"latitude": 43.238, "longitude": 76.889, "accuracy": 80})
return driver
```

# ── HUMAN TYPING ─────────────────────────────────────────

async def htype(el, text: str):
“”“Печатает как человек с редкими опечатками”””
for ch in text:
if random.random() < 0.03:                      # 3% опечатка
el.send_keys(random.choice(“фывапролдж”))
await asyncio.sleep(random.uniform(0.2, 0.5))
el.send_keys(Keys.BACKSPACE)
await asyncio.sleep(random.uniform(0.1, 0.3))
el.send_keys(ch)
await asyncio.sleep(random.uniform(0.04, 0.22))

# ── WHATSAPP LOGIC ────────────────────────────────────────

def is_logged_in(driver) -> bool:
“”“True если чаты уже загружены (сессия жива)”””
try:
driver.find_element(By.XPATH, “//div[@id=‘pane-side’]”)
return True
except NoSuchElementException:
return False

def wait_logged_in(driver, timeout=120) -> bool:
“”“Ждём входа (работает и для QR и для кода)”””
try:
WebDriverWait(driver, timeout).until(
EC.presence_of_element_located((By.XPATH, “//div[@id=‘pane-side’]”))
)
return True
except TimeoutException:
return False

def get_pairing_code(driver) -> str:
“”“Достаём код привязки со страницы”””
try:
spans = driver.find_elements(By.XPATH, “//div[@data-ref]//span | //div[contains(@class,‘pairing’)]//span”)
code = “”.join(s.text.strip() for s in spans if s.text.strip().isalnum() and len(s.text.strip()) <= 4)
return code[:8] if code else “”
except Exception:
return “”

async def enter_phone_and_get_code(driver, phone: str) -> str:
“”“Кликает ‘Link with phone number’, вводит номер, возвращает код”””
wait = WebDriverWait(driver, 20)
try:
btn = wait.until(EC.element_to_be_clickable((By.XPATH,
“//span[@role=‘button’ and (contains(.,‘Link with phone’) or contains(.,‘номер’))]”
“ | //div[@role=‘button’ and (contains(.,‘Link’) or contains(.,‘номер’))]”
)))
driver.execute_script(“arguments[0].click();”, btn)
await asyncio.sleep(1.5)
except TimeoutException:
pass  # может уже на экране ввода номера

```
# JS-вставка номера (React)
driver.execute_script(f"""
    var inp = document.querySelector('input[type="text"],input[inputmode="numeric"]');
    if(inp){{inp.focus();inp.value='';
      var nativeInput = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value');
      nativeInput.set.call(inp,'{phone}');
      inp.dispatchEvent(new Event('input',{{bubbles:true}}));
    }}
""")
await asyncio.sleep(1)

# Нажать Next / Далее
try:
    nxt = driver.find_element(By.XPATH,
        "//div[@role='button' and (contains(.,'Next') or contains(.,'Далее'))]")
    driver.execute_script("arguments[0].click();", nxt)
except NoSuchElementException:
    pass

await asyncio.sleep(3)
return get_pairing_code(driver)
```

async def change_profile(driver):
“”“Меняет имя и статус — раз в ~50 запусков”””
try:
wait = WebDriverWait(driver, 10)
# Меню → Профиль
driver.find_element(By.XPATH, “//div[@title=‘Меню’ or @title=‘Menu’]”).click()
await asyncio.sleep(0.8)
driver.find_element(By.XPATH, “//*[contains(.,‘Профиль’) or contains(.,‘Profile’)]”).click()
await asyncio.sleep(1.5)

```
    # Имя
    name_field = wait.until(EC.element_to_be_clickable((By.XPATH,
        "//div[@contenteditable='true'][1]")))
    name_field.click()
    driver.execute_script("arguments[0].innerText=''", name_field)
    await htype(name_field, random.choice(FAKE_NAMES))
    await asyncio.sleep(0.5)
    name_field.send_keys(Keys.ENTER)
    await asyncio.sleep(0.5)

    # Статус
    try:
        bio_field = driver.find_element(By.XPATH, "//div[@contenteditable='true'][2]")
        bio_field.click()
        driver.execute_script("arguments[0].innerText=''", bio_field)
        await htype(bio_field, random.choice(FAKE_BIOS))
        await asyncio.sleep(0.5)
        bio_field.send_keys(Keys.ENTER)
    except Exception:
        pass

    log.info("Профиль обновлён")
except Exception as e:
    log.warning(f"change_profile: {e}")
```

async def send_to_self(driver, phone: str):
“”“Пишет себе (Saved Messages)”””
wait = WebDriverWait(driver, 20)
driver.get(f”https://web.whatsapp.com/send?phone={phone}”)
inp = wait.until(EC.presence_of_element_located(
(By.XPATH, “//div[@contenteditable=‘true’][@data-tab]”)))
await asyncio.sleep(random.uniform(1, 2))
await htype(inp, fake.sentence(nb_words=random.randint(4, 12)))
await asyncio.sleep(random.uniform(0.3, 0.8))
inp.send_keys(Keys.ENTER)

def is_banned(driver) -> bool:
src = driver.page_source.lower()
return any(w in src for w in [“номер заблокирован”, “is banned”, “account is not allowed”, “spam”])

# ── FARM WORKER ───────────────────────────────────────────

FARM_TASKS: dict[str, asyncio.Task] = {}

async def farm_worker(phone: str):
log.info(f”[FARM] Старт: {phone}”)
change_counter = 0
while True:
driver = None
try:
driver = make_driver(phone)
driver.get(“https://web.whatsapp.com”)

```
        if not is_logged_in(driver):
            log.warning(f"[FARM] {phone} — сессия истекла, пропуск")
            break

        if is_banned(driver):
            log.error(f"[FARM] {phone} BANNED")
            break

        # Раз в 20 итераций меняем профиль
        change_counter += 1
        if change_counter % 20 == 0:
            await change_profile(driver)

        await send_to_self(driver, phone)
        db_touch(phone)
        log.info(f"[FARM] {phone} — сообщение отправлено ✅")

    except Exception as e:
        log.error(f"[FARM] {phone} ошибка: {e}")
    finally:
        if driver:
            driver.quit()

    pause = random.randint(FARM_MIN, FARM_MAX)
    log.info(f"[FARM] {phone} — пауза {pause//60} мин")
    await asyncio.sleep(pause)
```

# ── BOT ───────────────────────────────────────────────────

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

class S(StatesGroup):
phone = State()
code  = State()

def main_kb():
return InlineKeyboardMarkup(inline_keyboard=[
[InlineKeyboardButton(text=“📱 Войти по номеру”, callback_data=“login_phone”)],
[InlineKeyboardButton(text=“📷 Войти по QR”,     callback_data=“login_qr”)],
[InlineKeyboardButton(text=“📊 Аккаунты”,        callback_data=“accounts”)],
])

@dp.message(Command(“start”))
async def cmd_start(msg: types.Message):
if msg.from_user.id != ADMIN_ID:
return await msg.answer(“⛔ Нет доступа.”)
await msg.answer(“⚡ *Imperator v17*\nВыберите действие:”, parse_mode=“Markdown”, reply_markup=main_kb())

@dp.callback_query(F.data == “accounts”)
async def cb_accounts(cb: types.CallbackQuery):
accs = db_all_active()
text = f”📋 Активных аккаунтов: *{len(accs)}*\n” + “\n”.join(f”  • `{p}`” for p in accs) if accs else “Аккаунтов нет.”
await cb.message.answer(text, parse_mode=“Markdown”)

# ── QR LOGIN ──────────────────────────────────────────────

@dp.callback_query(F.data == “login_qr”)
async def cb_qr(cb: types.CallbackQuery, state: FSMContext):
await cb.message.answer(“📷 Введите номер телефона (нужен для папки сессии):”)
await state.set_state(S.phone)
await state.update_data(mode=“qr”)

# ── PHONE LOGIN ───────────────────────────────────────────

@dp.callback_query(F.data == “login_phone”)
async def cb_phone(cb: types.CallbackQuery, state: FSMContext):
await cb.message.answer(“📱 Введите номер (без +, пример: `77001234567`):”, parse_mode=“Markdown”)
await state.set_state(S.phone)
await state.update_data(mode=“phone”)

@dp.message(S.phone)
async def handle_phone(msg: types.Message, state: FSMContext):
data  = await state.get_data()
phone = msg.text.strip().replace(”+”, “”)
mode  = data.get(“mode”, “phone”)

```
status_msg = await msg.answer("⏳ Запускаю браузер...")

try:
    driver = make_driver(phone)
    driver.get("https://web.whatsapp.com")
    await asyncio.sleep(3)

    # Если сессия уже жива — сразу в фарм
    if is_logged_in(driver):
        driver.quit()
        await status_msg.edit_text("✅ Сессия актуальна! Аккаунт запущен в фарм.")
        _start_farm(phone)
        await state.clear()
        return

    if mode == "qr":
        await status_msg.edit_text(
            "📷 Откройте WhatsApp → *Связанные устройства* → *Привязать устройство* → "
            "отсканируйте QR-код.\n\n⏳ Ожидаю входа (до 2 мин)...",
            parse_mode="Markdown"
        )
        success = await asyncio.get_event_loop().run_in_executor(None, wait_logged_in, driver, 120)
        if success:
            driver.quit()
            db_save(phone, *([db_get(phone) or (random.choice(DEVICES)["ua"], "1920,1080", "Win32")][0]))
            await status_msg.edit_text("✅ Вход по QR выполнен! Фарм запущен.")
            _start_farm(phone)
        else:
            driver.quit()
            await status_msg.edit_text("❌ Timeout. Попробуйте снова.")
        await state.clear()
        return

    # Phone mode — получаем код
    await status_msg.edit_text("⏳ Получаю код привязки...")
    code = await enter_phone_and_get_code(driver, phone)

    if not code:
        # Попробуем достать ещё раз через 3 сек
        await asyncio.sleep(3)
        code = get_pairing_code(driver)

    if code:
        await state.update_data(phone=phone, driver_ref=id(driver))
        # Сохраняем driver глобально
        _DRIVERS[phone] = driver
        await status_msg.edit_text(
            f"🔑 Код привязки:\n\n`{code}`\n\n"
            "Откройте WhatsApp → *Связанные устройства* → *Привязать устройство* → "
            "*Привязать по номеру телефона* → введите код.\n\n"
            "Отправьте *любое сообщение* когда войдёте ✅",
            parse_mode="Markdown"
        )
        await state.set_state(S.code)
    else:
        driver.quit()
        await status_msg.edit_text("❌ Не удалось получить код. Попробуйте QR или другой номер.")
        await state.clear()

except Exception as e:
    log.error(f"Login error: {e}")
    await status_msg.edit_text(f"❌ Ошибка: {e}")
    await state.clear()
```

_DRIVERS: dict[str, webdriver.Chrome] = {}

@dp.message(S.code)
async def handle_code_confirm(msg: types.Message, state: FSMContext):
data  = await state.get_data()
phone = data.get(“phone”)
driver = _DRIVERS.get(phone)

```
status = await msg.answer("⏳ Проверяю вход...")

if driver:
    success = await asyncio.get_event_loop().run_in_executor(None, wait_logged_in, driver, 60)
    driver.quit()
    _DRIVERS.pop(phone, None)
    if success:
        await status.edit_text("✅ Вход выполнен! Фарм запущен.")
        _start_farm(phone)
    else:
        await status.edit_text("❌ Вход не подтверждён. Попробуйте снова /start")
else:
    await status.edit_text("⚠️ Сессия потеряна. Начните снова /start")

await state.clear()
```

def _start_farm(phone: str):
if phone not in FARM_TASKS or FARM_TASKS[phone].done():
FARM_TASKS[phone] = asyncio.create_task(farm_worker(phone))
log.info(f”Фарм-задача создана для {phone}”)

# ── MAIN ─────────────────────────────────────────────────

async def main():
if not BOT_TOKEN:
print(“❌ BOT_TOKEN не задан!”)
return

```
db_init()

# Восстанавливаем фарм для всех активных аккаунтов
for phone in db_all_active():
    _start_farm(phone)
    await asyncio.sleep(2)  # Не все сразу

log.info("⚡ Imperator v17 запущен")
await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
```

if **name** == “**main**”:
asyncio.run(main())
