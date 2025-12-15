# 1. Базовый образ
FROM python:3.11-slim

# 2. Установка системных зависимостей + ЛОКАЛИ И ВРЕМЯ
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl unzip gnupg ca-certificates jq tzdata locales \
    libx11-6 libx11-xcb1 libxcb1 libxcomposite1 libxcursor1 \
    libxdamage1 libxext6 libxfixes3 libxi6 libxrandr2 libxrender1 \
    libxss1 libxtst6 libnss3 libnspr4 libasound2 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdbus-1-3 libdrm2 libgbm1 \
    libglib2.0-0 libpango-1.0-0 libpangocairo-1.0-0 \
    fonts-liberation fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# Настройка времени на Европу (Амстердам), раз ты меняешь сервер на Нидерланды
ENV TZ=Europe/Amsterdam
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 3. Установка Chrome
RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends google-chrome-stable \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 4. Умная установка драйвера
RUN CHROME_VER=$(google-chrome --version | awk '{print $3}') \
    && echo "🔥 Installed Chrome: $CHROME_VER" \
    && URL="https://storage.googleapis.com/chrome-for-testing-public/$CHROME_VER/linux64/chromedriver-linux64.zip" \
    && wget -q -O /tmp/chromedriver.zip "$URL" \
    || (echo "⚠️ Exact version not found, finding closest match..." \
        && MAJOR=$(echo $CHROME_VER | cut -d. -f1) \
        && LATEST_URL=$(curl -s "https://googlechromelabs.github.io/chrome-for-testing/latest-versions-per-milestone-with-downloads.json" | jq -r ".milestones.\"$MAJOR\".downloads.chromedriver[] | select(.platform == \"linux64\") | .url") \
        && wget -q -O /tmp/chromedriver.zip "$LATEST_URL") \
    && unzip -o /tmp/chromedriver.zip -d /usr/local/bin/ \
    && mv /usr/local/bin/chromedriver-linux64/chromedriver /usr/local/bin/chromedriver \
    && chmod +x /usr/local/bin/chromedriver \
    && rm -rf /tmp/chromedriver.zip /usr/local/bin/chromedriver-linux64

# 5. Финал
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY wa_final_bot.py .

ENV CHROME_EXECUTABLE_PATH=/usr/bin/google-chrome
ENV SHM_SIZE=2g

CMD ["python", "wa_final_bot.py"]
