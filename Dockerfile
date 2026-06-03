FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    firefox-esr \
    fonts-liberation \
    fonts-noto-color-emoji \
    xvfb \
    wget \
    && rm -rf /var/lib/apt/lists/*

# Install geckodriver
RUN GECKO_VERSION=$(wget -qO- https://api.github.com/repos/mozilla/geckodriver/releases/latest | python3 -c "import sys,json; print(json.load(sys.stdin)['tag_name'])") && \
    wget -q "https://github.com/mozilla/geckodriver/releases/download/${GECKO_VERSION}/geckodriver-${GECKO_VERSION}-linux64.tar.gz" -O /tmp/geckodriver.tar.gz && \
    tar -xzf /tmp/geckodriver.tar.gz -C /usr/local/bin/ && \
    rm /tmp/geckodriver.tar.gz && \
    chmod +x /usr/local/bin/geckodriver

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bcncita/ ./bcncita/
COPY run.py .

RUN groupadd -g 1200 cita && \
    useradd -u 1200 -g cita -m -d /home/cita -s /bin/false cita && \
    mkdir -p /app/data && chown cita:cita /app/data

USER 1200:1200

EXPOSE 8080

LABEL org.opencontainers.image.source=https://github.com/nsoult-agentic/cita-tie

# Run headful under a virtual display (Xvfb) — headless Firefox is a WAF
# bot-detection signal. xvfb-run -a auto-picks a free display (restart-safe).
CMD ["xvfb-run", "-a", "--server-args=-screen 0 1920x1080x24", "python", "-u", "run.py"]
