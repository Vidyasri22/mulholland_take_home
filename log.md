# Work log

A running journal of decisions and reasoning while building Reznar's ontology,
extraction pipeline, and rarity analysis.

---

## 2026-06-01 - Setup & understanding the data

- Got the environment running: `uv`, pinned **Python 3.12** (the `pgserver`
  embedded-Postgres dependency only ships 3.12 wheels), `verify.py` → DB ready.
- Opened `data/items_combined.pdf`. Key discovery: **it's an image-only PDF** -
  39 pages, ~61 MB, and plain text extraction returns nothing but a purchase
  watermark. The actual item text is baked into the illustrations.
  - Implication: I can't parse text; I need a **vision model** to read each
    page. This also fits the brief ("we are an AI-first company").
- Rendered a few pages to inspect layout. Every item follows the same shape:
  **bold title → italic subtitle (type + rarity + attunement) → body text →
  illustration**, sometimes with a small embedded sub-table.

## 2026-06-01 - Ontology design

- Decided on **one entity, `MagicItem`**. Everything Reznar cares about is an
  attribute of an item, so extra tables would only add joins without meaning.
- Modelled the closed vocabularies as **enums** (`Rarity`, `ActualForm`), not
  tables small fixed sets with no attributes of their own.
- **Key design choice: two notions of "category".**
  - `printed_type` - the words literally printed under the name ("Wondrous
    item", "Armor", "Potion"), kept verbatim. The catalog's own (unreliable)
    label.
  - `actual_form` - what the item *really* is, judged from the illustration
    (head / neck / ring / weapon / potion / charm …). For worn items this
    doubles as the **body slot**, which is what enforces Reznar's
    "one-object-per-slot" rule.
  - Splitting them lets me cross-check the catalog against reality (e.g. a
    "Wondrous item" that's actually worn on the head).
- Attunement modelled as a boolean **plus** an optional free-text condition
  ("by an elf"), because some items restrict who can attune.
- Followed the `stormland` pattern: each fuzzy field is
  `Annotated[type, BeforeValidator, Hint]` so the validator canonicalizes messy
  input and the `Hint` text feeds the extraction prompt (no drift between the
  prompt and the validator).
- **Graceful degradation:** normalizers fall back to `UNKNOWN` instead of
  raising better to keep a row flagged "unknown" than drop the item.
- Briefly built a richer model, then **simplified** to the fields above to keep
  it defensible. (Later re-added the analytical dimensions  see 06-03.)

## 2026-06-02 - Extraction pipeline

- Architecture: **PDF page → PNG → vision LLM → raw JSON → `MagicItem`
  validation → Postgres**. The LLM proposes loosely; the Pydantic model
  disposes (normalizes "mask" → head, "very rare" → very_rare, rejects junk).
- The prompt's per-field instructions are generated from the ontology's `Hint`
  metadata, so they can never drift from what the validator expects.
- **Provider journey:** started with Anthropic (out of credits), tried Gemini
  (free tier disabled on my university Google account `limit: 0`), landed on
  **Groq** (genuinely free, hosts Llama-4 vision). Swapping providers only
  touched the API call; the ontology + validation + DB code were untouched
  which validated the design.
- **DB schema:** single table `magic_item (id uuid, source_page int, data
  jsonb)`. `jsonb` keeps it flexible while the ontology evolves and is trivial
  to query for Part 2.
- Robustness: per-page and per-item failures are logged and skipped (never
  crash the run); retries with backoff handle the free-tier rate limit.
- **First full run: ~86 items.** Reviewed the output:
  - Forms like weapon/body/neck/ring/potion landed correctly. Amulet → `neck`
    (correct - the slot, not the noun).
  - 9 items had `unknown` form. Two causes: (1) **shields** (no `shield` form
    in the enum yet) and (2) **masks** (the model couldn't read the form from
    the picture).
- **Fixes:** added a `SHIELD` form, and a **name-based fallback** - if the model
  can't read the form from the illustration, infer it from the name
  (a "Mask of …" is worn on the head, a "Shield of …" is a shield). This
  dropped unknown forms from 9 → 1.
- Noted a real data-quality artifact: **`Exo-Armor` appears twice** (pages 7 and
  9 with `Man of Iron` between them) an item whose entry spans a page break,
  so the model re-emitted the title. The pipeline de-duplicates by name and the
  cleaning step keeps the better-labelled copy.

## 2026-06-02 - Analysis notebook (Part 2)

- Framed `analysis.ipynb` as a **report for Reznar**, not just code: intro →
  catalog overview → predict rarity → flag anomalies → "what to tell Reznar".
- **EDA sanity checks first:** attunement rate and description length both rise
  monotonically with rarity - so they're real signals, shown as plain charts.
- **Feature choice:** small, interpretable, hand-built features grouped by
  hypothesis (Reznar's named dimensions, "how much the item does" proxies,
  attunement, and one-hot form). On ~80 items, interpretability beats throwing
  raw text (TF-IDF) at the model - that would overfit instantly.
- **Model + evaluation:** baseline vs Logistic Regression vs Random Forest,
  judged with **cross-validation** (each item scored by a model that didn't see
  it). Because rarity is *ordinal*, I report **within-one-tier** accuracy
  alongside exact accuracy and macro-F1 - calling a *very rare* item
  *legendary* is a near miss, not a wild error.
- **Anomalies:** flag items where the model is confident the catalog put them in
  the wrong tier (low probability on the stated tier + large gap to its best
  guess). Each flag comes with a plain-English reason.

## 2026-06-03 - AI-extracted dimensions + robustness

- **Upgraded the features to be AI-judged.** Reznar named four dimensions -
  offensive, defensive, targeted creatures/environments, usage limits. Instead
  of guessing these from keywords, I added them as structured fields on
  `MagicItem` (`is_offensive`, `is_defensive`, `target_creatures`,
  `target_environments`, `has_usage_limits`) so the vision model judges them
  while reading each page  **no extra API calls**, same pass.
  - The notebook now uses these directly. Controlled comparison (same data,
    keyword vs AI features): **within-one-tier 0.85 → 0.91**, macro-F1 0.45 →
    0.47. Modest metric gain, but far more interpretable and exactly matches the
    client's brief. The new dimensions also show up among the model's top
    features, confirming they carry real signal.
  - This also strengthened the ontology it now captures all four of Reznar's
    dimensions as first-class fields.
- **Database corruption fix.** The embedded Postgres kept crashing
  (`0xC0000142`, startup timeouts). Root cause: the project lives in a
  **OneDrive-synced folder**, and OneDrive locks the DB files mid-sync.
  - Fix: relocated the Postgres data directory **out of the project** to
    `%LOCALAPPDATA%` (never synced), via `db.py`, with a `REZNAR_PG_DIR`
    override. Also set `cleanup_mode=None` so the server persists between runs
    instead of cold-starting each time (Windows Defender was scanning the binary
    and exceeding the hard-coded 10 s startup timeout).
- **CV robustness:** the rarest tier can have very few items (e.g. 3 artifacts),
  so the cross-validation now sizes its folds to the smallest class instead of a
  hard-coded number.

## Honest caveats

- Small catalogue (~80 items), imbalanced, **no _common_ items at all** so the
  model's job is genuinely hard and exact accuracy is modest by nature.
- The data was read by an AI from pictures, so a little noise is expected. The
  anomaly flags are *"worth re-checking"*, not *"definitely wrong"*.
- The method improves automatically as the catalogue grows.
