FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=7860 \
    FASTEMBED_CACHE_PATH=/home/appuser/app/.cache/fastembed

# Hugging Face Spaces runs containers as uid 1000
RUN useradd -m -u 1000 appuser
WORKDIR /home/appuser/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=appuser app ./app
COPY --chown=appuser data ./data
RUN mkdir -p .cache && chown -R appuser:appuser /home/appuser/app
USER appuser

# Bake the embedding model + chunk vectors into the image: no downloads or re-embedding at startup
RUN python -m app.rag

EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/api/health')" || exit 1
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
