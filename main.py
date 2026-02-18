#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
🔱 IMPERATOR LITE — Оптимизировано для 2GB RAM
Функции: QR вход + само-сообщение
"""

import asyncio
import os
import logging
import random
import sys
import time
import re
import json
import aiosqlite
import io

from typing import Optional, Dict, Tuple
from datetime import datetime

from aiogram import Bot, Dispatcher, types, F
from aiogram.types import ErrorEvent, BufferedInputFile
from aiogram.filters import Command, StateFilter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

from playwright.async_api import async_playwright, Page, BrowserContext, Playwright
from playwright_stealth import stealth_async

# ==========================================
# ⚙️ CONFIG
# ==========================================
BOT_TOKEN  = os.getenv("BOT_TOKEN", "")
ADMIN_ID   = int(os.getenv("ADMIN_ID", "0"))
DB_NAME    = "lite.db"
SESSIONS   = os.path.abspath("./sessions")
os.makedirs(SESSIONS, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("ImpLite")

# Одновременно только 3 браузера → экономия RAM
BROWSER_SEM = asyncio.Semaphore(3)

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

async def db_get_farm_target(owner_id) -> Optional[dict]:
    async with aiosqlite.connect(DB_NAME) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""SELECT * FROM accounts
            WHERE status='active' AND owner_id=?
            ORDER BY last_act ASC LIMIT 1""", (owner_id,)) as c:
            r = await c.fetchone()
            return dict(r) if r else None

# ==========================================
# 🎭 BROWSER POOL (Singleton)
# ==========================================
_pw_instance = None

async def get_pw():
    global _pw_instance
    if not _pw_instance:
        _pw_instance = await async_playwright().start()
    return _pw_instance

async def stop_pw():
    global _pw_instance
    if _pw_instance:
        await _pw_instance.stop()
        _pw_instance = None

# Единственный UA — меньше памяти, меньше вариаций
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

async def open_browser(phone: str) -> Tuple[BrowserContext, Page]:
    pw = await get_pw()
    user_data = os.path.join(SESSIONS, phone)
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir=user_data,
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-translate",
            "--mute-audio",
            "--no-first-run",
            "--safebrowsing-disable-auto-update",
            "--window-size=1280,720",
            # Ключевые флаги экономии RAM:
            "--js-flags=--max-old-space-size=256",
            "--renderer-process-limit=1",
            "--single-process",          # ВАЖНО: один процесс вместо нескольких
        ],
        user_agent=UA,
        viewport={"width": 1280, "height": 720},
        locale="ru-RU",
    )
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    # Блокируем медиа и картинки → RAM -30%
    await page.route("**/*.{png,jpg,jpeg,gif,webp,svg,mp4,mp3,woff,woff2,ttf}", lambda r: r.abort())
    await stealth_async(page)
    return ctx, page

# ==========================================
# 📱 ACTIVE SESSIONS (для входа)
# ==========================================
class Sessions:
    _data: Dict[str, dict] = {}
    _lock = asyncio.Lock()

    @classmethod
    async def add(cls, phone, ctx, ua):
        async with cls._lock:
            cls._data[phone] = {"ctx": ctx, "ua": ua, "ts": time.time()}

    @classmethod
    async def get(cls, phone):
        async with cls._lock:
            return cls._data.get(phone)

    @classmethod
    async def remove(cls, phone):
        async with cls._lock:
            s = cls._data.pop(phone, None)
        if s:
            try: await s["ctx"].close()
            except: pass

    @classmethod
    async def cleanup(cls):
        """Убиваем сессии старше 5 минут"""
        now = time.time()
        async with cls._lock:
            dead = [p for p, d in cls._data.items() if now - d["ts"] > 300]
        for p in dead:
            await cls.remove(p)

# ==========================================
# 🚜 FARM (само-сообщение)
# ==========================================
SELF_MESSAGES = [
    "Записка себе: всё идёт по плану ✓",
    "Напоминание: проверить задачи",
    "Заметка: не забыть",
    "Тест соединения ок",
    "Онлайн",
    "Проверка ✓",
]

