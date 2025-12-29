import asyncio
import os
import logging
import sqlite3
import random
import psutil
import shutil
import sys
from datetime import datetime, timedelta
from typing import Optional, List, Dict

# --- СТОРОННИЕ БИБЛИОТЕКИ ---
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
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys

# ==========================================
# ⚙️ КОНФИГУРАЦИЯ v17.1 (CODE LOGIN FIX)
# ==========================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")
try:
    ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))
except:
    ADMIN_ID = 0

# КАНАЛ ДЛЯ ПОДПИСКИ (Без @ в ссылках, с @ для проверки)
REQUIRED_CHANNEL_ID = "@WhatsAppstatpro" 
REQUIRED_CHANNEL_URL = "https://t.me/WhatsAppstatpro"

# Настройки Инстанса
INSTANCE_ID = int(os.getenv("INSTANCE_ID", 1))
TOTAL_INSTANCES = int(os.getenv("TOTAL_INSTANCES", 1))

# ⚠️ ОПТИМИЗАЦИЯ: 2 БРАУЗЕРА (Т.к. 10 ГБ RAM)
BROWSER_SEMAPHORE = asyncio.Semaphore(2)

DB_NAME = 'imperator_final_v17.db'
SESSIONS_DIR = os.path.abspath("./sessions")

# Режимы грева
HEAT_MODES = {
    "TURBO": (15, 30),
    "MEDIUM": (40, 80),
    "SLOW": (120, 300)
}
CURRENT_MODE = "MEDIUM"

logging.basicConfig(level=logging.INFO, format=f'%(asctime)s | INST-{INSTANCE_ID} | %(levelname)s | %(message)s')
logger = logging.getLogger("Imperator")
fake = Faker('ru_RU')

if not os.path.exists(SESSIONS_DIR): os.makedirs(SESSIONS_DIR)

DEVICES = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0 Safari/537.36", "res": "1920,1080", "plat": "Win32"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/124.0.0.0 Safari/537.36", "res": "1440,900", "plat": "MacIntel"}
]

ACTIVE_DRIVERS = {}

class BotStates(StatesGroup):
    waiting_phone = State()

# ==========================================
# 🧠 PSEUDO-AI (Генератор диалогов)
# ==========================================
class DialogueAI:
    def __init__(self):
        self.phrases = [
            "Привет, как сам?", "Скинь документы", "Завтра буду", "Ок, договорились", 
            "Ты где?", "Я на месте", "Перезвони", "Да, все в силе", "Ку", "Доброй ночи"
        ]
    def generate(self):
        if random.random() < 0.4: return random.choice(self.phrases)
        return fake.sentence(nb_words=random.randint(2, 6))

ai_engine = DialogueAI()

# ==========================================
# 🛠 СИСТЕМНЫЕ ФУНКЦИИ
# ==========================================
def cleanup_zombie():
    for p in psutil.process_iter(['name']):
        if p.info['name'] in ['chrome', 'chromedriver']:
            try: p.kill()
            except: pass

def get_sys_status():
    mem = psutil.virtual_memory()
    cpu = psutil.cpu_percent()
    return f"CPU: {cpu}% | RAM Free: {mem.available//1024//1024}MB"

