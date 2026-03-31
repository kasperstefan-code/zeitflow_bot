FROM python:3.12-slim
WORKDIR /app

# Persistentes Volume für die Datenbank
ENV ZEITFLOW_DB_PATH=/data/zeitflow.db

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY zeitflow.py .

CMD ["python", "zeitflow.py"]
