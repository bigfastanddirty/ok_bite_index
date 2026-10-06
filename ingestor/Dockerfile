FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir requests==2.34.2 psycopg2-binary==2.9.12

COPY *.py ./

CMD ["python", "-u", "ingest.py"]
