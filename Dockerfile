# 1. Базовый образ
FROM python:3.11-slim

# 2. Устанавливаем ВСЕ зависимости для Chrome (чтобы не было Tab Crashed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    curl \
    gnupg \
    ca-certificates \
    unzip \
    # === КРИТИЧЕСКИЕ БИБЛИОТЕКИ ДЛЯ CHROME ===
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxi6 \
    libxrandr2 \
    libxrender1 \
    libxss1 \
    libxtst6 \
    libnss3 \
    libnspr4 \
    libasound2 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libgbm1 \
    libglib2.0-0 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    fonts-liberation \
    fonts-noto-color-emoji \
    # ==========================================
    && rm -rf /var/lib/apt/lists/*

# 3. Устанавливаем Google Chrome Stable (Последняя версия)
RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends google-chrome-stable \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# 4. Настройка рабочей папки
WORKDIR /app

# 5. Установка библиотек Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 6. Копируем код бота (убедитесь, что файл называется так же!)
COPY wa_final_bot.py .

# 7. Запуск
ENV CHROME_EXECUTABLE_PATH=/usr/bin/google-chrome
# Увеличиваем размер разделяемой памяти для Chrome (важно для Docker!)
ENV SHM_SIZE=2g 
CMD ["python", "wa_final_bot.py"]
