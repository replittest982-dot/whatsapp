FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

# ✅ Системные зависимости
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-rus \
    tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# ✅ Копируем requirements
COPY requirements.txt .

# ✅ Устанавливаем Python пакеты
RUN pip install --no-cache-dir -r requirements.txt

# ✅ Playwright browsers (КРИТИЧНО!)
RUN playwright install chromium
RUN playwright install-deps

# Копируем код
COPY . .

# Создаем директории
RUN mkdir -p /app/sessions /app/logs /app/backups

# Права
RUN chmod -R 755 /app

# Запуск
CMD ["python", "-u", "main.py"]
