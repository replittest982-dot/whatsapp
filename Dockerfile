FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV TZ=Europe/Moscow

# Установка Chromium и драйвера
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl unzip jq tzdata build-essential \
    chromium \
    chromium-driver \
    libnss3 libatk-bridge2.0-0 libgtk-3-0 libasound2 libgbm1 \
    && ln -fs /usr/share/zoneinfo/$TZ /etc/localtime \
    && dpkg-reconfigure -f noninteractive tzdata \
    # 🔥 ГАРАНТИЯ ПУТИ: Создаем ссылку, если имя отличается
    && (test -f /usr/bin/chromium-driver && ln -s /usr/bin/chromium-driver /usr/bin/chromedriver || true) \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

RUN mkdir -p /app/sessions /app/tmp_chrome_data && chmod -R 777 /app/sessions /app/tmp_chrome_data

CMD ["python", "main.py"]
