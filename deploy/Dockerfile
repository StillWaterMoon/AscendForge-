FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ASCENDFORGE_DATA_DIR=/var/lib/ascendforge \
    ASCENDFORGE_HOST=0.0.0.0 \
    ASCENDFORGE_PORT=8080

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY ascendforge ./ascendforge
COPY skills ./skills
COPY pyproject.toml README.md ./

RUN useradd --system --create-home --uid 10001 ascendforge \
    && mkdir -p /var/lib/ascendforge \
    && chown -R ascendforge:ascendforge /var/lib/ascendforge /app

USER ascendforge

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3).read(64)"

CMD ["python", "-m", "ascendforge", "serve"]
