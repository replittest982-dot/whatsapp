import os
import asyncio
import sqlite3
import random
import logging
import psutil
import shutil  # Для удаления папок
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
from selenium.common.exceptions import WebDriverException, TimeoutException

# ==========================================
# ⚙️ КОНФИГУРАЦИЯ
# ==========================================
INSTANCE_ID = int(os.getenv("INSTANCE_ID", "1"))
TOTAL_INSTANCES = int(os.getenv("TOTAL_INSTANCES", "1"))
BOT_TOKEN = os.getenv("BOT_TOKEN")
# Обязательно укажи свой ID как ADMIN_ID
try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
except:
    ADMIN_ID = 0

BROWSER_SEMAPHORE = asyncio.Semaphore(1) 
DB_PATH = "imperator_v16_3.db"
SESSION_DIR = "./sessions"

# Логирование
logging.basicConfig(level=logging.INFO, format=f'%(asctime)s | INST-{INSTANCE_ID} | %(levelname)s | %(message)s')
logger = logging.getLogger("Imperator")

fake = Faker('ru_RU')

if not os.path.exists(SESSION_DIR):
    os.makedirs(SESSION_DIR)

# Устройства
DEVICES = [
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0 Safari/537.36", "res": "1920,1080", "plat": "Win32"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/124.0.0.0 Safari/537.36", "res": "1440,900", "plat": "MacIntel"},
    {"ua": "Mozilla/5.0 (X11; Linux x86_64) Chrome/123.0.0.0 Safari/537.36", "res": "1366,768", "plat": "Linux x86_64"}
]

# Кэш активных драйверов
active_drivers = {}

class AddAccount(StatesGroup):
    waiting_phone = State()

# ==========================================
# 🛡️ SYSTEM & VALIDATION
# ==========================================

def is_memory_critical():
    mem = psutil.virtual_memory()
    free_mb = mem.available / 1024 / 1024
    return free_mb < 200

def validate_phone(phone: str) -> bool:
    """Проверка длины номера (чтобы не запускать браузер зря)"""
    if not phone.isdigit(): return False
    if len(phone) < 7 or len(phone) > 15: return False
    return True

def delete_session_folder(phone):
    """Полное удаление папки сессии"""
    path = os.path.join(SESSION_DIR, phone)
    if os.path.exists(path):
        try:
            shutil.rmtree(path)
            logger.info(f"Deleted session folder: {phone}")
            return True
        except Exception as e:
            logger.error(f"Error deleting session {phone}: {e}")
            return False
    return False

# ==========================================
# 🗄️ БАЗА ДАННЫХ
# ==========================================
def db_init():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    # Таблица аккаунтов
    cur.execute("""CREATE TABLE IF NOT EXISTS accounts (
        phone TEXT PRIMARY KEY, status TEXT, ua TEXT, res TEXT, plat TEXT, last_act DATETIME
    )""")
    # Таблица доступа (Whitelist)
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
# 🌐 SELENIUM CORE
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
    
    # Stealth Injection
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": f"Object.defineProperty(navigator, 'webdriver', {{get: () => undefined}}); "
                  f"Object.defineProperty(navigator, 'platform', {{get: () => '{plat}'}});"
    })
    driver.execute_cdp_cmd("Emulation.setGeolocationOverride", {"latitude": 43.2389, "longitude": 76.8897, "accuracy": 100})
    driver.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": "Asia/Almaty"})
    
    return driver, ua, res, plat

# ==========================================
# 🤖 BOT INTERFACE
# ==========================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- КЛАВИАТУРЫ ---
def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Новый аккаунт", callback_data="add_new")],
        [InlineKeyboardButton(text="📊 Статус системы", callback_data="sys_status")]
    ])

def kb_control(phone):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📷 ЧЕК", callback_data=f"c_{phone}"), InlineKeyboardButton(text="🔗 ВХОД", callback_data=f"l_{phone}")],
        [InlineKeyboardButton(text="⌨️ Ввести номер", callback_data=f"t_{phone}")],
        [InlineKeyboardButton(text="➡️ ЖМИ ДАЛЕЕ (ОК)", callback_data=f"n_{phone}")],
        [InlineKeyboardButton(text="✅ Я ВОШЕЛ (Сохр.)", callback_data=f"save_{phone}")],
        [InlineKeyboardButton(text="🗑 Удалить сессию", callback_data=f"del_{phone}")]
    ])

