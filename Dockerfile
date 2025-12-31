FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV TZ=Europe/Moscow

# Установка Chromium и драйвера (и кучи библиотек для стабильности)
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl unzip jq tzdata build-essential \
    chromium \
    chromium-driver \
    fonts-liberation libasound2 libnspr4 libnss3 libx11-xcb1 libxcomposite1 \
    libxcursor1 libxdamage1 libxext6 libxfixes3 libxi6 libxrandr2 libxrender1 \
    libxss1 libxtst6 xdg-utils libgbm1 libatk-bridge2.0-0 libgtk-3-0 \
    && ln -fs /usr/share/zoneinfo/$TZ /etc/localtime \
    && dpkg-reconfigure -f noninteractive tzdata \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .

RUN mkdir -p /app/sessions /app/tmp_chrome_data && chmod -R 777 /app/sessions /app/tmp_chrome_data

CMD ["python", "main.py"]
