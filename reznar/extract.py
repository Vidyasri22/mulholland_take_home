"""Extraction pipeline: messy image-PDF -> validated MagicItems -> Postgres.

The source PDF (`data/items_combined.pdf`) is image-based: each page is a
rendered illustration with the item text baked into the picture, so plain
text extraction yields almost nothing (only a purchase watermark). The
AI-first approach is therefore:

    PDF page -> PNG image -> Groq vision LLM -> raw JSON
             -> MagicItem(**raw) [normalize + validate] -> Postgres

We use Groq (free tier, OpenAI-compatible, hosts Llama-4 vision models).
Swapping the provider only touches `extract_page` + the client; the
ontology, validators, and DB loading are untouched.

Design notes
------------
* The model returns loose JSON (every field a plain string/bool). It
  proposes whatever word it sees ("mask", "very rare"); the Pydantic
  BeforeValidators in ontology.py canonicalize it ("mask" -> head,
  "very rare" -> very_rare) when we feed the raw dict into the strict
  `MagicItem`. LLM proposes, ontology disposes.
* The per-field instructions in the prompt are generated from the `Hint`
  metadata on `MagicItem`, so the prompt and the validator never drift.
* Failures are isolated per page and per item: one bad page or one item
  that won't validate is logged and skipped, never crashes the run. This
  is the "handle imperfect source gracefully" requirement.
* DB schema is one table, `magic_item (id, source_page, data jsonb)`.

Usage
-----
    $env:GROQ_API_KEY = "gsk_..."      # free key from console.groq.com

    uv run python -m reznar.extract --reset            # full run, fresh table
    uv run python -m reznar.extract --limit 2          # first 2 pages only
    uv run python -m reznar.extract --dry-run --limit 1  # no DB writes, just print
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from uuid import uuid4

import fitz  # pymupdf
import psycopg
from groq import Groq

import db
from reznar.ontology import ActualForm, Hint, MagicItem, infer_form

PDF_PATH = "data/items_combined.pdf"
DEFAULT_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

# Fields the LLM reads off the page (`source_page`/`id` are set by us).
_EXTRACTED_FIELDS = [
    "name",
    "printed_type",
    "rarity",
    "requires_attunement",
    "attunement_condition",
    "actual_form",
    "description",
    # the four pattern dimensions Reznar cares about (README), judged by the model
    "is_offensive",
    "is_defensive",
    "target_creatures",
    "target_environments",
    "has_usage_limits",
]


# --- prompt, built from the ontology's Hint metadata ------------------------

def _field_doc(field_name: str) -> str:
    """Pull a field's description, preferring its Hint metadata."""
    info = MagicItem.model_fields[field_name]
    for meta in info.metadata:
        if isinstance(meta, Hint):
            return meta.text
    return info.description or ""


def _field_guide() -> str:
    """Render the per-field instructions straight from the ontology."""
    return "\n".join(f"- {name}: {_field_doc(name)}" for name in _EXTRACTED_FIELDS)


_PROMPT = (
    "This is one page from a fantasy magic-item catalog. Each item has a bold "
    "TITLE, an italic subtitle line directly under it (its printed type, "
    "rarity, and whether it requires attunement), body text, and an "
    "illustration.\n\n"
    "Read the page and extract EVERY magic item whose title appears on it. "
    "Use these fields:\n"
    f"{_field_guide()}\n\n"
    "For actual_form, look at the ILLUSTRATION, not just the name. IGNORE the "
    "faint diagonal 'Order #...' purchase watermark — it is not an item.\n\n"
    'Respond with a single JSON object of the form '
    '{"items": [ {one object per item with the fields above} ]}. '
    "If a field is genuinely not present, omit it or set it to null."
)


# --- PDF rendering ----------------------------------------------------------

def render_page_png(page: fitz.Page, dpi: int) -> bytes:
    """Render a single PDF page to PNG bytes."""
    return page.get_pixmap(dpi=dpi).tobytes("png")


# --- Groq call --------------------------------------------------------------

