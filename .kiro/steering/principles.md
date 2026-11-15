# Séance — Engineering Principles

These are standing rules. Follow them in every decision, every file, every review.

---

## 1. Immutability

Captures are append-only. Once a snapshot is written to the store, it is never modified or
deleted. Database rows for `Capture` are never updated. If a re-capture finds new content, a
new `Capture` record is created; the old one is untouched.

## 2. Idempotency

Capturing identical content twice creates no duplicate. Before writing, compute the SHA-256 of
the normalized content and check whether that hash already exists in the store. If it does,
return the existing `Capture` record; write nothing new.

## 3. Ports and adapters

The core domain (`backend/core/`) depends on interfaces (abstract base classes or protocols),
not on concrete fetch libraries, database drivers, or archive services. All external dependencies
are injected through adapter implementations. Adding a new data source or storage backend must
not require changes to any file in `backend/core/`.

## 4. Evidence over claims

Every API response, certificate, and UI element that makes a factual claim must link back to a
stored capture. Results are explicitly labeled **verified** (hash confirmed against stored file)
or **not verified** (hash not yet confirmed or store unavailable). Never surface a claim without
its evidence link.

## 5. Respectful fetching

- Honor `robots.txt` before fetching any live URL.
- Rate-limit outbound requests; never hammer a host.
- Prefer official APIs and public archives (Wayback Machine CDX API) over scraping.
- Set a descriptive `User-Agent` that identifies the tool.

## 6. Fail visibly

Log every error with full context (URL, adapter, timestamp, exception). Surface errors in API
responses with structured detail. Never swallow an exception silently. Background workers must
record failure status on the `Capture` row and emit a log event.

## 7. Test with properties

Use Hypothesis for property-based tests on all core logic. Required properties (at minimum):

- **Hashing is deterministic:** `hash(content) == hash(content)` for any byte sequence.
- **Idempotent capture:** storing the same content twice yields exactly one stored file and one
  `Capture` record.
- **Normalization is idempotent:** `normalize(normalize(x)) == normalize(x)` for any input.
- **Content-addressing:** `sha256(retrieve(h)) == h` for any stored hash `h`.

Unit tests cover the happy path. Property tests cover the invariants above. Integration tests
cover adapter behavior against fixtures, not live network calls.
