# ==========================================
# 🏗️ STAGE 1: BASE & SYSTEM DEPS
# ==========================================
FROM python:3.11-slim AS base

# Переменные окружения для оптимизации Python и Playwright
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=off \
    DEBIAN_FRONTEND=noninteractive \
    # 🔥 ВАЖНО: Фиксируем путь браузеров, чтобы не качать их каждый раз в /root/.cache
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    # Путь для Tesseract OCR
    TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata/

# Установка системных пакетов (ОДНИМ СЛОЕМ для уменьшения размера)
# tesseract-ocr-* : Для чтения кодов
# fonts-* : Чтобы скриншоты были читаемыми
# libgbm1 : Нужен для headless chrome
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    tesseract-ocr \
    tesseract-ocr-rus \
    tesseract-ocr-eng \
    fonts-liberation \
    fonts-noto-color-emoji \
    libgbm1 \
    libnss3 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ==========================================
# 📦 STAGE 2: DEPENDENCIES & BROWSERS
# ==========================================
# Копируем только requirements сначала (кэширование Docker слоев)
COPY requirements.txt .

# Устанавливаем Python либы
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Устанавливаем Chromium (и его системные зависимости)
# --with-deps гарантирует, что Debian Slim получит всё для запуска браузера
RUN playwright install chromium && \
    playwright install-deps chromium

# ==========================================
# 🚀 STAGE 3: FINAL & RUN CODE
# ==========================================
# Копируем код (это меняется чаще всего, поэтому в конце)
COPY . .

# Создаем папки и даем права (на случай запуска не от root, но мы под root)
RUN mkdir -p /app/sessions /app/logs /app/tmp && \
    chmod -R 777 /app/sessions /app/logs /app/tmp

# HEALTHCHECK (Проверка жизни)
# Проверяет, что процесс python запущен. Если бот упал - контейнер станет unhealthy.
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD pgrep -f "main.py" || exit 1

# Запуск с буферизацией вывода (чтобы логи сразу летели в консоль)
CMD ["python", "-u", "main.py"]
