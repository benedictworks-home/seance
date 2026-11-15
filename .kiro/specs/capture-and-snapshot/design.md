# Spec: capture-and-snapshot — Design

> Status: **DRAFT**
> Requirements ref: requirements.md (APPROVED)

---

## 1. Module layout

```
backend/
├── src/
│   └── seance/
│       ├── core/
│       │   ├── domain.py          # Source, RawCapture, NormalizerResult, CaptureRecord
│       │   ├── ports.py           # FetchPort, BlobStore, CaptureRepository,
│       │   │                      # SourceRepository (Protocols)
│       │   ├── hasher.py          # sha256_hex(data: bytes) -> str
│       │   ├── normalizer.py      # DenyPattern, Denylist, Normalizer
│       │   ├── capture_service.py # CaptureService
│       │   └── exceptions.py      # domain exceptions
│       ├── adapters/
│       │   ├── http_adapter.py    # LiveHttpAdapter (implements FetchPort)
│       │   ├── robots_cache.py    # RobotsCache
│       │   ├── rate_limiter.py    # RateLimiter (threading.Lock)
│       │   ├── file_blob_store.py # FileBlobStore (implements BlobStore)
│       │   └── denylist_loader.py # load_denylist(path) — not in core/
│       ├── db/
│       │   ├── models.py          # SQLAlchemy ORM: SourceORM, CaptureORM
│       │   ├── repository.py      # SqlCaptureRepository, SqlSourceRepository
│       │   ├── migrations/        # Alembic migration scripts
│       │   └── schema.sql         # Reference DDL (triggers, privilege grants)
│       ├── resources/
│       │   └── denylist.yaml      # Default denylist of ad/analytics patterns
│       ├── settings.py            # Pydantic BaseSettings
│       ├── cli.py                 # CLI entry point
│       └── __main__.py            # python -m seance → cli.main()
├── tests/
└── pyproject.toml                 # package installed in editable mode: pip install -e .
```

All I/O is synchronous. There is no `asyncio` anywhere in this spec.

---

## 2. Domain objects (`core/domain.py`)

All domain objects are **frozen dataclasses** (immutable after construction).

### `Source`
Plain domain object; no ORM dependency. Fields: `id: UUID`, `url: str`,
`label: str | None`, `watch_schedule: str | None`.

### `RawCapture`
Fields per REQ-01. `content_type` is the bare MIME type (lowercase, no parameters).
`charset` is the `charset` parameter string from the `Content-Type` header, or `None`.
`raw_body` is the decompressed body bytes. `truncated: bool`. `error_reason: str | None`.

### `NormalizerResult`
Fields: `normalized_body: bytes`, `normalizer_version: str`,
`normalization_fallback: bool`.

### `CaptureRecord`
The value object passed to `CaptureRepository.insert`. Contains every field of the
`Capture` database model (REQ-13). Constructed by `CaptureService` from `RawCapture` +
`NormalizerResult` + computed hashes + `storage_path` returned by `BlobStore.put`.

---

## 3. Ports (`core/ports.py`)

Four `Protocol` interfaces. The core depends only on these; no concrete class is imported
anywhere in `core/`.

```python
class FetchPort(Protocol):
    def fetch(self, source: Source) -> RawCapture: ...

class BlobStore(Protocol):
    def put(self, sha256: str, data: bytes) -> str:
        """Verify sha256(data) == sha256 (raise BlobHashMismatchError on mismatch),
        write the blob if new, return the relative storage key ('ab/ab12….bin').
        No-op and returns the existing key if the hash is already stored."""
    def exists(self, sha256: str) -> bool: ...
    def get(self, sha256: str) -> bytes: ...

class CaptureRepository(Protocol):
    def insert(self, capture: CaptureRecord) -> None:
        """Open a fresh session, insert, commit, close."""
    def insert_in_new_transaction(self, capture: CaptureRecord) -> None:
        """Identical behaviour: open a fresh session, insert, commit, close.
        Exists as a distinct method so callers signal intent; a future implementation
        may apply different isolation or retry policies without changing call sites."""

class SourceRepository(Protocol):
    def get_or_create_by_url(self, url: str) -> Source:
        """Return existing Source for url, or insert a new one and return it."""
    def get(self, source_id: UUID) -> Source:
        """Return Source by id; raise SourceNotFoundError if missing."""
```

