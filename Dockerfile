FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CONFIG_DIR=/config \
    LIBRARY_ROOT=/roms \
    WEB_PORT=8585

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

VOLUME ["/config"]
EXPOSE 8585
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"WEB_PORT\"]}/api/meta')" || exit 1
CMD ["python", "-m", "app.main"]
