# Chronos — MVP Plan

Detect durable physical changes to city streets by pairing Mapillary street-level
photos of the same spot taken ≥2 years apart, judging each pair with a vision LLM,
and exploring results on a web map. Hackathon deadline: July 21.

**Positioning:** big changes are already in permit databases; small ones are in
no dataset at all. Chronos's story is its *detection floor* — the same judge that
sees a demolished building flags a spreading pavement crack (`magnitude: subtle`),
and the demo filters down to the layer no city has records of.

## Pipeline

```
ingest ──► images table ──► pairing.py ──► pairs table
                                             │
inspect ─► OpenAI vision (strict JSON) ──► judgments table
                                             │
serve  ──► FastAPI /api/changes ──► MapLibre map + before/after slider
```

All state lives in `data/` (SQLite DB + downloaded thumbnails), gitignored.
Every command is idempotent: re-running skips work already done.

## File structure

```
chronos/
├── README.md               # setup, quickstart, screenshot placeholder
├── PLAN.md
├── .env.example            # MAPILLARY_TOKEN, OPENAI_API_KEY, OPENAI_MODEL (optional)
├── .gitignore              # .env, data/, __pycache__/
├── requirements.txt        # 6 deps (below)
├── prompts/
│   └── inspector.md        # placeholder draft — you paste the real one over it
├── chronos/
│   ├── __init__.py
│   ├── __main__.py         # argparse CLI: ingest | inspect | serve
│   ├── config.py           # .env loading, paths, tunable constants
│   ├── db.py               # schema init, connection helper, upserts/queries
│   ├── pairing.py          # PURE pairing logic — no I/O, unit-tested
│   ├── mapillary.py        # Graph API: bbox search, paging, rate limit, cache, thumb download
│   ├── inspector.py        # OpenAI vision judge: strict schema, retries, storage
│   ├── server.py           # FastAPI app + API routes + static serving
│   └── static/
│       ├── index.html
│       ├── app.js          # map, markers, detail panel, slider (vanilla JS)
│       └── style.css
├── tests/
│   └── test_pairing.py
└── data/                   # gitignored: chronos.db, images/<id>.jpg
```

(The judge module is `inspector.py`, not `inspect.py`, to avoid shadowing the
stdlib `inspect` module; the CLI subcommand is still `inspect`.)

## Dependencies (6 of 10 allowed)

`fastapi`, `uvicorn`, `httpx`, `pydantic`, `python-dotenv`, `pytest`.
`sqlite3` is stdlib; MapLibre GL loads from CDN (no build step).

## Data model (SQLite)

- **images** — `id` TEXT PK (Mapillary id), `lon`, `lat`, `heading`, `captured_at`
  INTEGER (epoch ms), `sequence_id`, `is_pano`, `thumb_path`, `fetched_at`.
  Uses Mapillary's SfM-corrected `computed_geometry` / `computed_compass_angle`
  when present, falling back to the raw fields.
- **pairs** — `id` TEXT PK = `"<older_id>_<newer_id>"` (deterministic → idempotent
  re-ingest), `older_id`, `newer_id`, `distance_m`, `heading_diff_deg`, `gap_days`,
  `score`, `status` (`candidate` | `judged` | `error`), `error`, `created_at`.
- **judgments** — `pair_id` PK/FK, `model`, `changed` INT, `category`, `magnitude`,
  `confidence`, `evidence`, `raw_json`, `created_at`.
- **api_cache** — `key` PK (hash of URL+params), `body`, `fetched_at`. Caches
  Mapillary responses so re-running `ingest` is free unless `--refresh`.

"Never re-fetch / never re-judge" = thumbnails on disk + `api_cache` +
`inspect` only selecting pairs that have no judgment row.

## Pairing (`pairing.py`) — the tested core

Pure functions over an in-memory image list, no I/O:

1. **Filter:** drop panoramas (`is_pano`) and images with no compass heading —
   alignment can't be verified without one.
2. **Spatial grid:** bucket images into cells slightly larger than the distance
   threshold; compare only within the 3×3 neighborhood (avoids O(n²) across the
   whole bbox).
3. **Candidate predicate**, all required: haversine distance ≤ 15 m · circular
   heading diff ≤ 30° (355° vs 5° = 10°) · capture gap ≥ 730 days · different
   `sequence_id` (a camera pass never pairs with itself).
4. **Score:** `distance/15 + heading_diff/30`, ties broken by longer time gap
   (lower = better aligned).
5. **Greedy 1:1 matching:** walk candidates by score, accept a pair only if
   neither image is already used. One location → one pair instead of ten
   near-duplicates. This is the cost lever: every pair is an OpenAI call.

Constants (15 m / 30° / 730 d) live here, overridable via `ingest` flags.

Tests: haversine against known distances, heading wraparound, boundary values
(exactly 15.0 m / 730 d — inclusive), same-sequence exclusion, pano and
missing-heading exclusion, grid completeness vs a brute-force O(n²) reference,
greedy uniqueness.

## Ingestion (`mapillary.py` + `ingest`)

- `GET graph.mapillary.com/images` with `bbox=minLon,minLat,maxLon,maxLat`,
  fields for geometry/heading/capture time/sequence/`thumb_1024_url`/
  `thumb_2048_url`, cursor pagination, capped by `--limit` (default 2000 images).
- Rate limit: ≥200 ms between requests.
- **Thumbnails are downloaded to `data/images/` at ingest time** and skipped if
  present. Mapillary thumb URLs are signed and expire within hours — local
  copies make `inspect` and the demo immune to that, and they're what we send
  to OpenAI anyway.
