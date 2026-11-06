FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ZEITFLOW_DB_PATH=/data/zeitflow.db

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY zeitflow ./zeitflow

RUN useradd --create-home --uid 1000 zeitflow && mkdir -p /data && chown zeitflow /data
USER zeitflow

CMD ["python", "-m", "zeitflow"]
