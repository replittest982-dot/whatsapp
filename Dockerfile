FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Almaty

# 1. Установка системных зависимостей и Chromium
# Важно: chromium устанавливается явно, чтобы binary точно был в системе
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl unzip jq tzdata build-essential \
    chromium \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 2. Установка Python-библиотек
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Копирование кода
COPY main.py .

# 4. Создание папок с правами
RUN mkdir -p /app/sessions /app/tmp_chrome_data /root/.wdm \
    && chmod -R 777 /app/sessions /app/tmp_chrome_data /root/.wdm

CMD ["python", "main.py"]