async def farm_one(phone: str):
    """Отправляет само-сообщение и закрывает браузер"""
    async with BROWSER_SEM:
        ctx = None
        try:
            ctx, page = await open_browser(phone)
            await page.goto("https://web.whatsapp.com", timeout=60000, wait_until="domcontentloaded")

            # Проверка бана
            content = await page.content()
            if any(w in content.lower() for w in ["banned", "suspended", "spam"]):
                await db_update_act(phone, "banned")
                log.warning(f"🚫 {phone} BANNED")
                return

            # Ждём загрузку
            try:
                await page.wait_for_selector('[data-testid="chat-list"]', timeout=40000)
            except:
                log.warning(f"⚠️ {phone} — чат-лист не загрузился")
                return

            # Открываем чат с собой
            await page.goto(f"https://web.whatsapp.com/send?phone={phone}",
                            timeout=30000, wait_until="domcontentloaded")

            input_sel = 'div[contenteditable="true"][data-testid="conversation-compose-box-input"]'
            try:
                await page.wait_for_selector(input_sel, timeout=20000)
            except:
                log.warning(f"⚠️ {phone} — инпут не найден")
                return

            text = random.choice(SELF_MESSAGES)
            await page.click(input_sel)
            await page.keyboard.type(text, delay=random.randint(50, 100))
            await asyncio.sleep(random.uniform(0.5, 1.2))
            await page.keyboard.press("Enter")

            await asyncio.sleep(2)
            await db_update_act(phone, "active")
            log.info(f"✅ {phone} → само-сообщение отправлено: {text}")

        except Exception as e:
            log.error(f"farm_one({phone}) error: {e}")
        finally:
            if ctx:
                try: await ctx.close()
                except: pass

async def farm_manager():
    """Фармим всех юзеров по очереди, пауза 10 минут между итерациями"""
    log.info("🚜 FARM MANAGER STARTED")
    while True:
        try:
            async with aiosqlite.connect(DB_NAME) as db:
                rows = await (await db.execute(
                    "SELECT phone FROM accounts WHERE status='active' ORDER BY last_act ASC"
                )).fetchall()

            for (phone,) in rows:
                await farm_one(phone)
                await asyncio.sleep(random.randint(30, 60))  # пауза между аккаунтами

        except Exception as e:
            log.error(f"farm_manager: {e}")

        await asyncio.sleep(600)  # 10 минут до следующего раунда

async def session_cleaner():
    while True:
        await Sessions.cleanup()
        await asyncio.sleep(120)

# ==========================================
# 🤖 BOT
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
        [InlineKeyboardButton(text="▶️ Запустить фарм", callback_data="run_farm")],
    ]
    if is_admin:
        kb.append([InlineKeyboardButton(text="👑 Одобрить заявки", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="menu")]])

# --- AUTH ---
async def is_allowed(user_id: int) -> bool:
    if user_id == ADMIN_ID: return True
    async with aiosqlite.connect(DB_NAME) as db:
        r = await (await db.execute("SELECT approved FROM whitelist WHERE user_id=?", (user_id,))).fetchone()
        return bool(r and r[0])

@dp.errors()
async def on_error(e: ErrorEvent):
    log.error(f"Bot error: {e.exception}", exc_info=True)

@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    uid = msg.from_user.id
    if not await is_allowed(uid):
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("INSERT OR IGNORE INTO whitelist (user_id, username) VALUES (?, ?)",
                             (uid, msg.from_user.username)); await db.commit()
        if ADMIN_ID:
            await bot.send_message(ADMIN_ID,
                f"🔔 Новая заявка: @{msg.from_user.username} (id={uid})",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_{uid}")
                ]]))
        return await msg.answer("🔒 Заявка отправлена. Ждите одобрения.")
    await msg.answer("🔱 **IMPERATOR LITE**\nВход по QR + само-сообщения",
                     reply_markup=main_kb(uid == ADMIN_ID))

