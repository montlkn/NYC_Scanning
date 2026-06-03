# Future Embeddings Repo — design notes

## Why this exists

We removed CLIP, torch, and `open-clip-torch` from the `nyc_scan` API repo to make
Render deploys fast and cheap. The API no longer encodes user-submitted scan photos
into the `reference_embeddings` table. But the *photos themselves* are still being
saved to Cloudflare R2 on every scan (see `backend/routers/scan.py` — the initial
scan upload at the `upload_image(photo_bytes, f"scans/{scan_id}.jpg", ...)` call).

Once enough confirmed scans accumulate in R2, a **separate repo** will:

1. Read photos from R2.
2. Read scan metadata from Supabase (`scans` table on the buildings project).
3. Encode each photo with CLIP.
4. Write embeddings to `reference_embeddings`.

This keeps the live API container slim and stateless; the embedding work runs as a
batch / cron / on-demand job somewhere with GPU access (or a small CPU box —
ViT-B-32 at low throughput is fine on CPU).

## What "confirmed" means here

Only confirmed scans are training-grade. The trigger: `scans.confirmed_bin IS NOT NULL`
AND `scans.was_correct = true` (or at least `confirmed_bin` was in `candidate_bins[:3]`,
matching the old `was_in_top_3` gate). Map-picker confirmations
(`verification_method = 'map_picker'`) are gold-quality — user tapped the building on
the map themselves.

## Data contract

### Photo storage in R2
- **Bucket:** `settings.r2_bucket` (current `nyc_scan` value).
- **Key:** `scans/{scan_id}.jpg` (already the convention).
- **Public URL pattern:** `https://pub-234fc67c039149b2b46b864a1357763d.r2.dev/scans/{scan_id}.jpg`.
- **Thumbnails:** `scans/{scan_id}_thumb.jpg` if `create_thumbnail=True` was used.

### Scan metadata in Supabase (`buildingsClient`)
Per `CLAUDE.md`, the `scans` table lives on the **buildings** project, not the main
project. Columns the embedding job needs:
- `id` (uuid, the `scan_id`)
- `confirmed_bin` (varchar) — the ground-truth label
- `candidate_bins` (text[])
- `top_match_bin` (varchar)
- `was_correct` (bool)
- `confirmed_at` (timestamp)
- `gps_lat`, `gps_lng` (float)
- `compass_bearing` (float)
- `phone_pitch` (float)
- `verification_method` (varchar, e.g. `map_picker`, `top1`, `manual`)

### Embeddings destination (`reference_embeddings`)
Existing table. Columns the job writes (matches the old `services/clip_disambiguation.py::cache_embedding` signature before deletion):
- `bin` (varchar) — = `confirmed_bin`
- `angle` (int) — = `compass_bearing` rounded
- `image_key` (text) — = `scans/{scan_id}.jpg`
- `embedding` (vector(512)) — CLIP ViT-B-32 output
- `source` (text) — set to `user_scan` (vs. older `streetview`, `ondemand`)
- `created_at` (timestamp)

## Repo skeleton

```
nyc_scan_embeddings/
  README.md
  requirements.txt        # torch, torchvision, open-clip-torch, boto3, asyncpg or psycopg, supabase, pillow
  config.py               # mirrors r2_* and database_url from nyc_scan's settings
  embed.py                # CLI: `python embed.py --since 2026-05-01 --limit 1000`
  src/
    clip_model.py         # load + cache ViT-B-32; encode_photo(bytes) -> List[float]
    r2.py                 # download_photo(scan_id) -> bytes
    supabase.py           # fetch_unembedded_scans(since, limit), write_embedding(...)
    job.py                # orchestrate: list scans → fetch photo from R2 → encode → write row
```

## Job flow

```
fetch_unembedded_scans(since, limit):
    SELECT id, confirmed_bin, compass_bearing
    FROM scans
    WHERE confirmed_bin IS NOT NULL
      AND confirmed_at >= :since
      AND id NOT IN (
        SELECT DISTINCT image_key
        FROM reference_embeddings
        WHERE source = 'user_scan'
      )  -- or join on a dedicated processed_at column
    ORDER BY confirmed_at
    LIMIT :limit

for scan in scans:
    bytes = await r2.download_photo(scan.id)
    if not bytes: continue
    embedding = clip.encode_photo(bytes)
    await write_embedding(
        bin=scan.confirmed_bin,
        angle=int(scan.compass_bearing),
        image_key=f"scans/{scan.id}.jpg",
        embedding=embedding,
        source="user_scan",
    )
```

## When to start this repo

Wait until there are enough confirmed scans in R2 to justify the work — rough
threshold is "a few thousand `was_correct=true` rows", since below that the embedding
search benefit is marginal. Check with:

```sql
SELECT count(*) FROM scans
WHERE confirmed_bin IS NOT NULL AND was_correct = true;
```

## Cost / infra

- **GPU not required.** ViT-B-32 on CPU runs maybe 5-10 photos/sec — fine for a
  nightly batch even at 50k photos.
- **Storage:** R2 reads are cheap (Cloudflare doesn't charge egress to the embedding
  worker if it's also on Cloudflare, otherwise it's ~$0.36/GB).
- **Where to run:** a tiny dedicated Render service with cron, a GitHub Action on
  a schedule, or local cron on any always-on machine. Keep it OUT of the API repo —
  the whole point of this split is to never need torch in production API again.

## Open questions

- Do we need to backfill embeddings for scans collected before this split? If yes,
  the job's first run is the backfill.
- Schema change to `reference_embeddings`: add `source = 'user_scan'` enum value if
  it's constrained; otherwise the column is free-form text.
- Deduplication: same `(bin, angle, image_key)` should be unique — add a unique
  constraint in a follow-up migration.
