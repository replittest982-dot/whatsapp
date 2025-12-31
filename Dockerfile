FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV TZ=Europe/Moscow

# 1. Установка Chromium, драйвера и ВСЕХ библиотек для запуска
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl unzip jq tzdata build-essential \
    chromium \
    chromium-driver \
    # Пакет библиотек "На всякий случай" (fix missing .so errors)
    fonts-liberation libasound2 libnspr4 libnss3 libx11-xcb1 libxcomposite1 \
    libxcursor1 libxdamage1 libxext6 libxfixes3 libxi6 libxrandr2 libxrender1 \
    libxss1 libxtst6 xdg-utils libgbm1 libatk-bridge2.0-0 libgtk-3-0 \
    && ln -fs /usr/share/zoneinfo/$TZ /etc/localtime \
    && dpkg-reconfigure -f noninteractive tzdata \
    # 🔥 ХИТРОСТЬ: Ищем реальный бинарник драйвера (не скрипт) и ставим его в /usr/bin/chromedriver
    && DRIVER_PATH=$(dpkg -L chromium-driver | grep -E "chromedriver$" | head -n 1) \
    && echo "Real driver found at: $DRIVER_PATH" \
    && rm -f /usr/bin/chromedriver \
    && ln -s "$DRIVER_PATH" /usr/bin/chromedriver \
    && chmod +x /usr/bin/chromedriver \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

RUN mkdir -p /app/sessions /app/tmp_chrome_data && chmod -R 777 /app/sessions /app/tmp_chrome_data

CMD ["python", "main.py"]
