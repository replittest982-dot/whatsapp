"""
⚡ IMPERATOR v17 — Playwright Edition (Вацап бот В3)
- Пишет только сам себе.
- Отправляет уведомление в Telegram, когда успешно зашел.
- Максимально оптимизировано потребление памяти (браузеры закрываются на время сна).
- v17.1: Отправляет скриншот QR-кода и кода привязки в Telegram.
"""

import asyncio, os, logging, random, sys, re
from datetime import datetime
from typing import Optional

import uvloop
import aiosqlite
import psutil
from faker import Faker
from playwright.async_api import async_playwright, Page, Browser, BrowserContext
from playwright_stealth import stealth_async
import google.generativeai as genai

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

# ── CONFIG ───────────────────────────────────────────────

BOT_TOKEN    = os.environ.get("BOT_TOKEN", "")
ADMIN_ID     = int(os.environ.get("ADMIN_ID", 0))
GEMINI_KEY   = os.environ.get("GEMINI_API_KEY", "")
DB           = "imp17.db"
SESS_DIR     = os.path.join(os.getcwd(), "sessions")
os.makedirs(SESS_DIR, exist_ok=True)

# НАСТРОЙКА ВРЕМЕНИ (В МИНУТАХ)
FARM_MIN_MINUTES = int(os.environ.get("FARM_MIN_MINUTES", 5))
FARM_MAX_MINUTES = int(os.environ.get("FARM_MAX_MINUTES", 15))

FARM_MIN = FARM_MIN_MINUTES * 60
FARM_MAX = FARM_MAX_MINUTES * 60

FAKE_NAMES = ["Алексей", "Максим", "Иван", "Дмитрий", "Сергей", "Артём", "Владимир", "Андрей"]
FAKE_BIOS  = ["Всё хорошо 🌿", "На связи", "Работаю 💼", "Не беспокоить 🔕", "Живу и радуюсь ☀️"]

DEVICES = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", "w": 1920, "h": 1080, "plat": "Win32", "mobile": False},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36", "w": 1440, "h": 900,  "plat": "MacIntel", "mobile": False},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", "w": 1366, "h": 768,  "plat": "Linux x86_64", "mobile": False},
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log  = logging.getLogger(__name__)
fake = Faker("ru_RU")

# ── GLOBALS ──────────────────────────────────────────────
bot: Bot = None
dp = Dispatcher(storage=MemoryStorage())
_CONTEXTS: dict[str, tuple] = {}
FARM_TASKS: dict[str, asyncio.Task] = {}
_gemini_model = None

# ── GEMINI ───────────────────────────────────────────────

def get_gemini():
    global _gemini_model
    if not _gemini_model and GEMINI_KEY:
        genai.configure(api_key=GEMINI_KEY)
        _gemini_model = genai.GenerativeModel("gemini-1.5-flash")
    return _gemini_model

async def gen_message() -> str:
    """Генерирует осмысленное короткое сообщение через Gemini"""
    model = get_gemini()
    if model:
        try:
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: model.generate_content(
                    "Напиши одно короткое бытовое сообщение как будто пишешь себе заметку "
                    "или напоминание. 1-2 предложения, по-русски, без кавычек, без эмодзи. "
                    "Например: нужно купить хлеб и молоко. Или: позвонить маме вечером."
                )
            )
            text = resp.text.strip()
            if text:
                return text
        except Exception as e:
            log.warning(f"Gemini error: {e}")

    fallbacks = [
        "не забыть купить продукты",
        "позвонить завтра утром",
        "оплатить счёт до пятницы",
        "записаться к врачу на следующей неделе",
        "забрать посылку с почты",
        "напомнить себе про встречу в среду",
    ]
    return random.choice(fallbacks)

# ── DATABASE (aiosqlite) ──────────────────────────────────

