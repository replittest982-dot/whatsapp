import os
import asyncio
import sqlite3
import random
import logging
import psutil
import shutil
from datetime import datetime

# --- СТОРОННИЕ БИБЛИОТЕКИ ---
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile

from faker import Faker
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys

# ==========================================
# ⚙️ КОНФИГУРАЦИЯ
# ==========================================
INSTANCE_ID = int(os.getenv("INSTANCE_ID", "1"))
TOTAL_INSTANCES = int(os.getenv("TOTAL_INSTANCES", "1"))
BOT_TOKEN = os.getenv("BOT_TOKEN")
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
except:
    ADMIN_ID = 0

BROWSER_SEMAPHORE = asyncio.Semaphore(1) 
DB_PATH = "imperator_v16_final.db"
SESSION_DIR = "./sessions"

logging.basicConfig(level=logging.INFO, format=f'%(asctime)s | INST-{INSTANCE_ID} | %(levelname)s | %(message)s')
logger = logging.getLogger("Imperator")
fake = Faker('ru_RU')

if not os.path.exists(SESSION_DIR):
    os.makedirs(SESSION_DIR)

DEVICES = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0 Safari/537.36", "res": "1920,1080", "plat": "Win32"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/124.0.0.0 Safari/537.36", "res": "1440,900", "plat": "MacIntel"},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) Chrome/123.0.0.0 Safari/537.36", "res": "1366,768", "plat": "Linux x86_64"}
]

active_drivers = {}

class AddAccount(StatesGroup):
    waiting_phone = State()

# ==========================================
# 🛡️ SYSTEM UTILS
# ==========================================
def is_memory_critical():
    mem = psutil.virtual_memory()
    return (mem.available / 1024 / 1024) < 200

def validate_phone(phone: str) -> bool:
    return phone.isdigit() and 7 <= len(phone) <= 15

def delete_session_folder(phone):
    path = os.path.join(SESSION_DIR, phone)
    if os.path.exists(path):
        try:
            shutil.rmtree(path)
            return True
        except: return False
    return False

