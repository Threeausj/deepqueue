# syntax=docker/dockerfile:1
ARG NODE_IMAGE=node:22-bookworm-slim
ARG PYTHON_IMAGE=python:3.13-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.26

FROM ${NODE_IMAGE} AS node

FROM node AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/index.html frontend/vite.config.js ./
COPY frontend/src ./src
RUN npm run build

FROM node AS codex
ARG CODEX_VERSION=0.153.4
RUN npm install --prefix /opt/codex --no-audit --no-fund "@openai/codex@${CODEX_VERSION}" \
    && /opt/codex/node_modules/.bin/codex --version

FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS python-build
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
# Export exact versions and hashes; install from PyPI instead of requiring the
# development lockfile's mirror to be reachable from every deployment region.
RUN uv export --frozen --no-dev --no-emit-project --format requirements-txt --output-file requirements.txt \
    && python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --require-hashes -r requirements.txt
COPY src ./src
COPY --from=frontend /build/src/deepqueue/web_static ./src/deepqueue/web_static
RUN /opt/venv/bin/pip install --no-cache-dir --no-deps .

FROM ${PYTHON_IMAGE} AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates git libstdc++6 openssh-client procps ripgrep tmux \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 deepqueue \
    && useradd --uid 10001 --gid deepqueue --create-home --shell /bin/bash deepqueue \
    && mkdir -p /var/lib/deepqueue /home/deepqueue/.codex \
    && chown -R deepqueue:deepqueue /var/lib/deepqueue /home/deepqueue
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /opt/codex /opt/codex
COPY --from=python-build /opt/venv /opt/venv
RUN ln -s /opt/codex/node_modules/.bin/codex /usr/local/bin/codex
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEEPQUEUE_HOME=/var/lib/deepqueue \
    CODEX_HOME=/home/deepqueue/.codex
USER deepqueue
WORKDIR /var/lib/deepqueue
RUN codex --version && deepqueue --help > /dev/null
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-m", "deepqueue.container", "healthcheck"]
ENTRYPOINT ["python", "-m", "deepqueue.container"]
CMD ["serve"]
