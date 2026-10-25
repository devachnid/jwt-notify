FROM python:3.11-slim

WORKDIR /srv

# requirements.lock pins every dependency by hash; regenerate it from
# requirements.txt with pip-compile (see the README).
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

COPY app ./app

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000
# --limit-concurrency answers 503 past 50 connections instead of letting
# requests pile up in memory.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--limit-concurrency", "50"]
