# 1. Базовый образ
FROM python:3.11-slim

# 2. Устанавливаем минимальные системные зависимости и ПАКЕТЫ СТАБИЛЬНОСТИ
# Эти пакеты критически важны для HEADLESS-режима и предотвращают "tab crashed"
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Базовые инструменты:
    wget \
    gnupg \
    ca-certificates \
    # 💥 Пакеты для HEADLESS-СТАБИЛЬНОСТИ:
    libnss3 \
    libxcomposite1 \
    libxrandr2 \
    libgbm1 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    # Очистка
    && rm -rf /var/lib/apt/lists/*

# 3. Устанавливаем Google Chrome
RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    # !!! Устанавливаем фиксированную стабильную версию Chrome 120 (вместо 'stable') !!!
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends google-chrome-stable=120.0.6099.109-1 \
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
