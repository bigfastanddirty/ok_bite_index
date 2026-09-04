FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn psycopg2-binary
COPY app.py index.html ./
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