def kb_admin_decision(user_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять", callback_data=f"ap_{user_id}"),
         InlineKeyboardButton(text="🚫 Отклонить", callback_data=f"rj_{user_id}")]
    ])

# --- ХЕНДЛЕРЫ ДОСТУПА ---
@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    user_id = msg.from_user.id
    if db_check_access(user_id):
        await msg.answer(f"🔱 **Imperator v16.3**\nДобро пожаловать, Босс.", reply_markup=kb_main())
    else:
        db_add_request(user_id, msg.from_user.username)
        await msg.answer("🔒 **Доступ запрещен.**\nВаш запрос отправлен владельцу. Ожидайте.")
        if ADMIN_ID != 0:
            await bot.send_message(ADMIN_ID, f"👤 **Новый запрос!**\nID: {user_id}\nUser: @{msg.from_user.username}", 
                                   reply_markup=kb_admin_decision(user_id))

@dp.callback_query(F.data.startswith("ap_"))
async def approve_user(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    target_id = int(cb.data.split("_")[1])
    db_approve_user(target_id, True)
    await cb.message.edit_text(f"✅ Пользователь {target_id} одобрен.")
    try: await bot.send_message(target_id, "✅ **Доступ разрешен!** Нажмите /start")
    except: pass

@dp.callback_query(F.data.startswith("rj_"))
async def reject_user(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    target_id = int(cb.data.split("_")[1])
    db_approve_user(target_id, False)
    await cb.message.edit_text(f"🚫 Пользователь {target_id} отклонен.")

# --- ХЕНДЛЕРЫ БРАУЗЕРА ---
@dp.callback_query(F.data == "add_new")
async def add_start(cb: types.CallbackQuery, state: FSMContext):
    if is_memory_critical(): return await cb.answer("❌ Мало RAM!", show_alert=True)
    await cb.message.answer("📞 Введите номер телефона (7-15 цифр):")
    await state.set_state(AddAccount.waiting_phone)

@dp.message(AddAccount.waiting_phone)
async def phone_process(msg: types.Message, state: FSMContext):
    phone = "".join(filter(str.isdigit, msg.text))
    await state.clear()
    
    # ВАЛИДАЦИЯ НОМЕРА
    if not validate_phone(phone):
        return await msg.answer("❌ **Ошибка!** Номер слишком короткий или длинный.\nПопробуйте снова через меню.")

    msg_status = await msg.answer(f"🚀 Запуск Chrome для {phone}...")
    
    async with BROWSER_SEMAPHORE:
        try:
            driver, ua, res, plat = await asyncio.to_thread(get_driver, phone)
            active_drivers[phone] = {"driver": driver, "ua": ua, "res": res, "plat": plat}
            
            await asyncio.to_thread(driver.get, "https://web.whatsapp.com")
            await msg_status.edit_text(f"✅ Браузер готов!\n📱 Номер: {phone}\n\n👇 Используй панель:", reply_markup=kb_control(phone))
        except Exception as e:
            await msg_status.edit_text(f"❌ Crash: {str(e)[:50]}")

@dp.callback_query(F.data.startswith("c_"))
async def screen_check(cb: types.CallbackQuery):
    p = cb.data.split("_")[1]
    if p in active_drivers:
        try:
            png = await asyncio.to_thread(active_drivers[p]["driver"].get_screenshot_as_png)
            await cb.message.answer_photo(BufferedInputFile(png, "s.png"), caption=f"Status: {p}")
        except: await cb.answer("Ошибка скрина", show_alert=True)
    await cb.answer()

@dp.callback_query(F.data.startswith("l_"))
async def click_link(cb: types.CallbackQuery):
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
    await cb.answer("Клик 'Связать'")

@dp.callback_query(F.data.startswith("t_"))
async def type_number(cb: types.CallbackQuery):
    p = cb.data.split("_")[1]
    d = active_drivers.get(p)
    if not d: return

    # JS ВВОД
    js = f"""
        var i = document.querySelector('input[type="text"]') || document.querySelector('div[contenteditable="true"]');
        if(i) {{
            i.focus();
            document.execCommand('insertText', false, '{p}');
            i.dispatchEvent(new Event('input', {{bubbles:true}}));
        }}
    """
    d["driver"].execute_script(js)
    await cb.answer("Номер введен!")

@dp.callback_query(F.data.startswith("n_"))
async def click_next(cb: types.CallbackQuery):
    p = cb.data.split("_")[1]
    d = active_drivers.get(p)
    if not d: return
    
    # КЛИК ПО КНОПКЕ "ДАЛЕЕ"
    js = """
        var btns = document.querySelectorAll('[role="button"]');
        var found = false;
        btns.forEach(b => {
            if(b.innerText.includes("Next") || b.innerText.includes("Далее") || b.innerText.includes("OK")) {
                b.click();
                found = true;
            }
        });
        if(!found) {
             // Попытка нажать просто первую активную кнопку Primary
             var p = document.querySelector('button.type-primary');
             if(p) p.click();
        }
    """
    d["driver"].execute_script(js)
    await cb.answer("Нажато ДАЛЕЕ/ОК")

@dp.callback_query(F.data.startswith("save_"))
async def save_session(cb: types.CallbackQuery):
    """Я ВОШЕЛ: Сохраняем и чистим память"""
    p = cb.data.split("_")[1]
    data = active_drivers.pop(p, None)
    
    if data:
        db_save_acc(p, data['ua'], data['res'], data['plat'])
        try: 
            await asyncio.to_thread(data["driver"].quit)
        except: pass
        
    await cb.message.edit_text(f"✅ Аккаунт {p} сохранен и переведен в режим ФАРМА.\nБраузер закрыт для экономии памяти.")

@dp.callback_query(F.data.startswith("del_"))
async def delete_session_btn(cb: types.CallbackQuery):
    """УДАЛИТЬ СЕССИЮ: Удаляем файлы и из БД"""
    p = cb.data.split("_")[1]
    data = active_drivers.pop(p, None)
    
    # 1. Закрываем драйвер
    if data:
        try: await asyncio.to_thread(data["driver"].quit)
        except: pass
    
    # 2. Удаляем папку
    await asyncio.to_thread(delete_session_folder, p)
    
    # 3. Чистим БД
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM accounts WHERE phone=?", (p,))
    conn.commit()
    conn.close()
    
    await cb.message.edit_text(f"🗑 Сессия {p} полностью удалена (файлы + база).")

@dp.callback_query(F.data == "sys_status")
async def sys_stat(cb: types.CallbackQuery):
    mem = psutil.virtual_memory()
    await cb.answer(f"RAM Free: {mem.available/1024/1024:.0f} MB\nDrivers: {len(active_drivers)}", show_alert=True)

# ==========================================
# 🚜 ФАРМ (В фоне)
# ==========================================
async def farm_task(phone):
    if is_memory_critical(): return
    async with BROWSER_SEMAPHORE:
        try:
            driver, ua, res, plat = await asyncio.to_thread(get_driver, phone)
            await asyncio.to_thread(driver.get, "https://web.whatsapp.com")
            
            wait = WebDriverWait(driver, 40)
            wait.until(EC.presence_of_element_located((By.ID, "side"))) # Проверка входа
            
            # SOLO ФАРМ (Заметки)
            if random.random() < 0.8:
                driver.get(f"https://web.whatsapp.com/send?phone={phone}")
                inp = wait.until(EC.presence_of_element_located((By.XPATH, "//div[@contenteditable='true'][@data-tab='10']")))
                
                # Имитация печати
                text = fake.sentence()
                for char in text:
                    if random.random() < 0.05:
                        inp.send_keys("x")
                        await asyncio.sleep(0.1)
                        inp.send_keys(Keys.BACKSPACE)
                    inp.send_keys(char)
                    await asyncio.sleep(random.uniform(0.05, 0.15))
                
                inp.send_keys(Keys.ENTER)
                
                # Обновляем активность
                conn = sqlite3.connect(DB_PATH)
                conn.execute("UPDATE accounts SET last_act=? WHERE phone=?", (datetime.now(), phone))
                conn.commit()
                conn.close()
                
            await asyncio.sleep(random.randint(5, 15))
        except: pass
        finally:
            if 'driver' in locals():
                try: await asyncio.to_thread(driver.quit)
                except: pass

async def farm_loop():
    while True:
        await asyncio.sleep(60)
        conn = sqlite3.connect(DB_PATH)
        # Берем аккаунты только своего инстанса
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
