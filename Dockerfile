FROM rclone/rclone:1.75.2 AS rclone

FROM python:3.12-slim
ARG VERSION=dev
ENV PYTHONUNBUFFERED=1 \
    CONFIG_DIR=/config \
    SOURCE_DIR=/photos \
    PORT=8080 \
    TZ=UTC \
    APP_VERSION=${VERSION}
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY app /app
WORKDIR /app
VOLUME ["/config"]
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/health',timeout=4)" || exit 1
CMD ["python", "main.py"]
