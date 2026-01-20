FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Системные зависимости + Tesseract
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl build-essential tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng \
    fonts-liberation fonts-noto-color-emoji libgbm1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Установка Chromium
RUN playwright install chromium && playwright install-deps chromium

COPY . .

# Папки для данных
RUN mkdir -p sessions logs

CMD ["python", "main.py"]
