# Сборка для Railway/любого Linux: Word → PDF через LibreOffice
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer libreoffice-common \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY core.py web_app.py ./

# Railway задаёт PORT
ENV PORT=8000
EXPOSE 8000
CMD uvicorn web_app:app --host 0.0.0.0 --port ${PORT}
