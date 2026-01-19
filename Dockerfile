FROM python:3.11-slim

# Системные зависимости и Tesseract
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl build-essential tesseract-ocr tesseract-ocr-rus \
    fonts-liberation fonts-noto-color-emoji libgbm1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Установка браузеров Playwright
RUN playwright install chromium && playwright install-deps chromium

COPY . .

# Создаем папки для работы
RUN mkdir -p sessions logs

# Запуск
CMD ["python", "main.py"]
