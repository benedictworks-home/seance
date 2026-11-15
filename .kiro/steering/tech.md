# Séance — Tech Steering

## Stack

| Layer            | Technology |
|------------------|------------|
| Frontend         | Next.js (TypeScript) |
| API              | Python FastAPI |
| Database         | PostgreSQL (Supabase acceptable) |
| Background jobs  | Python workers + a task queue (e.g. ARQ or Celery) |
| Snapshot store   | Content-addressed flat files; file name = SHA-256 hex digest |
| Testing          | pytest + Hypothesis (property-based) for backend core |

## Repository layout

```
/
├── backend/          # FastAPI app, workers, core domain logic
│   ├── core/         # domain: RawCapture, normalizer, hasher, store, diff engine
│   ├── adapters/     # fetch adapters (HTTP, JSON endpoint, web-archive)
│   ├── api/          # FastAPI routers and schemas
│   ├── workers/      # background job definitions
│   └── tests/
├── frontend/         # Next.js app
│   ├── app/          # App Router pages (timeline, diff view, certificate, ghost view)
│   └── components/
├── docs/             # Architecture notes, ADRs
└── README.md
```

## Architecture: ports and adapters

The core domain (`backend/core/`) depends only on interfaces, never on HTTP libraries,
database drivers, or specific archive services directly.

**Primary port — fetch interface:**

```
fetch(source: Source) -> RawCapture
```

Implementations live in `backend/adapters/`:
- `LiveHttpAdapter` — fetches live URLs via HTTP/HTTPS.
- `JsonEndpointAdapter` — fetches and normalizes JSON responses.
- `WebArchiveAdapter` — queries the Wayback Machine (or compatible) for the most recent
  archived snapshot of a dead URL.

Adding a new adapter never requires changes to the core.

**Snapshot store interface:**

```
store(raw: RawCapture) -> Capture   # content-addressed, append-only
exists(sha256: str) -> bool
retrieve(sha256: str) -> RawCapture
```

**Provenance / anchor interface:**

```
anchor(capture: Capture) -> Anchor  # optional; Stellar implementation behind this interface
```

## Data model (logical)

```
Source
  id, url, label, created_at, watch_schedule

Capture
  id, source_id, sha256, captured_at, adapter, status, storage_path

Diff
  id, from_capture_id, to_capture_id, changes (JSONB), created_at

Anchor                        # optional
  id, capture_id, tx_id, anchored_at, chain (Stellar | stub)
```

## Key invariants (enforced in code and tests)

- Captures are append-only; no row is ever updated or deleted.
- Two captures of identical content share one stored file (idempotent by SHA-256).
- The snapshot store is content-addressed: `stored_file_name == sha256(content)`.
