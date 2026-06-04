# How to run

End-to-end steps to reproduce the extraction and the analysis. Everything is
driven by `uv`; you do **not** need to install Postgres or create a conda env.

---

## What this does

```
data/items_combined.pdf  (image-only PDF)
        │  render each page → PNG
        ▼
   Groq vision LLM  ──►  raw JSON per item
        │  validate + normalize against reznar/ontology.py
        ▼
   local Postgres  (table: magic_item)
        │
        ▼
   analysis.ipynb  ──►  rarity prediction + anomaly report
```

The source PDF is image-based (the text is baked into the pictures), so a
vision model reads each page. The Pydantic models in `reznar/ontology.py`
validate and canonicalize whatever the model returns.

---

## 1. Prerequisites

- **Python 3.12** (the embedded Postgres dependency `pgserver` only ships
  wheels for 3.12)
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)**
- A **free Groq API key** - get one at <https://console.groq.com> (no credit
  card required)

## 2. Install

```bash
uv python install 3.12
uv python pin 3.12      # ensures the project uses 3.12 (pgserver needs it)
uv sync                 # installs all dependencies into .venv/
```

Verify the local Postgres starts (downloads a Postgres binary on first run):

```bash
uv run python verify.py
```

You should see `postgres is ready`.

## 3. Set your API key

The extraction calls Groq, so set your key in the shell first.

**PowerShell (Windows):**
```powershell
$env:GROQ_API_KEY = "gsk_...your key..."
```

**bash/zsh (macOS/Linux):**
```bash
export GROQ_API_KEY="gsk_...your key..."
```

## 4. Populate the database from the PDF

```bash
uv run python -m reznar.extract --reset
```

This renders all 39 pages, extracts every item, validates it, and loads the
`magic_item` table. It prints each item and finishes with a summary like:

```
Done. 84 items extracted (1 with an unknown field), 1 failed validation.
```

> The vision model is non-deterministic, so the exact count varies slightly
> between runs (typically ~80–87 items). This is expected.

**Let it finish** (~1–2 minutes). The free Groq tier is rate-limited; the
pipeline retries automatically with backoff.

### Useful flags
| Flag | Purpose |
|---|---|
| `--reset` | drop and recreate the table before loading (clean run) |
| `--limit N` | only process the first N pages (quick test) |
| `--dry-run` | extract and print, but don't write to the database |
| `--model NAME` | use a different Groq model (default `meta-llama/llama-4-scout-17b-16e-instruct`) |
| `--dpi N` | page render resolution (default 150) |

Example smoke test (one page, no DB writes):
```bash
uv run python -m reznar.extract --dry-run --limit 1
```

## 5. Run the analysis notebook

```bash
uv run jupyter notebook analysis.ipynb
```

Then **Run All**. The notebook reads the `magic_item` table directly, predicts
rarity, and reports the items that look mis-tiered. (In VS Code, open the
notebook and select the `.venv` kernel instead.)

---

## Where the database lives / how to reset

The embedded Postgres data directory is kept **outside the project tree** on
purpose - cloud-sync clients (OneDrive, Dropbox) lock database files mid-sync
and corrupt the server. By default it lives in a local cache:

- Windows: `%LOCALAPPDATA%\reznar-pg`
- macOS/Linux: `$XDG_CACHE_HOME/reznar-pg` or `~/.cache/reznar-pg`

Override the location with the `REZNAR_PG_DIR` environment variable.

**To reset the database**, stop any running Postgres, delete that directory,
and re-run the extraction:

```powershell
# Windows
Remove-Item -Recurse -Force $env:LOCALAPPDATA\reznar-pg
uv run python -m reznar.extract --reset
```

(Optional) Browse the database in a web UI:
```bash
uv run python web.py    # requires pgweb installed; opens http://localhost:8081
```

---

## Troubleshooting

- **`pgserver ... no wheel for the current platform`** → you're not on Python
  3.12. Run `uv python pin 3.12 && uv sync`.
- **`Timeout starting server` / DB connection errors** → a Postgres instance is
  stuck. Stop it (`taskkill /F /IM postgres.exe` on Windows, or reboot) and
  retry. Make sure the data dir is **not** inside a synced cloud folder.
- **`Set GROQ_API_KEY ...`** → the key isn't set in the current shell (env vars
  don't persist between terminals). Re-set it (step 3).
- **`429 RESOURCE_EXHAUSTED` (Groq)** → free-tier rate limit; the pipeline
  retries automatically. If it persists, wait a minute and re-run.

---

## Project layout

| Path | What it is |
|---|---|
| `reznar/ontology.py` | Pydantic entity models (the ontology) |
| `reznar/extract.py` | the extraction pipeline (PDF → Groq → Postgres) |
| `analysis.ipynb` | Part 2: rarity prediction + anomaly report |
| `db.py` | local Postgres helper (data dir kept off cloud-sync) |
| `data/items_combined.pdf` | the source catalog |
