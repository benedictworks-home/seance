# Spec: capture-and-snapshot — Tasks

> Requirements: requirements.md (APPROVED)
> Design: design.md (DRAFT)

Tasks are ordered so the first group produces a **thin end-to-end vertical slice**: a real
URL can be fetched, hashed, stored as a blob, and recorded as a `Capture` row via a single
runnable entry point. Hardening tasks (SSRF, robots.txt, database enforcement) follow once
the slice works.

Everything is synchronous. There is no `asyncio` in this spec.

---

## Group 1 — Vertical slice (do these first, in order)

- [ ] **TASK-01 — Project scaffold, settings, and CI**

  Create the package structure `backend/src/seance/{core,adapters,db,resources}/` per
  design §1. Add `pyproject.toml` configured so `pip install -e .` installs the package
  in editable mode (using `[project]` with `packages = [{include = "seance", from = "src"}]`
  or the equivalent for the chosen build backend). Add an empty `__main__.py` that
  imports and calls `cli.main()`.

  Add the following dependencies to `pyproject.toml`:

  *Runtime:* `httpx`, `beautifulsoup4`, `sqlalchemy`, `psycopg[binary]`, `alembic`,
  `pydantic-settings`, `pyyaml`, `brotli`, `protego`.

  *Dev/test:* `hypothesis`, `pytest`, `respx`, `trustme`, `pytest-httpserver`.

  Implement `seance/settings.py` with all `Settings` fields from design §11, including
  `SEANCE_DATABASE_URL`, `SEANCE_MIGRATOR_DATABASE_URL`, `SEANCE_APP_DB_PASSWORD`, and
  `SEANCE_DENYLIST_PATH` (optional override; `None` loads the bundled default via
  `importlib.resources`). Write a smoke test that loads `Settings` from environment
  variables and asserts that required fields are present.

  Add a **GitHub Actions workflow** at `.github/workflows/ci.yml` that:
  - runs on push and pull request to `main`,
  - sets up Python 3.12,
  - installs the package in editable mode (`pip install -e ".[dev]"`),
  - starts a Postgres service container,
  - runs `pytest --hypothesis-seed=0`.

  Add a **Kiro hook** at `.kiro/hooks/on-save.yaml` that runs `pytest` on any
  `backend/**/*.py` file save, so test feedback is immediate during development.

- [ ] **TASK-02 — Domain objects**

  Implement `seance/core/domain.py`: frozen dataclasses for `Source`, `RawCapture`,
  `NormalizerResult`, `CaptureRecord`. Implement `seance/core/exceptions.py`:
  `BlobNotFoundError`, `BlobHashMismatchError`, `SourceNotFoundError`,
  `SSRFBlockedError`, `InvalidURLError`. No logic; no imports from outside `core/`.

  Add unit tests: construct each object, assert frozen (attempting `setattr` raises
  `FrozenInstanceError`).

- [ ] **TASK-03 — Ports**

  Implement `seance/core/ports.py`: `FetchPort`, `BlobStore`, `CaptureRepository`,
  `SourceRepository` as `typing.Protocol` classes with method signatures matching design
  §3. Include the docstring on `BlobStore.put` explaining the return value and the
  docstrings on both `CaptureRepository` methods explaining session ownership.

  Add a test confirming that an in-memory fake implementing each method satisfies
  `isinstance(fake, TheProtocol)` via structural subtyping (protocols decorated with
  `@runtime_checkable`).

- [ ] **TASK-04 — SHA-256 hasher**

  Implement `seance/core/hasher.py`: `sha256_hex(data: bytes) -> str` using
  `hashlib.sha256`, returning a lowercase 64-character hex string.

  Add property test P1: for any `bytes` value `b`,
  `sha256_hex(b) == hashlib.sha256(b).hexdigest()`.

- [ ] **TASK-05 — FileBlobStore**

  Implement `seance/adapters/file_blob_store.py` per design §6. Key points:
  - `put` verifies the hash before writing; raises `BlobHashMismatchError` on mismatch.
  - `put` returns the relative storage key `"{prefix}/{sha256}.bin"`.
  - Atomic write: `NamedTemporaryFile(delete=False)` in prefix dir → `fsync` →
    `os.link` rename (POSIX) → `fsync` dir. The temp file is deleted in a `finally`
    clause on any error path so no orphaned `.tmp` files accumulate.
  - On `FileExistsError` from `os.link` (concurrent writer won), delete temp in
    `finally` and return `rel_key`.
  - `get` raises `BlobNotFoundError` for a missing hash.

  Add file-level property tests using `tmp_path`:
  - **P2** — idempotent: two `put` calls with same `(h, data)` → one file, same key.
  - **P7** — round-trip: `get(sha256(b)) == b`.
  - **P8** — content-addressing: `sha256(get(sha256(b))) == sha256(b)`.
  - **P9** — mismatched hash: `put(wrong_hash, data)` raises, writes no file.
  - **P10** — concurrent: two threads calling `put` for same `(h, data)` simultaneously
    leave exactly one correct file (no partial writes, no errors after the race).

