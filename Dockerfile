FROM python:3.11-slim

# Unbuffered output so `docker compose logs` is live; no .pyc clutter.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
COPY config ./config
COPY scripts ./scripts

# Run as an unprivileged user. data/ and logs/ are bind-mounted from the host
# (see docker-compose.yml) and must be writable by uid 1000.
RUN useradd --create-home --uid 1000 bot \
    && mkdir -p data logs \
    && chown -R bot:bot /app
USER bot

CMD ["python", "-m", "bot.main"]