async def db_init():
    async with aiosqlite.connect(DB) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS accounts (
            phone TEXT PRIMARY KEY,
            ua TEXT, res TEXT, plat TEXT,
            status TEXT DEFAULT 'active',
            last_active TEXT
        )""")
        await db.commit()

async def db_save(phone, ua, res, plat):
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            "INSERT OR REPLACE INTO accounts VALUES (?,?,?,?,'active',?)",
            (phone, ua, res, plat, datetime.now().isoformat())
        )
        await db.commit()

async def db_get(phone):
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT ua,res,plat FROM accounts WHERE phone=?", (phone,)) as cur:
            return await cur.fetchone()

async def db_all_active():
    async with aiosqlite.connect(DB) as db:
        async with db.execute("SELECT phone FROM accounts WHERE status='active'") as cur:
            return [r[0] for r in await cur.fetchall()]

async def db_touch(phone):
    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE accounts SET last_active=? WHERE phone=?",
                         (datetime.now().isoformat(), phone))
        await db.commit()

# ── PLAYWRIGHT BROWSER ────────────────────────────────────

async def make_context(phone: str, playwright) -> tuple[BrowserContext, dict]:
    cfg = await db_get(phone)
    if cfg:
        ua, res, plat = cfg
        w, h = map(int, res.split(","))
        dev = {"ua": ua, "w": w, "h": h, "plat": plat, "mobile": False}
    else:
        dev = random.choice(DEVICES)
        await db_save(phone, dev["ua"], f"{dev['w']},{dev['h']}", dev["plat"])

    sess_path = os.path.join(SESS_DIR, phone)
    os.makedirs(sess_path, exist_ok=True)

    browser: Browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            # ── ВАЖНО: убираем --disable-images, иначе QR не отрисуется! ──
            "--js-flags=--max-old-space-size=256",
        ]
    )

    context: BrowserContext = await browser.new_context(
        user_agent=dev["ua"],
        viewport={"width": dev["w"], "height": dev["h"]},
        locale="ru-RU",
        timezone_id="Asia/Almaty",
        permissions=["geolocation"],
        geolocation={"latitude": 43.238, "longitude": 76.889},
        storage_state=os.path.join(sess_path, "state.json") if os.path.exists(
            os.path.join(sess_path, "state.json")) else None,
        extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9"}
    )

    context.on("page", lambda page: asyncio.ensure_future(stealth_async(page)))
    return context, dev

async def save_session(context: BrowserContext, phone: str):
    sess_path = os.path.join(SESS_DIR, phone)
    os.makedirs(sess_path, exist_ok=True)
    await context.storage_state(path=os.path.join(sess_path, "state.json"))

async def schedule_context_cleanup(phone: str, delay: int = 300):
    """Очищает контекст авторизации, если пользователь забил и не ввел код/QR."""
    await asyncio.sleep(delay)
    if phone in _CONTEXTS:
        ctx_data = _CONTEXTS.pop(phone, None)
        if ctx_data:
            context, page, pw = ctx_data
            try:
                await context.close()
                await pw.stop()
                log.info(f"[CLEANUP] Контекст {phone} очищен по таймауту (5 минут).")
            except Exception as e:
                log.error(f"[CLEANUP] Ошибка очистки {phone}: {e}")

# ── HUMAN TYPING ─────────────────────────────────────────

async def htype(page: Page, selector: str, text: str):
    await page.click(selector)
    for ch in text:
        if random.random() < 0.03:
            wrong = random.choice("фывапролдж")
            await page.keyboard.type(wrong, delay=random.randint(40, 150))
            await asyncio.sleep(random.uniform(0.2, 0.5))
            await page.keyboard.press("Backspace")
            await asyncio.sleep(random.uniform(0.1, 0.3))
        await page.keyboard.type(ch, delay=random.randint(40, 220))

# ── WHATSAPP HELPERS ──────────────────────────────────────

async def is_logged_in(page: Page) -> bool:
    try:
        await page.wait_for_selector("#pane-side", timeout=5000)
        return True
    except Exception:
        return False

async def wait_logged_in(page: Page, timeout=120) -> bool:
    try:
        await page.wait_for_selector("#pane-side", timeout=timeout * 1000)
        return True
    except Exception:
        return False

async def get_pairing_code(page: Page) -> str:
    try:
        await asyncio.sleep(3)
        spans = await page.query_selector_all(
            "div[data-ref] span, div[class*='pairing'] span, div[role='button'] span"
        )
        parts = []
        for s in spans:
            t = (await s.text_content() or "").strip()
            if t and len(t) <= 4 and (t.isalnum() or t.isdigit()):
                parts.append(t)
        code = "".join(parts)[:8]
        if len(code) >= 4:
            return code
        return await ocr_code(page)
    except Exception as e:
        log.warning(f"get_pairing_code: {e}")
        return ""

async def ocr_code(page: Page) -> str:
    try:
        import pytesseract
        from PIL import Image
        import io
        screenshot = await page.screenshot(type="png")
        img = Image.open(io.BytesIO(screenshot))
        text = pytesseract.image_to_string(
            img,
            config="--psm 6 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"
        )
        match = re.search(r'[A-Z0-9]{4}[-\s]?[A-Z0-9]{4}', text.upper())
        if match:
            return match.group().replace("-", "").replace(" ", "")
    except Exception as e:
        log.warning(f"OCR error: {e}")
    return ""

async def enter_phone_and_get_code(page: Page, phone: str) -> str:
    try:
        btn = await page.wait_for_selector(
            "span[role='button']:has-text('Link with phone'), "
            "span[role='button']:has-text('номер'), "
            "div[role='button']:has-text('Link')",
            timeout=15000
        )
        await btn.click()
        await asyncio.sleep(1.5)
    except Exception:
        pass

    await page.evaluate(f"""
        var inp = document.querySelector('input[type="text"],input[inputmode="numeric"]');
        if(inp){{
            inp.focus();
            var nativeSet = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value');
            nativeSet.set.call(inp,'{phone}');
            inp.dispatchEvent(new Event('input',{{bubbles:true}}));
            inp.dispatchEvent(new Event('change',{{bubbles:true}}));
        }}
    """)
    await asyncio.sleep(1)

    try:
        nxt = await page.wait_for_selector(
            "div[role='button']:has-text('Next'), div[role='button']:has-text('Далее')",
            timeout=5000
        )
        await nxt.click()
    except Exception:
        pass

    return await get_pairing_code(page)

async def change_profile(page: Page):
    try:
        await page.click("div[title='Меню'], div[title='Menu']")
        await asyncio.sleep(0.7)
        await page.click("text=Профиль, text=Profile")
        await asyncio.sleep(1.5)

        name_field = await page.wait_for_selector("div[contenteditable='true']", timeout=5000)
        await name_field.triple_click()
        await page.keyboard.type(random.choice(FAKE_NAMES), delay=random.randint(50, 180))
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.5)

        fields = await page.query_selector_all("div[contenteditable='true']")
        if len(fields) >= 2:
            await fields[1].triple_click()
            await page.keyboard.type(random.choice(FAKE_BIOS), delay=random.randint(50, 180))
            await page.keyboard.press("Enter")

        log.info("Профиль обновлён")
    except Exception as e:
        log.warning(f"change_profile: {e}")

async def send_to_self(page: Page, phone: str):
    text = await gen_message()
    await page.goto(f"https://web.whatsapp.com/send?phone={phone}", wait_until="domcontentloaded")
    inp_sel = "div[contenteditable='true'][data-tab]"
    await page.wait_for_selector(inp_sel, timeout=20000)
    await asyncio.sleep(random.uniform(1, 2.5))
    await htype(page, inp_sel, text)
    await asyncio.sleep(random.uniform(0.3, 0.8))
    await page.keyboard.press("Enter")
    log.info(f"Отправлено (Сам себе): «{text[:50]}»")

def is_banned_html(html: str) -> bool:
    src = html.lower()
    return any(w in src for w in ["номер заблокирован", "is banned", "account is not allowed", "spam"])

# ── SCREENSHOT HELPERS ────────────────────────────────────

async def take_qr_screenshot(page: Page) -> bytes:
    """
    Пытается снять скриншот именно canvas с QR-кодом.
    Если элемент не найден — делает скриншот всей страницы.
    Перед скриншотом ждёт 5 секунд, чтобы QR успел отрисоваться.
    """
    await asyncio.sleep(5)  # ждём прогрузки QR

    # Список возможных селекторов QR-canvas у WhatsApp Web
    qr_selectors = [
        "canvas[aria-label='Scan me!']",
        "canvas[aria-label='Scan this QR code to link a device']",
        "div[data-ref] canvas",
        "div[class*='qr'] canvas",
        "canvas",                        # последний резерв — любой canvas
    ]

    for selector in qr_selectors:
        try:
            el = await page.wait_for_selector(selector, timeout=3000)
            if el:
                log.info(f"[SCREENSHOT] QR найден по селектору: {selector}")
                return await el.screenshot(type="png")
        except Exception:
            continue

    # Ничего не нашли — полный скриншот страницы
    log.warning("[SCREENSHOT] QR-элемент не найден, делаем скриншот всей страницы")
    return await page.screenshot(type="png", full_page=False)


async def take_code_screenshot(page: Page) -> bytes:
    """
    Пытается снять скриншот блока с кодом привязки.
    Если элемент не найден — делает скриншот всей страницы.
    Перед скриншотом ждёт 5 секунд, чтобы код успел отрисоваться.
    """
    await asyncio.sleep(5)  # ждём прогрузки кода

    code_selectors = [
        "div[class*='pairing-code']",
        "div[data-ref]",
        "div[class*='landing-main']",
        "div[class*='pairing']",
    ]

    for selector in code_selectors:
        try:
            el = await page.wait_for_selector(selector, timeout=3000)
            if el:
                log.info(f"[SCREENSHOT] Блок кода найден по селектору: {selector}")
                return await el.screenshot(type="png")
        except Exception:
            continue

    log.warning("[SCREENSHOT] Блок кода не найден, делаем скриншот всей страницы")
    return await page.screenshot(type="png", full_page=False)

# ── FARM WORKER ───────────────────────────────────────────

async def farm_worker(phone: str):
    log.info(f"[FARM] Старт воркера: {phone}")
    change_counter = 0
    first_run = True

    while True:
        try:
            async with async_playwright() as pw:
                context, dev = await make_context(phone, pw)
                page = await context.new_page()
                await stealth_async(page)

                await page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")

                if not await is_logged_in(page):
                    log.warning(f"[FARM] {phone} — сессия истекла")
                    if bot:
                        await bot.send_message(ADMIN_ID, f"⚠️ Аккаунт {phone} вылетел (сессия истекла).")
                    await context.close()
                    break

                if first_run:
                    if bot:
                        await bot.send_message(
                            ADMIN_ID,
                            f"🟢 WhatsApp [{phone}] успешно в сети!\n"
                            f"Интервал: {FARM_MIN_MINUTES}-{FARM_MAX_MINUTES} мин.\n"
                            f"Режим: Пишу сам себе."
                        )
                    first_run = False

                html = await page.content()
                if is_banned_html(html):
                    log.error(f"[FARM] {phone} BANNED")
                    if bot:
                        await bot.send_message(ADMIN_ID, f"❌ Аккаунт {phone} ЗАБЛОКИРОВАН!")
                    await context.close()
                    break

                change_counter += 1
                if change_counter % 20 == 0:
                    await change_profile(page)

                await send_to_self(page, phone)
                await save_session(context, phone)
                await db_touch(phone)

                await context.close()

        except Exception as e:
            log.error(f"[FARM] {phone} ошибка: {e}")

        pause = random.randint(FARM_MIN, FARM_MAX)
        log.info(f"[FARM] {phone} — следующий запуск через {pause//60} мин")
        await asyncio.sleep(pause)

# ── BOT HANDLERS ──────────────────────────────────────────

class S(StatesGroup):
    phone = State()
    code  = State()

def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Войти по номеру", callback_data="login_phone")],
        [InlineKeyboardButton(text="📷 Войти по QR",      callback_data="login_qr")],
        [InlineKeyboardButton(text="📋 Аккаунты",        callback_data="accounts")],
    ])

@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    if msg.from_user.id != ADMIN_ID:
        return await msg.answer("⛔ Нет доступа.")
    await msg.answer(
        f"⚡ *Imperator v17 (Вацап бот В3)*\n"
        f"Интервал работы: {FARM_MIN_MINUTES} - {FARM_MAX_MINUTES} минут.\n"
        f"Выберите действие:",
        parse_mode="Markdown",
        reply_markup=main_kb()
    )

@dp.callback_query(F.data == "accounts")
async def cb_accounts(cb: types.CallbackQuery):
    accs = await db_all_active()
    if accs:
        text = f"📋 Активных: *{len(accs)}*\n" + "\n".join(f"  • `{p}`" for p in accs)
    else:
        text = "Аккаунтов нет."
    await cb.message.answer(text, parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data == "login_qr")
async def cb_qr(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("📷 Введите номер телефона (для папки сессии):")
    await state.set_state(S.phone)
    await state.update_data(mode="qr")
    await cb.answer()

@dp.callback_query(F.data == "login_phone")
async def cb_phone(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("📱 Введите номер (без +, пример: `77001234567`):", parse_mode="Markdown")
    await state.set_state(S.phone)
    await state.update_data(mode="phone")
    await cb.answer()

# ── ГЛАВНЫЙ ОБРАБОТЧИК (переписан) ───────────────────────

@dp.message(S.phone)
async def handle_phone(msg: types.Message, state: FSMContext):
    data  = await state.get_data()
    phone = msg.text.strip().replace("+", "")
    mode  = data.get("mode", "phone")

    status_msg = await msg.answer("⏳ Запускаю браузер...")

    try:
        pw = await async_playwright().start()
        context, dev = await make_context(phone, pw)
        page = await context.new_page()
        await stealth_async(page)
        await page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")
        await asyncio.sleep(3)

        # ── Уже залогинен — просто запускаем фарм ────────
        if await is_logged_in(page):
            await save_session(context, phone)
            await context.close()
            await pw.stop()
            await status_msg.edit_text("✅ Сессия актуальна! Фарм запущен.")
            _start_farm(phone)
            await state.clear()
            return

        # ── Режим QR ──────────────────────────────────────
        if mode == "qr":
            await status_msg.edit_text("⏳ Ожидаю появления QR-кода...")

            # Делаем скриншот QR (внутри функции уже есть asyncio.sleep(5))
            try:
                screenshot_bytes = await take_qr_screenshot(page)
                await msg.answer_photo(
                    photo=BufferedInputFile(screenshot_bytes, filename="qr.png"),
                    caption=(
                        "📷 Откройте WhatsApp → *Связанные устройства* → *Привязать устройство*\n"
                        "Отсканируйте QR-код выше.\n\n"
                        "⏳ Ожидаю подтверждения входа (до 2 мин)..."
                    ),
                    parse_mode="Markdown"
                )
                await status_msg.delete()
            except Exception as e:
                log.warning(f"[QR] Не удалось отправить скриншот: {e}")
                await status_msg.edit_text(
                    "📷 Откройте WhatsApp → *Связанные устройства* → *Привязать устройство*\n"
                    "Отсканируйте QR-код.\n\n⏳ Ожидаю входа (до 2 мин)...",
                    parse_mode="Markdown"
                )

            if await wait_logged_in(page, 120):
                await save_session(context, phone)
                await context.close()
                await pw.stop()
                await msg.answer("✅ Вход по QR выполнен! Фарм запущен.")
                _start_farm(phone)
            else:
                await context.close()
                await pw.stop()
                await msg.answer("❌ Timeout. QR устарел, попробуйте снова /start")
            await state.clear()
            return

        # ── Режим Phone (код привязки) ────────────────────
        await status_msg.edit_text("⏳ Запрашиваю код привязки...")
        code = await enter_phone_and_get_code(page, phone)

        if not code:
            await asyncio.sleep(3)
            code = await get_pairing_code(page)

        if code:
            # Делаем скриншот страницы с кодом (внутри функции уже есть asyncio.sleep(5))
            screenshot_sent = False
            try:
                screenshot_bytes = await take_code_screenshot(page)
                await msg.answer_photo(
                    photo=BufferedInputFile(screenshot_bytes, filename="code.png"),
                    caption=(
                        f"🔑 Код привязки: `{code}`\n\n"
                        "WhatsApp → *Связанные устройства* → *Привязать устройство* → "
                        "*По номеру телефона* → введите код.\n\n"
                        "Когда войдёте — отправьте любое сообщение ✅"
                    ),
                    parse_mode="Markdown"
                )
                await status_msg.delete()
                screenshot_sent = True
            except Exception as e:
                log.warning(f"[CODE] Не удалось отправить скриншот: {e}")

            # Фоллбэк — просто текст, если скриншот не отправился
            if not screenshot_sent:
                await status_msg.edit_text(
                    f"🔑 Код привязки:\n\n`{code}`\n\n"
                    "WhatsApp → *Связанные устройства* → *Привязать устройство* → "
                    "*По номеру телефона* → введите код.\n\n"
                    "Когда войдёте — отправьте любое сообщение ✅",
                    parse_mode="Markdown"
                )

            _CONTEXTS[phone] = (context, page, pw)
            asyncio.create_task(schedule_context_cleanup(phone))
            await state.update_data(phone=phone)
            await state.set_state(S.code)

        else:
            await context.close()
            await pw.stop()
            await status_msg.edit_text("❌ Не удалось получить код. Попробуйте войти по QR.")
            await state.clear()

    except Exception as e:
        log.error(f"Login error: {e}")
        try:
            await status_msg.edit_text(f"❌ Ошибка: {e}")
        except Exception:
            await msg.answer(f"❌ Ошибка: {e}")
        await state.clear()

# ── ПОДТВЕРЖДЕНИЕ КОДА ────────────────────────────────────

@dp.message(S.code)
async def handle_code_confirm(msg: types.Message, state: FSMContext):
    data     = await state.get_data()
    phone    = data.get("phone")
    ctx_data = _CONTEXTS.get(phone)

    status = await msg.answer("⏳ Проверяю вход...")

    if ctx_data:
        context, page, pw = ctx_data
        if await wait_logged_in(page, 60):
            await save_session(context, phone)
            await context.close()
            await pw.stop()
            _CONTEXTS.pop(phone, None)
            await status.edit_text("✅ Вход выполнен! Фарм запущен.")
            _start_farm(phone)
        else:
            await context.close()
            await pw.stop()
            _CONTEXTS.pop(phone, None)
            await status.edit_text("❌ Вход не подтверждён. Попробуйте /start")
    else:
        await status.edit_text("⚠️ Сессия потеряна (таймаут 5 минут). Начните снова /start")

    await state.clear()

# ── START FARM ────────────────────────────────────────────

def _start_farm(phone: str):
    if phone not in FARM_TASKS or FARM_TASKS[phone].done():
        FARM_TASKS[phone] = asyncio.create_task(farm_worker(phone))
        log.info(f"[FARM] Задача создана: {phone}")

# ── MAIN ─────────────────────────────────────────────────

async def main():
    global bot
    if not BOT_TOKEN:
        log.critical("❌ BOT_TOKEN не задан! Бот не запустится должным образом.")
        return

    bot = Bot(token=BOT_TOKEN)
    await db_init()

    for phone in await db_all_active():
        _start_farm(phone)
        await asyncio.sleep(3)

    log.info("⚡ Imperator v17.1 (Вацап бот В3) запущен")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    uvloop.install()
    asyncio.run(main())
