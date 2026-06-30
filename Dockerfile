# syntax=docker/dockerfile:1.7
#
# Multi-stage Dockerfile for remedy-server.
# Builds a single image that includes every external binary the API's
# optional endpoints need: Ghostscript, veraPDF (+ Java), ocrmypdf,
# Node + pa11y + lighthouse, and Playwright Chromium.
#
# Target image size is ~2.5 GB. If you don't need HTML validation or
# HTML→PDF conversion, use the `slim` target instead to drop Node +
# Playwright (saves ~1.2 GB).

# ---------------------------------------------------------------------------
# 1. Base with system deps
# ---------------------------------------------------------------------------

FROM python:3.13-slim-bookworm AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:/opt/node/bin:$PATH"

# System packages needed by the engine:
#   ghostscript        → pdf_ghostscript.redistill_pdf
#   default-jre        → veraPDF (needs Java 17+)
#   ocrmypdf           → /v1/pdf/ocr
#   libmagic1, libgl1  → pymupdf / pillow runtime deps
#   fontconfig         → QuestPDF sidecar font discovery
#   curl, ca-certificates, unzip → installer bootstraps below
#   fonts-liberation, fonts-dejavu → fallback fonts for chromium + html→pdf
#   build-essential, pkg-config → some wheels still compile (fonttools, pikepdf)
RUN apt-get update && apt-get install --no-install-recommends -y \
        build-essential \
        ca-certificates \
        curl \
        default-jre-headless \
        fontconfig \
        fonts-dejavu \
        fonts-liberation \
        ghostscript \
        libgl1 \
        libmagic1 \
        ocrmypdf \
        pkg-config \
        tesseract-ocr \
        tesseract-ocr-eng \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# Non-root user for the running service.
RUN groupadd --system app && useradd --system --gid app --home /app --shell /usr/sbin/nologin app

# ---------------------------------------------------------------------------
# 2. Build QuestPDF sidecar
# ---------------------------------------------------------------------------

FROM --platform=$BUILDPLATFORM mcr.microsoft.com/dotnet/sdk:9.0-bookworm-slim AS questpdf

ARG TARGETARCH
WORKDIR /src

RUN apt-get update && apt-get install --no-install-recommends -y \
        clang \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

COPY sidecar/QuestPdfRenderer/QuestPdfRenderer.csproj ./
RUN dotnet restore

COPY sidecar/QuestPdfRenderer/ ./
RUN case "${TARGETARCH}" in \
        amd64) rid="linux-x64" ;; \
        arm64) rid="linux-arm64" ;; \
        *) echo "unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
    esac \
    && dotnet publish -c Release -r "${rid}" --self-contained true -o /out

# ---------------------------------------------------------------------------
# 3. Install veraPDF
# ---------------------------------------------------------------------------

FROM base AS verapdf

# verapdf-installer.zip must be present in the build context. Fetch with:
#   curl -fsSLO https://software.verapdf.org/releases/1.30/verapdf-greenfield-1.30.1-installer.zip \
#     && mv verapdf-greenfield-*-installer.zip verapdf-installer.zip
# (skipping curl during build avoids transient outbound network failures.)
COPY verapdf-installer.zip /tmp/verapdf/installer.zip

