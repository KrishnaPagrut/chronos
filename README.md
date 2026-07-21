# Chronos

Detect durable physical changes to city streets from crowdsourced street-level
imagery. Chronos pairs Mapillary photos of the same spot taken years apart,
judges each pair with a vision LLM, and puts the results on a map with a
before/after slider.

![Chronos map UI](docs/screenshot-light.png)

Big changes (construction, demolition) are already tracked in permit databases.
Small ones — a repaved crosswalk, a removed bollard, a spreading pavement
crack — exist in no dataset at all. Chronos's story is its **detection floor**:
the same judge that flags a finished building flags a `subtle` surface change,
and the map filters down to the layer no city has records of.

## How it works

```
ingest ──► Mapillary images ──► pairing ──► candidate pairs
                                               │
inspect ─► vision LLM (strict JSON) ────► judgments
                                               │
serve  ──► FastAPI + MapLibre map ──► before/after explorer
```

1. **Ingest** pulls street-level images for a bounding box from the Mapillary
   Graph API (auto-tiling dense areas, caching every response), then finds
   pairs: two photos within 15 m and 30° of heading, captured ≥ 2 years apart,
   from different capture sequences. Greedy 1:1 matching keeps one
   best-aligned pair per location.
2. **Inspect** sends each pair to the OpenAI vision API with a strict JSON
   schema. The model must describe both images *before* judging, then reports
   `category`, `magnitude` (major / moderate / subtle), calibrated
   `confidence`, and one sentence of `evidence`. Verdicts under 0.40
   confidence are coerced to `no_change` in code.
3. **Serve** renders judged pairs on a MapLibre map — markers colored by
   category, magnitude filter chips, and a slider that wipes between the two
   captures. Drag the **pegman** onto the map for a Street View mode: drop it
   anywhere to walk through the latest Mapillary imagery, with the detected
   changes floating as markers in the scene.

Everything is idempotent: images, pairs, judgments, and raw API responses live
in SQLite, and no pair is ever re-fetched or re-judged.

## Built with AI

I built Chronos in close partnership with Codex, and I want to be upfront about
that. The architecture decisions were mine and Codex helped me move fast on the
implementation. I chose to keep the pairing logic pure and unit tested, to make
every stage idempotent so nothing is ever re-fetched or re-judged, to put the
low-confidence backstop in code instead of trusting the prompt, and to validate
the model output on the server before anything reaches the browser. Codex turned
those calls into working code quickly, from the FastAPI layer to the MapLibre
and mapillary-js frontend to the strict output schemas.

The clearest example was the hardest problem in the project. Mapillary's API
returns an error on dense city areas because the region holds too much data to
serve in one request. I diagnosed that the bounding box itself was the problem
and decided to split it recursively into smaller tiles, fetch each one, and then
stitch the results back together, deduplicating images and pairing across the
tile seams so the cuts I made for the network stay invisible to the geometry. Codex
helped me implement that recursive subdivision and reassembly fast, but the
decision of what to cut and how to stitch it back was the part that had to be
right, and that was mine.

The takeaway for me was that working with Codex well is a decision-making skill and
not a delegation trick. The quality of the output tracked directly with the
constraints and judgment I brought to it.

## Setup