@dp.callback_query(F.data == "menu")
async def cb_menu(cb: types.CallbackQuery):
    await cb.message.edit_text("🔱 **IMPERATOR LITE**",
                               reply_markup=main_kb(cb.from_user.id == ADMIN_ID))

# --- MY NUMBERS ---
@dp.callback_query(F.data == "my_numbers")
async def cb_my_numbers(cb: types.CallbackQuery):
    uid = cb.from_user.id
    async with aiosqlite.connect(DB_NAME) as db:
        rows = await (await db.execute(
            "SELECT phone, status, last_act FROM accounts WHERE owner_id=?", (uid,)
        )).fetchall()
    if not rows:
        return await cb.message.edit_text("📭 Нет аккаунтов", reply_markup=back_kb())

    lines = []
    for phone, status, last_act in rows:
        icon = "🟢" if status == "active" else "🔴"
        ago  = int((time.time() - last_act) / 60)
        lines.append(f"{icon} +{phone} — {ago} мин. назад")

    await cb.message.edit_text(
        "📱 **Ваши аккаунты:**\n\n" + "\n".join(lines),
        reply_markup=back_kb()
    )

# --- ADD QR ---
@dp.callback_query(F.data == "add_qr")
async def cb_add_qr(cb: types.CallbackQuery, state: FSMContext):
    await cb.message.edit_text("📱 Введите номер телефона (79...)")
    await state.set_state(St.phone)

@dp.message(StateFilter(St.phone))
async def proc_phone(msg: types.Message, state: FSMContext):
    phone = re.sub(r'\D', '', msg.text)
    if len(phone) < 10:
        return await msg.answer("❌ Неверный формат. Пример: 79001234567")

    st_msg = await msg.answer("🚀 Открываю браузер...")
    ctx = None
    try:
        ctx, page = await open_browser(phone)
        await page.goto("https://web.whatsapp.com", timeout=60000, wait_until="domcontentloaded")

        # Ждём QR
        await page.wait_for_selector("canvas", timeout=30000)
        await asyncio.sleep(1)

        screenshot_path = f"qr_{phone}.png"
        await page.screenshot(path=screenshot_path)

        await Sessions.add(phone, ctx, UA)
        ctx = None  # не закрываем — контекст живёт до подтверждения

        await msg.answer_photo(
            FSInputFile(screenshot_path),
            caption=f"📷 Отсканируйте QR в WhatsApp → Связанные устройства\n\nНомер: +{phone}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Я отсканировал", callback_data=f"qrdone_{phone}")
            ]])
        )
        try: os.remove(screenshot_path)
        except: pass

    except Exception as e:
        log.error(f"QR err {phone}: {e}")
        await msg.answer("❌ Ошибка запуска браузера. Попробуйте ещё раз.")
        if ctx:
            try: await ctx.close()
            except: pass

    await st_msg.delete()
    await state.clear()

@dp.callback_query(F.data.startswith("qrdone_"))
async def qr_done(cb: types.CallbackQuery, state: FSMContext):
    phone = cb.data.split("_", 1)[1]
    sess  = await Sessions.get(phone)
    if not sess:
        return await cb.answer("❌ Сессия истекла. Начните заново.", show_alert=True)

    await cb.message.edit_text("⏳ Проверяю вход...")
    try:
        page = sess["ctx"].pages[0]
        # Проверяем наличие чат-листа
        await page.wait_for_selector('[data-testid="chat-list"]', timeout=20000)
        await db_add_account(phone, sess["ua"], cb.from_user.id)
        await cb.message.edit_text(
            f"✅ **+{phone} добавлен!**\nАккаунт будет сам себе писать раз в ~10 минут.",
            reply_markup=back_kb()
        )
    except:
        # Проверяем 2FA
        try:
            pin_sel = 'div[role="textbox"][aria-label="PIN"]'
            page = sess["ctx"].pages[0]
            if await page.locator(pin_sel).count() > 0:
                await cb.message.edit_text("🔒 Требуется 2FA PIN — введите его:")
                await state.set_state(St.fa2)
                await state.update_data(phone=phone)
                return
        except: pass
        await cb.message.edit_text("❌ Вход не выполнен. Попробуйте заново.", reply_markup=back_kb())
        await Sessions.remove(phone)

