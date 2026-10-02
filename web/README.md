# KNOCK web UI

React + TypeScript + Vite + Tailwind CSS. Serves as the admin web UI for
KNOCK -- settings, debug/test tools, and bridge process supervision (see
the repo root `README.md`'s "Web UI" section for what's built so far).

## Development

```bash
npm install
npm run dev
```

Runs on `http://localhost:5173` with API calls proxied to a FastAPI
backend on `http://127.0.0.1:8000` (see `vite.config.ts`) -- start that
separately:

```bash
# from the repo root
uvicorn knock.api.app:app --reload
```

## Build

```bash
npm run build   # tsc -b && vite build -- output in dist/
npm run lint    # oxlint
```

In production (and in the Docker image), the built `dist/` is served
directly by the FastAPI backend itself -- see `_web_dist_dir()` in
`src/knock/api/app.py`.