`BlobStore.put` returns the **relative storage key** (e.g. `"ab/ab1234….bin"`).
`CaptureService` stores this value as `storage_path` on `CaptureRecord`. The core never
constructs or interprets file paths; that knowledge belongs to `FileBlobStore`.

---

## 4. `CaptureService` (`core/capture_service.py`)

Receives `FetchPort`, `BlobStore`, `CaptureRepository`, and `Normalizer` via constructor
injection. Contains no I/O itself.

```python
CaptureService(
    fetcher: FetchPort,
    store: BlobStore,
    repo: CaptureRepository,
    normalizer: Normalizer,
)

def capture(self, source: Source) -> CaptureRecord:
    """Always returns a CaptureRecord. Never raises (except if the error-row
    insert fails, in which case the original exception is re-raised)."""
```

**Flow (matches REQ-17 steps, uniform error handling throughout):**

```python
raw = None          # set before the try so the except can tell whether fetch ran
error_recorded = False  # guard: prevent a second _record_error call from the outer except
                        # if _record_error itself raises

try:
    # Step 1: fetch always returns, never raises
    raw = fetcher.fetch(source)

    # Step 2: derive status
    status = _map_status(raw)

    # Step 3: non-ok fetch → record and return immediately (before any blob work)
    if status != "ok":
        record = _build_error_record(source, raw, status)
        error_recorded = True
        return _record_error(source, record)   # returns the record

    # Steps 4–6: hash
    raw_sha256  = sha256_hex(raw.raw_body)
    result      = normalizer.normalize(raw)    # never raises
    norm_sha256 = sha256_hex(result.normalized_body)

    # Steps 7–8: blob writes (may raise BlobHashMismatchError or I/O error)
    raw_key  = store.put(raw_sha256,  raw.raw_body)
    norm_key = store.put(norm_sha256, result.normalized_body)

    # Step 9: insert ok row (may raise DB error)
    record = _build_ok_record(source, raw, result, raw_sha256, norm_sha256, norm_key)
    repo.insert(record)
    return record

except Exception as exc:
    if error_recorded:
        # _record_error already ran and raised; just propagate
        raise
    # Build the best error record we can from whatever we have
    record = (
        _build_internal_error_record_from_raw(source, raw, exc)
        if raw is not None
        else _build_internal_error_record_no_raw(source, exc)
    )
    return _record_error(source, record, exc)
```

