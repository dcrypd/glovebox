FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    GLOVEBOX_DB=/data/glovebox.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

RUN useradd --system --create-home --uid 1000 glovebox \
    && mkdir -p /data \
    && chown -R glovebox:glovebox /data /app

USER glovebox

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