Requires Python 3.12 and two API keys:
[Mapillary](https://www.mapillary.com/dashboard/developers) (free) and
[OpenAI](https://platform.openai.com/api-keys).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then paste your two keys into .env
```

## Usage

```bash
# 1. Fetch imagery for a bbox and build candidate pairs (free)
python -m chronos ingest --bbox -122.43,37.77,-122.40,37.79 --limit 500

# subtle-change preset: tighter alignment for surface-level detail
python -m chronos ingest --bbox ... --max-dist 8 --max-heading 15

# 2. Judge pairs with the vision model (costs ~$0.01/pair; --dry-run to preview)
python -m chronos inspect --dry-run
python -m chronos inspect --limit 25

# 3. Explore the results
python -m chronos serve            # http://127.0.0.1:8000
```

The ingest summary prints how many pairs a bbox produced *before* you spend
anything on judging. `inspect --image-size 2048` sends higher-resolution
thumbnails for surface-focused runs.

## Live demo

The deployed explorer is deliberately read-only. It serves the curated
`demo_data/` bundle (a SQLite snapshot plus only the thumbnails used by judged
pairs), so it needs **no** Mapillary token, OpenAI key, ingestion, or judging at
runtime. Live-data controls such as **Search this area** and Street View are
hidden on the deployment; the evidence map and before/after explorer remain
fully interactive.

Deploy with [Render](https://render.com) using the committed `render.yaml`:

1. Push this repository, including `demo_data/`, to GitHub.
2. In Render, choose **New → Blueprint**, connect the repository, and approve
   the `chronos-explorer` service described by `render.yaml`.
3. Click **Apply**. Do not add any environment secrets. Render installs the
   pinned requirements and starts `chronos.demo_server` on its provided `PORT`.
4. Open the generated `https://chronos-explorer.onrender.com` URL and verify a
   marker opens its local before/after photos.

To run the exact production explorer locally without any secrets:

```bash
CHRONOS_DEMO_ONLY=1 uvicorn chronos.demo_server:app --host 127.0.0.1 --port 8000
```

The normal `python -m chronos serve` command remains the local development
server and can use the separate, gitignored `data/` directory.

## UI

- Markers colored by change category (palette validated for color-vision
  deficiency in both themes; the two color-adjacent categories also get
  distinct marker shapes)
- Magnitude filter — loads with Major + Moderate active, Subtle as an opt-in
  chip
- Click a marker: before/after wipe slider, capture dates, the model's
  description of each image, its evidence sentence, and pair geometry
- Light and dark themes (follows the system, toggle in the header)
- **Search this area** — pan or zoom to a new neighborhood and a button appears;
  it runs detection on that viewport in two cost-safe steps: first it fetches
  imagery and finds candidate pairs (free), then shows how many pairs it found
  and the cost to judge them, spending OpenAI credits only when you confirm
- **Street View mode** — drag the pegman onto the map to drop into a navigable
  360° Mapillary panorama (via [mapillary-js](https://github.com/mapillary/mapillary-js)),
  with detected changes as clickable 3D markers and a "you are here" indicator
  synced back to the map. Areas without nearby panoramas show an explicit
  availability message rather than a fixed-field-of-view photo.
- Drag the dividers between the map, Street View, and detail panes to resize
  them; click the ⤢ button on any before/after to open it fullscreen
- Deep links: `/?pair=<pair_id>` preselects a marker, `/?sv=<lat>,<lon>` opens
  Street View at a point, `/?c=<lat>,<lon>&z=<zoom>` pins the map view, and
  `/?theme=dark` forces a theme

![Street View mode](docs/screenshot-streetview.png)

> **Note on the Mapillary token:** Street View runs the viewer in the browser,
> so the server exposes the token via `/api/config`. Mapillary access tokens are
> client-usable by design (like a Maps API key); for a public deployment, use a
> token scoped to read-only.

## Project layout

```
chronos/
├── chronos/
│   ├── __main__.py     # CLI: ingest | inspect | serve
│   ├── pairing.py      # pure pairing logic (unit-tested, no I/O)
│   ├── mapillary.py    # Graph API client: tiling, paging, cache, thumbnails
│   ├── inspector.py    # vision judge: strict schema, retries, backstops
│   ├── server.py       # FastAPI: JSON API + static UI
│   ├── db.py           # SQLite schema and helpers
│   └── static/         # vanilla JS + MapLibre GL + mapillary-js (no build step)
├── prompts/inspector.md  # the judging prompt
├── tests/                # pairing + inspector tests
└── data/                 # gitignored: SQLite DB + cached thumbnails
```

6 dependencies, no build step, runs entirely locally.
