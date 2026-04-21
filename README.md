# Website Risk Investigator

Paste a URL, get an evidence-backed risk verdict. A crawler walks the site
(seed page + homepage + a handful of linked pages), screenshots each one, and
pulls out forms, legal pages, outbound links, language, and trackers. A
deterministic rule engine scores the evidence and issues a verdict. A
vision-capable LLM then writes two companion reports: a plain-English
translation of the verdict, and a McKinsey-style "deep review" (governing
thought + 2–4 supporting pillars + contradictions + caveats) grounded in
every source it cites.

The LLM never overrides the verdict; it only explains and cross-checks it.

## Prerequisites

- **Docker Desktop** (includes Docker Compose). Windows, macOS, and Linux all
  work. [Install here.](https://www.docker.com/products/docker-desktop/)
- **Git**, to clone the repo.
- **~3 GB free RAM** for the worker — it runs a headless Chromium.
- **Anthropic API key** *(optional)*. You only need one if you want the LLM
  narrative or the Deep Review; the verdict itself is deterministic and works
  without any key. The app uses BYOK: you paste the key into the web UI,
  it's stored only in your browser's localStorage, and every request sends
  it with that one call. Nothing is ever saved server-side. Get a key from
  [console.anthropic.com](https://console.anthropic.com/).

## Run the app

```bash
git clone https://github.com/<your-username>/<repo-name>.git
cd <repo-name>
docker compose up --build
```

The first build takes 5–10 minutes (it downloads Playwright + Chromium,
~1 GB). Subsequent starts take seconds.

Once the containers are up, open:

- **Web UI:** http://localhost:8000
- **API docs:** http://localhost:8000/docs
- **MinIO console** (screenshots + raw HTML storage): http://localhost:9001
  — login `wri` / `wri-secret`

To stop:

```bash
docker compose down           # stops containers, keeps data
docker compose down -v        # stops + wipes Postgres and MinIO volumes
```

## How to use it

1. Open http://localhost:8000.
2. If you want LLM reports, click **Add** next to "Anthropic API key" and
   paste your key. It stays in your browser.
3. Paste any URL and click **Investigate**.
4. Watch the status bar. A crawl takes 30–90 seconds depending on how
   responsive the target site is.
5. When done, the detail page has three tabs:
   - **Summary & Deep Review** — the verdict + LLM reports
   - **Findings** — every signal the rule engine fired
   - **Pages** — every page crawled, with screenshots

## Configuration

`docker-compose.yml` ships with sane defaults for local development; you do
not need to create a `.env` file to run the app. Copy `.env.example` to
`.env` only if you want to override a default (different Postgres password,
different crawl rate limit, etc.). `.env` is gitignored.

The Anthropic key is **not** configured via `.env` — it's either entered in
the browser (BYOK, preferred) or set as a shell environment variable before
running `docker compose up` if you want the server to own it:

```bash
# Optional: set ANTHROPIC_API_KEY in your shell for server-side fallback
export ANTHROPIC_API_KEY=sk-ant-...
docker compose up --build
```

## Architecture at a glance

```
app/
  api/         FastAPI — REST endpoints + static UI
  worker/      RQ worker — runs the crawl + scoring
  crawler/     Planner, Playwright renderer, extractors, pipeline
  aggregator/  Deterministic rule engine (finding → verdict)
  reporter/    Track 1 translator + Track 2 Minto-style deep reviewer
  web/static/  Vanilla HTML + CSS + JS UI (no build step)
db/
  001_initial.sql   Schema, loaded on first Postgres boot
tests/         Pytest, no network, no API calls
docker-compose.yml  Local stack: postgres + redis + minio + api + worker
```

The four backing services are Postgres (verdicts, findings, pages),
Redis (the RQ job queue), MinIO (S3-compatible storage for screenshots and
raw HTML), and Playwright (headless Chromium for rendering).

## Running the test suite

Tests run outside Docker using your local Python. They don't touch the
network or spin up any of the above services.

```bash
pip install -e .[dev]
pytest
```

## License

Add a `LICENSE` file before sharing broadly. The defaults most open-source
projects reach for are MIT (permissive) or Apache 2.0 (permissive with a
patent grant).
