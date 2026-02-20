# 1. Используем официальный образ Playwright
FROM mcr.microsoft.com/playwright/python:v1.41.0-jammy

# 2. Настраиваем системный часовой пояс
ENV TZ=Asia/Almaty
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# 3. Ставим системные пакеты (OCR и видео) БЕЗ лишнего мусора
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    tesseract-ocr-rus \
    ffmpeg \
    libsm6 \
    libxext6 \
    && rm -rf /var/lib/apt/lists/*

# 4. Копируем и ставим Python зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. УДАЛЯЕМ ЛИШНИЕ БРАУЗЕРЫ (Оставляем только Chromium для экономии места и ОЗУ)
RUN rm -rf /ms-playwright/firefox* /ms-playwright/webkit*

# 6. Копируем весь код
COPY . .

# 7. Подготавливаем права (fix для Linux/Aeza/BotHost)
RUN mkdir -p sessions && \
    touch imp17.db && \
    chmod -R 777 /app

# 8. Запуск
CMD ["python", "main.py"]
