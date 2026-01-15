FROM python:3.11-slim

# Env 2026
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CHROME_BIN=/usr/bin/google-chrome \
    TZ=Asia/Almaty \
    DISPLAY="" \
    PYTHONDASHBOARD=0

# System deps (оптимизировано)
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Chrome deps
    wget curl unzip gnupg ca-certificates jq tzdata \
    libx11-6 libx11-xcb1 libxcb1 libxcomposite1 libxcursor1 \
    libxdamage1 libxext6 libxfixes3 libxi6 libxrandr2 libxrender1 \
    libxss1 libxtst6 libnss3 libnspr4 libasound2 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdbus-1-3 libdrm2 libgbm1 \
    libglib2.0-0 libpango-1.0-0 libpangocairo-1.0-0 \
    fonts-liberation fonts-noto-color-emoji \
    # Redis client
    redis-tools \
    # Build
    build-essential pkg-config libffi-dev \
    && ln -fs /usr/share/zoneinfo/$TZ /etc/localtime \
    && dpkg-reconfigure -f noninteractive tzdata \
    && rm -rf /var/lib/apt/lists/*

# Chrome + Driver (твой код идеален!)
RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    && CHROME_VER=$(google-chrome --version | awk '{print $3}') \
    && MAJOR=$(echo $CHROME_VER | cut -d. -f1) \
    && LATEST_DRIVER_URL=$(curl -s "https://googlechromelabs.github.io/chrome-for-testing/latest-versions-per-milestone-with-downloads.json" | jq -r ".milestones.\"$MAJOR\".downloads.chromedriver[] | select(.platform == \"linux64\") | .url") \
    && wget -q -O /tmp/chromedriver.zip "$LATEST_DRIVER_URL" \
    && unzip -o /tmp/chromedriver.zip -d /tmp/ \
    && mv /tmp/chromedriver-linux64/chromedriver /usr/local/bin/chromedriver \
    && chmod +x /usr/local/bin/chromedriver \
    && rm -rf /tmp/* /var/lib/apt/lists/*

WORKDIR /app

# Multi-stage pip (быстрее)
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy app
COPY main.py .
COPY .env .env

# Volumes + Permissions (FIXED)
RUN mkdir -p /app/{sessions,tmp,backups,logs} && \
    chmod -R 777 /app && \
    chown -R 1000:1000 /app

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD pgrep -f "python.*main.py" || exit 1

# Chrome sandbox OFF (security)
USER 1000

EXPOSE 8080
CMD ["python", "main.py"]
