# Single image reused by api + worker. Worker needs Playwright + Chromium; the
# api technically doesn't, but sharing one image keeps deploys simple.
#
# Base: python:3.11-slim-bookworm from Docker Hub. We install Chromium and its
# system deps via `playwright install --with-deps chromium` rather than pulling
# the prebuilt MCR image. First build is a few minutes longer; subsequent builds
# are fully cached.
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# OS packages needed at build time: curl for pip's TLS, plus the essentials that
# `playwright install --with-deps` expects to be present on apt-based distros.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first for layer caching.
COPY pyproject.toml /app/pyproject.toml
RUN pip install --upgrade pip && \
    pip install -e /app

# Install Chromium + all required OS libs via Playwright's own installer.
# --with-deps pulls the correct apt packages for Debian bookworm.
RUN playwright install --with-deps chromium

# App code is mounted as a volume in docker-compose for dev; copying here makes
# the image self-contained for prod.
COPY . /app

EXPOSE 8000
