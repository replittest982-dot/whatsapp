import asyncio
import os
import logging
import sqlite3
import random
import psutil
import shutil
import sys
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict

# --- СТОРОННИЕ БИБЛИОТЕКИ ---
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from faker import Faker

# --- SELENIUM ---
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import WebDriverException, TimeoutException

# ==========================================
# ⚙️ КОНФИГУРАЦИЯ v18.0 TITANIUM
# ==========================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except:
    ADMIN_ID = 0

REQUIRED_CHANNEL_ID = "@WhatsAppstatpro" 
REQUIRED_CHANNEL_URL = "https://t.me/WhatsAppstatpro"

INSTANCE_ID = int(os.getenv("INSTANCE_ID", 1))
TOTAL_INSTANCES = int(os.getenv("TOTAL_INSTANCES", 1))

# Лимит браузеров (2 для 10ГБ RAM - безопасно)
BROWSER_SEMAPHORE = asyncio.Semaphore(2)

DB_NAME = 'imperator_titan_v18.db'
SESSIONS_DIR = os.path.abspath("./sessions")
TMP_DIR = os.path.abspath("./tmp_chrome_data")

# Режимы грева
HEAT_MODES = {
    "TURBO": (15, 30),
    "MEDIUM": (60, 180),
    "SLOW": (300, 600)
}
CURRENT_MODE = "MEDIUM"

logging.basicConfig(
    level=logging.INFO, 
    format=f'%(asctime)s | INST-{INSTANCE_ID} | %(levelname)s | %(name)s | %(message)s'
)
logger = logging.getLogger("Imperator")
fake = Faker('ru_RU')

# Создание папок
for d in [SESSIONS_DIR, TMP_DIR]:
    if not os.path.exists(d): os.makedirs(d)

# БАЗА УСТРОЙСТВ (Rotated UA + Platform)
DEVICES = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "res": "1920,1080", "plat": "Win32"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "res": "1440,900", "plat": "MacIntel"},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36", "res": "1366,768", "plat": "Linux x86_64"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/124.0.0.0", "res": "1920,1080", "plat": "Win32"}
]

ACTIVE_DRIVERS = {}

class BotStates(StatesGroup):
    waiting_phone = State()
    waiting_user_id = State() # Для админки добавления юзеров

# ==========================================
# 🧠 AI-ГЕНЕРАТОР ДИАЛОГОВ (Улучшенный)
# ==========================================
class DialogueAI:
    def __init__(self):
        self.greetings = ["Привет", "Ку", "Здарова", "Добрый день", "Хай", "Салам"]
        self.questions = ["Как дела?", "Ты где?", "Что нового?", "Когда встреча?", "Скинь инфу", "Ты тут?", "Есть минута?"]
        self.answers = ["Норм", "Работаю", "В пути", "Скоро буду", "Да, слушаю", "Ок", "Принял"]
        self.smiles = ["))", "👍", "👋", "🫡", "🔥"]

    def generate(self):
        """Генерирует живой диалог с 20% шансом на смайл"""
        mode = random.choice(['greet', 'ask', 'answer', 'fake'])
        text = ""
        
        if mode == 'greet':
            text = f"{random.choice(self.greetings)}. {random.choice(self.questions)}"
        elif mode == 'ask':
            text = random.choice(self.questions)
        elif mode == 'answer':
            text = random.choice(self.answers)
        else:
            text = fake.sentence(nb_words=random.randint(2, 6))

        if random.random() < 0.2:
            text += f" {random.choice(self.smiles)}"
            
        return text

ai_engine = DialogueAI()

# ==========================================
# 🛠 СИСТЕМНЫЕ УТИЛИТЫ
# ==========================================
def cleanup_zombie_processes():
    """Жесткая зачистка перед стартом"""
    killed = 0
    for proc in psutil.process_iter(['name']):
        try:
            if proc.info['name'] in ['chrome', 'chromedriver', 'google-chrome', 'zygot']:
                proc.kill()
                killed += 1
        except: pass
    
    # Очистка временных папок Chrome
    try:
        if os.path.exists(TMP_DIR):
            shutil.rmtree(TMP_DIR)
        os.makedirs(TMP_DIR)
    except: pass
    
    if killed: logger.warning(f"🧹 Zombie Cleanup: Killed {killed} procs.")