def extract_page(client: Groq, png: bytes, model: str) -> list[dict]:
    """Send one page image to Groq and return the raw item dicts it reports.

    Retries a few times with backoff so a transient error or free-tier rate
    limit doesn't lose the page.
    """
    b64 = base64.standard_b64encode(png).decode()
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": _PROMPT},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"},
                },
            ],
        }
    ]

    last_exc: Exception | None = None
    for attempt, backoff in enumerate((5, 15, 30), start=1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=8192,
            )
            data = json.loads(resp.choices[0].message.content or "{}")
            if isinstance(data, list):
                return data
            items = data.get("items", [])
            return items if isinstance(items, list) else []
        except Exception as exc:  # noqa: BLE001 - retry transient/rate-limit errors
            last_exc = exc
            if attempt < 3:
                time.sleep(backoff)
    raise last_exc  # exhausted retries


# --- Postgres ---------------------------------------------------------------

def ensure_table(conn: psycopg.Connection, reset: bool) -> None:
    with conn.cursor() as cur:
        if reset:
            cur.execute("drop table if exists magic_item")
        cur.execute(
            """
            create table if not exists magic_item (
                id          uuid primary key,
                source_page int,
                data        jsonb not null
            )
            """
        )
    conn.commit()


def insert_item(conn: psycopg.Connection, item: MagicItem) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "insert into magic_item (id, source_page, data) values (%s, %s, %s)",
            (str(item.id), item.source_page, json.dumps(item.model_dump(mode="json"))),
        )
    conn.commit()


# --- orchestration ----------------------------------------------------------

def run(limit: int | None, model: str, dpi: int, reset: bool, dry_run: bool) -> None:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        sys.exit("Set GROQ_API_KEY (free key from https://console.groq.com).")
    client = Groq(api_key=api_key)

    doc = fitz.open(PDF_PATH)
    pages = range(len(doc)) if limit is None else range(min(limit, len(doc)))

    conn = None
    if not dry_run:
        conn = db.connect()
        ensure_table(conn, reset)

    n_items = n_failed = n_unknown = 0
    for i in pages:
        page_no = i + 1
        png = render_page_png(doc[i], dpi)
        try:
            raw_items = extract_page(client, png, model)
        except Exception as exc:  # noqa: BLE001 - one bad page shouldn't kill the run
            print(f"  [page {page_no}] API error: {exc}", file=sys.stderr)
            continue

        for raw in raw_items:
            # graceful fallback: if the model couldn't read the form off the
            # illustration, infer it from the name (Mask -> head, Shield -> shield)
            af = raw.get("actual_form")
            if af is None or infer_form(af) == ActualForm.UNKNOWN:
                raw["actual_form"] = raw.get("name", "")
            try:
                item = MagicItem(**raw, source_page=page_no, id=uuid4())
            except Exception as exc:  # noqa: BLE001 - log & skip invalid rows
                n_failed += 1
                print(f"  [page {page_no}] skipped invalid item: {exc}", file=sys.stderr)
                continue

            if item.rarity == "unknown" or item.actual_form == "unknown":
                n_unknown += 1

            n_items += 1
            tag = f"  [page {page_no}] {item.name} "
            print(f"{tag:<48} {item.rarity:<10} {item.actual_form}")
            if conn is not None:
                insert_item(conn, item)

    if conn is not None:
        conn.close()

    print(
        f"\nDone. {n_items} items extracted"
        f" ({n_unknown} with an unknown field), {n_failed} failed validation."
        + ("  [dry-run: nothing written]" if dry_run else "")
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract magic items from the catalog PDF.")
    ap.add_argument("--limit", type=int, default=None, help="only process the first N pages")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"Groq model (default {DEFAULT_MODEL})")
    ap.add_argument("--dpi", type=int, default=150, help="page render resolution (default 150)")
    ap.add_argument("--reset", action="store_true", help="drop and recreate the table first")
    ap.add_argument("--dry-run", action="store_true", help="extract and print, but don't write to DB")
    args = ap.parse_args()
    run(args.limit, args.model, args.dpi, args.reset, args.dry_run)


if __name__ == "__main__":
    main()