- [ ] **TASK-06 — Denylist loader and Normalizer**

  Create `seance/resources/denylist.yaml` with an initial set of ad, analytics, and
  tracking patterns (e.g. elements with `id` or `class` matching `ad`, `ads`,
  `advertisement`, `google-analytics`, `gtm`, `tracking`, `banner`). Include a `version`
  field (e.g. `"1.0"`).

  Implement `seance/adapters/denylist_loader.py`: `load_denylist(path: Path | None) -> Denylist`.
  When `path` is `None`, load the bundled `seance/resources/denylist.yaml` via
  `importlib.resources.files("seance.resources").joinpath("denylist.yaml").read_text()`.
  When `path` is provided, read that file instead. Both paths parse YAML using `pyyaml`
  and construct `Denylist` / `DenyPattern` dataclasses from `seance/core/normalizer.py`.

  Implement `seance/core/normalizer.py`: `DenyPattern`, `Denylist`, `Normalizer` per
  design §5. Constructor takes a `Denylist`; `normalize()` performs no I/O and never
  raises.

  Add tests:
  - **P4** — idempotency over structured HTML: generate `RawCapture` objects using a
    Hypothesis strategy built with `hypothesis.strategies.builds` that assembles
    `<html>`, `<head>`, and `<body>` elements containing text, links, and paragraphs
    (not `text()` filtered by regex). Assert
    `normalize(wrap(normalize(r).normalized_body, "text/html", None)).normalized_body
    == normalize(r).normalized_body`.
  - **P5** — JSON key order: for any `dict` generated by Hypothesis, any permutation of
    top-level keys produces the same `normalized_sha256`.
  - **P6** — never raises: for any `bytes` and any `content_type` string,
    `normalize(...)` returns a `NormalizerResult` without raising.
  - Unit test: loading `seance/resources/denylist.yaml` and applying it to a fixture
    HTML document removes the expected elements without altering unrelated content.

- [ ] **TASK-07 — Minimal HTTP adapter (happy path only)**

  Implement `seance/adapters/http_adapter.py` for the happy path: synchronous
  `httpx.Client` with `follow_redirects=False` and `trust_env=False`, a manual redirect
  loop up to `max_redirects`, a total per-request deadline checked on each loop
  iteration, body decompression (gzip, deflate, brotli — the decompressed byte count is
  compared against `max_body_bytes`, not the content-length header), body-size cap with
  `truncated` flag, `Content-Type` parsing into `content_type` + `charset`, ASCII
  User-Agent from `Settings.contact_url`.

  Constructor signature per design §7:
  ```python
  LiveHttpAdapter(
      settings,
      rate_limiter,
      resolver=_default_resolver,
      address_policy=_default_address_policy,
  )
  ```
  `RobotsCache` is created internally by `LiveHttpAdapter.__init__`; there is no
  `robots_cache` constructor argument. No SSRF guard, no robots.txt, no rate limiting
  yet — `resolver` and `address_policy` are present in the signature but not called
  until TASK-11; robots cache is created but not invoked until TASK-12.

  Test with `respx` mocks:
  - 200 response with HTML body → correct `RawCapture` fields.
  - 204 No Content → `raw_body = b""`, `truncated = False`, `status_code = 204`.
  - Redirect chain (301 → 200) → `requested_url` ≠ `final_url`.
  - Redirect limit exceeded → `status_code = 0`, `error_reason = "redirect_limit"`.
  - Total deadline exceeded → `status_code = 0`, `error_reason = "timeout"`.
  - Network error → `status_code = 0`, `error_reason = "network_error"`.
  - Decompressed body exceeding limit → `truncated = True`, body capped.
  - Deadline exceeded mid-body (slow trickle simulated) → `status_code = 0`,
    `error_reason = "timeout"`, partial body discarded.
  - Brotli-compressed body → correct decompressed `raw_body`.

