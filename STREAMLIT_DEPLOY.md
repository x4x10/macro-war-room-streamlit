# Streamlit deploy wrapper

This repository is a Next.js + Node gateway app. Streamlit Community Cloud cannot directly host that architecture as-is. The Streamlit entrypoint in `streamlit_app.py` is a wrapper that:

- fetches the live Macro War Room API,
- renders a lightweight Streamlit risk dashboard,
- optionally embeds the full Next.js dashboard URL in an iframe.

## Files added

- `streamlit_app.py` - Streamlit entrypoint.
- `requirements.txt` - Python dependencies for Streamlit Cloud.
- `.streamlit/config.toml` - Streamlit theme.

## Streamlit Cloud settings

In Streamlit Community Cloud, create a new app from GitHub and use:

- Repository: this repo
- Branch: `main` or your current branch
- Main file path: `streamlit_app.py`

Advanced settings / Secrets:

```toml
API_BASE_URL = "https://joe-pipes-any-zope.trycloudflare.com"
APP_EMBED_URL = "https://joe-pipes-any-zope.trycloudflare.com"
REQUEST_TIMEOUT = "12"
```

`API_BASE_URL` must be a public URL that exposes the gateway routes:

- `/api/health`
- `/api/health/data`
- `/api/dashboard/summary/v2`

For a durable deployment, replace the temporary Cloudflare tunnel with a Railway/Fly/Render gateway URL and update the Streamlit secret.

## Limitation

The default Cloudflare URL is temporary and depends on the local machine staying online. If the tunnel stops, the Streamlit wrapper will load but show an API connection error.