RUN mkdir -p /opt/verapdf \
    && unzip -q /tmp/verapdf/installer.zip -d /tmp/verapdf \
    && JAR="$(find /tmp/verapdf -maxdepth 3 -type f -name 'verapdf-izpack-installer-*.jar' | head -n1)" \
    && [ -n "$JAR" ] || (echo "verapdf installer jar not found" >&2; exit 1) \
    && echo '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n<AutomatedInstallation langpack="eng">\n  <com.izforge.izpack.panels.htmlhello.HTMLHelloPanel id="welcome"/>\n  <com.izforge.izpack.panels.target.TargetPanel id="target"><installpath>/opt/verapdf</installpath></com.izforge.izpack.panels.target.TargetPanel>\n  <com.izforge.izpack.panels.packs.PacksPanel id="packs">\n    <pack index="0" name="verapdf-gui" selected="true"/>\n  </com.izforge.izpack.panels.packs.PacksPanel>\n  <com.izforge.izpack.panels.install.InstallPanel id="install"/>\n  <com.izforge.izpack.panels.finish.FinishPanel id="finish"/>\n</AutomatedInstallation>' > /tmp/verapdf/auto.xml \
    && java -jar "$JAR" /tmp/verapdf/auto.xml \
    && ln -s /opt/verapdf/verapdf /usr/local/bin/verapdf \
    && rm -rf /tmp/verapdf

# ---------------------------------------------------------------------------
# 4. Install Node + pa11y + Lighthouse + ACE (DAISY accessibility checker)
# ---------------------------------------------------------------------------

FROM base AS node-tools
ARG NODE_VERSION=20.17.0

RUN curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
    | tar -xJf - -C /opt \
    && mv "/opt/node-v${NODE_VERSION}-linux-x64" /opt/node \
    && /opt/node/bin/npm install -g pa11y@8 lighthouse@12 @daisy/ace@1.3.0 \
    && /opt/node/bin/npm cache clean --force

# ---------------------------------------------------------------------------
# 4b. Install EPUBCheck (W3C structural validator — Java)
# ---------------------------------------------------------------------------

FROM base AS epubcheck
ARG EPUBCHECK_VERSION=5.2.1