def get_server_load_status():
    mem = psutil.virtual_memory()
    return f"RAM Free: {mem.available//1024//1024}MB | CPU: {psutil.cpu_percent()}%"

# ==========================================
# 🗄️ БАЗА ДАННЫХ
# ==========================================
def db_init():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # Таблица аккаунтов (добавил ban_date для лога смертей)
    c.execute('''CREATE TABLE IF NOT EXISTS accounts (
        phone TEXT PRIMARY KEY, status TEXT, ua TEXT, res TEXT, plat TEXT, 
        last_act DATETIME, created_at DATETIME, ban_date DATETIME
    )''')
    # Вайтлист + поле is_unlimited для "особых" юзеров
    c.execute('''CREATE TABLE IF NOT EXISTS whitelist (
        user_id INTEGER PRIMARY KEY, approved INTEGER DEFAULT 0, is_unlimited INTEGER DEFAULT 0
    )''')
    conn.commit(); conn.close()

def db_get_active_phones():
    conn = sqlite3.connect(DB_NAME)
    res = conn.execute("SELECT phone FROM accounts WHERE status='active'").fetchall()
    conn.close()
    return [r[0] for r in res]

def db_get_my_targets():
    conn = sqlite3.connect(DB_NAME)
    q = f"SELECT phone, created_at FROM accounts WHERE status='active' AND (rowid % {TOTAL_INSTANCES}) = ({INSTANCE_ID}-1)"
    res = conn.execute(q).fetchall()
    conn.close()
    return res

def db_save(phone, ua, res, plat):
    conn = sqlite3.connect(DB_NAME)
    now = datetime.now()
    conn.execute("""
        INSERT INTO accounts (phone, status, ua, res, plat, last_act, created_at) VALUES (?, 'active', ?, ?, ?, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET status='active', last_act=excluded.last_act
    """, (phone, ua, res, plat, now, now))
    conn.commit(); conn.close()

def db_ban(phone):
    conn = sqlite3.connect(DB_NAME)
    # Логируем дату смерти
    conn.execute("UPDATE accounts SET status='banned', ban_date=? WHERE phone=?", (datetime.now(), phone))
    conn.commit(); conn.close()

def db_check_limit(user_id):
    """Проверка лимита (3 аккаунта), если не Unlimited"""
    # В данной реализации мы не связываем phone c user_id жестко в БД, 
    # но для "Кнопки Юзер" реализуем логику проверки прав.
    if user_id == ADMIN_ID: return True
    conn = sqlite3.connect(DB_NAME)
    res = conn.execute("SELECT is_unlimited FROM whitelist WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return res and res[0] == 1

def db_set_unlimited(target_id):
    conn = sqlite3.connect(DB_NAME)
    conn.execute("UPDATE whitelist SET approved=1, is_unlimited=1 WHERE user_id=?", (target_id,))
    conn.commit(); conn.close()

# ==========================================
# 🌐 SELENIUM (TITANIUM CORE - ANTI-CRASH)
# ==========================================
def get_driver(phone, ua=None, res=None, plat=None):
    # Генерация устройства
    if not ua:
        conn = sqlite3.connect(DB_NAME)
        row = conn.execute("SELECT ua, res, plat FROM accounts WHERE phone=?", (phone,)).fetchone()
        conn.close()
        if row: ua, res, plat = row
        else: 
            d = random.choice(DEVICES) # Теперь тут разные платформы
            ua, res, plat = d['ua'], d['res'], d['plat']

    options = Options()
    
    # 📁 ПУТИ: Основной профиль + Уникальная папка для временных файлов процесса
    # Это предотвращает конфликты Lockfile, которые крашат Chrome
    profile_path = os.path.join(SESSIONS_DIR, phone)
    unique_tmp = os.path.join(TMP_DIR, f"tmp_{phone}_{random.randint(1000,9999)}")
    if not os.path.exists(unique_tmp): os.makedirs(unique_tmp)

    options.add_argument(f"--user-data-dir={profile_path}")
    options.add_argument(f"--data-path={unique_tmp}")
    options.add_argument(f"--disk-cache-dir={unique_tmp}")
    
    options.add_argument("--headless=new")
    
    # 🔥 ЯДЕРНЫЙ ПАКЕТ ФЛАГОВ ПРОТИВ КРАШЕЙ 🔥
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage") # Самый важный флаг для /dev/shm
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-extensions")
    options.add_argument("--no-zygote") # Отключает лишние процессы (экономия RAM)
    options.add_argument("--single-process") # Если краши продолжатся - это поможет, но медленнее
    options.add_argument("--disable-features=VizDisplayCompositor")
    options.add_argument("--disable-breakpad")
    
    # Spoofing
    options.add_argument(f"--user-agent={ua}")
    options.add_argument(f"--window-size={res}")
    options.page_load_strategy = 'eager'

    try:
        driver = webdriver.Chrome(options=options)
        
        # Скрываем Selenium
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        })
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": f"Object.defineProperty(navigator, 'platform', {{get: () => '{plat}'}});"
        })
        
        return driver, ua, res, plat, unique_tmp
    except Exception as e:
        logger.error(f"Driver Init Failed: {e}")
        return None, None, None, None, None

