FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update && apt-get install -y --no-install-recommends \
    # --- libs de sistema do Chromium/Playwright ---
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxcb1 \
    libxkbcommon0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    wget \
    # --- OCR: binário do Tesseract + idioma português ---
    # o app.py chama pytesseract.image_to_string(img, lang="por")
    # (antes vinham do nixpacks.toml, que o Railway IGNORA quando existe Dockerfile)
    tesseract-ocr \
    tesseract-ocr-por \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .

# CORREÇÃO PRINCIPAL: instalar a partir do requirements.txt.
# A versão anterior era "pip install --no-cache-dir flask==3.0.3 playwright==1.44.0",
# que copiava o requirements.txt e nunca o usava — PyMuPDF, pytesseract, Pillow e
# pdfplumber jamais foram instalados. Como o app.py importa fitz/pytesseract dentro
# de try/except, o serviço subia normal e o OCR ficava desligado em silêncio.
RUN pip install --no-cache-dir -r requirements.txt

RUN python -m playwright install chromium

COPY . .
CMD ["python", "app.py"]