- Then runs pairing across all images in the DB and `INSERT OR IGNORE`s pairs.
- Prints a summary: images fetched, pairs found, ready-to-judge count — so you
  see whether a bbox is worth spending OpenAI calls on.

## Inspection (`inspector.py` + `inspect`)

- Selects pairs with no judgment, best score first, capped by `--limit`
  (default 25 — spend control).
- Request: chat completions; content = `prompts/inspector.md` text + two labeled
  base64 images ("OLDER, captured 2018-06-11" / "NEWER, captured 2024-09-02"),
  detail high.
- **Structured outputs** (`response_format: json_schema`, `strict: true`),
  mirrored by a pydantic model:
  `changed` bool · `category` enum(`construction`, `demolition`,
  `storefront_change`, `signage`, `road_infrastructure`, `surface_condition`,
  `street_furniture`, `vegetation`, `other`, `no_change`) ·
  `magnitude` enum(`major`, `moderate`, `subtle`) — the detection-floor axis ·
  `confidence` 0–1 · `evidence` string (one sentence).
  ⚠ Must agree with your `inspector.md` — when you paste it we reconcile in
  whichever direction you prefer.
- `--image-size 2048` sends higher-resolution thumbnails for surface-focused
  runs (~2–3× the token cost, still cents at demo scale); default 1024.
- Retries with backoff on 429/5xx (3 attempts); a failing pair is marked
  `status=error` and skipped next run unless `--retry-errors`.
- `--dry-run` prints how many pairs would be judged (+ rough cost), no API calls.
- Model: `OPENAI_MODEL` env (default `gpt-4o`), `--model` flag overrides.

## Server + UI (`server.py`, `static/`)

Routes:
- `GET /` → `index.html`; `/static/*` assets
- `GET /api/changes` → judged pairs: `{pair_id, lat, lon, category, magnitude,
  confidence, evidence, changed, older: {image_id, date}, newer: {…}}`;
  `?include_unchanged=1` adds `no_change` pairs
- `GET /api/stats` → counts (images, pairs, judged, changed) for a header bar
- `GET /images/{image_id}.jpg` → served from the local thumbnail cache

UI (vanilla JS + MapLibre GL from CDN):
- Basemap: OSM raster tiles via an inline style — zero API keys.
- Markers colored by category (construction orange, demolition red, storefront
  purple, signage blue, road slate, surface rose, furniture cyan, vegetation
  green, other gray) + a **magnitude filter (Major / Moderate / Subtle chips)**
  — the "detection floor" demo beat — and a "show unchanged" toggle; map fits
  to marker bounds on load.
- Click → side panel: before/after slider (two stacked `<img>` + range input
  driving a clip), capture dates, category chip, magnitude tag, confidence bar,
  evidence.

## CLI

```
python -m chronos ingest --bbox -122.4200,37.7580,-122.4060,37.7690 \
    [--limit 2000] [--max-dist 15] [--max-heading 30] [--min-gap-days 730] [--refresh]

# subtle-change preset: better-aligned pairs for surface detail, fewer of them
python -m chronos ingest --bbox ... --max-dist 8 --max-heading 15

python -m chronos inspect [--limit 25] [--model gpt-4o] [--image-size 1024|2048] \
    [--dry-run] [--retry-errors]
python -m chronos serve [--port 8000]
```

Run from the repo root; no packaging/install step.

## Decisions & defaults — veto any

1. **OpenAI via raw httpx, not the `openai` SDK** — the stack names httpx;
   keeps deps at 6. Happy to swap.
2. **Thumbnails downloaded locally at ingest** (~150–300 KB each) — required
   anyway because Mapillary URLs expire.
3. **Greedy 1:1 pairing** — fewer, better pairs over exhaustive near-duplicates.
4. **Category enum + magnitude axis as listed** — reconciled with your prompt
   when pasted.
5. **OSM raster basemap** — swappable for any MapLibre style URL later.
6. **Default model `gpt-4o`** via chat completions; env/flag to change.

## Milestones → commits

1. **Scaffold + pairing** — layout, config, db schema, `pairing.py` + tests green
   → `scaffold: project layout, config, db schema; pairing module with tests`
2. **Ingest** — Mapillary client, caching, thumbnails, pair generation
   → `ingest: mapillary bbox fetch, caching, thumbnails, candidate pairs`
3. **Inspect** — OpenAI judge, structured outputs, storage
   → `inspect: openai vision judge with strict schema, judgment storage`
4. **UI + docs** — map, slider, README
   → `serve: maplibre map with category markers and before/after slider; README`

## Risks

- **Bbox coverage** — the idea needs an area Mapillary re-photographed years
  apart. Mitigation: ingest summary shows pair count *before* any OpenAI spend;
  test in dense areas (SF, Amsterdam, Berlin).
- **OpenAI cost** — `inspect --limit 25` default (~50 hi-detail images/run).
- **Parallax false positives** — 15 m / 30° still allows angle differences; the
  score ordering feeds best-aligned pairs first, and the inspector prompt's
  "durable change" framing does the rest. The `subtle` tier is the least
  reliable — curate demo examples you've verified by eye.

## Definition of done (restated)

`ingest` on a test bbox → candidate pairs in SQLite · `inspect` → structured
judgments · `serve` → marker + slider flow · README with setup + screenshot
placeholder · runs locally with just the two API keys.
