# Tube with its own yt-dlp. Build from the repo root: docker compose up -d --build
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# yt-dlp needs a JavaScript runtime for YouTube.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno
RUN pip install --no-cache-dir -U "yt-dlp[default]"
WORKDIR /app
COPY server/ server/
COPY deploy/entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
