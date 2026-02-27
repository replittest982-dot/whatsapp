cat > /mnt/user-data/outputs/Dockerfile << 'EOF'
FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    wget ca-certificates \
    libglib2.0-0 libnss3 libfontconfig1 \
    libx11-6 libx11-xcb1 libxcb1 libxcomposite1 \
    libxcursor1 libxdamage1 libxext6 libxfixes3 \
    libxi6 libxrandr2 libxrender1 libxss1 libxtst6 \
    libatk1.0-0 libatk-bridge2.0-0 libgtk-3-0 \
    libgbm1 libasound2 --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Только Chrome, ChromeDriver скачает webdriver-manager сам
RUN wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get update && apt-get install -y /tmp/chrome.deb \
    && rm /tmp/chrome.deb \
    && google-chrome --version

WORKDIR /app
RUN mkdir -p /app/sessions

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY wa_final_bot.py .

ENV BOT_TOKEN=""
ENV ADMIN_ID=""
ENV PYTHONUNBUFFERED=1
ENV WDM_LOG=0

CMD ["python", "wa_final_bot.py"]
EOF
echo "Done"
