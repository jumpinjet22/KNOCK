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

# The API process spawns bridge subprocesses of its own (see
# knock.core.supervisor) -- without a real init process as PID 1, `docker
# stop` would only ever signal uvicorn itself and leave those orphaned.
RUN apt-get update && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY src /app/src
# Not installed as part of the package (outside src/) -- these are the
# manual, hands-on dev tools for generating LoRA training data locally
# (see scripts/generate_scenarios.py/generate_training_data.py), run
# directly as `python scripts/...py` with this image's own cwd (/app) as
# the working directory. Harmless to ship in every image; only ever
# invoked by the Training page's script-runner panel, itself gated behind
# KNOCK_TRAINING_MODE=1.
COPY scripts /app/scripts

RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir .

# knock.api.app looks for `web/dist` relative to its cwd (WORKDIR here) --
# see `_web_dist_dir()` in src/knock/api/app.py.
COPY --from=web-build /web/dist /app/web/dist

EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "knock.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
