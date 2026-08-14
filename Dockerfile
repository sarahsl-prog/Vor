FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Cloud Run sets $PORT; default to 8080 for local runs.
CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}
