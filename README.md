# Remedy Server

**PDF + Office accessibility remediation + conversion engine, exposed as an HTTP API.**

Every tool, checker, fixer, and pipeline lives behind FastAPI at `/v1/*`. No frontend — bring your own client.

Names: distribution / service / container / Docker image label is `remedy-server`. The Python import package remains `project_remedy` (e.g. `from project_remedy.pdf_checker import ...`), and the dev CLI script is `remedy-pdf`. Public API paths under `/v1/*` and SQLite identifiers are unchanged.

---

## Quick start (local dev)

```bash
# 1. Install
uv sync --extra dev                       # or: pip install -e .[dev]
./.venv/bin/playwright install chromium   # for HTML validation + HTML→PDF

# 2. Configure (optional — defaults work for local dev)
cp .env.example .env

# 3. Run
./.venv/bin/uvicorn backend.app.main:app --reload   # → http://127.0.0.1:8000

# 4. Smoke test
curl http://127.0.0.1:8000/healthz        # {"ok": true}
curl http://127.0.0.1:8000/readyz         # readiness: SQLite + job dir + worker
curl http://127.0.0.1:8000/openapi.json   # full schema
# Interactive docs: http://127.0.0.1:8000/docs
```

## Deploy (single-node)

### Option A — docker-compose (recommended)

One-command deploy to any Linux host with Docker. Ships Ghostscript + veraPDF + EPUBCheck + ACE + pa11y + Lighthouse + Playwright Chromium + ocrmypdf baked in. Caddy fronts it with automatic HTTPS via Let's Encrypt.

```bash
# On the server:
git clone https://github.com/johnnyrobotai/remedy-server
cd remedy-server

# Set domain + optional Caddy global options + your API key.
cp .env.example .env
# edit .env:
#   DOMAIN=api.example.com
#   CADDY_GLOBAL_OPTIONS=email ops@example.com
#   APP_API_KEY=<generated>
#   CORS_ALLOW_ORIGINS=https://your-client.example

# Build + start.
docker compose up -d --build

# Follow logs.
docker compose logs -f remedy-server
```

The compose deployment runs with `APP_ENV=production`: startup fails if `APP_API_KEY` is empty or `CORS_ALLOW_ORIGINS` contains `*`. It stores SQLite, backups, and engine artefacts under the `job_state` named volume, and uses `/readyz` for container health.

The build takes ~10 minutes first time (includes Chromium, veraPDF, the QuestPDF sidecar, and Playwright downloads) and produces a large image. Use `target: runtime-slim` in `docker-compose.yml` to skip Node + Playwright if you don't need HTML validation or HTML→PDF.

### Option B — systemd (no Docker)

Use [`deploy/systemd/remedy-server.service`](deploy/systemd/remedy-server.service) as the native service template. Summary: install Ghostscript / veraPDF / ocrmypdf / pa11y / Lighthouse via apt + npm, set up a `remedy` system user, drop the unit file in `/etc/systemd/system/`, point Caddy at `127.0.0.1:8000`, and store secrets in `/etc/remedy-server.env`.

### Updating

```bash
# docker-compose:
cd remedy-server
git pull
docker compose up -d --build

# systemd:
cd /opt/remedy-server
sudo -u remedy git pull
sudo -u remedy uv sync
sudo systemctl restart remedy-server
```

### Example: remediate a PDF end-to-end

```bash
# Upload → async job
curl -X POST http://127.0.0.1:8000/v1/remediate \
     -F "file=@input.pdf"
# {"id":"<id>","kind":"remediate_pdf","status":"queued",...}

# Poll status
curl http://127.0.0.1:8000/v1/jobs/<id>
# {"id":"...","status":"done","stage":"complete","progress":1.0,...}

# Download
curl -OJ http://127.0.0.1:8000/v1/jobs/<id>/result    # remediated.pdf
curl -OJ http://127.0.0.1:8000/v1/jobs/<id>/report    # HTML conformance report
```

