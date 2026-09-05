FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir requests psycopg2-binary

COPY ingest.py .
COPY odwc_regs.py .
COPY odwc_species.py .
COPY run_sync.py .
COPY backfill_weather.py .

CMD ["python", "-u", "ingest.py"]
