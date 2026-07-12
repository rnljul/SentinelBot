FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/app/data/media_restrictions.sqlite3

WORKDIR /app

RUN adduser --disabled-password --gecos "" appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py ./

RUN mkdir -p /app/data && chown -R appuser:appuser /app
USER appuser

CMD ["python", "bot.py"]