### Example: convert PDF → accessible HTML

```bash
curl -X POST http://127.0.0.1:8000/v1/convert/pdf-to-html -F "file=@input.pdf"
```

### Example: re-remediate a corpus with the Qwen3-VL LoRA router

For a large PDF corpus such as the downloaded `lamission.edu` set, use
[`runbooks/lamission-router-remediation.md`](runbooks/lamission-router-remediation.md).

The recommended first pass is **default sampled router remediation**: serve the
Qwen3-VL-32B LoRA adapters behind an OpenAI-compatible RunPod endpoint, configure
`VISION_BASE_URL` plus `OLLAMA_VISION_TASK_MODELS`, then run
`tools/remediate_pdf_corpus.py` with document-level sharding and `--resume`.
Spin up or connect to a RunPod GPU pod first; the runbook assumes a 1x H200
pod with persistent storage for the base model cache, LoRA adapters, logs, and
optional corpus outputs.
Do not use `tools/batch_remediate.py` for that objective; it intentionally calls
the CLI with `--no-vision`.

---

## Endpoints

### High-level (async jobs)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/remediate` | Upload PDF **or** Office (DOCX/PPTX/XLSX/DOC/PPT/XLS) → async remediation. |
| `POST` | `/v1/office/remediate` | Office-only variant of the above. |
| `POST` | `/v1/convert/pdf-to-html` | PDF → accessible HTML (async). |
| `POST` | `/v1/convert/office-to-html` | Office → accessible HTML (async). |
| `POST` | `/v1/convert/html-to-pdf` | HTML → tagged PDF (async, via Playwright). |
| `POST` | `/v1/convert/html-to-epub` | HTML → EPUB Accessibility 1.1 package (async). |
| `POST` | `/v1/vision-plan/run` | **Opt-in Tier 3.** Vision-planner rescue (async). Not the default. |
| `GET` | `/v1/jobs/{id}` | Status (`queued`/`running`/`done`/`failed`), stage, progress. |
| `GET` | `/v1/jobs/{id}/result` | Download the produced file (media type depends on job kind). |
| `GET` | `/v1/jobs/{id}/report` | HTML conformance report (when available). |
| `DELETE` | `/v1/jobs/{id}` | Remove job + on-disk artifacts. |

### Synchronous: PDF analysis

| Method | Path | Returns |
|---|---|---|
| `POST` | `/v1/pdf/check` | All accessibility checks (Adobe-equivalent). |
| `POST` | `/v1/pdf/tags` | Structure-tree dump. |
| `POST` | `/v1/pdf/info` | Metadata, pages, tagged/language flags, font keys. |
| `POST` | `/v1/pdf/reading-order` | XY-Cut++ reading order per page. |
| `POST` | `/v1/pdf/screen-reader` | Screen-reader simulation + issues. |
| `POST` | `/v1/pdf/alt-text/audit` | Missing / generic alt text findings. |
| `POST` | `/v1/pdf/artifacts` | Artifact marker counts per page. |
| `POST` | `/v1/pdf/fonts/check` | Per-font ToUnicode / embedding / encoding status. |

### Synchronous: PDF mutations

| Method | Path | Returns |
|---|---|---|
| `POST` | `/v1/pdf/fix` | Run `fix_and_verify` — JSON with changes + `download_token`. |
| `GET` | `/v1/pdf/fix/download/{token}` | Download the fixed PDF. |
| `POST` | `/v1/pdf/fix/{rule_id}` | Apply a single fix rule → PDF inline. |
| `POST` | `/v1/pdf/vision/alt-text` | Generate alt text for an uploaded image. |
| `POST` | `/v1/pdf/contrast/audit` | Vision-detected WCAG contrast issues. |
| `POST` | `/v1/pdf/contrast/fix` | Remediate contrast issues → PDF inline. |
| `POST` | `/v1/pdf/rebuild` | Faithful rebuild (`force_mode=preserving\|mode_a\|mode_b\|simple_font`). |
| `GET` | `/v1/pdf/rebuild/download/{token}` | Download rebuilt PDF. |
| `POST` | `/v1/pdf/redistill` | Ghostscript redistillation (optional `use_ocr=1`). |
| `POST` | `/v1/pdf/ocr` | ocrmypdf subprocess (requires `ocrmypdf` on PATH). |

