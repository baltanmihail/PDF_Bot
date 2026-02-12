# Сборка для Railway/любого Linux: Word → PDF через LibreOffice
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer libreoffice-common \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY core.py auth_db.py web_app.py ./

# Railway задаёт PORT при запуске
EXPOSE 8000
CMD ["python", "web_app.py"]
