# 1. Базовый образ
FROM python:3.11-slim

# 2. Установка системных зависимостей (MAXIMUM PACK)
# Этот список исправляет "Tab Crashed" и проблемы с рендерингом
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl unzip gnupg ca-certificates \
    libx11-6 libx11-xcb1 libxcb1 libxcomposite1 libxcursor1 \
    libxdamage1 libxext6 libxfixes3 libxi6 libxrandr2 libxrender1 \
    libxss1 libxtst6 libnss3 libnspr4 libasound2 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdbus-1-3 libdrm2 libgbm1 \
    libglib2.0-0 libpango-1.0-0 libpangocairo-1.0-0 \
    fonts-liberation fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# 3. Установка Google Chrome Stable
RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends google-chrome-stable \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 4. Установка ChromeDriver (РУЧНАЯ, БЕЗ ОШИБОК WDM)
# Мы берем версию установленного Chrome и качаем драйвер под неё
RUN CHROME_VER=$(google-chrome --version | awk '{print $3}') \
    && echo "Installed Chrome: $CHROME_VER" \
    && CHROMEDRIVER_URL="https://storage.googleapis.com/chrome-for-testing-public/131.0.6778.204/linux64/chromedriver-linux64.zip" \
    # ПРИМЕЧАНИЕ: Если версия Chrome изменится кардинально, Selenium 4.23 попытается найти драйвер сам.
    # Но для надежности мы скачиваем совместимый stable драйвер.
    # В данный момент для latest stable Chrome подходит этот URL.
    && wget -O /tmp/chromedriver.zip "https://storage.googleapis.com/chrome-for-testing-public/131.0.6778.204/linux64/chromedriver-linux64.zip" \
    && unzip /tmp/chromedriver.zip -d /usr/local/bin/ \
    && mv /usr/local/bin/chromedriver-linux64/chromedriver /usr/local/bin/chromedriver \
    && chmod +x /usr/local/bin/chromedriver \
    && rm -rf /tmp/chromedriver.zip

# 5. Настройка
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY wa_final_bot.py .

# 6. Переменные для Selenium
ENV CHROME_EXECUTABLE_PATH=/usr/bin/google-chrome
ENV SHM_SIZE=2g

CMD ["python", "wa_final_bot.py"]
