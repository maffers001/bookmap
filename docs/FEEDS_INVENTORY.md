# Feeds Inventory

Generated during research scaffolding.

## Location

- **FEEDS_DIR:** `C:\Bookmap\Feeds`
- **Format:** Bookmap `.bmf` (Replay only)
- **Contents:** NQ + ES front-month contracts per session (e.g. NQM6/ESM6, NQU6/ESU6)

## Snapshot (at scaffolding time)

| Metric | Value |
|--------|-------|
| `.bmf` count | 335 |
| Total size | ~142 GB |
| Naming | `feed_YYYYMMDD_HHMMSS_mmm.bmf` (also older `feed_Multiplatform_...`) |
| Date span (sample) | 2024-09 through 2026-09 |

## Initial corpus plan

User extracts **5–10 random RTH days** (files that include UK 14:30–17:00) via Session Extractor into `data/raw/<session_id>/`.

## Session ID convention

Default: stem of the `.bmf` filename without extension, e.g. `feed_20260918_091523_227`.