### Synchronous: Office + conversions

| Method | Path | Returns |
|---|---|---|
| `POST` | `/v1/office/check` | `evaluate_office_acceptance` report (checks + screen-reader + package validity). |
| `POST` | `/v1/convert/extract-markdown` | Inline markdown extract (sync, no job). |

### Synchronous: validators

| Method | Path | Requires |
|---|---|---|
| `POST` | `/v1/validate/html` | Upload HTML → axe + pa11y + Lighthouse merged report. Needs pa11y / lighthouse / chromium installed. |
| `POST` | `/v1/validate/html/wave` | Form field `url=...` → WAVE API. Needs `WAVE_API_KEY`. |
| `POST` | `/v1/validate/pdf/verapdf` | Upload PDF → veraPDF JSON. Needs `verapdf` binary + Java 17+. |
| `POST` | `/v1/validate/pdf/adobe` | Upload PDF → Adobe PDF Services report. Needs `ADOBE_CLIENT_ID` + `ADOBE_CLIENT_SECRET`. |
| `POST` | `/v1/validate/pdf/wcag` | Upload PDF → 2-tier vision WCAG verifier. Needs a vision provider. |

### Infrastructure

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` | Liveness (unauthenticated). |
| `GET` | `/readyz` | Readiness: SQLite open/query, writable job directory, worker running. |
| `GET` | `/docs` | Swagger UI. |
| `GET` | `/openapi.json` | OpenAPI schema. |

---

## Auth

Set `APP_API_KEY` in `.env` to require clients to send `X-API-Key: <key>` on all `/v1/*` routes. Leave empty only for local/dev. When `APP_ENV=production`, the app refuses to start unless `APP_API_KEY` and `OLLAMA_API_KEY` are set and `CORS_ALLOW_ORIGINS` does not contain `*`.

## AI provider

The active cloud provider is **Ollama Cloud** (`kimi-k2.7-code:cloud`) via `OllamaClient`. `vision_planner` runs through the same Ollama-backed factory used by `pdf_vision.py`.

---

## What the engine does

Each job kind maps to a dedicated handler in `backend/app/engine_service.py`. The **default** PDF remediation (`/v1/remediate` for PDFs) runs:

1. **`fix_and_verify()`** — 48 auto-fix functions + up to 3 verify-refine cycles + optional veraPDF-driven conformance repair.
2. **`evaluate_pdf_acceptance()`** — composite gate (checker + screen reader + veraPDF + visual diff).
3. **`generate_document_report()`** — per-PDF HTML conformance report.

Vision-planner / Tier-3 agentic remediation is **not** part of this default flow. It's an opt-in tool (`/v1/vision-plan/run`) because the deterministic path is faster, cheaper, and more reliable on the corpus we've tested.

For the public module map, see the layout section below.

---

## Configuration

All env-driven. See `.env.example` for the full list. Key knobs:

**HTTP layer** (`backend/app/config.py`):
`APP_ENV`, `APP_DOCS_ENABLED`, `APP_API_KEY`, `MAX_UPLOAD_MB`, `JOB_STORE_PATH`, `JOB_DIR`,
`JOB_RETENTION_HOURS`, `JOB_BACKUP_DIR`, `JOB_BACKUP_KEEP_N`, `JOB_BACKUP_INTERVAL_HOURS`,
`WORKER_CONCURRENCY`, `CORS_ALLOW_ORIGINS`,
`RATE_LIMIT_DEFAULT`, `RATE_LIMIT_UPLOADS`, `RATE_LIMIT_STORAGE`.

**Engine** (`src/project_remedy/config.py`):
- Ollama Cloud: `OLLAMA_BASE_URL` (default `https://ollama.com/v1`), `OLLAMA_API_KEY`,
  `OLLAMA_MODEL`, `OLLAMA_VISION_MODEL`, `OLLAMA_ESCALATION_MODEL` — all default to
  `kimi-k2.7-code:cloud`. `APP_ENV=production` requires `OLLAMA_API_KEY` to be non-empty.
  Optional `OLLAMA_VISION_FALLBACK_MODELS` and `OLLAMA_VISION_FALLBACK_BASE_URLS`
  configure ordered vision fallbacks such as a local `gemma4:26b` model.
  The `/v1` path is used because `OllamaClient` speaks the OpenAI-compatible
  `/chat/completions` surface; new direct callers should prefer native
  `https://ollama.com/api` with `/api/chat`.
- Quality layer model separation: `QUALITY_JUDGE_MODEL` and `BEHAVIORAL_TEST_MODEL`
  must be different model families from `OLLAMA_MODEL`, `OLLAMA_VISION_MODEL`, and
  `OLLAMA_ESCALATION_MODEL`. The default separated setup uses
  `QUALITY_JUDGE_MODEL=mistral-large-3:675b` and
  `BEHAVIORAL_TEST_MODEL=gemma4:31b-cloud`.
  Runtime checks reject same-family judge or behavioral models.
- Task-routed vision adapters: `VISION_BASE_URL`, `OLLAMA_VISION_MODEL`,
  `OLLAMA_VISION_TASK_MODELS`, `OLLAMA_VISION_TASK_BASE_URLS`,
  `OLLAMA_VISION_ROUTER_ALLOW_FALLBACK`, `OLLAMA_VISION_MAX_INFLIGHT`,
  `OLLAMA_VISION_GATE_TIMEOUT_SECONDS`, `OLLAMA_VISION_MAX_TOKENS`.
  See the LAMission corpus runbook for the production router profile.
- Ghostscript: `GHOSTSCRIPT_ENABLED`, `GHOSTSCRIPT_PATH`
- veraPDF: `VERAPDF_PATH`
- EPUB validation: `EPUBCHECK_PATH`, `ACE_PATH`. Missing validators are recorded as skipped in job metadata.
- Rebuild tier (semantic-rebuild escalation, opt-in per job via `allow_semantic_rebuild`):
  `REBUILD_ENABLED` (default `true`), `REBUILD_BACKEND=questpdf|typst` (default `questpdf`),
  `REBUILD_TYPST_TIMEOUT_S` (default `120`). Per job, `POST /v1/remediate` also accepts an
  `allow_semantic_rebuild` form field and an optional `rebuild_backend` form field that
  overrides `REBUILD_BACKEND` for that job. The Typst backend adds a compile-time PDF/UA-1
  gate (`typst compile --pdf-standard ua-1`) plus a struct-tree assertion pass — verifying the
  compiled tag tree round-trips the semantic AST — before the shared veraPDF acceptance gate.
- Adobe PDF Services: `ADOBE_CLIENT_ID` / `ADOBE_CLIENT_SECRET`
- WAVE: `WAVE_API_KEY`
- Contrast: `CONTRAST_ENABLED`, `CONTRAST_LEVEL`, `CONTRAST_DPI`
- Branding (HTML output): `BRAND_NAME`, `BRAND_PRIMARY`, `BRAND_ACCENT`, `BRAND_NEUTRAL`, `BRAND_START_URL`
- Feature flags: `FONT_MODE_A_ENABLED`, `FONT_MODE_B_ENABLED`, `SIMPLE_FONT_*`, `VISION_PLANNER_AS_FALLBACK`

See `config.example.yaml` for the equivalent YAML template.

---

## CLI

Optional dev CLI for local inspection:

```bash
remedy-pdf check   file.pdf
remedy-pdf fix     file.pdf -o fixed.pdf
remedy-pdf report  fixed.pdf --original file.pdf
remedy-pdf tags    file.pdf
remedy-pdf vision  file.pdf
# ... remedy-pdf --help
```

The API is the primary interface; the CLI is for ad-hoc debugging.

---

## Development

```bash
./.venv/bin/python -m pytest -v       # all tests
./.venv/bin/python -m pytest tests/unit -v  # unit tests only
./.venv/bin/uvicorn backend.app.main:app --reload
curl http://127.0.0.1:8000/openapi.json | jq '.paths | keys'
```

Use `./.venv/bin/python -m pytest` in this repo. `uv run pytest` can resolve to miniconda's Python and miss the project dependencies.

### Layout

```
remedy-server/
├── backend/app/                HTTP API (FastAPI)
│   ├── main.py                 app factory + lifespan worker
│   ├── config.py               Settings (env-backed)
│   ├── routes.py               /v1/remediate, /v1/convert/*, /v1/jobs/*
│   ├── pdf_routes.py           /v1/pdf/* analysis
│   ├── pdf_fix_routes.py       /v1/pdf/* fix + rebuild + redistill + ocr
│   ├── office_routes.py        /v1/office/*
│   ├── validate_routes.py      /v1/validate/*
│   ├── vision_plan_routes.py   /v1/vision-plan/run (opt-in Tier 3)
│   ├── engine_service.py       job-kind dispatcher
│   ├── jobs.py                 JobStore (SQLite) + JobWorker
│   └── auth.py                 X-API-Key dependency
├── src/project_remedy/         engine
│   ├── pdf_checker.py          accessibility checks
│   ├── pdf_fixer.py            auto-fixes + fix_and_verify
│   ├── pdf_acceptance.py       composite acceptance gate
│   ├── pdf_vision.py           vision provider + VisionAnalyzer
│   ├── pdf_ghostscript.py      redistillation
│   ├── pdf_rebuilder.py        hybrid rebuild
│   ├── pdf_wcag_verifier.py    2-tier WCAG verifier
│   ├── compliance_report.py    HTML conformance report
│   ├── tag_tree_reader.py      screen-reader simulation
│   ├── extractor.py            doc → markdown
│   ├── converter.py            markdown → accessible HTML
│   ├── validator.py            axe + pa11y + lighthouse + WAVE
│   ├── vision.py               HTML-path alt text / chart recreation
│   ├── html_to_pdf.py          HTML → tagged PDF (Playwright + pikepdf)
│   ├── html_strategy_remediator.py    pre-LLM HTML fixes
│   ├── accessibility_strategies.py    LLM remediation strategies
│   ├── office_remediator.py    DOCX/PPTX/XLSX remediation
│   ├── office_acceptance.py    Office acceptance gate
│   ├── vision_planner/         Tier-3 grounder/planner/executor
│   ├── contrast/               color-contrast detection + remediation
│   ├── content_stream/         BDC/EMC + graphics-state tooling
│   ├── faithful_rebuild/       Mode A / Mode B / simple-font rebuild
│   ├── ocr_escalation.py       OCR triage
│   ├── adobe_checker.py        Adobe PDF Services integration
│   ├── ollama_client.py        Ollama Cloud/local client
│   ├── liteparse_adapter.py    LiteParse triage
│   ├── xy_cut.py               XY-Cut++ reading order (Hancom port)
│   ├── vision_prompts.py       shared prompts
│   ├── token_tracker.py        API usage counter
│   ├── image_extractor.py      PDF image extraction
│   ├── cli_pdf.py              dev CLI (remedy-pdf)
│   ├── database.py             in-memory DocumentJob store
│   ├── config.py               PipelineConfig + APIConfig + etc.
│   ├── models.py               DocumentJob, FileType, ExtractedImage, etc.
│   └── logging_config.py
└── tests/
    ├── api/                    HTTP tests
    └── test_*.py               engine tests
```

---

## License

MIT. See `LICENSE`.
