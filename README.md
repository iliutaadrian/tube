# Tube

A private front page for YouTube: my subscriptions plus YouTube searches for the interests I name.
No Shorts, no autoplay, no infinite scroll, no YouTube home page.

## What it does

- **Subscriptions**: newest uploads from the channels in `server/channels.seed.json` (add or remove more in the app). Checked every hour with `yt-dlp`, dates from the channel RSS feed.
- **Interests**: name what you are into ("woodworking jigs"). Each interest is searched on YouTube once a day with `yt-dlp`.
- **For you**: the interests' results, taken in turns. Watched and "Not interested" videos drop out.
- **Search**: your channels by title, plus a live YouTube search.
- **Watch later, liked, history, resume**: stored in `data/state.db`.
- **Download to Plex**: one button on any video. `yt-dlp` on the homelab saves a 720p mp4 with embedded cover into `download_dir` (the folder Pinchflat already fills), one download at a time. Remove `download_dir` from `tube.json` to switch it off.
- **Save to device**: a second button hands the video file to the phone or laptop you are on. If the homelab does not have it yet, it fetches it first (so it lands on Plex too) and the save starts by itself.
- Videos play through the `youtube-nocookie.com` embed.

## Layout

| Path | What |
|---|---|
| `server/tube.py` | The server. Python standard library only. |
| `server/web/` | The web app. Plain JS, no build step. |
| `server/channels.seed.json` | Channels subscribed on first start. |
| `docker-compose.yml`, `deploy/` | The container for the homelab. |
| `tests/` | `python3 -m unittest discover -s tests` |

## Setup

```sh
# 1. Config
cp server/tube.example.json server/tube.json   # then set "token": openssl rand -hex 24

# 2. Run
python3 server/tube.py                           # http://localhost:8791, paste the token once per browser
```

## Deploy (homelab)

```sh
rsync -av --exclude tube.json --exclude __pycache__ docker-compose.yml .dockerignore server deploy homelab:/DATA/Sites/tube/
ssh homelab 'cd /DATA/Sites/tube && docker compose up -d --build'
```

- The image carries its own `yt-dlp`, `ffmpeg` and `deno`. yt-dlp updates at start and once a day.
- `"ytdlp": "yt-dlp"` in `tube.json` uses the copy in the image.
- The `x-casaos` block gives the CasaOS tile its name and icon.

`server/tube.json` lives only on the server. `data/state.db` (subscriptions, history) is created there and is never overwritten by a deploy.
