FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    wget gnupg unzip ca-certificates curl jq \
    libglib2.0-0 libnss3 libfontconfig1 \
    libx11-6 libx11-xcb1 libxcb1 libxcomposite1 \
    libxcursor1 libxdamage1 libxext6 libxfixes3 \
    libxi6 libxrandr2 libxrender1 libxss1 libxtst6 \
    libatk1.0-0 libatk-bridge2.0-0 libgtk-3-0 \
    libgbm1 libasound2 --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Google Chrome
RUN wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get update && apt-get install -y /tmp/chrome.deb \
    && rm /tmp/chrome.deb

# ChromeDriver для Chrome 115+ (новый API)
RUN CHROME_VER=$(google-chrome --version | awk '{print $3}') \
    && CHROME_MAJOR=$(echo $CHROME_VER | cut -d. -f1) \
    && echo "Chrome: $CHROME_VER (major: $CHROME_MAJOR)" \
    && curl -s "https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json" \
    | jq -r ".versions[] | select(.version | startswith(\"${CHROME_VER}\")) | .downloads.chromedriver[] | select(.platform==\"linux64\") | .url" \
    | head -1 > /tmp/cd_url \
    && if [ ! -s /tmp/cd_url ]; then \
        curl -s "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions-with-downloads.json" \
        | jq -r ".channels.Stable.downloads.chromedriver[] | select(.platform==\"linux64\") | .url" > /tmp/cd_url; \
    fi \
    && echo "Downloading: $(cat /tmp/cd_url)" \
    && wget -q "$(cat /tmp/cd_url)" -O /tmp/cd.zip \
    && unzip /tmp/cd.zip -d /tmp/cd_extracted/ \
    && find /tmp/cd_extracted/ -name "chromedriver" -exec cp {} /usr/local/bin/chromedriver \; \
    && chmod +x /usr/local/bin/chromedriver \
    && rm -rf /tmp/cd.zip /tmp/cd_extracted /tmp/cd_url \
    && chromedriver --version

WORKDIR /app
RUN mkdir -p /app/sessions

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY wa_final_bot.py .

ENV BOT_TOKEN=""
ENV ADMIN_ID=""
ENV PYTHONUNBUFFERED=1

CMD ["python", "wa_final_bot.py"]
