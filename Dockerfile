# Используем официальный образ Playwright, в котором уже есть все браузеры
FROM mcr.microsoft.com/playwright/python:v1.41.0-jammy

WORKDIR /app

# Устанавливаем Tesseract OCR и дополнительные библиотеки для работы с фото и видео
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-rus \
    ffmpeg \
    libsm6 \
    libxext6 \
    && rm -rf /var/lib/apt/lists/*

# Копируем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь код бота
COPY . .

# Создаем папку сессий и файл базы данных заранее, даем права (fix для Aeza/Linux)
RUN mkdir -p sessions && \
    touch imp17.db && \
    chmod -R 777 /app

# Команда запуска
CMD ["python", "main.py"]
