FROM python:3.12-slim

RUN apt-get update && apt-get install -y \
    openvpn \
    iproute2 \
    iptables \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Данные (PKI, конфиги OpenVPN, SQLite БД)
RUN mkdir -p /data/pki /data/openvpn

ENV DATA_DIR=/data
ENV PYTHONPATH=/app/backend

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
