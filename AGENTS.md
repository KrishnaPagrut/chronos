# AGENTS.md — Chronos

Context and working guide for coding agents on this repository. Read this first.

## What Chronos is

Chronos detects **durable physical changes to city streets** by comparing
crowdsourced street-level photos of the same location taken years apart, and
judging each pair with a vision LLM.

The pitch is the **detection floor**: big changes (a demolished building, new
construction) already live in municipal permit databases; small ones (a repaved
crosswalk, a removed bollard, a spreading pavement crack) exist in no dataset at
all. The same judge that flags a finished building flags a `subtle` surface
change, and the map filters down to the layer no city has records of. The
`magnitude` axis (major / moderate / subtle) is the product's core idea.

## Pipeline

```
ingest ──► images table ──► pairing.py ──► pairs table
                                             │
inspect ─► OpenAI vision (strict JSON) ──► judgments table
                                             │
serve  ──► FastAPI /api/* ──► MapLibre map + before/after slider + Street View
```

All state lives in `data/` (SQLite DB + downloaded thumbnails), which is
**gitignored**. Every command is idempotent: images, pairs, judgments, and raw
API responses are cached, and no pair is ever re-fetched or re-judged.

## Stack and hard constraints (do not violate)

- **Python 3.12** backend: FastAPI, httpx, `sqlite3` (stdlib), pydantic.
- **Frontend**: a single-page vanilla-JS app with MapLibre GL + mapillary-js
  loaded from a CDN. **No build step, no bundler, no framework.**
- **Dependencies stay under 10.** Current count is 6 (see `requirements.txt`):
  `fastapi, uvicorn, httpx, pydantic, python-dotenv, pytest`. The OpenAI call is
  made with raw `httpx`, not the `openai` SDK, deliberately — do not add the SDK.
- **No Docker, no Postgres, no React.**
- Single CLI entrypoint: `python -m chronos {ingest,inspect,serve}`.
- Config via `.env` at repo root: `MAPILLARY_TOKEN`, `OPENAI_API_KEY`,
  optional `OPENAI_MODEL` (default `gpt-4o`). `.env.example` is committed; never
  commit `.env`.
- The pairing logic is isolated in `chronos/pairing.py` as **pure functions**
  (no I/O, stdlib only) so it stays unit-testable. Keep it that way.
- Rate-limit and cache all external API calls.

## Repository layout

```
chronos/
├── __main__.py      # argparse CLI: ingest | inspect | serve
├── config.py        # .env loading, filesystem paths, thumb_path()
├── db.py            # SQLite schema + all query/upsert helpers
├── pairing.py       # PURE pairing logic (haversine, headings, spatial grid, greedy 1:1)
├── mapillary.py     # Graph API client: bbox tiling, paging, rate limit, cache, thumbnails
├── inspector.py     # OpenAI vision judge: strict JSON schema, retries, confidence floor
├── server.py        # FastAPI app: JSON API + static file serving
└── static/
    ├── index.html   # single page
    ├── app.js       # map, markers, filters, detail panel, street view, search-this-area
    └── style.css    # theming (light/dark), CVD-validated category palette
prompts/inspector.md # the judging prompt (authoritative; schema mirrors it)
tests/               # test_pairing.py (21), test_inspector.py (11)
data/                # gitignored: chronos.db + images/<id>_<size>.jpg
docs/                # screenshots referenced by README
PLAN.md              # original approved design doc
```

## Setup and run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env         # paste MAPILLARY_TOKEN and OPENAI_API_KEY

# Fetch imagery for a bbox and build candidate pairs (free — no OpenAI)
python -m chronos ingest --bbox -122.4270,37.7480,-122.4060,37.7640 --limit 2500

# Judge candidate pairs with the vision model (spends ~$0.01/pair)
python -m chronos inspect --dry-run          # preview count + cost, no calls
python -m chronos inspect --limit 25

