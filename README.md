# Séance

> *Talk to the dead web.*

Web pages disappear. Statistics get silently revised. A figure you cited last month may resolve
to a 404 today, or quietly show a different number, with no trace of what it originally said.

**Séance** captures sources over time, detects exactly what changed, resurrects dead pages from
public web archives, and issues a citation certificate proving what a source said and when.

---

## What it does

| Ritual     | Meaning |
|------------|---------|
| **Summon** | Submit a URL or JSON endpoint. Séance fetches a snapshot and stores it, content-addressed by its SHA-256 hash. |
| **Watch**  | The source is re-fetched on a schedule. Every change is detected and shown as a side-by-side diff on a version timeline. |
| **Resurrect** | If a source returns 404/410 or times out, Séance queries public web archives for the most recent surviving version and surfaces it as a labeled "ghost version" with its origin archive and capture date. |
| **Certify** | A citation certificate is generated: source URL, capture timestamp, SHA-256 content hash, hash-chain link to the previous version, and a link to the stored evidence. |

---

## Architecture

- **Frontend:** Next.js — timeline of versions, side-by-side diff, ghost-found view, certificate page.
- **API:** Python FastAPI.
- **Database:** PostgreSQL.
- **Workers:** background jobs for scheduled re-capture, diff computation, and archive lookup.
- **Adapters:** live HTTP, JSON endpoint, web-archive — all behind a single `fetch(source) -> RawCapture` interface. New adapters require no changes to the core.
- **Snapshot store:** content-addressed, append-only. File name equals the SHA-256 hex digest of the normalized content.

---

## Engineering principles

1. **Immutability** — captures are append-only; nothing is ever overwritten.
2. **Idempotency** — capturing identical content twice creates one stored record.
3. **Ports and adapters** — the core depends on interfaces, not on specific libraries or services.
4. **Evidence over claims** — every output links back to a stored capture; results are labeled verified or not verified.
5. **Respectful fetching** — honor `robots.txt`, rate-limit, prefer official APIs and archives.
6. **Fail visibly** — log and surface errors; never swallow them.
7. **Test with properties** — Hypothesis property-based tests for all core invariants.

---

## Status

**Built during Kiroween 2026 — hackathon category: Resurrection.**

This project was started from scratch during the submission period.

---

## Quick start

*Setup instructions will be added as the implementation progresses.*

## License

MIT