# Uses curl to fetch the GitHub release. For fully reproducible/offline
# builds, switch to a COPY of a pre-staged epubcheck-*.zip the same way
# the veraPDF stage does (see stage 3).
RUN mkdir -p /opt/epubcheck \
    && curl -fsSL -o /tmp/epubcheck.zip \
        "https://github.com/w3c/epubcheck/releases/download/v${EPUBCHECK_VERSION}/epubcheck-${EPUBCHECK_VERSION}.zip" \
    && unzip -q /tmp/epubcheck.zip -d /tmp/epubcheck-unpack \
    && mv /tmp/epubcheck-unpack/epubcheck-${EPUBCHECK_VERSION}/* /opt/epubcheck/ \
    && rm -rf /tmp/epubcheck.zip /tmp/epubcheck-unpack \
    && printf '%s\n' '#!/bin/sh' 'exec java -jar /opt/epubcheck/epubcheck.jar "$@"' > /usr/local/bin/epubcheck \
    && chmod +x /usr/local/bin/epubcheck

# ---------------------------------------------------------------------------
# 5. Python dependencies
# ---------------------------------------------------------------------------

FROM base AS python-deps

# Build venv separately so the final runtime image stays cacheable.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    UV_LINK_MODE=copy

# Copy only the dependency manifest first so the wheel layer caches.
COPY pyproject.toml uv.lock /tmp/build/
WORKDIR /tmp/build

# Install locked runtime dependencies.
RUN pip install --upgrade pip setuptools wheel uv \
    && uv sync --frozen --no-dev --no-install-project --active --compile-bytecode

# ---------------------------------------------------------------------------
# 6. Playwright Chromium (requires Python env from step 5)
# ---------------------------------------------------------------------------

FROM python-deps AS playwright
RUN playwright install --with-deps chromium \
    && rm -rf /root/.cache/ms-playwright/.links

# ---------------------------------------------------------------------------
# 7. Runtime image (combines everything + app source)
# ---------------------------------------------------------------------------

FROM base AS runtime

COPY --from=verapdf /opt/verapdf /opt/verapdf
RUN ln -s /opt/verapdf/verapdf /usr/local/bin/verapdf
COPY --from=epubcheck /opt/epubcheck /opt/epubcheck
COPY --from=epubcheck /usr/local/bin/epubcheck /usr/local/bin/epubcheck
COPY --from=questpdf /out/remedy-questpdf /usr/local/bin/remedy-questpdf
COPY --from=node-tools /opt/node /opt/node
COPY --from=python-deps /opt/venv /opt/venv
# Playwright browsers are in /root/.cache/ms-playwright after install;
# relocate under /opt so the non-root user can read them.
COPY --from=playwright /root/.cache/ms-playwright /opt/ms-playwright
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

WORKDIR /app
COPY --chown=app:app pyproject.toml README.md ./
COPY --chown=app:app src ./src
COPY --chown=app:app backend ./backend
# tools/ is imported at runtime by backend/app/quality_routes.py and used as
# QUALITY_CORPUS_ROOT_PATH (default ./tools/corpus_annotations/v1).
COPY --chown=app:app tools ./tools

# Reinstall the project in editable mode so it picks up the COPY'd source.
RUN pip install --no-deps -e . \
    && mkdir -p /app/job_data /app/state/output /app/state/logs /app/tmp \
    && chown -R app:app /app/job_data /app/state /app/tmp /app

USER app
EXPOSE 8000

ENV JOB_DIR=/app/job_data \
    JOB_STORE_PATH=/app/state/jobs.db \
    JOB_BACKUP_DIR=/app/state/job_backups \
    OUTPUT_DIR=/app/state/output \
    LOG_DIR=/app/state/logs \
    TMPDIR=/app/tmp \
    VERAPDF_PATH=/usr/local/bin/verapdf \
    EPUBCHECK_PATH=/usr/local/bin/epubcheck \
    ACE_PATH=/opt/node/bin/ace \
    REMEDY_QUESTPDF_BINARY=/usr/local/bin/remedy-questpdf \
    GHOSTSCRIPT_ENABLED=true \
    GHOSTSCRIPT_PATH=/usr/bin/gs \
    LOG_FORMAT=json

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/readyz || exit 1

CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]

# ---------------------------------------------------------------------------
# 8. Optional slim target (no Node / Playwright — skips HTML validation,
#     HTML→PDF conversion, and HTML→EPUB ACE verification, ~1.2 GB smaller).
#     EPUB conversion still produces an EPUB; verify report records that
#     both validators were skipped (the verifier fails soft).
# ---------------------------------------------------------------------------

FROM base AS runtime-slim

COPY --from=verapdf /opt/verapdf /opt/verapdf
RUN ln -s /opt/verapdf/verapdf /usr/local/bin/verapdf
COPY --from=questpdf /out/remedy-questpdf /usr/local/bin/remedy-questpdf
COPY --from=python-deps /opt/venv /opt/venv

WORKDIR /app
COPY --chown=app:app pyproject.toml README.md ./
COPY --chown=app:app src ./src
COPY --chown=app:app backend ./backend
# tools/ is imported at runtime by backend/app/quality_routes.py and used as
# QUALITY_CORPUS_ROOT_PATH (default ./tools/corpus_annotations/v1).
COPY --chown=app:app tools ./tools

RUN pip install --no-deps -e . \
    && mkdir -p /app/job_data /app/state/output /app/state/logs /app/tmp \
    && chown -R app:app /app/job_data /app/state /app/tmp /app

USER app
EXPOSE 8000

ENV JOB_DIR=/app/job_data \
    JOB_STORE_PATH=/app/state/jobs.db \
    JOB_BACKUP_DIR=/app/state/job_backups \
    OUTPUT_DIR=/app/state/output \
    LOG_DIR=/app/state/logs \
    TMPDIR=/app/tmp \
    VERAPDF_PATH=/usr/local/bin/verapdf \
    REMEDY_QUESTPDF_BINARY=/usr/local/bin/remedy-questpdf \
    GHOSTSCRIPT_ENABLED=true \
    GHOSTSCRIPT_PATH=/usr/bin/gs \
    LOG_FORMAT=json

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/readyz || exit 1

CMD ["uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
