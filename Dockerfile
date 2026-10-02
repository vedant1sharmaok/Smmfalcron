FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN adduser --disabled-password --gecos "" --uid 10001 falaron

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY assets ./assets

RUN mkdir -p /data && chown -R falaron:falaron /app /data
USER falaron

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health/ready', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "app"]
