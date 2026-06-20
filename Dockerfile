FROM python:3.12-slim

WORKDIR /app

# System deps for reportlab (PDF) and psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY pipeline/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# App code (mounted as volume in dev; copied in prod)
COPY pipeline/ ./pipeline/

# Storage directory for bill PDFs and generated letters
RUN mkdir -p /app/storage

EXPOSE 8001

# Default: run the API server.
# Override in docker-compose for the worker: command: python pipeline/worker.py
CMD ["uvicorn", "pipeline.api:app", "--host", "0.0.0.0", "--port", "8001"]