# ==========================================
# 🤖 BOT UI & LOGIC
# ==========================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- Middlewares ---
async def check_sub(user_id):
    try:
        m = await bot.get_chat_member(REQUIRED_CHANNEL_ID, user_id)
        return m.status in ['member', 'administrator', 'creator']
    except: return False

# --- Helper: Auto-Kill Session (120s Timer) ---
async def auto_kill_session(phone, chat_id, tmp_path):
    """Таймер 120 секунд. Удаляет сессию и временные файлы."""
    await asyncio.sleep(120)
    
    if phone in ACTIVE_DRIVERS:
        logger.info(f"⏳ Timeout for {phone}. Killing session.")
        d = ACTIVE_DRIVERS.pop(phone, None)
        if d:
            try: await asyncio.to_thread(d['driver'].quit)
            except: pass
            
        # Удаляем папку сессии и tmp
        shutil.rmtree(os.path.join(SESSIONS_DIR, phone), ignore_errors=True)
        if tmp_path and os.path.exists(tmp_path): shutil.rmtree(tmp_path, ignore_errors=True)
        
        try:
            await bot.send_message(chat_id, f"❌ **Время вышло!** (120с)\nСессия для +{phone} удалена. Начни заново.")
        except: pass

# --- Keyboards ---
def kb_main(user_id):
    btns = [
        [InlineKeyboardButton(text="➕ ДОБАВИТЬ АККАУНТ", callback_data="add_acc")],
        [InlineKeyboardButton(text="📊 СТАТИСТИКА", callback_data="stats")],
        [InlineKeyboardButton(text="⚙️ НАСТРОЙКИ", callback_data="settings"), 
         InlineKeyboardButton(text="🆘 ПОМОЩЬ", callback_data="help")]
    ]
    # Кнопка админа для добавления "Юзеров"
    if user_id == ADMIN_ID:
        btns.append([InlineKeyboardButton(text="👑 ДОБАВИТЬ ЮЗЕРА (БЕЗЛИМИТ)", callback_data="add_vip")])
    return InlineKeyboardMarkup(inline_keyboard=btns)

def kb_settings():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{'✅' if CURRENT_MODE=='TURBO' else ''} TURBO (15-30s)", callback_data="set_TURBO")],
        [InlineKeyboardButton(text=f"{'✅' if CURRENT_MODE=='MEDIUM' else ''} MEDIUM (1-3m)", callback_data="set_MEDIUM")],
        [InlineKeyboardButton(text=f"{'✅' if CURRENT_MODE=='SLOW' else ''} SLOW (5-10m)", callback_data="set_SLOW")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu")]
    ])

def kb_login_process(phone):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 ОБНОВИТЬ КОД", callback_data=f"getcode_{phone}")],
        [InlineKeyboardButton(text="✅ Я ВВЕЛ КОД (СОХРАНИТЬ)", callback_data=f"finish_{phone}")]
    ])

