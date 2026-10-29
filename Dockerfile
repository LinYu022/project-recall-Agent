ARG NODE_IMAGE=node:22.22.0-bookworm-slim
ARG PYTHON_IMAGE=python:3.12.14-slim-bookworm
FROM ghcr.io/astral-sh/uv:0.11.26 AS uv
FROM ${NODE_IMAGE} AS frontend
WORKDIR /frontend
RUN npm install --global pnpm@11.25.0
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile --ignore-scripts
COPY frontend/index.html frontend/vite.config.js ./
COPY frontend/src ./src
RUN pnpm run build

FROM ${PYTHON_IMAGE}
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /workspace
COPY pyproject.toml uv.lock ./
RUN UV_HTTP_TIMEOUT=120 uv sync --locked --no-dev --python /usr/local/bin/python
COPY . .
COPY --from=frontend /frontend/dist ./frontend/dist
RUN useradd --create-home --uid 10001 recall && mkdir -p .local && chown -R recall:recall /workspace
USER recall
EXPOSE 8017
CMD [".venv/bin/python", "-m", "portal", "--host", "0.0.0.0"]
