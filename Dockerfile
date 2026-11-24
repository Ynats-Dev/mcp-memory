FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -u 1000 -m contexte \
 && mkdir /data && chown contexte:contexte /data

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

ARG VERSION=""
USER contexte
ENV DATA_DIR=/data PYTHONUNBUFFERED=1 TZ=Europe/Paris APP_VERSION=$VERSION
VOLUME /data
