# Build the React frontend separately -- keeps Node entirely out of the
# final runtime image.
FROM node:22-slim AS web-build

WORKDIR /web

COPY web/package.json web/package-lock.json /web/
RUN npm ci

COPY web /web
RUN npm run build

# Runtime image.
FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY src /app/src

RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .

# knock.api.app looks for `web/dist` relative to its cwd (WORKDIR here) --
# see `_web_dist_dir()` in src/knock/api/app.py.
COPY --from=web-build /web/dist /app/web/dist

EXPOSE 8000

CMD ["uvicorn", "knock.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
