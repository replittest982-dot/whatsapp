FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Фиксируем путь браузеров и скачиваем Chromium
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install chromium && playwright install-deps chromium

COPY . .

RUN mkdir -p /app/sessions

CMD ["python", "-u", "main.py"]
