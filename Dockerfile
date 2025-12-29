# 1. Базовый образ (Легкий Python)
FROM python:3.11-slim

# 2. Переменные окружения для стабильности
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
# Указываем Chrome, чтобы Selenium его нашел
ENV CHROME_BIN=/usr/bin/google-chrome

# 3. Установка системных зависимостей + Timezone
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl unzip gnupg ca-certificates jq tzdata \
    libx11-6 libx11-xcb1 libxcb1 libxcomposite1 libxcursor1 \
    libxdamage1 libxext6 libxfixes3 libxi6 libxrandr2 libxrender1 \
    libxss1 libxtst6 libnss3 libnspr4 libasound2 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdbus-1-3 libdrm2 libgbm1 \
    libglib2.0-0 libpango-1.0-0 libpangocairo-1.0-0 \
    fonts-liberation fonts-noto-color-emoji \
    && ln -fs /usr/share/zoneinfo/Europe/Moscow /etc/localtime \
    && dpkg-reconfigure -f noninteractive tzdata \
    && rm -rf /var/lib/apt/lists/*

# 4. Установка Google Chrome (Stable)
RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# 5. Умная установка Chromedriver (под версию Chrome)
RUN CHROME_VER=$(google-chrome --version | awk '{print $3}') \
    && echo "🔥 Installed Chrome: $CHROME_VER" \
    && MAJOR=$(echo $CHROME_VER | cut -d. -f1) \
    # Пытаемся скачать драйвер через JSON API Google
    && LATEST_DRIVER_URL=$(curl -s "https://googlechromelabs.github.io/chrome-for-testing/latest-versions-per-milestone-with-downloads.json" | jq -r ".milestones.\"$MAJOR\".downloads.chromedriver[] | select(.platform == \"linux64\") | .url") \
    && echo "⬇️ Downloading driver from: $LATEST_DRIVER_URL" \
    && wget -q -O /tmp/chromedriver.zip "$LATEST_DRIVER_URL" \
    && unzip -o /tmp/chromedriver.zip -d /tmp/ \
    && mv /tmp/chromedriver-linux64/chromedriver /usr/local/bin/chromedriver \
    && chmod +x /usr/local/bin/chromedriver \
    && rm -rf /tmp/chromedriver.zip /tmp/chromedriver-linux64

# 6. Настройка рабочей папки
WORKDIR /app

# Копируем зависимости и ставим их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код бота (Убедись, что файл называется main.py или wa_final_bot.py)
COPY main.py .

# 7. 🔥 ВАЖНО: Создаем папки для сессий и TMP (Fix Crash)
# Даем права 777, чтобы любой юзер мог писать туда
RUN mkdir -p /app/sessions /app/tmp_chrome_data \
    && chmod -R 777 /app/sessions /app/tmp_chrome_data

# Запуск
CMD ["python", "main.py"]