@dp.message(StateFilter(St.fa2))
async def proc_2fa(msg: types.Message, state: FSMContext):
    data  = await state.get_data()
    phone = data.get("phone")
    sess  = await Sessions.get(phone)
    if not sess:
        return await msg.answer("❌ Сессия умерла. Начните заново.")
    try:
        page    = sess["ctx"].pages[0]
        pin_sel = 'div[role="textbox"][aria-label="PIN"]'
        await page.click(pin_sel)
        await page.keyboard.type(msg.text.strip(), delay=80)
        await page.wait_for_selector('[data-testid="chat-list"]', timeout=20000)
        await db_add_account(phone, sess["ua"], msg.from_user.id)
        await msg.answer(f"✅ +{phone} добавлен (2FA)!", reply_markup=back_kb())
    except:
        await msg.answer("❌ Неверный PIN.", reply_markup=back_kb())
    finally:
        await Sessions.remove(phone)
        await state.clear()

# --- RUN FARM MANUALLY ---
@dp.callback_query(F.data == "run_farm")
async def cb_run_farm(cb: types.CallbackQuery):
    uid = cb.from_user.id
    async with aiosqlite.connect(DB_NAME) as db:
        rows = await (await db.execute(
            "SELECT phone FROM accounts WHERE status='active' AND owner_id=?", (uid,)
        )).fetchall()

    if not rows:
        return await cb.answer("❌ Нет активных аккаунтов", show_alert=True)

    await cb.answer("▶️ Запускаю фарм...", show_alert=True)

    async def _run():
        for (phone,) in rows:
            await farm_one(phone)
            await asyncio.sleep(5)
        await bot.send_message(uid, f"✅ Фарм завершён. Аккаунтов: {len(rows)}", reply_markup=main_kb(uid == ADMIN_ID))

    asyncio.create_task(_run())

# --- ADMIN ---
@dp.callback_query(F.data == "admin")
async def cb_admin(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer("❌ Нет доступа", show_alert=True)
    async with aiosqlite.connect(DB_NAME) as db:
        reqs = await (await db.execute(
            "SELECT user_id, username FROM whitelist WHERE approved=0"
        )).fetchall()

    if not reqs:
        return await cb.message.edit_text("✅ Нет заявок", reply_markup=back_kb())

    kb = []
    for uid, uname in reqs:
        kb.append([InlineKeyboardButton(text=f"✅ @{uname} ({uid})", callback_data=f"approve_{uid}")])
    kb.append([InlineKeyboardButton(text="🔙", callback_data="menu")])
    await cb.message.edit_text(f"👑 Заявки ({len(reqs)}):", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("approve_"))
async def cb_approve(cb: types.CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    uid = int(cb.data.split("_")[1])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE whitelist SET approved=1 WHERE user_id=?", (uid,))
        await db.commit()
    await cb.answer("✅ Одобрено")
    try: await bot.send_message(uid, "✅ Ваша заявка одобрена! Напишите /start")
    except: pass
    await cb_admin(cb)

# ==========================================
# 🚀 MAIN
# ==========================================
async def main():
    if not BOT_TOKEN:
        log.critical("❌ BOT_TOKEN не задан!")
        return
    await db_init()
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("🔥 IMPERATOR LITE STARTED")

    await asyncio.gather(
        farm_manager(),
        session_cleaner(),
        dp.start_polling(bot),
    )

if __name__ == "__main__":
    if sys.platform != "win32":
        try:
            import uvloop
            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        except ImportError:
            pass
    asyncio.run(main())
