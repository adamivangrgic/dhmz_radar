FROM python:3.11-slim

# System deps:
#   tesseract-ocr     — for optional timestamp OCR
#   libglib2.0-0      — required by OpenCV
#   libgomp1          — required by OpenCV (OpenMP)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    libglib2.0-0 \
    libgomp1 \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Everything flat — Python + frontend in /app
COPY . /app/

# /data is the persistent volume: config.json lives here
VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--log-level", "info", "--access-log"]
