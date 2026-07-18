# Inspector prompt (placeholder — replace with your real prompt)

The `inspect` command sends the text of this file, followed by two labeled
street-level photos (OLDER and NEWER) of the same location, and asks the vision
model to judge whether a durable physical change occurred between them.

The model must return JSON matching the strict schema Chronos enforces (defined
in `chronos/inspector.py`, built in milestone 3):

- `changed` (boolean): did a durable physical change occur?
- `category` (enum): `construction`, `demolition`, `storefront_change`,
  `signage`, `road_infrastructure`, `surface_condition`, `street_furniture`,
  `vegetation`, `other`, `no_change`
- `magnitude` (enum): `major`, `moderate`, `subtle` — the "detection floor" axis
- `confidence` (number, 0–1)
- `evidence` (string): one sentence citing exactly what changed

Ignore transient differences — parked cars, pedestrians, lighting, weather, and
seasonal foliage — and report only durable changes to the built environment.

> When you paste your real prompt over this file, its categories and fields must
> match the schema in `chronos/inspector.py`. We reconcile the two then.
