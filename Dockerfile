FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DENALI_DATA_DIR=/data
# pango + czcionki z polskimi znakami: PDF eksportu awaryjnego (WeasyPrint)
RUN apt-get update && apt-get install -y --no-install-recommends libpango-1.0-0 libpangoft2-1.0-0 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --system --uid 1000 --create-home denali && mkdir /data && chown denali /data
WORKDIR /app
COPY pyproject.toml README.md ./
COPY denali ./denali
RUN pip install --no-cache-dir ".[pdf]"
USER denali
EXPOSE 8000
VOLUME /data
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"
CMD ["python", "-m", "denali", "serve", "--host", "0.0.0.0"]
