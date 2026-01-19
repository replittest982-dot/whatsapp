# Использование 3.11-slim оправдано для экономии места
FROM python:3.11-slim

# Настройка переменных окружения
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DEBIAN_FRONTEND=noninteractive

# 1. Установка системных зависимостей + Tesseract OCR (КРИТИЧНО для v38.0)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    tesseract-ocr \
    tesseract-ocr-rus \
    tesseract-ocr-eng \
    libtesseract-dev \
    # Дополнительные либы для корректной работы шрифтов в Chromium
    fonts-liberation \
    fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 2. Оптимизация установки Python зависимостей (кэширование слоев)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 3. Установка Chromium и системных зависимостей для Playwright
# Флаг --with-deps установит все необходимые .so библиотеки для браузера
RUN playwright install chromium && playwright install-deps chromium

# 4. Копирование кода
COPY . .

# 5. Подготовка структуры папок (важно для монтирования томов)
RUN mkdir -p sessions logs tmp && chmod -R 777 sessions logs tmp

# Лимит памяти для Chrome лучше задавать при запуске (shm-size), 
# но в контейнере мы просто запускаем бота
CMD ["python", "main.py"]
