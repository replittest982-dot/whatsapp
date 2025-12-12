# 1. Базовый образ
FROM python:3.11-slim

# 2. Устанавливаем только необходимые инструменты для установки Chrome
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    gnupg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 3. Устанавливаем Google Chrome (Chrome сам установит все необходимые зависимости)
RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends google-chrome-stable \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# 6. Настройка рабочей папки
WORKDIR /app

# 7. Установка библиотек Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 8. Копируем код бота
COPY wa_final_bot.py .

# 9. Задаем переменную для Chrome и запускаем
ENV CHROME_EXECUTABLE_PATH=/usr/bin/google-chrome
CMD ["python", "wa_final_bot.py"]
