FROM python:3.11-slim

# Переменные
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python либы
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 🔥 УСТАНОВКА CHROMIUM (Самая важная часть)
RUN playwright install chromium --with-deps

# Код
COPY . .

# Создаем папки
RUN mkdir -p sessions logs tmp

CMD ["python", "main.py"]
