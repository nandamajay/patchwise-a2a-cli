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

COPY backend/ ./backend/
COPY frontend/ ./frontend/

EXPOSE 7788

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "7788", "--reload"]
