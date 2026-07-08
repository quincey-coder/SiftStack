# Apify Actor Dockerfile — Python 3.12 + Playwright + Chromium
FROM apify/actor-python-playwright:3.12

# Tesseract OCR binary — pytesseract (used by the Bell foreclosure PDF scraper)
# needs the native `tesseract` binary, which the base image doesn't ship. Without
# it, scanned Bell foreclosure PDFs fail OCR ("tesseract is not installed") and
# addresses come through blank/garbled. Base runs as non-root, so switch to root
# to apt-install, then switch back.
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
USER myuser

# Copy requirements first for better layer caching
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY src/ ./src/
COPY .actor/ ./.actor/

# Playwright browsers are pre-installed in the base image.
# Set working directory so imports from src/ work.
ENV PYTHONPATH=/home/myuser/src

CMD ["python", "src/main.py"]
