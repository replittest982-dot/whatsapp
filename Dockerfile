# 1. Базовый образ
FROM python:3.11-slim

# 2. Переменные окружения
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV TZ=Europe/Moscow

# 3. Установка Chromium и драйвера (Стабильная сборка Debian)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl unzip jq tzdata build-essential \
    chromium \
    chromium-driver \
    libnss3 libatk-bridge2.0-0 libgtk-3-0 libasound2 libgbm1 \
    && ln -fs /usr/share/zoneinfo/$TZ /etc/localtime \
    && dpkg-reconfigure -f noninteractive tzdata \
    # Очистка мусора
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 4. Настройка проекта
WORKDIR /app

# Копируем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY main.py .

# 5. Права на папки (Важно для Chromium)
RUN mkdir -p /app/sessions /app/tmp_chrome_data \
    && chmod -R 777 /app/sessions /app/tmp_chrome_data

# Указываем переменные для Selenium (на всякий случай)
ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_BIN=/usr/bin/chromedriver

CMD ["python", "main.py"]
