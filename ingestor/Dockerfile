FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir requests psycopg2-binary
COPY ingest.py .
CMD ["python", "-u", "ingest.py"]
