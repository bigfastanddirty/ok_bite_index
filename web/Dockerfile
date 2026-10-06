FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi==0.141.1 uvicorn==0.52.4 psycopg2-binary==2.9.12 ephem==4.2.1

COPY app.py quality.py astronomy.py index.html methodology.html ./

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
