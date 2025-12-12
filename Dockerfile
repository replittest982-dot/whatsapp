# 1. Базовый образ
FROM python:3.11-slim

# 2. Устанавливаем wget, gnupg и системные библиотеки
# ВАЖНО: Я УДАЛИЛ libgconf-2-4, как просил Александр
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    ca-certificates \
    libnss3 \
    libfontconfig \
    libx11-6 \
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
    --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# 3. Устанавливаем Google Chrome (исправленный метод для Debian Trixie)
# Очищаем кэш перед установкой для избежания ошибок dpkg
RUN apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/* \
    && wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# 6. Настройка рабочей папки
WORKDIR /app

# 7. Установка библиотек Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 8. Копируем код бота
COPY main.py .

# 9. Задаем переменную для Chrome и запускаем
ENV CHROME_EXECUTABLE_PATH=/usr/bin/google-chrome
CMD ["python", "main.py"]
