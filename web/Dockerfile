FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    screen \
    curl \
    git \
    procps \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir "rich>=13.0.0"

COPY backend/ ./backend/
COPY frontend/ ./frontend/

RUN mkdir -p /app/frontend/static/xterm && \
    curl -L -o /app/frontend/static/xterm/xterm.js https://unpkg.com/xterm@5.3.0/lib/xterm.js && \
    curl -L -o /app/frontend/static/xterm/xterm.css https://unpkg.com/xterm@5.3.0/css/xterm.css && \
    curl -L -o /app/frontend/static/xterm/xterm-addon-fit.js https://unpkg.com/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js

EXPOSE 7788

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "7788", "--reload"]
