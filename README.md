# Instagram Details API

Scrapes **public** Instagram profiles (no login) and returns posts split
into photos and videos, deduplicated, with simple caching and rate limiting.

## ⚠️ Important limitation

Without an Instagram login, the public web endpoint only reliably exposes
the most recent batch of a profile's posts (usually ~12, occasionally a bit
more via one extra page attempt). This API does **not** log in, does **not**
bypass private profiles, and will never fabricate data — it returns exactly
what it could safely retrieve, and reports the real count.

## Installation

```bash
pip install -r requirements.txt
```

## Local run

```bash
python app.py
```

Server starts on `http://localhost:10000` by default.

## API

### `GET /instagram/details?url=<profile_url>`
Returns photos + videos.

### `GET /instagram/videos?url=<profile_url>`
Returns videos only (lighter payload — used by the `instavideo` bot command).

### `GET /health`
Health check.

### Example

```
GET https://YOUR-RENDER-APP.onrender.com/instagram/videos?url=https://www.instagram.com/anil_lyrics_8/
```

### Response example

```json
{
  "success": true,
  "username": "anil_lyrics_8",
  "profile_url": "https://www.instagram.com/anil_lyrics_8/",
  "videos": [
    { "type": "video", "url": "https://www.instagram.com/reel/XYZ789/" }
  ],
  "total_videos": 10,
  "note": "Retrieved without login; Instagram only exposes a limited recent batch of public posts this way."
}
```

### Error example

```json
{ "success": false, "error": "This profile is private. Only public profiles are supported." }
```

## Render deployment

1. Push this folder to a GitHub repo.
2. Create a new **Web Service** on Render, pointing at the repo.
3. **Build Command:**
   ```
   pip install -r requirements.txt
   ```
4. **Start Command:**
   ```
   gunicorn -k uvicorn.workers.UvicornWorker app:app --bind 0.0.0.0:$PORT
   ```
5. Set environment variables (all optional, see below).
6. Deploy. Health check path: `/health`.

## Environment variables

| Variable        | Default | Description                                      |
|-----------------|---------|---------------------------------------------------|
| `MAX_POSTS`     | `5000`  | Safety cap on posts collected per profile          |
| `CACHE_TTL`     | `1800`  | Seconds to cache a profile's result (0 = disabled) |
| `RATE_LIMIT`    | `10`    | Max requests per minute per IP (0 = disabled)      |
| `PORT`          | `10000` | Port (Render sets this automatically)              |
