FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

# Установка системных зависимостей
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-rus \
    && rm -rf /var/lib/apt/lists/*

# Копируем requirements
COPY requirements.txt .

# Устанавливаем Python пакеты
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY main.py .

# Создаем директории
RUN mkdir -p /app/sessions /app/logs

# Права
RUN chmod -R 755 /app

# Запуск
CMD ["python", "-u", "main.py"]
