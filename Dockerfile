# 1. Используем легкий образ Python
FROM python:3.11-slim

# 2. Настройка переменных окружения
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CHROME_BIN=/usr/bin/google-chrome \
    TZ=Asia/Almaty \
    DISPLAY=""

# 3. Установка системных зависимостей и шрифтов (Критично для WA Web)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl unzip gnupg ca-certificates jq tzdata \
    # Библиотеки для Chrome
    libnss3 libatk-bridge2.0-0 libcups2 libdrm2 libgbm1 libasound2 \
    libxcomposite1 libxdamage1 libxrandr2 libpango-1.0-0 libxkbcommon0 \
    # Шрифты (Важно, чтобы ИИ видел текст на странице без квадратиков)
    fonts-ipafont-gothic fonts-wqy-zenhei fonts-kacst fonts-freefont-ttf \
    # Инструменты сборки
    build-essential pkg-config libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# 4. Установка Google Chrome Stable
RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# 5. Рабочая директория
WORKDIR /app

# 6. Установка Python зависимостей (используем кэш для скорости)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 7. Копирование кода
COPY . .

# 8. Создание необходимых директорий и права доступа
RUN mkdir -p /app/sessions /app/media /app/logs /app/tmp_chrome_data && \
    chmod -R 777 /app

# 9. Healthcheck для мониторинга живого процесса
HEALTHCHECK --interval=60s --timeout=15s --start-period=10s --retries=3 \
    CMD pgrep -f "python main.py" || exit 1

# 10. Запуск от не-root пользователя (рекомендуется для безопасности)
# USER 1000 

CMD ["python", "main.py"]
