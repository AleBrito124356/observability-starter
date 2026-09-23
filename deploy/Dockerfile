# Build context is the repository root (see docker-compose.yml).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# The app writes its own structured, trace-correlated "request.completed" line
# per request and silences uvicorn's plain-text access line (LOG_REQUESTS=true).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
