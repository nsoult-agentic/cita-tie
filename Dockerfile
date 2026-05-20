FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    fonts-liberation \
    fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bcncita/ ./bcncita/
COPY run.py .

RUN useradd -r -s /bin/false cita && \
    mkdir -p /app/data && chown cita:cita /app/data

USER cita

EXPOSE 8080

LABEL org.opencontainers.image.source=https://github.com/nsoult-agentic/cita-tie

CMD ["python", "-u", "run.py"]