# ==========================================
# 🗄️ БАЗА ДАННЫХ
# ==========================================
def db_init():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS accounts (
        phone TEXT PRIMARY KEY, status TEXT, ua TEXT, res TEXT, plat TEXT, 
        last_act DATETIME, created_at DATETIME
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS whitelist (user_id INTEGER PRIMARY KEY, approved INTEGER DEFAULT 0)""")
    conn.commit(); conn.close()

def db_get_active_phones():
    conn = sqlite3.connect(DB_NAME)
    res = conn.execute("SELECT phone FROM accounts WHERE status='active'").fetchall()
    conn.close()
    return [r[0] for r in res]

def db_get_targets_for_instance():
    conn = sqlite3.connect(DB_NAME)
    query = f"SELECT phone, created_at FROM accounts WHERE status='active' AND (rowid % {TOTAL_INSTANCES}) = ({INSTANCE_ID}-1)"
    res = conn.execute(query).fetchall()
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
    conn.execute("UPDATE accounts SET status='banned' WHERE phone=?", (phone,))
    conn.commit(); conn.close()

# ==========================================
# 🌐 SELENIUM ENGINE
# ==========================================
def get_driver(phone, ua=None, res=None, plat=None):
    if not ua:
        conn = sqlite3.connect(DB_NAME)
        row = conn.execute("SELECT ua, res, plat FROM accounts WHERE phone=?", (phone,)).fetchone()
        conn.close()
        if row: ua, res, plat = row
        else: 
            d = random.choice(DEVICES)
            ua, res, plat = d['ua'], d['res'], d['plat']

    opt = Options()
    opt.add_argument(f"--user-data-dir={os.path.join(SESSIONS_DIR, phone)}")
    opt.add_argument("--headless=new")
    opt.add_argument("--no-sandbox")
    opt.add_argument("--disable-gpu")
    opt.add_argument("--disable-extensions") # Оптимизация RAM
    opt.add_argument(f"--user-agent={ua}")
    opt.add_argument(f"--window-size={res}")
    
    driver = webdriver.Chrome(options=opt)
    
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    })
    
    return driver, ua, res, plat

# ==========================================
# 🤖 BOT UI
# ==========================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- Middlewares ---
async def check_sub(user_id):
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL_ID, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except: return False # Если бот не админ, вернет False, но для теста можно True

# --- Keyboards ---
def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ ДОБАВИТЬ АККАУНТ", callback_data="add_acc")],
        [InlineKeyboardButton(text="⚙️ НАСТРОЙКИ ГРЕВА", callback_data="settings")],
        [InlineKeyboardButton(text="📊 СТАТИСТИКА", callback_data="stats")]
    ])

def kb_settings():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{'✅' if CURRENT_MODE=='TURBO' else ''} TURBO", callback_data="set_TURBO")],
        [InlineKeyboardButton(text=f"{'✅' if CURRENT_MODE=='MEDIUM' else ''} MEDIUM", callback_data="set_MEDIUM")],
        [InlineKeyboardButton(text=f"{'✅' if CURRENT_MODE=='SLOW' else ''} SLOW", callback_data="set_SLOW")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="menu")]
    ])

def kb_login_process(phone):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 ОБНОВИТЬ КОД (Скрин)", callback_data=f"getcode_{phone}")],
        [InlineKeyboardButton(text="✅ Я ВВЕЛ КОД (Сохранить)", callback_data=f"finish_{phone}")]
    ])

# --- Handlers ---
@dp.message(Command("start"))
async def start(msg: types.Message):
    # 1. Проверка подписки
    if not await check_sub(msg.from_user.id):
        return await msg.answer(f"❌ **Нет подписки!**\nДля доступа подпишись:", 
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Подписаться", url=REQUIRED_CHANNEL_URL)]]))

    # 2. Проверка доступа
    conn = sqlite3.connect(DB_NAME)
    res = conn.execute("SELECT approved FROM whitelist WHERE user_id=?", (msg.from_user.id,)).fetchone()
    conn.close()
    
    if not res:
        conn = sqlite3.connect(DB_NAME)
        conn.execute("INSERT INTO whitelist (user_id) VALUES (?)", (msg.from_user.id,))
        conn.commit(); conn.close()
        if ADMIN_ID: await bot.send_message(ADMIN_ID, f"Запрос доступа: {msg.from_user.id} (@{msg.from_user.username})", 
                                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅", callback_data=f"ap_{msg.from_user.id}")]]))
        return await msg.answer("🔒 Ожидайте одобрения админа.")
    
    if res[0] == 0: return await msg.answer("🔒 Ожидание...")

    await msg.answer("🔱 **Imperator v17.1**\nГотов к работе.", reply_markup=kb_main())

@dp.callback_query(F.data.startswith("ap_"))
async def approve(cb: types.CallbackQuery):
    uid = int(cb.data.split("_")[1])
    conn = sqlite3.connect(DB_NAME); conn.execute("UPDATE whitelist SET approved=1 WHERE user_id=?", (uid,)); conn.commit(); conn.close()
    await bot.send_message(uid, "✅ Доступ открыт! /start")
    await cb.answer("Одобрено")

@dp.callback_query(F.data == "menu")
async def back_menu(cb: types.CallbackQuery):
    await cb.message.edit_text("Главное меню", reply_markup=kb_main())

@dp.callback_query(F.data == "settings")
async def settings_menu(cb: types.CallbackQuery):
    await cb.message.edit_text(f"🔥 **Режим грева:** {CURRENT_MODE}", reply_markup=kb_settings())

@dp.callback_query(F.data.startswith("set_"))
async def set_mode(cb: types.CallbackQuery):
    global CURRENT_MODE
    CURRENT_MODE = cb.data.split("_")[1]
    await cb.message.edit_text(f"✅ Режим установлен: **{CURRENT_MODE}**", reply_markup=kb_main())

@dp.callback_query(F.data == "stats")
async def stats(cb: types.CallbackQuery):
    phones = db_get_active_phones()
    await cb.answer(f"📱 Активных: {len(phones)}\n💻 {get_sys_status()}", show_alert=True)

# --- ADD ACCOUNT LOGIC (AUTO-INPUT FIX) ---
@dp.callback_query(F.data == "add_acc")
async def add_start(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("📞 Введите номер телефона (только цифры):")
    await state.set_state(BotStates.waiting_phone)

@dp.message(BotStates.waiting_phone)
async def add_process(msg: types.Message, state: FSMContext):
    phone = "".join(filter(str.isdigit, msg.text))
    await state.clear()
    
    m = await msg.answer(f"🚀 Запускаю браузер для +{phone}...\nПытаюсь переключиться на код привязки...")
    
    async with BROWSER_SEMAPHORE:
        try:
            driver, ua, res, plat = await asyncio.to_thread(get_driver, phone)
            ACTIVE_DRIVERS[phone] = {"driver": driver, "ua": ua, "res": res, "plat": plat}
            
            await asyncio.to_thread(driver.get, "https://web.whatsapp.com")
            
            # 🔥 СКРИПТ: ИЩЕМ КНОПКУ 'Link with phone' И ЖМЕМ ЕЁ 🔥
            driver.execute_script(f"""
                var attempts = 0;
                var check = setInterval(function(){{
                    // 1. Ищем кнопку "Link with phone number" (Связать с номером)
                    // Ищем по разным селекторам, так как WA меняет верстку
                    var linkBtn = document.querySelector('span[role="button"]');
                    if(linkBtn && (linkBtn.innerText.includes('Link') || linkBtn.innerText.includes('Связать'))) {{
                         linkBtn.click();
                    }}
                    
                    // Попытка найти кнопку по тексту через XPath (самый надежный способ)
                    var xpathLink = document.evaluate("//*[contains(text(), 'Link with phone') or contains(text(), 'Связать')]", document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                    if(xpathLink) xpathLink.click();

                    // 2. Если появилось поле ввода - пишем номер
                    var inp = document.querySelector('input[type="text"]');
                    if(inp) {{
                        clearInterval(check);
                        inp.focus();
                        document.execCommand('selectAll');
                        document.execCommand('delete');
                        document.execCommand('insertText', false, '+{phone}');
                        
                        setTimeout(function(){{
                            // Жмем Далее
                            var b = document.querySelector('button.type-primary') || document.querySelector('div[role="button"][class*="primary"]');
                            if(b) b.click();
                        }}, 500);
                    }}
                    
                    if(attempts++ > 30) clearInterval(check);
                }}, 1000);
            """)
            
            # Ждем 10 секунд, чтобы код успел сгенерироваться
            await asyncio.sleep(10)
            
            # Делаем скриншот
            png = await asyncio.to_thread(driver.get_screenshot_as_png)
            await m.delete()
            await msg.answer_photo(
                BufferedInputFile(png, "code.png"), 
                caption=f"✅ Номер +{phone} введен!\n\n**Введи этот код на телефоне.**\nЕсли кода нет на скрине - нажми 'Обновить код'.", 
                reply_markup=kb_login_process(phone)
            )
            
        except Exception as e:
            await m.edit_text(f"Error: {e}")

@dp.callback_query(F.data.startswith("getcode_"))
async def get_code_manual(cb: types.CallbackQuery):
    p = cb.data.split("_")[1]
    d = ACTIVE_DRIVERS.get(p)
    if d:
        await asyncio.sleep(1) # Небольшая пауза
        png = await asyncio.to_thread(d['driver'].get_screenshot_as_png)
        await cb.message.answer_photo(BufferedInputFile(png, "code.png"), caption="Актуальный экран:")
    await cb.answer()

@dp.callback_query(F.data.startswith("finish_"))
async def finish(cb: types.CallbackQuery):
    p = cb.data.split("_")[1]
    d = ACTIVE_DRIVERS.pop(p, None)
    if d:
        db_save(p, d['ua'], d['res'], d['plat'])
        try: await asyncio.to_thread(d['driver'].quit)
        except: pass
    await cb.message.edit_text(f"✅ Аккаунт {p} добавлен в HIVE (Сетку)!")

# ==========================================
# 🚜 HIVE MIND FARM (ОБЩЕНИЕ БОТОВ)
# ==========================================
async def hive_worker(phone, created_at):
    driver = None
    try:
        active_phones = db_get_active_phones()
        # Ищем цель (другого бота)
        targets = [t for t in active_phones if t != phone]
        target_phone = random.choice(targets) if targets else phone
        
        async with BROWSER_SEMAPHORE:
            logger.info(f"🐝 {phone} -> {target_phone} ({CURRENT_MODE})")
            driver, ua, res, plat = await asyncio.to_thread(get_driver, phone)
            
            await asyncio.to_thread(driver.get, f"https://web.whatsapp.com/send?phone={target_phone}")
            wait = WebDriverWait(driver, 60)
            
            try:
                # Ждем поле ввода
                inp = wait.until(EC.presence_of_element_located((By.XPATH, "//footer//div[@contenteditable='true']")))
                
                text = ai_engine.generate()
                for char in text:
                    inp.send_keys(char)
                    await asyncio.sleep(random.uniform(0.05, 0.2))
                inp.send_keys(Keys.ENTER)
                
                # Обновляем БД
                conn = sqlite3.connect(DB_NAME)
                conn.execute("UPDATE accounts SET last_act=? WHERE phone=?", (datetime.now(), phone))
                conn.commit(); conn.close()
                
                logger.info(f"✅ Sent: '{text}'")
                await asyncio.sleep(2)
                
            except TimeoutException:
                # Проверка на бан
                src = driver.page_source.lower()
                if "not allowed" in src or "spam" in src or "banned" in src:
                    db_ban(phone)
                    shutil.rmtree(os.path.join(SESSIONS_DIR, phone), ignore_errors=True)
                    logger.error(f"💀 BAN: {phone} is dead.")

    except Exception as e:
        logger.error(f"Hive Error {phone}: {e}")
    finally:
        if driver: 
            try: await asyncio.to_thread(driver.quit)
            except: pass

async def hive_loop():
    logger.info("🐝 NEURAL HIVE STARTED")
    while True:
        try:
            min_d, max_d = HEAT_MODES[CURRENT_MODE]
            my_accounts = db_get_targets_for_instance()
            
            if not my_accounts:
                await asyncio.sleep(30)
                continue
                
            for phone, created_at in my_accounts:
                if phone in ACTIVE_DRIVERS: continue
                await hive_worker(phone, created_at)
                await asyncio.sleep(random.randint(10, 20))
            
            slp = random.randint(min_d, max_d)
            logger.info(f"💤 Hive sleep: {slp}s")
            await asyncio.sleep(slp)
            
        except Exception as e:
            logger.error(f"Loop Err: {e}")
            await asyncio.sleep(10)

async def main():
    cleanup_zombie()
    db_init()
    asyncio.create_task(hive_loop())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
