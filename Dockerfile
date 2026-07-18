FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /var/data

ENV DATA_DIR=/var/data
ENV PORT=8000

EXPOSE 8000

CMD uvicorn servidor_mlb:app --host 0.0.0.0 --port ${PORT:-8000}
