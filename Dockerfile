FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --system financial-engine \
    && useradd --system --gid financial-engine --create-home financial-engine

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m pip install --no-cache-dir . \
    && install -d -o financial-engine -g financial-engine /data

USER financial-engine

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).read()"]

ENTRYPOINT ["financial-engine-web"]
CMD ["--host", "0.0.0.0", "--port", "8080", "--db", "/data/financial_engine.db"]