# --- Handlers ---
@dp.message(Command("start"))
async def start_handler(msg: types.Message):
    # 1. Проверка подписки
    if not await check_sub(msg.from_user.id):
        return await msg.answer(
            f"❌ **Нет подписки!**\nКанал: {REQUIRED_CHANNEL_URL}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Подписаться", url=REQUIRED_CHANNEL_URL)]])
        )

    # 2. Whitelist
    conn = sqlite3.connect(DB_NAME)
    res = conn.execute("SELECT approved FROM whitelist WHERE user_id=?", (msg.from_user.id,)).fetchone()
    conn.close()

    if not res:
        conn = sqlite3.connect(DB_NAME)
        conn.execute("INSERT INTO whitelist (user_id) VALUES (?)", (msg.from_user.id,))
        conn.commit(); conn.close()
        if ADMIN_ID: 
            await bot.send_message(ADMIN_ID, f"Запрос: {msg.from_user.id} (@{msg.from_user.username})", 
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Пустить", callback_data=f"ap_{msg.from_user.id}")]])
            )
        return await msg.answer("🔒 Заявка отправлена админу.")

    if res[0] == 0: return await msg.answer("🔒 Доступ не открыт.")

    await msg.answer("🔱 **Imperator v18.0 Titan**\nСистема в строю.", reply_markup=kb_main(msg.from_user.id))

@dp.callback_query(F.data.startswith("ap_"))
async def approve(cb: types.CallbackQuery):
    uid = int(cb.data.split("_")[1])
    conn = sqlite3.connect(DB_NAME); conn.execute("UPDATE whitelist SET approved=1 WHERE user_id=?", (uid,)); conn.commit(); conn.close()
    await bot.send_message(uid, "✅ Доступ открыт! /start")
    await cb.answer("Ок")

@dp.callback_query(F.data == "menu")
async def back_menu(cb: types.CallbackQuery):
    await cb.message.edit_text("Главное меню", reply_markup=kb_main(cb.from_user.id))

@dp.callback_query(F.data == "settings")
async def settings_menu(cb: types.CallbackQuery):
    await cb.message.edit_text(f"🔥 Режим: {CURRENT_MODE}", reply_markup=kb_settings())

@dp.callback_query(F.data.startswith("set_"))
async def set_mode(cb: types.CallbackQuery):
    global CURRENT_MODE
    CURRENT_MODE = cb.data.split("_")[1]
    await cb.message.edit_text(f"✅ Режим: **{CURRENT_MODE}**", reply_markup=kb_main(cb.from_user.id))

@dp.callback_query(F.data == "stats")
async def show_stats(cb: types.CallbackQuery):
    phones = db_get_active_phones()
    # 26. Статистика (кол-во номеров + сервер)
    await cb.answer(f"📱 Всего активных: {len(phones)}\n💻 {get_server_load_status()}\n🤖 Инстанс: {INSTANCE_ID}", show_alert=True)

@dp.callback_query(F.data == "help")
async def help_menu(cb: types.CallbackQuery):
    # 46. Красивый текст помощи
    text = (
        "📚 **Инструкция по добавлению:**\n\n"
        "1. Нажми '➕ Добавить аккаунт'.\n"
        "2. Введи номер телефона (только цифры).\n"
        "3. Бот сам нажмет кнопки и пришлет скрин с кодом.\n"
        "4. Введи этот код в WhatsApp на телефоне.\n"
        "5. **ВАЖНО:** Нажми кнопку 'Я ВВЕЛ КОД' в течение 120 сек.\n\n"
        "⚠️ *Если не нажать кнопку — сессия удалится.*"
    )
    await cb.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="menu")]]))