# ==========================================
# 🗄️ БАЗА ДАННЫХ
# ==========================================
def db_init():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS accounts (
        phone TEXT PRIMARY KEY, status TEXT, ua TEXT, res TEXT, plat TEXT, last_act DATETIME
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS whitelist (
        user_id INTEGER PRIMARY KEY, username TEXT, approved INTEGER DEFAULT 0
    )""")
    conn.commit()
    conn.close()

def db_check_access(user_id):
    if user_id == ADMIN_ID: return True
    conn = sqlite3.connect(DB_PATH)
    res = conn.execute("SELECT approved FROM whitelist WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return res and res[0] == 1

def db_add_request(user_id, username):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR IGNORE INTO whitelist (user_id, username, approved) VALUES (?, ?, 0)", (user_id, username))
    conn.commit()
    conn.close()

def db_approve_user(user_id, status):
    conn = sqlite3.connect(DB_PATH)
    if status:
        conn.execute("UPDATE whitelist SET approved=1 WHERE user_id=?", (user_id,))
    else:
        conn.execute("DELETE FROM whitelist WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def db_save_acc(phone, ua, res, plat):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT OR REPLACE INTO accounts VALUES (?, 'active', ?, ?, ?, ?)",
                 (phone, ua, res, plat, datetime.now()))
    conn.commit()
    conn.close()

# ==========================================
# 🌐 SELENIUM
# ==========================================
def get_driver(phone):
    conn = sqlite3.connect(DB_PATH)
    acc = conn.execute("SELECT ua, res, plat FROM accounts WHERE phone=?", (phone,)).fetchone()
    conn.close()
    
    ua, res, plat = (acc[0], acc[1], acc[2]) if acc else (DEVICES[0]['ua'], DEVICES[0]['res'], DEVICES[0]['plat'])
    
    opt = Options()
    opt.add_argument(f"--user-data-dir={os.path.abspath(os.path.join(SESSION_DIR, phone))}")
    opt.add_argument("--headless=new")
    opt.add_argument("--no-sandbox")
    opt.add_argument("--disable-gpu")
    opt.add_argument(f"--user-agent={ua}")
    opt.add_argument(f"--window-size={res}")
    opt.page_load_strategy = 'eager'
    
    driver = webdriver.Chrome(options=opt)
    
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": f"Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}}); "
                  f"Object.defineProperty(navigator, 'platform', {{get: () => '{plat}'}});"
    })
    driver.execute_cdp_cmd("Emulation.setGeolocationOverride", {"latitude": 43.2389, "longitude": 76.8897, "accuracy": 100})
    driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": "Asia/Almaty"})
    
    return driver, ua, res, plat

# ==========================================
# 🤖 BOT UI
# ==========================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

def kb_control(phone):
    # РАЗДЕЛЬНЫЕ КНОПКИ ДЛЯ КАЖДОГО ШАГА
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 1. ЧЕК ЭКРАНА", callback_data=f"c_{phone}")],
        [InlineKeyboardButton(text="🔗 2. НАЖАТЬ 'ВХОД ПО НОМЕРУ'", callback_data=f"l_{phone}")],
        [InlineKeyboardButton(text="⌨️ 3. ВПИСАТЬ ЦИФРЫ", callback_data=f"t_{phone}")],
        [InlineKeyboardButton(text="➡️ 4. ЖМИ ДАЛЕЕ (ОК)", callback_data=f"n_{phone}")],
        [InlineKeyboardButton(text="✅ ВСЕ ОК (СОХРАНИТЬ)", callback_data=f"save_{phone}")],
        [InlineKeyboardButton(text="🗑 УДАЛИТЬ СЕССИЮ", callback_data=f"del_{phone}")]
    ])

def kb_admin(uid):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"ap_{uid}"),
         InlineKeyboardButton(text="🚫 Отклонить", callback_data=f"rj_{uid}")]
    ])

# --- ACCESS ---
@dp.message(Command("start"))
async def start(msg: types.Message):
    uid = msg.from_user.id
    if db_check_access(uid):
        await msg.answer(f"🔱 **Imperator v16.3**\nСтатус: Online", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Новый аккаунт", callback_data="add_new")],
            [InlineKeyboardButton(text="📊 Статус", callback_data="sys_status")]
        ]))
    else:
        db_add_request(uid, msg.from_user.username)
        await msg.answer("🔒 Ожидайте подтверждения администратора.")
        if ADMIN_ID: await bot.send_message(ADMIN_ID, f"Запрос: {uid} (@{msg.from_user.username})", reply_markup=kb_admin(uid))

@dp.callback_query(F.data.startswith("ap_"))
async def ap(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    uid = int(cb.data.split("_")[1])
    db_approve_user(uid, True)
    await cb.message.edit_text(f"✅ {uid} принят.")
    try: await bot.send_message(uid, "✅ Доступ открыт! /start")
    except: pass

@dp.callback_query(F.data.startswith("rj_"))
async def rj(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    db_approve_user(int(cb.data.split("_")[1]), False)
    await cb.message.edit_text("🚫 Отклонено.")

# --- BROWSER ACTIONS ---
@dp.callback_query(F.data == "add_new")
async def add(cb: types.CallbackQuery, state: FSMContext):
    if is_memory_critical(): return await cb.answer("Мало RAM!", show_alert=True)
    await cb.message.answer("Введите номер (7-15 цифр):")
    await state.set_state(AddAccount.waiting_phone)

@dp.message(AddAccount.waiting_phone)
async def proc_phone(msg: types.Message, state: FSMContext):
    phone = "".join(filter(str.isdigit, msg.text))
    await state.clear()
    if not validate_phone(phone): return await msg.answer("Некорректный номер.")
    
    m = await msg.answer(f"🚀 Запуск {phone}...")
    async with BROWSER_SEMAPHORE:
        try:
            driver, ua, res, plat = await asyncio.to_thread(get_driver, phone)
            active_drivers[phone] = {"driver": driver, "ua": ua, "res": res, "plat": plat}
            await asyncio.to_thread(driver.get, "https://web.whatsapp.com")
            await m.edit_text(f"✅ Готов: {phone}\nСледуй шагам:", reply_markup=kb_control(phone))
        except Exception as e:
            await m.edit_text(f"Error: {e}")

@dp.callback_query(F.data.startswith("c_"))
async def check(cb: types.CallbackQuery):
    p = cb.data.split("_")[1]
    if p in active_drivers:
        try:
            png = await asyncio.to_thread(active_drivers[p]["driver"].get_screenshot_as_png)
            await cb.message.answer_photo(BufferedInputFile(png, "s.png"))
        except: await cb.answer("Ошибка фото", show_alert=True)
    await cb.answer()

@dp.callback_query(F.data.startswith("l_"))
async def lnk(cb: types.CallbackQuery):
    p = cb.data.split("_")[1]
    d = active_drivers.get(p)
    if not d: return
    d["driver"].execute_script("""
        var xpaths = ["//*[contains(text(), 'Link with phone')]", "//*[contains(text(), 'Связать')]", "//*[contains(text(), 'Log in')]"];
        for(var i=0;i<xpaths.length;i++){
            var el = document.evaluate(xpaths[i], document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
            if(el){ el.click(); break; }
        }
    """)
    await cb.answer("Нажато Вход")

@dp.callback_query(F.data.startswith("t_"))
async def typ(cb: types.CallbackQuery):
    p = cb.data.split("_")[1]
    d = active_drivers.get(p)
    if not d: return
    d["driver"].execute_script(f"""
        var i = document.querySelector('input[type="text"]') || document.querySelector('div[contenteditable="true"]');
        if(i) {{ i.focus(); document.execCommand('insertText', false, '{p}'); i.dispatchEvent(new Event('input', {{bubbles:true}})); }}
    """)
    await cb.answer("Номер введен")

@dp.callback_query(F.data.startswith("n_"))
async def nxt(cb: types.CallbackQuery):
    p = cb.data.split("_")[1]
    d = active_drivers.get(p)
    if not d: return
    # УСИЛЕННЫЙ ПОИСК КНОПКИ ДАЛЕЕ
    d["driver"].execute_script("""
        // 1. По тексту
        var buttons = document.querySelectorAll('[role="button"], button');
        var clicked = false;
        buttons.forEach(b => {
            var t = b.innerText.toLowerCase();
            if(!clicked && (t.includes('next') || t.includes('далее') || t.includes('ok'))) {
                b.click(); clicked = true;
            }
        });
        // 2. По классу Primary (если текст не сработал)
        if(!clicked) {
            var p = document.querySelector('div[role="button"][class*="primary"]');
            if(p) p.click();
        }
    """)
    await cb.answer("Попытка нажать ДАЛЕЕ")

@dp.callback_query(F.data.startswith("save_"))
async def sv(cb: types.CallbackQuery):
    p = cb.data.split("_")[1]
    data = active_drivers.pop(p, None)
    if data:
        db_save_acc(p, data['ua'], data['res'], data['plat'])
        try: await asyncio.to_thread(data["driver"].quit)
        except: pass
    await cb.message.edit_text(f"✅ {p} сохранен и ушел в фарм.")

@dp.callback_query(F.data.startswith("del_"))
async def dl(cb: types.CallbackQuery):
    p = cb.data.split("_")[1]
    data = active_drivers.pop(p, None)
    if data:
        try: await asyncio.to_thread(data["driver"].quit)
        except: pass
    delete_session_folder(p)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM accounts WHERE phone=?", (p,))
    conn.commit()
    conn.close()
    await cb.message.edit_text(f"🗑 {p} удален.")

@dp.callback_query(F.data == "sys_status")
async def st(cb: types.CallbackQuery):
    await cb.answer(f"RAM Free: {psutil.virtual_memory().available/1024/1024:.0f} MB", show_alert=True)

# ==========================================
# 🚜 ФАРМ (ИСПРАВЛЕННЫЙ)
# ==========================================
async def farm_task(phone):
    if is_memory_critical(): return
    async with BROWSER_SEMAPHORE:
        try:
            driver, ua, res, plat = await asyncio.to_thread(get_driver, phone)
            await asyncio.to_thread(driver.get, "https://web.whatsapp.com")
            
            # Ждем прогрузки
            wait = WebDriverWait(driver, 60)
            # Ждем появления списка чатов (значит вошли)
            wait.until(EC.presence_of_element_located((By.ID, "side"))) 
            await asyncio.sleep(5)

            # SOLO: Пишем самому себе (Самый надежный метод)
            # Используем прямую ссылку, чтобы открыть чат с собой
            driver.get(f"https://web.whatsapp.com/send?phone={phone}")
            
            # ИЩЕМ ПОЛЕ ВВОДА (УНИВЕРСАЛЬНЫЙ СЕЛЕКТОР)
            # Ищем contenteditable в footer (это всегда панель ввода)
            inp = wait.until(EC.presence_of_element_located((By.XPATH, "//footer//div[@contenteditable='true']")))
            
            # Печатаем
            text = fake.sentence()
            for char in text:
                inp.send_keys(char)
                await asyncio.sleep(random.uniform(0.05, 0.2))
            
            await asyncio.sleep(1)
            inp.send_keys(Keys.ENTER)
            logger.info(f"Farm success: {phone}")
            
            # Обновляем дату
            conn = sqlite3.connect(DB_PATH)
            conn.execute("UPDATE accounts SET last_act=? WHERE phone=?", (datetime.now(), phone))
            conn.commit()
            conn.close()

        except Exception as e:
            logger.error(f"Farm fail {phone}: {e}")
        finally:
            if 'driver' in locals():
                try: await asyncio.to_thread(driver.quit)
                except: pass

async def farm_loop():
    while True:
        await asyncio.sleep(40)
        conn = sqlite3.connect(DB_PATH)
        target = conn.execute(f"SELECT phone FROM accounts WHERE (rowid % {TOTAL_INSTANCES}) = ({INSTANCE_ID}-1) ORDER BY last_act ASC LIMIT 1").fetchone()
        conn.close()
        if target and target[0] not in active_drivers:
            asyncio.create_task(farm_task(target[0]))

async def main():
    db_init()
    asyncio.create_task(farm_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
