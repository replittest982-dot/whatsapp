# ... (твои импорты остаются те же)

# ==========================================
# ⚙️ CONFIGURATION (ПРАВКА)
# ==========================================
@dataclass
class Config:
    BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
    ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))
    PROXY_URL: str = os.getenv("PROXY_URL", "") # ФОРМАТ: http://user:pass@ip:port
    # ... остальные конфиги без изменений
    GEO_LAT: float = 43.2389
    GEO_LON: float = 76.8897
    TIMEZONE: str = "Asia/Almaty"

cfg = Config()

# ==========================================
# 🎮 PLAYWRIGHT CORE (ПРАВКА ПОД АЛМАТЫ И АНТИ-БАН)
# ==========================================

async def setup_browser(pw: Playwright, phone: str, device: dict) -> Tuple[BrowserContext, Page]:
    user_data = os.path.join(cfg.SESSIONS_DIR, phone)
    
    # Настройка прокси
    proxy_settings = None
    if cfg.PROXY_URL:
        proxy_settings = {"server": cfg.PROXY_URL}

    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir=user_data, 
        headless=True,
        proxy=proxy_settings, # ТУТ МЕНЯЕТСЯ IP
        args=[
            "--disable-blink-features=AutomationControlled", 
            "--no-sandbox", 
            "--disable-dev-shm-usage",
            f"--window-size={device['res']['width']},{device['res']['height']}"
        ],
        user_agent=device['ua'], 
        viewport=device['res'], 
        locale="ru-RU", 
        timezone_id=cfg.TIMEZONE,
        geolocation={"latitude": cfg.GEO_LAT, "longitude": cfg.GEO_LON}, 
        permissions=["geolocation"]
    )
    
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    # Скрываем автоматизацию через stealth
    await stealth_async(page)
    return ctx, page

# --- ИСПРАВЛЕННЫЙ FINISH_LOGIN (БЕЗ ОШИБОК EDIT_TEXT) ---
@dp.callback_query(F.data.startswith("done_"))
async def finish_login(cb: types.CallbackQuery, state: FSMContext):
    phone = cb.data.split("_")[1]
    sess = await ActiveSessions.get(phone)
    if not sess: return await cb.answer("❌ Сессия истекла", show_alert=True)
    
    # Вместо edit_text (который падает на фото), шлем новое сообщение
    status_msg = await cb.message.answer("⏳ Проверяю статус входа...")
    try:
        page = sess['context'].pages[0]
        try:
            await page.wait_for_selector(SELECTORS['chat_list'], timeout=20000)
            await db_add_account(phone, sess['ua'], sess['plat'], sess['res'], cb.from_user.id)
            await status_msg.edit_text(f"✅ Аккаунт +{phone} успешно добавлен!")
            await ActiveSessions.remove(phone)
            # Удаляем сообщение с QR, чтобы не висело
            try: await cb.message.delete()
            except: pass
        except:
            if await page.locator(SELECTORS['2fa_input']).count() > 0:
                await status_msg.edit_text("🔒 Введите 2FA PIN в чат:")
                await state.set_state(States.waiting_2fa)
                await state.update_data(phone=phone)
                return 
            raise Exception("No chat list")
    except Exception as e:
        await status_msg.edit_text(f"❌ Не удалось войти: {str(e)}")
        await ActiveSessions.remove(phone)

# ==========================================
# 🚜 FARM WORKER (УСИЛЕННЫЙ АНТИ-БАН)
# ==========================================
async def farm_worker(acc):
    # ... начало функции как у тебя
    try:
        await rate_limiter.acquire(phone, min_delay=rate_limit_sec)
        ctx, page = await setup_browser(pw, phone, device)
        
        # Рандомная пауза перед заходом
        await asyncio.sleep(random.uniform(5, 10))
        await page.goto("https://web.whatsapp.com", timeout=60000)
        
        # Имитация движения мыши
        await page.mouse.move(random.randint(0, 500), random.randint(0, 500))
        
        # ... (логика проверки бана)

        if mode == 'solo' or (mode == 'normal' and random.random() < 0.6):
            await page.click(SELECTORS['search_box'])
            await asyncio.sleep(random.uniform(1, 2))
            await human_type_v2(page, SELECTORS['search_box'], phone)
            await asyncio.sleep(1)
            await page.keyboard.press("Enter")
            
            await asyncio.sleep(random.uniform(2, 4)) # Пауза перед вводом сообщения
            
            if await page.locator(SELECTORS['input_box']).count() > 0:
                text = await ai.generate("self")
                # Печатаем с опечатками и задержками
                await human_type_v2(page, SELECTORS['input_box'], text)
                await asyncio.sleep(random.uniform(1, 3))
                await page.keyboard.press("Enter")
                
                await db_log_message(phone, phone, text, True, method='solo')
                logger.info(f"✅ {phone} SOLO OK")
        
        # Обновляем время активности, чтобы менеджер знал, что всё ок
        await db_update_act(phone, 'active')
        # Даем странице «подышать» перед закрытием
        await asyncio.sleep(random.uniform(5, 10))

    except Exception as e:
        logger.error(f"🚨 Ошибка фарма {phone}: {e}")
    finally:
        if ctx: await ctx.close()
            