# --- ADMIN: ADD VIP USER ---
@dp.callback_query(F.data == "add_vip")
async def add_vip_start(cb: types.CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    await cb.message.answer("👑 Введите ID пользователя для выдачи безлимита:")
    await state.set_state(BotStates.waiting_user_id)

@dp.message(BotStates.waiting_user_id)
async def add_vip_finish(msg: types.Message, state: FSMContext):
    try:
        uid = int(msg.text)
        db_set_unlimited(uid)
        await msg.answer(f"✅ Пользователь {uid} теперь VIP (Безлимит).")
    except:
        await msg.answer("❌ Ошибка. Введите числовой ID.")
    await state.clear()

# --- ADD ACCOUNT (AUTO-INPUT + 120s TIMER) ---
@dp.callback_query(F.data == "add_acc")
async def add_start(cb: types.CallbackQuery, state: FSMContext):
    # Проверка лимита (если не админ и не вип - ограничение 3, но тут мы просто даем добавлять всем одобренным, 
    # а логику ограничения можно внедрить жестче, если нужно. Сейчас - свободно).
    await cb.message.answer("📞 Введите номер (только цифры):")
    await state.set_state(BotStates.waiting_phone)

@dp.message(BotStates.waiting_phone)
async def add_process(msg: types.Message, state: FSMContext):
    phone = "".join(filter(str.isdigit, msg.text))
    await state.clear()
    
    status_msg = await msg.answer(f"🚀 Запуск Chrome для +{phone}...\n⏳ Инициализация (Titanium Mode)...")
    
    async with BROWSER_SEMAPHORE:
        try:
            # Запускаем драйвер и получаем путь к tmp папке для очистки
            driver, ua, res, plat, tmp_path = await asyncio.to_thread(get_driver, phone)
            
            if not driver: 
                return await status_msg.edit_text("❌ Ошибка драйвера. Сервер перегружен или краш.")
            
            ACTIVE_DRIVERS[phone] = {"driver": driver, "ua": ua, "res": res, "plat": plat, "tmp": tmp_path}
            await asyncio.to_thread(driver.get, "https://web.whatsapp.com")
            
            # 🔥 JS: ПОИСК КНОПКИ + ВВОД 🔥
            driver.execute_script(f"""
                var attempts = 0;
                var existCondition = setInterval(function() {{
                    // 1. Жмем 'Link with phone number'
                    var linkBtn = document.querySelector('span[role="button"]');
                    if (linkBtn && (linkBtn.innerText.includes('Link') || linkBtn.innerText.includes('Связать'))) linkBtn.click();
                    
                    var xp = document.evaluate("//*[contains(text(), 'Link with phone')]", document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                    if(xp) xp.click();

                    // 2. Вводим номер
                    var input = document.querySelector('input[type="text"]');
                    if (input) {{
                        clearInterval(existCondition);
                        input.focus();
                        document.execCommand('selectAll');
                        document.execCommand('delete');
                        document.execCommand('insertText', false, '+{phone}');
                        
                        setTimeout(function(){{
                            // Жмем Далее
                            var nextBtn = document.querySelector('button.type-primary') || document.querySelector('div[role="button"][class*="primary"]');
                            if(nextBtn) nextBtn.click();
                        }}, 800);
                    }}
                    
                    if (++attempts > 50) clearInterval(existCondition);
                }}, 1000);
            """)
            
            # Ждем код (чуть дольше для надежности)
            await asyncio.sleep(15)
            
            png = await asyncio.to_thread(driver.get_screenshot_as_png)
            await status_msg.delete()
            await msg.answer_photo(
                BufferedInputFile(png, "code.png"), 
                caption=f"✅ **Код для +{phone}**\n\n⏱ Таймер: 120 секунд.\nВведи код в телефоне и нажми кнопку 'Я ВВЕЛ КОД'.",
                reply_markup=kb_login_process(phone)
            )
            
            # 🔥 ЗАПУСК ТАЙМЕРА СМЕРТИ (120 СЕКУНД) 🔥
            asyncio.create_task(auto_kill_session(phone, msg.chat.id, tmp_path))
            
        except Exception as e:
            await status_msg.edit_text(f"❌ Ошибка: {e}")

@dp.callback_query(F.data.startswith("getcode_"))
async def manual_get_code(cb: types.CallbackQuery):
    p = cb.data.split("_")[1]
    d = ACTIVE_DRIVERS.get(p)
    if d:
        await asyncio.sleep(1)
        try:
            png = await asyncio.to_thread(d['driver'].get_screenshot_as_png)
            await cb.message.answer_photo(BufferedInputFile(png, "code.png"), caption="Актуальный экран:")
        except:
            await cb.answer("Ошибка получения скрина", show_alert=True)
    await cb.answer()

@dp.callback_query(F.data.startswith("finish_"))
async def finish_setup(cb: types.CallbackQuery):
    p = cb.data.split("_")[1]
    d = ACTIVE_DRIVERS.pop(p, None) # Забираем из активных (таймер теперь не сработает)
    
    if d:
        db_save(p, d['ua'], d['res'], d['plat'])
        try: await asyncio.to_thread(d['driver'].quit)
        except: pass
        # Чистим tmp папку этого сеанса
        if d['tmp'] and os.path.exists(d['tmp']):
            shutil.rmtree(d['tmp'], ignore_errors=True)
            
        await cb.message.edit_text(f"✅ Аккаунт +{p} успешно сохранен!")
        # 3. Уведомление о входе (тут просто эдит, но можно и отдельно)
    else:
        await cb.message.edit_text("❌ Время вышло или сессия не найдена.")

# ==========================================
# 🚜 HIVE MIND: СЕТКА БОТОВ
# ==========================================
async def hive_worker(phone, created_at):
    driver = None
    tmp_path = None
    try:
        active_phones = db_get_active_phones()
        targets = [t for t in active_phones if t != phone]
        target_phone = random.choice(targets) if targets else phone
        
        async with BROWSER_SEMAPHORE:
            logger.info(f"🐝 {phone} -> {target_phone} ({CURRENT_MODE})")
            
            driver, ua, res, plat, tmp_path = await asyncio.to_thread(get_driver, phone)
            if not driver: return

            await asyncio.to_thread(driver.get, f"https://web.whatsapp.com/send?phone={target_phone}")
            wait = WebDriverWait(driver, 60)
            
            try:
                # Ждем поле ввода (значит чат загрузился)
                inp = wait.until(EC.presence_of_element_located((By.XPATH, "//footer//div[@contenteditable='true']")))
                
                # 25. ИГНОР ЛС: Мы не читаем входящие от других (кроме ботов, они сами откроются).
                # Тут мы просто пишем.
                
                text = ai_engine.generate()
                # 20. Имитация печати (быстро/медленно)
                for char in text:
                    inp.send_keys(char)
                    await asyncio.sleep(random.uniform(0.05, 0.15))
                inp.send_keys(Keys.ENTER)
                
                conn = sqlite3.connect(DB_NAME)
                conn.execute("UPDATE accounts SET last_act=? WHERE phone=?", (datetime.now(), phone))
                conn.commit(); conn.close()
                
                logger.info(f"✅ Sent: '{text}'")
                await asyncio.sleep(3)
                
            except TimeoutException:
                # Проверка на БАН
                try:
                    src = driver.page_source.lower()
                    if "not allowed" in src or "spam" in src or "banned" in src:
                        db_ban(phone)
                        shutil.rmtree(os.path.join(SESSIONS_DIR, phone), ignore_errors=True)
                        logger.error(f"💀 BAN: {phone}")
                except: pass

    except Exception as e:
        logger.error(f"Worker Error {phone}: {e}")
    finally:
        if driver: 
            try: await asyncio.to_thread(driver.quit)
            except: pass
        if tmp_path and os.path.exists(tmp_path):
            shutil.rmtree(tmp_path, ignore_errors=True)

async def hive_loop():
    logger.info("🐝 HIVE MIND ЗАПУЩЕН")
    while True:
        try:
            min_delay, max_delay = HEAT_MODES[CURRENT_MODE]
            my_accounts = db_get_my_targets()
            
            if not my_accounts:
                await asyncio.sleep(30)
                continue
            
            for phone, created_at in my_accounts:
                if phone in ACTIVE_DRIVERS: continue
                await hive_worker(phone, created_at)
                await asyncio.sleep(random.randint(15, 25))
            
            slp = random.randint(min_delay, max_delay)
            logger.info(f"💤 Сон {slp}с...")
            await asyncio.sleep(slp)
            
        except Exception as e:
            logger.error(f"Loop Error: {e}")
            await asyncio.sleep(10)

# ==========================================
# 🚀 ЗАПУСК
# ==========================================
async def main():
    if not BOT_TOKEN:
        logger.critical("❌ НЕТ ТОКЕНА!")
        sys.exit(1)

    cleanup_zombie_processes()
    db_init()
    asyncio.create_task(hive_loop())
    
    logger.info(f"🚀 Imperator v18.0 (Titanium) started.")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped.")
