#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import os
import logging
import random
import sys
import time
import re
import aiosqlite

from typing import Optional, Dict, Tuple
from datetime import datetime

from aiogram import Bot, Dispatcher, types, F
from aiogram.types import ErrorEvent, BufferedInputFile
from aiogram.filters import Command, StateFilter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from playwright.async_api import async_playwright, Page, BrowserContext
from playwright_stealth import stealth_async

# ==========================================
# ⚙️ CONFIG
# ==========================================
BOT_TOKEN  = os.getenv("BOT_TOKEN", "")
ADMIN_ID   = int(os.getenv("ADMIN_ID", "0"))
# Формат прокси: http://user:pass@ip:port
PROXY_URL  = os.getenv("PROXY_URL", "") 
DB_NAME    = "lite.db"
SESSIONS   = os.path.abspath("./sessions")
os.makedirs(SESSIONS, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("ImpLite")

BROWSER_SEM = asyncio.Semaphore(2) # Для 2GB RAM лучше держать 2 одновременно

# ==========================================
# 🗄️ DATABASE
# ==========================================
async def db_init():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("""CREATE TABLE IF NOT EXISTS accounts (
            phone TEXT PRIMARY KEY,
            owner_id INTEGER,
            status TEXT DEFAULT 'active',
            last_act REAL DEFAULT 0,
            ua TEXT,
            created_at REAL
        )""")
        await db.execute("""CREATE TABLE IF NOT EXISTS whitelist (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            approved INTEGER DEFAULT 0
        )""")
        await db.commit()

async def db_add_account(phone, ua, owner_id):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""INSERT INTO accounts (phone, ua, owner_id, last_act, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(phone) DO UPDATE SET status='active', last_act=?""",
            (phone, ua, owner_id, time.time(), time.time(), time.time()))
        await db.commit()

async def db_update_act(phone, status='active'):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE accounts SET last_act=?, status=? WHERE phone=?",
                         (time.time(), status, phone))
        await db.commit()

# ==========================================
# 🎭 BROWSER ENGINE
# ==========================================
_pw_instance = None

async def get_pw():
    global _pw_instance
    if not _pw_instance:
        _pw_instance = await async_playwright().start()
    return _pw_instance

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

async def open_browser(phone: str) -> Tuple[BrowserContext, Page]:
    pw = await get_pw()
    user_data = os.path.join(SESSIONS, phone)
    
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--single-process",
    ]
    
    proxy_cfg = None
    if PROXY_URL:
        proxy_cfg = {"server": PROXY_URL}

    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir=user_data,
        headless=True,
        args=launch_args,
        user_agent=UA,
        proxy=proxy_cfg,
        viewport={"width": 1280, "height": 720},
        locale="ru-RU",
        timezone_id="Asia/Almaty" # Ставим время Алматы
    )
    
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    await page.route("**/*.{png,jpg,jpeg,gif,webp,svg,mp4,mp3,woff,woff2}", lambda r: r.abort())
    await stealth_async(page)
    return ctx, page

# ==========================================
# 📱 SESSIONS MANAGER
# ==========================================
class Sessions:
    _data: Dict[str, dict] = {}

    @classmethod
    async def add(cls, phone, ctx, ua):
        cls._data[phone] = {"ctx": ctx, "ua": ua, "ts": time.time()}

    @classmethod
    async def get(cls, phone):
        return cls._data.get(phone)

    @classmethod
    async def remove(cls, phone):
        s = cls._data.pop(phone, None)
        if s:
            try: await s["ctx"].close()
            except: pass

# ==========================================
# 🚜 FARM LOGIC (АНТИ-БАН)
# ==========================================
MESSAGES = ["Привет!", "Тут?", "Ок", "Запись сделана.", "Всё в порядке.", "Check."]

async def farm_one(phone: str):
    async with BROWSER_SEM:
        ctx = None
        try:
            ctx, page = await open_browser(phone)
            # Заходим с рандомной задержкой
            await asyncio.sleep(random.uniform(2, 5))
            await page.goto("https://web.whatsapp.com", timeout=60000)

            # Проверка загрузки (ждем список чатов)
            try:
                await page.wait_for_selector('[data-testid="chat-list"]', timeout=30000)
            except:
                log.warning(f"⚠️ {phone} — не залогинен или забанен")
                return

            # Имитация активности: открываем чат с самим собой
            await page.goto(f"https://web.whatsapp.com/send?phone={phone}")
            input_sel = 'div[contenteditable="true"][data-testid="conversation-compose-box-input"]'
            
            await page.wait_for_selector(input_sel, timeout=20000)
            await asyncio.sleep(random.uniform(1, 3))
            
            # Печатаем как человек
            await page.click(input_sel)
            msg_text = random.choice(MESSAGES)
            await page.keyboard.type(msg_text, delay=random.randint(100, 250))
            await asyncio.sleep(1)
            await page.keyboard.press("Enter")
            
            await db_update_act(phone)
            log.info(f"✅ {phone} отправил сообщение самому себе.")
            await asyncio.sleep(2)

        except Exception as e:
            log.error(f"❌ Ошибка в фарме {phone}: {e}")
        finally:
            if ctx: await ctx.close()

async def farm_manager():
    while True:
        async with aiosqlite.connect(DB_NAME) as db:
            rows = await (await db.execute("SELECT phone FROM accounts WHERE status='active'")).fetchall()
        
        for (phone,) in rows:
            await farm_one(phone)
            # Большая пауза между аккаунтами для безопасности
            await asyncio.sleep(random.randint(60, 120))
        
        await asyncio.sleep(1800) # Раз в 30 минут

# ==========================================
# 🤖 BOT HANDLERS
# ==========================================
bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

class St(StatesGroup):
    phone = State()
    fa2   = State()

def main_kb(is_admin=False):
    kb = [
        [InlineKeyboardButton(text="📱 Мои номера",  callback_data="my_numbers")],
        [InlineKeyboardButton(text="➕ Добавить QR", callback_data="add_qr")],
    ]
    if is_admin:
        kb.append([InlineKeyboardButton(text="👑 Админ-панель", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    await msg.answer("🔱 **IMPERATOR LITE**\nБот активен.", reply_markup=main_kb(msg.from_user.id == ADMIN_ID))

@dp.callback_query(F.data == "add_qr")
async def cb_add_qr(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.answer("Введите номер телефона (например, 79001234567):")
    await state.set_state(St.phone)

@dp.message(St.phone)
async def proc_phone(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    wait_msg = await msg.answer("⏳ Генерация QR... Пожалуйста, подождите.")
    
    try:
        ctx, page = await open_browser(phone)
        await page.goto("https://web.whatsapp.com", timeout=60000)
        await page.wait_for_selector("canvas", timeout=30000)
        
        path = f"qr_{phone}.png"
        await page.screenshot(path=path)
        await Sessions.add(phone, ctx, UA)

        await msg.answer_photo(FSInputFile(path), caption=f"Отсканируйте QR для +{phone}", 
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                                   InlineKeyboardButton(text="✅ Готово", callback_data=f"qrdone_{phone}")
                               ]]))
        os.remove(path)
    except Exception as e:
        await msg.answer(f"❌ Ошибка: {e}")
    finally:
        await wait_msg.delete()

@dp.callback_query(F.data.startswith("qrdone_"))
async def qr_done(cb: types.CallbackQuery):
    phone = cb.data.split("_")[1]
    sess = await Sessions.get(phone)
    if not sess:
        return await cb.answer("❌ Сессия не найдена", show_alert=True)

    # ИСПРАВЛЕНИЕ: Вместо edit_text шлем новое сообщение или answer
    status_msg = await cb.message.answer("📡 Проверяю статус входа...")
    
    try:
        page = sess["ctx"].pages[0]
        await page.wait_for_selector('[data-testid="chat-list"]', timeout=30000)
        await db_add_account(phone, sess["ua"], cb.from_user.id)
        await status_msg.edit_text(f"✅ Аккаунт +{phone} успешно добавлен!")
    except:
        await status_msg.edit_text("❌ Не удалось подтвердить вход. Попробуйте еще раз.")
    finally:
        await Sessions.remove(phone)

async def main():
    await db_init()
    asyncio.create_task(farm_manager())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
