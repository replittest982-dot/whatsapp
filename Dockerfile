version: '3.8'

services:
  imperator_v17:
    build: .
    container_name: imperator_v17
    restart: unless-stopped
    # 💥 КРИТИЧЕСКИ ВАЖНО: Увеличиваем shared memory, иначе Chromium упадет (Aw, Snap!)
    shm_size: '1g'
    env_file:
      - .env
    environment:
      - TZ=Asia/Almaty  # Синхронизация времени контейнера с гео-позицией браузера
    volumes:
      - ./sessions:/app/sessions
      - ./imp17.db:/app/imp17.db
    # Ограничение логов, чтобы не забить диск сервера
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