- [ ] **TASK-08 — Database schema, repository, and SourceRepository**

  Write `seance/db/schema.sql` with both tables and indexes (no triggers yet — those
  come in TASK-13). Write the initial Alembic migration (using
  `SEANCE_MIGRATOR_DATABASE_URL`) that creates both tables, the index, and the
  `seance_app` role (password from `settings.app_db_password`) with `SELECT`, `INSERT`,
  `UPDATE` on `source` and `SELECT`, `INSERT` on `capture`.

  Implement `seance/db/models.py`: `SourceORM` and `CaptureORM` SQLAlchemy ORM models
  with `to_domain()` and `from_record()` / `from_domain()` methods.

  Implement `seance/db/repository.py`:
  - `SqlCaptureRepository`: `insert` and `insert_in_new_transaction`, each opening their
    own synchronous `Session`, committing, and closing (design §9).
  - `SqlSourceRepository`: `get_or_create_by_url` and `get`.

  **Test against a real Postgres database only** (no SQLite). Use a pytest fixture that
  runs the Alembic migration against a test Postgres instance and tears it down after.
  Tests:
  - `insert` twice → two rows with different `id` values.
  - `insert_in_new_transaction`: confirm the row is committed even when simulated in
    isolation (the method always opens its own session; there is no outer session to roll
    back against at this stage — that isolation is verified again in TASK-09).
  - `get_or_create_by_url`: called twice with the same URL → returns the same `Source`
    id both times, with one row in the table.
  - `get`: raises `SourceNotFoundError` for an unknown id.

- [ ] **TASK-09 — CaptureService**

  Implement `seance/core/capture_service.py`: `CaptureService`, `_map_status`. Wire
  REQ-17 steps using injected in-memory fakes for all ports.

  Implement the `raw = None` / `error_recorded` pattern from design §4:
  - `raw` is set to `None` before the `try`; the `except` uses it to decide whether to
    build an error record from `raw.status_code` (blob/DB failures) or from fallbacks
    (unexpected pre-fetch exception).
  - `error_recorded = True` is set before calling `_record_error` in step 3, so if that
    call raises, the outer `except` does not attempt a second `_record_error`.

  Add unit tests (all use fakes):
  - **P3** — two `service.capture(source)` calls for identical content: second call
    produces a second row in the fake `CaptureRepository` but no new entry in the fake
    `BlobStore` (the hash already exists).
  - Happy path: 200 response → `ok` row, `storage_path` equals the key returned by the
    fake `BlobStore.put`.
  - Non-ok fetch (404, timeout, robots sentinel) → error row with correct `status`,
    no blob written.
  - Blob write failure at step 7 → `internal_error` row with the real `status_code`
    (200) from the `RawCapture`; no `ok` row inserted.
  - Blob write failure at step 8 → same as above.
  - `repo.insert` failure at step 9 → caught by `except Exception`; `internal_error`
    row recorded via `insert_in_new_transaction`; `capture()` returns the error record.
  - Unhandled exception during fetch (fake raises) → `internal_error` row with fallback
    fields (`source.url`, current time, `"unknown"` adapter); `capture()` returns the
    error record.
  - Error-row insert failure (`insert_in_new_transaction` raises) → logs CRITICAL,
    re-raises the **original** exception.
  - `_map_status` unit tests: one test per row in the REQ-14 table.

- [ ] **TASK-10 — Runnable entry point (vertical slice complete)**

  Add `seance/cli.py` with a `main()` function and the command
  `python -m seance capture <url>` (via `seance/__main__.py`) that:
  1. Loads `Settings`.
  2. Runs the Alembic migration (using `SEANCE_MIGRATOR_DATABASE_URL`) if the schema is
     not yet current.
  3. Calls `source_repo.get_or_create_by_url(url)` to obtain or create the `Source` row.
  4. Wires all remaining dependencies per design §12.
  5. Calls `service.capture(source)`.
  6. Prints the resulting `CaptureRecord` as pretty-printed JSON.

  Manual smoke test: `python -m seance capture https://example.com` produces a
  `Capture` row in Postgres and a blob file at `blobs/<prefix>/<hash>.bin`.

---

## Group 2 — Hardening (after the slice passes)

