FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Системные зависимости (Chrome + Tesseract)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates build-essential \
    tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng \
    fonts-liberation fonts-noto-color-emoji \
    libgbm1 libnss3 libasound2 libxkbcommon0 libxrandr2 \
    libxdamage1 libxcomposite1 libxrender1 libxi6 \
    libpangocairo-1.0-0 libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ✅ КЛЮЧЕВОЙ ФИКС: Устанавливаем в ~/.cache И копируем в образ
RUN playwright install chromium && \
    playwright install-deps

# Создаем папки
RUN mkdir -p sessions logs tmp && chmod -R 777 sessions logs tmp

COPY . .

HEALTHCHECK --interval=30s CMD pgrep -f main.py || exit 1
CMD ["python", "-u", "main.py"]