# Serve the map UI
python -m chronos serve                       # http://127.0.0.1:8000
```

**Run commands from the repo root** so `chronos` is importable (running a script
by absolute path does not put the repo on `sys.path`).

## CLI reference

- `ingest --bbox minLon,minLat,maxLon,maxLat [--limit 2000] [--max-dist 15]
  [--max-heading 30] [--min-gap-days 730] [--refresh]`
  Subtle-change preset: `--max-dist 8 --max-heading 15`.
  (There is a `--bbox` argparse shim so a leading-negative longitude parses.)
- `inspect [--limit 25] [--model gpt-4o] [--image-size 1024|2048] [--dry-run]
  [--retry-errors]`
- `serve [--port 8000]`

## Data model (SQLite, `data/chronos.db`)

- **images** — `id` PK (Mapillary id), `lon`, `lat`, `heading`, `captured_at`
  (epoch ms), `sequence_id`, `is_pano`, `thumb_path`, `thumb_url`, `fetched_at`.
  Prefers Mapillary's SfM-corrected `computed_geometry`/`computed_compass_angle`,
  falling back to the raw fields.
- **pairs** — `id` PK = `"<older_id>_<newer_id>"`, `older_id`, `newer_id`,
  `distance_m`, `heading_diff_deg`, `gap_days`, `score` (lower = better aligned),
  `status` (`candidate` | `judged` | `error`), `error`, `created_at`.
- **judgments** — `pair_id` PK/FK, `model`, `old_description`, `new_description`,
  `changed`, `category`, `magnitude`, `confidence`, `evidence`, `raw_json`,
  `created_at`. `magnitude` is kept on every row (even `no_change`) so precision
  can be reported per tier.
- **api_cache** — `key` PK (request signature, token stripped), `body`,
  `fetched_at`. Makes re-running `ingest` free unless `--refresh`.

## Key module notes

- **pairing.py** — a candidate pair must satisfy ALL of: haversine distance
  ≤ `max_dist_m` (default 15), circular heading diff ≤ `max_heading_deg`
  (default 30), capture gap ≥ `min_gap_days` (default 730), and different
  `sequence_id`. Panoramas and images with no heading are dropped. A spatial
  grid keeps candidate search near-O(n). `find_pairs` does greedy 1:1 matching
  (best score first) so one location yields one pair, not many near-duplicates —
  this is the cost lever, since every returned pair is one OpenAI call. Boundary
  conditions are inclusive and covered by tests (including a grid-vs-brute-force
  completeness test).
- **mapillary.py** — the Graph `/images` endpoint returns HTTP 500 ("reduce the
  amount of data") on a large/dense bbox even with `limit=1`, so `fetch_bbox`
  **tiles** the box (~500 m start tiles), and any tile the endpoint rejects is
  split into quadrants down to ~40 m. `time_budget_s` caps wall-clock time for
  interactive use. Duplicate images across overlapping tiles collapse on the
  primary key; pairing runs globally afterward so tile-seam pairs are still found.
- **inspector.py** — OpenAI chat-completions with structured outputs
  (`response_format: json_schema`, `strict: true`), mirrored by a pydantic
  `Report`. **Field order is generation order**: `old_description`,
  `new_description` come BEFORE the verdict fields, so the model describes before
  it judges. Two prompt rules are also enforced in code: `confidence < 0.40`
  coerces `changed=false, category=no_change`; `{OLD_DATE}`/`{NEW_DATE}` are
  substituted with real capture dates. Retries with backoff on 429/5xx.
- **server.py** — read-mostly over SQLite. Endpoints: `GET /api/changes`
  (`?include_unchanged=1`), `/api/config` (serves the Mapillary token to the
  browser — that token type is client-usable, like a Maps key), `/api/nearest`
  (Street View entry), `/api/stats`, `/api/search_area` + `/api/judge_area` +
  `/api/job/{id}` (the "search this area" background-job pipeline), `/images/{id}.jpg`,
  `/` and `/static/*`.
- **static/** — markers colored by category with a palette validated for
  color-vision deficiency in both light and dark themes; the two color-adjacent
  categories also use distinct marker shapes. Magnitude filter loads Major +
  Moderate on, Subtle opt-in. Features: before/after wipe slider, fullscreen
  image lightbox, resizable panes, pegman → mapillary-js Street View with change
  markers in-scene, and "search this area". Deep links: `?pair=<id>`,
  `?pair=<id>&expand=1`, `?sv=<lat>,<lon>`, `?c=<lat>,<lon>&z=<zoom>`, `?theme=`.

## Categories and magnitude (authoritative enums)

`category`: `construction, demolition, storefront_change, signage,
road_infrastructure, surface_condition, street_furniture, vegetation, other,
no_change`.
`magnitude`: `major, moderate, subtle`.

These must match `prompts/inspector.md` and the schema in `inspector.py`. The
prompt file is the source of truth — reconcile the schema to it if they diverge.

## Testing

```bash
python -m pytest -q          # 32 tests
```
`tests/test_pairing.py` covers the pure geometry/predicate/greedy logic;
`tests/test_inspector.py` covers the schema contract, the confidence floor, date
substitution, and retry/backoff (via an httpx MockTransport — no network).

## Gotchas

- **Run from the repo root** (see Setup) or imports fail.
- **Thumbnails and the DB are gitignored** — a fresh `git clone` starts with an
  empty map. The populated `data/` only exists locally; to move the demo, copy
  `data/chronos.db` and `data/images/`.
- **Judging costs money and is rate-limited.** GPT-4o vision calls are
  token-heavy, so throughput is capped by the account's TPM limit regardless of
  concurrency; expect 429s under parallel load (they are free and retryable).
  Always confirm before large `inspect` runs.
- **Coverage is uneven.** Change detection only works where Mapillary has both
  old and new photos of the same spot. Central/east SF (Mission, Valencia,
  Market, SoMa) has rich multi-year coverage; many other areas are sparse.
- **Never re-fetch / never re-judge**: rely on `api_cache`, on-disk thumbnails,
  and `inspect` selecting only pairs without a judgment.

## Conventions

- Commit messages: short, plain, imperative, one line (e.g. "Add before/after
  slider"). No tool-attribution trailers.
- Match the surrounding code's style and comment density. Keep `pairing.py` pure.
- Prefer editing existing modules over adding files or dependencies.