**Why `raw = None` before the `try`:** if `fetcher.fetch` itself raises unexpectedly
(shouldn't happen by contract, but defensive), the `except` branch has no `RawCapture`
and must use fallback field values (`source.url`, current UTC time, `"unknown"` adapter,
`status_code = 0`). When `raw` is available — including blob-write failures at steps 7–8
— the error record uses the **real** `status_code` from `raw` (e.g. `200`) so REQ-14's
`internal_error` annotation is correct.

**Why `error_recorded = True`:** step 3 calls `_record_error` before returning. If that
call raises (because `insert_in_new_transaction` failed), control falls to the `except`
block. Without the guard, the `except` would call `_record_error` a second time,
producing a duplicate row or masking the original error. The flag prevents that retry.

**`_record_error(source, record, exc=None) -> CaptureRecord`:**
1. Log at ERROR level (CRITICAL when `exc` is not `None`) with full context: source id,
   url, status, `error_reason`, exception type and message.
2. Call `repo.insert_in_new_transaction(record)`.
3. If that insert raises: log CRITICAL with both the original error and the insert
   failure, then re-raise the **original** exception (`exc` if present, otherwise the
   insert failure).
4. Return `record`.

`capture()` therefore **always returns a `CaptureRecord`** — either an `ok` record or an
error record. It raises only if `insert_in_new_transaction` fails while recording an
error, in which case the original error is re-raised.

**`_map_status(raw: RawCapture) -> str`** is a module-level pure function mapping
`(status_code, error_reason)` → status string per REQ-14. Covered by unit tests in
isolation.

**Note on `except Exception` vs `except BaseException`:** `BaseException` would catch
`KeyboardInterrupt` and `SystemExit`, preventing clean shutdown. `Exception` is the
correct boundary here; it covers all application-level errors.

---

## 5. `Normalizer` (`core/normalizer.py`)

```python
@dataclass(frozen=True)
class DenyPattern:
    field: str          # "id" | "class"
    pattern: str        # regex string

@dataclass(frozen=True)
class Denylist:
    version: str
    patterns: list[DenyPattern]

class Normalizer:
    def __init__(self, denylist: Denylist) -> None: ...
    def normalize(self, raw: RawCapture) -> NormalizerResult: ...
```

`normalize()` performs **no I/O**. The denylist is injected at construction; loading
it from disk is done in `adapters/denylist_loader.py`, not in `core/`.

**Content-type dispatch** (all matching on lowercased `content_type`):
- `text/html` → `_normalize_html(raw, denylist)`
- `application/json` or `application/*+json` → `_normalize_json(raw)`
- `text/xml`, `application/xml`, `application/*+xml` → passthrough
- anything else → passthrough

**`normalizer_version`** is composed as
`f"{NORMALIZER_CORE_VERSION}+denylist-{denylist.version}+bs4-{bs4.__version__}"`.

**HTML path** uses `BeautifulSoup(html_str, "html.parser")`. Steps per REQ-07. Charset
priority: `raw.charset` → `<meta>` scan in first 1024 bytes → UTF-8 fallback. After all
transforms, rewrites every `<meta charset>` and `<meta http-equiv="Content-Type">` to
declare `charset=utf-8`, then re-serializes with `soup.encode("utf-8")`.

**JSON path** per REQ-08: decode (charset → UTF-8 → UTF-16), `json.loads`, check for
non-finite floats (traverse the parsed object; set fallback if found),
`json.dumps(sort_keys=True, separators=(',', ':'), ensure_ascii=False)`, encode UTF-8.

**Fallback** for any exception in either path: catch, set `normalization_fallback=True`,
return `raw.raw_body` unchanged, log warning with content type, body length, exception.

---

## 6. `FileBlobStore` (`adapters/file_blob_store.py`)

Implements `BlobStore`. Storage root configured via `Settings.blob_store_root`.

**`put(sha256, data) -> str`:**
1. Verify `hashlib.sha256(data).hexdigest() == sha256`; raise `BlobHashMismatchError` on
   mismatch.
2. `prefix = sha256[:2]`; `rel_key = f"{prefix}/{sha256}.bin"`;
   `final_path = root / rel_key`.
3. If `final_path.exists()`: return `rel_key` immediately (no-op).
4. Create `root / prefix /` if needed (`mkdir(parents=True, exist_ok=True)`).
5. Create a `NamedTemporaryFile(dir=prefix_dir, delete=False, suffix=".tmp")` and write
   all data to it. Use `delete=False` so the file survives the `with` block; clean it
   up explicitly in a `finally` clause if the rename step fails.
6. `os.fsync(tmp_fd)` and close the file descriptor.
7. Rename using a **no-replace** mechanism:
   - POSIX: `os.link(tmp, final_path)` then `os.unlink(tmp)`. On `FileExistsError`
     (concurrent writer won), unlink the temp in `finally` and return `rel_key`.
   - Windows (dev only): `os.replace(tmp, final_path)` — not no-replace, documented
     limitation; production runs on Linux CI.
8. `fsync` the prefix directory fd to make the entry durable.
9. Return `rel_key`.

The `finally` clause ensures the temp file is always deleted on any error path, so no
orphaned `.tmp` files accumulate.

**`get(sha256) -> bytes`:** Read and return file contents; raise `BlobNotFoundError` if
missing.

---

## 7. `LiveHttpAdapter` (`adapters/http_adapter.py`)

Implements `FetchPort`. Uses **synchronous `httpx.Client`** with
`follow_redirects=False`. All I/O is blocking.

**Construction:**
```python
LiveHttpAdapter(
    settings: Settings,
    rate_limiter: RateLimiter,
    resolver: Callable[[str], list[str]] = _default_resolver,
    address_policy: Callable[[str], bool] = _default_address_policy,
)
```

`LiveHttpAdapter` creates its own `RobotsCache` internally during `__init__`, passing
`self._fetch_raw` as the fetch callable and `settings.robots_ttl_seconds` as the TTL.
There is no `robots_cache` constructor argument; the cache is an implementation detail
of the adapter, not an external dependency. This simplifies construction and avoids the
circular-reference problem of constructing a `RobotsCache` before the adapter exists.

`resolver` and `address_policy` are injectable callables with real defaults:
- `_default_resolver(hostname) -> list[str]`: calls `socket.getaddrinfo` and returns all
  resolved address strings.
- `_default_address_policy(addr_str) -> bool`: returns `True` (blocked) if
  `not ipaddress.ip_address(addr_str).is_global` or if the address is in
  `100.64.0.0/10` or `::ffff:0:0/96`.

Injecting these in tests allows SSRF cases and the local TLS test to supply controlled
address lists without network calls, while the production code uses the real defaults.

The `httpx.Client` is constructed with:
- `follow_redirects=False`
- `trust_env=False` (disables `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, system proxies)
- `timeout=httpx.Timeout(connect=settings.http_timeout, read=settings.http_timeout,
  write=settings.http_timeout, pool=settings.http_timeout)`

A **total per-request deadline** is tracked via a monotonic start time recorded at the
beginning of `fetch()`. It is checked in two places:
1. At the top of each redirect-loop iteration (between hops).
2. Inside the body-chunk read loop — after each chunk is appended, compare
   `time.monotonic() - start_time` against `settings.http_timeout`. If exceeded, stop
   reading, set `truncated = True`, and mark the response as a timeout sentinel.

This ensures a slow server that trickles the body also triggers the deadline.

**Manual redirect loop:**
`fetch()` implements its own redirect loop up to `settings.max_redirects`. On each hop:
1. Check total deadline; return `timeout` sentinel if exceeded.
2. Run `_check_ssrf(hostname)` using the injected `resolver` and `address_policy`.
   Returns the pinned IP string, or `None` → return `ssrf_blocked` sentinel immediately.
3. Check robots.txt for this host/path via the internal `RobotsCache`.
4. Call `rate_limiter.wait(hostname)`.
5. Issue the request with IP-pinning (see below).
6. Read body in chunks, checking the total deadline after each chunk (see above).
7. If response is 3xx: extract `Location`, validate scheme (must be `http`/`https`),
   loop. On cross-host redirect, check robots.txt for the new host before looping.
8. After `max_redirects` hops without a final response: return `redirect_limit` sentinel.

**SSRF guard (`_check_ssrf(hostname) -> str | None`):**
1. Addresses = `self._resolver(hostname)`. On DNS failure → return `None` (caller
   returns `network_error` sentinel; DNS failure does not map to `ssrf_blocked`).
2. For each address string `a`: blocked if `self._address_policy(a)` returns `True`.
3. If any address is blocked: return `None` (caller returns `ssrf_blocked` sentinel).
4. Otherwise: return the first non-blocked address as the pinned IP.

**IP-pinning with correct SNI and certificate verification:**
For each request, the adapter rewrites the URL to substitute the pinned IP as the host.
For IPv6 addresses, the IP is wrapped in brackets (e.g. `https://[::1]/path`). The
adapter sets:
- `Host: <original_hostname>` in the request headers.
- `extensions={"sni_hostname": original_hostname.encode()}` for TLS SNI.
- An `ssl.SSLContext` with `check_hostname=True` and `verify_mode=CERT_REQUIRED` that
  verifies against the original hostname.

TLS certificate validation uses the original hostname regardless of the IP the connection
goes to.

**Body cap applies to decompressed bytes:** The body is read in chunks after
decompression; the cap (`settings.max_body_bytes`) is compared against the cumulative
length of decompressed bytes, not the compressed content-length header.

**robots.txt fetching:**
- Fetched via `_fetch_raw(url)` — the same guarded, IP-pinned, rate-limited internal
  method used for resource fetches. The robots.txt fetch itself is subject to SSRF rules.
- Body capped at **500 KiB** (RFC 9309 §2.5 minimum parsing limit), independent of
  `max_body_bytes`.
- Parsed using **`protego`** (see ADR at `docs/adr/001-protego-robots-parser.md`).
  `protego` is used from the start: it handles both `*` path-prefix wildcards and `$`
  end-anchors, is RFC 9309-aware, and avoids the unguarded `.read()` method of
  `urllib.robotparser`. The fetched bytes are decoded as UTF-8 (`errors="replace"`) and
  passed to `protego.Protego.parse(content, url)`.
- RFC 9309 outcome table applied (REQ-05).

**Rate limiter:** `rate_limiter.wait(hostname)` is called before every outbound request
(resource and robots.txt fetches alike).

**Return value assembly:** Parses `Content-Type` header into `content_type` (bare MIME,
lowercased) and `charset` (parameter value, or `None`). Body decompressed and capped.
Populates all `RawCapture` fields including `requested_url`, `final_url` (URL of the last
hop), `fetched_at` (set before the first request).

---

## 8. Rate limiter and robots cache (`adapters/`)

### `RateLimiter` (`rate_limiter.py`)
Per-host minimum interval. State: `threading.Lock`-protected dict
`{hostname: last_request_time}`.

```python
def wait(self, hostname: str) -> None:
    with self._lock:
        last = self._times.get(hostname, 0.0)
        remaining = self._interval - (time.monotonic() - last)
        self._times[hostname] = time.monotonic() + max(remaining, 0.0)
    if remaining > 0:
        time.sleep(remaining)   # sleep OUTSIDE the lock
```

The lock is held only to read and update `_times`, not during the sleep. This prevents
one thread's sleep from blocking other threads that need to record their own timestamps.

### `RobotsCache` (`robots_cache.py`)

Takes a **fetch callable** at construction rather than a reference to the adapter:

```python
RobotsCache(
    fetch: Callable[[str], tuple[int, bytes]],  # (status_code, body)
    ttl_seconds: float,
)
```

This decouples `RobotsCache` from `LiveHttpAdapter` and makes it trivially testable with
a lambda. `LiveHttpAdapter.__init__` constructs the cache internally, passing
`self._fetch_raw`. There is no `robots_cache` argument on `LiveHttpAdapter`.

Robots.txt is parsed with **`protego`** (`protego.Protego.parse(content, url)`), which
handles both `*` wildcards and `$` end-anchors. See `docs/adr/001-protego-robots-parser.md`.

Per-process in-memory dict `{hostname: (Protego | None, expires_at)}`. `None` means
"fully disallowed" (5xx / unreachable). `is_allowed(hostname, path, user_agent)`:
- Cache hit and not expired → return cached result.
- Cache miss or expired → call `fetch(robots_url)`, apply RFC 9309 outcome table, cache
  and return.

---

## 9. `SqlCaptureRepository` and `SqlSourceRepository` (`db/repository.py`)

Both implement their respective `Protocol`. Both use **synchronous SQLAlchemy `Session`**
with psycopg (psycopg3 sync driver).

**Transaction ownership: each method opens its own `Session`, commits, and closes.**
No session is passed in. Both `insert` and `insert_in_new_transaction` are identical in
implementation at this stage; the distinction is in the interface for future policy
flexibility.

```python
class SqlCaptureRepository:
    def __init__(self, session_factory: sessionmaker) -> None: ...

    def insert(self, capture: CaptureRecord) -> None:
        with self._session_factory() as session:
            session.add(CaptureORM.from_record(capture))
            session.commit()

    def insert_in_new_transaction(self, capture: CaptureRecord) -> None:
        with self._session_factory() as session:
            session.add(CaptureORM.from_record(capture))
            session.commit()

class SqlSourceRepository:
    def get_or_create_by_url(self, url: str) -> Source:
        with self._session_factory() as session:
            obj = session.execute(select(SourceORM).where(SourceORM.url == url)).scalar()
            if obj is None:
                obj = SourceORM(url=url)
                session.add(obj)
                session.commit()
                session.refresh(obj)
            return obj.to_domain()

    def get(self, source_id: UUID) -> Source:
        with self._session_factory() as session:
            obj = session.get(SourceORM, source_id)
            if obj is None:
                raise SourceNotFoundError(source_id)
            return obj.to_domain()
```

---

## 10. Database schema

### Tables

**`source`**
```sql
CREATE TABLE source (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    url             TEXT NOT NULL,
    label           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    watch_schedule  TEXT
);
```

**`capture`**
```sql
CREATE TABLE capture (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id              UUID NOT NULL REFERENCES source(id) ON DELETE RESTRICT,
    requested_url          TEXT NOT NULL,
    final_url              TEXT NOT NULL,
    captured_at            TIMESTAMPTZ NOT NULL,
    adapter                TEXT NOT NULL,
    status_code            INTEGER NOT NULL,
    status                 TEXT NOT NULL,
    error_reason           TEXT,
    content_type           TEXT,
    raw_sha256             CHAR(64),
    normalized_sha256      CHAR(64),
    normalizer_version     TEXT,
    normalization_fallback BOOLEAN NOT NULL DEFAULT FALSE,
    truncated              BOOLEAN NOT NULL DEFAULT FALSE,
    storage_path           TEXT
);

CREATE INDEX ix_capture_source_normalized
    ON capture (source_id, normalized_sha256);
```

### Append-only triggers and privileges (REQ-20)

```sql
-- capture: block all UPDATE, DELETE, and TRUNCATE
CREATE OR REPLACE FUNCTION capture_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'capture rows are append-only';
END;
$$;
CREATE TRIGGER trg_capture_immutable
    BEFORE UPDATE OR DELETE ON capture
    FOR EACH ROW EXECUTE FUNCTION capture_immutable();
CREATE TRIGGER trg_capture_no_truncate
    BEFORE TRUNCATE ON capture
    EXECUTE FUNCTION capture_immutable();

REVOKE UPDATE, DELETE ON capture FROM seance_app;

-- source: block DELETE; block changes to id, url, created_at on UPDATE
CREATE OR REPLACE FUNCTION source_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'source rows cannot be deleted';
    END IF;
    -- Raise only if the value actually changed
    IF NEW.id          IS DISTINCT FROM OLD.id         OR
       NEW.url         IS DISTINCT FROM OLD.url        OR
       NEW.created_at  IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'source.id, url, and created_at are immutable';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_source_guard
    BEFORE UPDATE OR DELETE ON source
    FOR EACH ROW EXECUTE FUNCTION source_guard();

REVOKE DELETE ON source FROM seance_app;
-- INSERT and UPDATE (for label, watch_schedule) remain granted.
```

### Role creation

The initial Alembic migration creates the `seance_app` role (if it does not already
exist) and grants it `SELECT`, `INSERT`, `UPDATE` on `source` and `SELECT`, `INSERT` on
`capture`.

### Migrations

Alembic manages all schema changes. The initial migration creates both tables, all
indexes, all triggers, the role, and the privilege grants in a single revision.

---

## 11. Configuration (`settings.py`)

`Settings` is a Pydantic `BaseSettings` model in `seance/settings.py`. All values are
overridable via environment variables or a `.env` file.

| Setting | Default | Description |
|---------|---------|-------------|
| `SEANCE_CONTACT_URL` | *(required)* | Inserted into User-Agent header. |
| `SEANCE_BLOB_STORE_ROOT` | `./blobs` | Root directory for `FileBlobStore`. |
| `SEANCE_DATABASE_URL` | *(required)* | SQLAlchemy sync DSN for the app role (psycopg). |
| `SEANCE_MIGRATOR_DATABASE_URL` | *(required)* | DSN for the privileged migrator role (runs Alembic). |
| `SEANCE_APP_DB_PASSWORD` | *(required)* | Password set when the migration creates the `seance_app` role. |
| `SEANCE_HTTP_TIMEOUT` | `30` | Per-hop and total request timeout in seconds. |
| `SEANCE_MAX_BODY_BYTES` | `52428800` | 50 MB decompressed body size cap. |
| `SEANCE_MAX_REDIRECTS` | `10` | Maximum redirect hops. |
| `SEANCE_RATE_LIMIT_SECONDS` | `1.0` | Per-host minimum interval. |
| `SEANCE_ROBOTS_TTL_SECONDS` | `86400` | robots.txt cache TTL (24 h). |
| `SEANCE_DENYLIST_PATH` | `None` | Override path for denylist YAML. When `None`, the bundled `seance/resources/denylist.yaml` is loaded via `importlib.resources`. |

---

## 12. Dependency wiring

Application startup (CLI `main()` or future worker) wires all dependencies once:

```python
from seance.settings import Settings
from seance.adapters.denylist_loader import load_denylist
from seance.adapters.file_blob_store import FileBlobStore
from seance.adapters.rate_limiter import RateLimiter
from seance.adapters.http_adapter import LiveHttpAdapter
from seance.db.repository import SqlCaptureRepository, SqlSourceRepository
from seance.core.normalizer import Normalizer
from seance.core.capture_service import CaptureService
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

settings     = Settings()
denylist     = load_denylist(settings.denylist_path)
normalizer   = Normalizer(denylist)
blob_store   = FileBlobStore(settings.blob_store_root)
rate_lim     = RateLimiter(settings.rate_limit_seconds)
# RobotsCache is created internally by LiveHttpAdapter; not constructed here.
http_adap    = LiveHttpAdapter(settings, rate_lim)
engine       = create_engine(settings.database_url)
session_fac  = sessionmaker(engine)
repo         = SqlCaptureRepository(session_fac)
source_repo  = SqlSourceRepository(session_fac)
service      = CaptureService(http_adap, blob_store, repo, normalizer)
```

For tests, any component is replaced with a fake implementing the same `Protocol`.

---

## 13. Error taxonomy

| Exception | Raised by | Meaning |
|-----------|-----------|---------|
| `BlobHashMismatchError` | `FileBlobStore.put` | Caller passed wrong hash for data |
| `BlobNotFoundError` | `FileBlobStore.get` | Hash not in store |
| `SourceNotFoundError` | `SqlSourceRepository.get` | No Source for given id |
| `SSRFBlockedError` | (internal to adapter) | Converted to `RawCapture` sentinel |
| `InvalidURLError` | (internal to adapter) | Converted to `RawCapture` sentinel |

`SSRFBlockedError` and `InvalidURLError` never propagate past `LiveHttpAdapter.fetch`.
They are defined in `core/exceptions.py` for use in adapter unit tests.

---

## 14. Test strategy

| Layer | Tool | Properties / cases covered |
|-------|------|---------------------------|
| `core/hasher.py` | pytest + Hypothesis | P1: output matches `hashlib.sha256` for any bytes |
| `core/normalizer.py` | pytest + Hypothesis | P4 (structured HTML generator via `hypothesis.strategies.builds`), P5 (JSON key permutations), P6 (arbitrary bytes never raise) |
| `core/capture_service.py` | pytest (in-memory fakes for all ports) | P3 (two calls → two rows, no new blobs second time); REQ-17 flow; `_map_status` unit tests; blob-failure path; unhandled-exception path; error-row insert failure |
| `adapters/file_blob_store.py` | pytest + Hypothesis + `tmp_path` | P2, P7, P8, P9, P10 (concurrent threads) |
| `adapters/http_adapter.py` | pytest + `respx` + injectable `resolver`/`address_policy` + `trustme`/`pytest-httpserver` | SSRF guard (all blocked ranges via injected policy), mid-redirect SSRF block, DNS failure, robots.txt outcomes (protego), redirect loop, body-chunk deadline check, rate limiting, TLS cert verification with IP-pinned connection |
| `db/repository.py` | pytest + real Postgres test DB | `insert` / `insert_in_new_transaction` isolation; trigger enforcement (UPDATE/DELETE/TRUNCATE on `capture`; immutable-field UPDATE on `source`); FK restrict on `source` delete |

---

## 15. ADR: robots.txt parser choice

> See `docs/adr/001-protego-robots-parser.md` (created by TASK-06).

**Decision:** use `protego` for robots.txt parsing from the start.

**Reasons:**
- `urllib.robotparser.RobotFileParser.read()` performs its own unguarded HTTP request,
  bypassing SSRF controls. Using `.parse()` instead avoids this, but `urllib.robotparser`
  still does not support the `$` end-anchor used by many real-world robots.txt files.
- `protego` (the parser used by Scrapy, based on Google's spec) supports both `*`
  wildcards and `$` end-anchors, is actively maintained, and accepts the content string
  directly, requiring no workarounds.
- The added dependency (`protego`) is small and has no transitive C dependencies.

**Rejected alternative:** `urllib.robotparser` with `.parse()` — insufficient wildcard
support; would require a future migration when `$` anchors are encountered in production.