- [ ] **TASK-11 — SSRF protection**

  Extend `LiveHttpAdapter` with the full SSRF guard from design §7:
  - `_check_ssrf(hostname)` calls the injected `resolver` and `address_policy`.
  - Connect to the pinned IP; for IPv6 addresses, bracket the IP in the rewritten URL.
  - Set `Host` header and `sni_hostname` extension; verify TLS against original hostname.
  - Re-check on every redirect hop.
  - `trust_env=False` already set in TASK-07.

  Tests using the injectable `resolver` and `address_policy` callables:
  - Each blocked IP category → `ssrf_blocked` sentinel (inject a policy that blocks the
    test address; no real DNS resolution needed).
  - Mid-redirect to blocked IP → `ssrf_blocked`.
  - DNS failure (inject a resolver that raises) → `network_error`.
  - Local TLS server test (`trustme` + `pytest-httpserver`): inject a resolver returning
    `["127.0.0.1"]` and a permissive policy; confirm a certificate issued for the
    hostname is accepted when connecting to `127.0.0.1`; confirm a mismatched cert is
    rejected.

- [ ] **TASK-12 — robots.txt and rate limiting**

  Implement `seance/adapters/rate_limiter.py`: `RateLimiter` per design §8. The
  `threading.Lock` is held only to read and update timestamps; `time.sleep` is called
  **outside** the lock.

  Implement `seance/adapters/robots_cache.py`: `RobotsCache(fetch, ttl_seconds)` per
  design §8. `fetch` is a callable `(url: str) -> tuple[int, bytes]`. Parses with
  **`protego`** (`protego.Protego.parse(content, url)`). robots.txt body capped at
  500 KiB before parsing. Create `docs/adr/001-protego-robots-parser.md` documenting
  the decision (see design §15).

  Wire both into `LiveHttpAdapter.fetch`: robots.txt check on initial host and on each
  cross-host redirect; rate limit on every outbound request. The `RobotsCache` is
  already created internally by `LiveHttpAdapter`; TASK-12 activates the calls to it.

  Tests:
  - `Disallow: /` → `error_reason = "robots_forbidden"`.
  - 5xx robots.txt response → `error_reason = "robots_unavailable"`.
  - 4xx robots.txt response → fetch proceeds normally.
  - Cache hit → `fetch` callable not called on second request to same host.
  - Cross-host redirect → robots.txt checked for new host before following.
  - Rate limiter: `time.sleep` called outside the lock (verify via mock).
  - robots.txt with `*` wildcard path → parsed correctly by protego.
  - robots.txt with `$` end-anchor → parsed correctly by protego.

- [ ] **TASK-13 — Database append-only enforcement**

  Add a new Alembic migration (using `SEANCE_MIGRATOR_DATABASE_URL`) that installs:
  - `BEFORE UPDATE OR DELETE` trigger on `capture` (raises for any row-level change).
  - `BEFORE TRUNCATE` trigger on `capture` (raises).
  - `BEFORE UPDATE OR DELETE` trigger on `source` (guards `id`, `url`, `created_at`
    using `IS DISTINCT FROM`; blocks all deletes).
  - Revoke `UPDATE`, `DELETE` on `capture` from `seance_app`.
  - Revoke `DELETE` on `source` from `seance_app`.
  - Confirm `ON DELETE RESTRICT` FK is present (from TASK-08 migration).

  Tests against real Postgres:
  - Direct `UPDATE` on `capture` raises exception.
  - `DELETE` on `capture` raises.
  - `TRUNCATE capture` raises.
  - `UPDATE source SET label = 'x'` succeeds.
  - `UPDATE source SET url = 'x'` raises.
  - `UPDATE source SET url = source.url` (same value) succeeds (`IS DISTINCT FROM`).
  - `DELETE` on `source` with no captures raises (trigger).
  - `DELETE` on `source` with captures raises (FK restrict).

- [ ] **TASK-14 — Property test suite completion**

  Run the full test suite with `pytest --hypothesis-seed=0` and confirm all P1–P10 pass.
  Fix any flakiness found by the seed. Ensure the GitHub Actions CI workflow (TASK-01)
  goes green end-to-end.

- [ ] **TASK-15 — Integration smoke test**

  Add a pytest integration test (marked `@pytest.mark.integration`, skipped unless
  `SEANCE_INTEGRATION=1`) that runs the full pipeline against `https://example.com`:
  - Creates a `Source` row via `source_repo.get_or_create_by_url`.
  - Calls `service.capture(source)`.
  - Asserts `status = "ok"`, `truncated = False`, the blob file exists at
    `settings.blob_store_root / record.storage_path`, and
    `sha256_hex(blob_file.read_bytes()) == record.normalized_sha256`.

  This test requires a live network connection and is excluded from normal CI runs.
