FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src

COPY entrypoint.sh .

RUN mkdir -p /app/data

RUN chmod +x /app/entrypoint.sh

ENV PYTHONPATH=/app

ENV PORT=8000

EXPOSE 8000

ENTRYPOINT ["/app/entrypoint.sh"]
