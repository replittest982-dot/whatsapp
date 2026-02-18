FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

# Минимум системных зависимостей — tesseract убран (не нужен для QR-входа)
RUN apt-get update && apt-get install -y \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Только Chromium — без Firefox и WebKit
RUN playwright install chromium
RUN playwright install-deps chromium

COPY . .

RUN mkdir -p /app/sessions

CMD ["python", "-u", "main.py"]
