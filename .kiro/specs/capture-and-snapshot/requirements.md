# Spec: capture-and-snapshot — Requirements

> Status: **APPROVED — design and tasks may now be written.**

---

## Scope

This spec covers the foundational capture pipeline:

- The `RawCapture` value object returned by any fetch adapter
- The live HTTP fetch adapter (`LiveHttpAdapter`)
- The normalizer (strips noise so diffs reflect only real content changes)
- SHA-256 hashing of both raw and normalized content
- The blob store (content-addressed, one file per unique hash)
- The `Capture` record (append-only, one row per fetch event)
- Idempotency rules: same content → one blob, but always a new `Capture` row
- The `Source` domain object, `CaptureRepository` port, and `SourceRepository` port
- The `Source` and `Capture` database models
- SSRF protection, redirect limits, body-size cap, timeouts
- Atomic writes, error capture rows, and robots.txt caching

It does not cover the diff engine, the web-archive adapter, the API layer, or the frontend.
Those are separate specs.

---

## Requirements

Requirements use EARS notation:
- **Ubiquitous:** `The <system> shall <action>.`
- **Event-driven:** `When <trigger>, the <system> shall <action>.`
- **Unwanted behaviour:** `If <condition>, the <system> shall <action>.`
- **State-driven:** `While <state>, the <system> shall <action>.`
- **Optional feature:** `Where <feature>, the <system> shall <action>.`

---

### REQ-01 — RawCapture value object

The system shall define a `RawCapture` value object with the following fields:

| Field            | Type             | Description |
|------------------|------------------|-------------|
| `requested_url`  | `str`            | The URL as submitted by the caller, before any redirects. |
| `final_url`      | `str`            | The URL actually fetched after all redirects; equals `requested_url` if no redirect occurred. |
| `fetched_at`     | `datetime` (UTC) | Timestamp set by the adapter immediately before issuing the network request. |
| `status_code`    | `int`            | HTTP status code of the final response; `0` for timeout/network/redirect-limit errors; `-1` for adapter-level blocks (robots-forbidden, SSRF, invalid URL). |
| `error_reason`   | `str \| None`    | Machine-readable reason string for adapter-level blocks and errors; `None` for plain HTTP responses. See REQ-04 and REQ-05 for defined values. |
| `content_type`   | `str`            | The bare MIME type from the final response `Content-Type` header (e.g. `"text/html"`), excluding parameters; empty string if the header is absent. |
| `charset`        | `str \| None`    | The `charset` parameter extracted from the `Content-Type` header (e.g. `"utf-8"`); `None` if the parameter is absent. Stored separately so REQ-07 can decode without re-parsing. |
| `raw_body`       | `bytes`          | The decompressed response body (transfer-encoding such as gzip or Brotli is decoded before storage); empty bytes for non-ok outcomes. |
| `truncated`      | `bool`           | `True` if `raw_body` was cut at the body-size limit; `False` otherwise. |
| `adapter`        | `str`            | Identifier of the adapter (e.g. `"live_http"`). |

The `RawCapture` type shall be immutable after construction (a frozen dataclass or equivalent).

---

### REQ-02 — Source domain object and FetchPort interface

The system shall define a `Source` **domain object** (a plain Python dataclass or named
tuple, independent of any ORM model) with the following fields:

| Field            | Type           | Description |
|------------------|----------------|-------------|
| `id`             | UUID           | Stable identifier. |
| `url`            | `str`          | The URL being tracked. |
| `label`          | `str \| None`  | Optional human-readable name. |
| `watch_schedule` | `str \| None`  | Cron expression or interval string. |

The system shall define a `FetchPort` interface (abstract base class or `Protocol`) with a
single method:

```
fetch(source: Source) -> RawCapture
```

`FetchPort` accepts the **domain** `Source` object, not an ORM model. All fetch adapters
shall implement `FetchPort`. The core domain shall depend only on `FetchPort`, never on a
concrete adapter class. Adapters shall always return a `RawCapture`; they shall never raise
exceptions to signal fetch outcomes (see REQ-03, REQ-04, REQ-05).

---

### REQ-03 — Live HTTP adapter — basic fetch

When `LiveHttpAdapter.fetch(source)` is called, the system shall perform an HTTP GET request
to `source.url` and return a `RawCapture` populated from the response.

**User-Agent:** The live HTTP adapter shall set a `User-Agent` header whose value is pure
ASCII and matches the form `Seance/0.1 (+<contact-url>)`, where `<contact-url>` is a
**configurable setting** (e.g. `SEANCE_CONTACT_URL`) with no hardcoded default hostname.
An example value when the setting is `https://example.com/seance`:
`Seance/0.1 (+https://example.com/seance)`. The product token used in `robots.txt` matching
shall be `Seance` (ASCII, no accent; RFC 9309 §2.2.1 requires the product token to be a
substring of the User-Agent header value).

The adapter shall decompress the response body before populating `raw_body`, handling at
minimum `gzip`, `deflate`, and `br` (Brotli) transfer encodings.

The live HTTP adapter shall enforce a configurable default request timeout. Where no explicit
timeout is configured, the default shall be **30 seconds**. If the request exceeds the
timeout, the adapter shall return a `RawCapture` with `status_code = 0`,
`error_reason = "timeout"`, and `raw_body = b""`.

If the HTTP request raises a network-level error (DNS failure, connection refused, etc.), the
adapter shall return a `RawCapture` with `status_code = 0`, `error_reason = "network_error"`,
and `raw_body = b""`.

The live HTTP adapter shall not follow more than **10 redirects**. If the redirect limit is
exceeded, the adapter shall return a `RawCapture` with `status_code = 0`,
`error_reason = "redirect_limit"`, and `raw_body = b""`.

The live HTTP adapter shall enforce a configurable maximum response body size. Where no
explicit limit is configured, the default shall be **50 MB**. If the response body exceeds
this limit, the adapter shall truncate `raw_body` at the limit and set `truncated = True`.

**204 and bodyless 2xx responses:** When the server returns a 2xx status code with no body
(e.g. HTTP 204 No Content, or a 200 with `Content-Length: 0`), the adapter shall return
a `RawCapture` with `status_code` set to the actual code, `raw_body = b""`, and
`truncated = False`. The capture service shall treat this as a valid `ok` capture; the empty
body is a legitimate representation of the resource at that moment.

---

### REQ-04 — Live HTTP adapter — SSRF protection

The live HTTP adapter shall reject any URL whose scheme is not `http` or `https` without
making a network request. For such URLs it shall return a `RawCapture` with
`status_code = -1` and `error_reason = "invalid_url"`.

The live HTTP adapter shall disable environment-level proxy settings (e.g. `HTTP_PROXY`,
`HTTPS_PROXY`, `ALL_PROXY` environment variables and any system proxy configuration).
Connections shall be made directly; routing through a proxy would bypass SSRF controls.

Before opening a connection, the live HTTP adapter shall resolve the target hostname to
**all** of its IP addresses. If **any** resolved address is blocked (see rule below), the
adapter shall return `status_code = -1`, `error_reason = "ssrf_blocked"` without opening a
connection. If DNS resolution itself fails, the adapter shall return `status_code = 0`,
`error_reason = "network_error"`.

**Block rule:** An address is blocked if `ipaddress.ip_address(addr).is_global` is `False`.
This covers loopback, private, link-local, unspecified, and multicast ranges as defined by
the Python `ipaddress` module. In addition, the following ranges shall be blocked explicitly
even if a future Python version changes their `is_global` classification:

- `100.64.0.0/10` (shared address space, RFC 6598)
- `::ffff:0:0/96` (IPv4-mapped IPv6)

The adapter shall connect to one of the resolved IP addresses directly, **bypassing any
further DNS resolution**, to prevent DNS-rebinding attacks. For HTTPS connections, the
adapter shall still use the **original hostname** for TLS SNI and the `Host` header, and
shall verify the TLS certificate against the original hostname, not the IP address.

On every redirect hop, the adapter shall re-resolve the redirect target's hostname and
re-apply the same block rule to all resolved addresses before following the redirect.

Robots.txt fetches (REQ-05) shall be subject to the same SSRF rules, redirect rules, and
redirect-hop re-checks as any other request.

---

### REQ-05 — Live HTTP adapter — robots.txt

Before fetching the resource URL, the live HTTP adapter shall fetch and parse `robots.txt`
for the **initial** target host. If a redirect crosses to a different host at any point, the
adapter shall fetch and check `robots.txt` for the new host before following the redirect.

**robots.txt fetch outcomes** (per RFC 9309 §§2.3.1.3–2.3.1.4):

| robots.txt fetch result | Behaviour | `error_reason` if blocked |
|-------------------------|-----------|---------------------------|
| 2xx — file retrieved | Parse and enforce the rules. | — |
| 4xx — file unavailable | Treat the host as fully permitted (RFC 9309 §2.3.1.3). | — |
| 5xx — server error | Treat the host as fully disallowed (RFC 9309 §2.3.1.4). | `"robots_unavailable"` |
| Network error or timeout | Treat the host as fully disallowed (equivalent to unreachable). | `"robots_unavailable"` |

If the host is disallowed because `robots.txt` **explicitly forbids** the path, the adapter
shall return `status_code = -1`, `error_reason = "robots_forbidden"`.

If the host is disallowed because `robots.txt` is **unreachable or a server error**
(5xx / network failure), the adapter shall return `status_code = -1`,
`error_reason = "robots_unavailable"`.

In both cases the adapter shall not raise an exception. The capture service maps both
`error_reason` values to `status = "robots_forbidden"` per REQ-14.

The adapter shall cache parsed `robots.txt` results per host for a configurable TTL
(default: **24 hours**, per RFC 9309 §2.4). The cache is **per-process in-memory** and is
not shared across worker processes or persisted to disk.

The live HTTP adapter shall enforce a configurable per-host minimum interval between
consecutive outbound requests to that host (default: **1 second**). This rate limit is
**per-process**. When a request is made before the interval has elapsed, the adapter shall
wait until the interval has passed.

---

### REQ-06 — Normalizer result and interface

The system shall define a `NormalizerResult` value object with the following fields:

| Field                    | Type    | Description |
|--------------------------|---------|-------------|
| `normalized_body`        | `bytes` | The normalized content bytes. |
| `normalizer_version`     | `str`   | Version string identifying the exact rules applied (e.g. `"1.0"`). |
| `normalization_fallback` | `bool`  | `True` if the normalizer fell back to raw bytes due to a processing failure or unsupported input; `False` otherwise. |

The system shall define a `Normalizer` that is **constructed with its denylist injected**:

```python
class Normalizer:
    def __init__(self, denylist: Denylist) -> None: ...
    def normalize(self, raw: RawCapture) -> NormalizerResult: ...
```

The denylist is loaded once at construction time (by the caller, e.g. from a configuration
file). The `normalize()` method performs **no I/O** of any kind; it reads only from the
injected denylist and the `RawCapture` argument.

The normalizer shall be pure and stateless: given the same `RawCapture` and version it shall
always produce the same `NormalizerResult` (no randomness, no I/O, no side effects).

The normalizer shall **never raise an exception** on arbitrary byte input. If processing
fails at any stage, the normalizer shall catch the exception, set
`normalization_fallback = True`, populate `normalized_body` with `RawCapture.raw_body`
unchanged, log the exception with full context (content type, body length, exception type
and message), and return the result. A normalization failure does not prevent a `Capture`
row from being recorded (REQ-17 step 5 continues); the row will carry
`normalization_fallback = True`.

---

### REQ-07 — HTML normalization

The normalizer shall apply HTML normalization to any `RawCapture` whose `content_type` is
`text/html`. The HTML parser used shall be **BeautifulSoup with the `html.parser` backend**
(Python standard library). The `normalizer_version` shall include the BeautifulSoup library
version and the denylist version identifier so that changes to either invalidate the version
string.

HTML normalization steps, applied in order:

1. **Decode** the byte string to Unicode. Priority order for encoding detection:
   a. The `charset` field on `RawCapture` (from the `Content-Type` header).
   b. A `<meta charset="...">` or `<meta http-equiv="Content-Type" content="...charset=...">` tag
      found in the first 1024 bytes of the body.
   c. Fall back to UTF-8.
   If decoding with the detected encoding raises an error, fall back to UTF-8 with
   `errors="replace"`. (This is the only source of `RawCapture.charset`; REQ-01 ensures
   the adapter populates it from the response header, resolving the REQ-01/REQ-07 split.)

2. **Remove** all `<script>` tags and their full contents.

3. **Remove** all `<style>` tags and their full contents.

4. **Remove** HTML comments.

5. **Remove** elements and attributes matching patterns in a **configurable, versioned
   denylist** loaded from an external configuration file. The denylist targets common ad,
   analytics, and tracking patterns (e.g. elements whose `id` or `class` matches a listed
   pattern). The denylist file carries its own version identifier, which is incorporated
   into `normalizer_version`.

6. **Strip or canonicalize** volatile server-injected timestamps (e.g. HTML comments or
   `<meta>` attributes containing ISO-8601 date strings or bare Unix epoch integers).

7. **Rewrite charset declarations**: replace or remove every `<meta charset>` and
   `<meta http-equiv="Content-Type">` tag so the output declares UTF-8. This ensures that
   running the normalizer on its own output — which is now valid UTF-8 HTML — produces an
   identical result, satisfying property P4.

8. **Re-encode** the result as UTF-8 bytes using BeautifulSoup's serializer.

---

### REQ-08 — JSON and structured normalization

The normalizer shall detect content type by **lowercasing `RawCapture.content_type` before
all matching**. For example, `"Text/HTML"`, `"text/html"`, and `"TEXT/HTML"` are all treated
identically.

The normalizer shall detect JSON content by checking whether the lowercased `content_type`:
- equals `application/json`, or
- matches the pattern `application/*+json` (e.g. `application/geo+json`,
  `application/ld+json`).

For detected JSON content, the normalizer shall:

1. Decode the body. If `RawCapture.charset` is set, use that encoding; otherwise try UTF-8;
   if that fails, try UTF-16 (which JSON files sometimes use); if that also fails, set
   `normalization_fallback = True` and return the raw bytes unchanged.
2. Parse the decoded string as JSON using Python's `json.loads`. Reject `NaN` and
   `Infinity` values: if the parsed document contains IEEE 754 non-finite floats (which
   standard JSON does not permit), set `normalization_fallback = True` and return the raw
   bytes unchanged, logging a warning.
3. Re-serialize with `json.dumps(obj, sort_keys=True, separators=(',', ':'),
   ensure_ascii=False)` and encode the result as UTF-8.

The normalizer shall detect XML content by checking whether the lowercased `content_type`:
- equals `text/xml` or `application/xml`, or
- matches the pattern `application/*+xml` (e.g. `application/atom+xml`,
  `application/rss+xml`).

For detected XML content and all other content types not matched above, the normalizer
shall pass `RawCapture.raw_body` through unchanged.

---

### REQ-09 — SHA-256 hashing

After normalization, the system shall compute two SHA-256 hex digests:

- `raw_sha256`: SHA-256 of `RawCapture.raw_body`.
- `normalized_sha256`: SHA-256 of `NormalizerResult.normalized_body`.

Both shall be computed using Python's `hashlib.sha256` and represented as lowercase hex
strings of exactly 64 characters.

---

### REQ-10 — BlobStore interface

The system shall define a `BlobStore` interface with the following methods:

```
put(sha256: str, data: bytes) -> str
    # Verifies sha256(data) == sha256; raises BlobHashMismatchError on mismatch.
    # Writes the blob if not already stored.
    # Returns the relative storage key (e.g. "ab/ab12….bin") whether the blob was
    # newly written or already existed.

exists(sha256: str) -> bool

get(sha256: str) -> bytes
    # Raises BlobNotFoundError if the hash is not stored.
```

The core domain shall depend only on `BlobStore`, not on any concrete implementation.
`CaptureService` uses the key returned by `put` directly as `storage_path` on the
`CaptureRecord`. The core never constructs or interprets storage paths.

---

### REQ-11 — FileBlobStore implementation

When `put(sha256, data)` is called, the blob store shall **first** verify that
`hashlib.sha256(data).hexdigest() == sha256`. If the hashes do not match, it shall raise
`BlobHashMismatchError(expected=sha256, actual=<computed>)` immediately without writing
any file.

If the hash matches and no file for that hash exists, the blob store shall write `data` to
a file at `<root>/<sha256[0:2]>/<sha256>.bin`. Files are organized into two-character
prefix subdirectories to avoid filesystem limits on directory entry counts.

The write shall be **atomic and durable**:
1. Write to a temporary file in the same prefix subdirectory.
2. Call `fsync` on the temporary file's file descriptor to flush to disk.
3. Rename the temporary file to the final path using a **no-replace** mechanism (e.g.
   `os.link` + unlink of the temp, or a platform-appropriate exclusive rename). If the
   final path already exists (a concurrent writer beat this one), silently ignore the
   "already exists" error and discard the temp file; the existing file is correct.
4. Call `fsync` on the prefix subdirectory to make the directory entry durable.

When `put(sha256, data)` completes without error, the blob is guaranteed to be on disk.
No partial write shall appear at the final path.

The blob store shall never modify or delete an existing file after the initial write.

When `get(sha256)` is called for a hash that does not exist, the blob store shall raise
`BlobNotFoundError` with the missing hash included in the message.

---

### REQ-12 — Repository ports

The system shall define a `CaptureRepository` interface with the following methods:

```
insert(capture: CaptureRecord) -> None
    # Opens a fresh session, inserts one Capture row, commits, closes.

insert_in_new_transaction(capture: CaptureRecord) -> None
    # Identical behaviour: opens a fresh session, inserts, commits, closes.
    # Exists as a distinct method so callers signal intent; a future implementation
    # may apply different isolation or retry policies without changing call sites.
```

The system shall define a `SourceRepository` interface with the following methods:

```
get_or_create_by_url(url: str) -> Source
    # Returns existing Source for url, or inserts a new one and returns it.
    # Opens its own session, commits, and closes per call.

get(source_id: UUID) -> Source
    # Returns Source by id; raises SourceNotFoundError if missing.
```

Each repository method opens its own session and commits per call. No session is shared
between calls or passed in by the caller.

The capture service shall use `insert` for successful captures and
`insert_in_new_transaction` for error rows, so error records commit even if the main
session has rolled back (see REQ-17 and REQ-18).

The core domain shall depend only on `CaptureRepository` and `SourceRepository`, not on
any ORM or database driver. Property tests shall use in-memory fake implementations.

---

### REQ-13 — Capture record model

The system shall define a `Capture` database model with the following fields:

| Field                    | Type           | Constraints |
|--------------------------|----------------|-------------|
| `id`                     | UUID           | Primary key, generated on insert. |
| `source_id`              | UUID           | Foreign key → `Source.id`; not null. |
| `requested_url`          | `text`         | Not null; taken from `RawCapture.requested_url`, or `source.url` if no `RawCapture` exists. |
| `final_url`              | `text`         | Not null; taken from `RawCapture.final_url`, or `source.url` if no `RawCapture` exists. |
| `captured_at`            | `timestamptz`  | Not null; taken from `RawCapture.fetched_at`, or the current UTC time if no `RawCapture` exists. |
| `adapter`                | `text`         | Not null; taken from `RawCapture.adapter`, or `"unknown"` if no `RawCapture` exists. |
| `status_code`            | `int`          | Not null; taken from `RawCapture.status_code`, or `0` if no `RawCapture` exists. |
| `status`                 | `text`         | Not null; see REQ-14 for allowed values. |
| `error_reason`           | `text`         | Nullable; taken from `RawCapture.error_reason`; null when `status = "ok"`. |
| `content_type`           | `text`         | Nullable; taken from `RawCapture.content_type`; null for non-ok status. |
| `raw_sha256`             | `char(64)`     | Nullable; null for non-ok status. |
| `normalized_sha256`      | `char(64)`     | Nullable; null for non-ok status. |
| `normalizer_version`     | `text`         | Nullable; null for non-ok status. |
| `normalization_fallback` | `bool`         | Not null; `False` for non-ok status. |
| `truncated`              | `bool`         | Not null; taken from `RawCapture.truncated`; `False` for non-ok status. |
| `storage_path`           | `text`         | Nullable; relative path to the normalized blob; null for non-ok status. |

`Capture` rows shall never be updated or deleted after insert (append-only).

The database shall **revoke `UPDATE` and `DELETE` privileges** on the `capture` table from
the application database role used at runtime. A database-level trigger shall additionally
raise an error on any attempt to update or delete a `capture` row, providing defense in
depth. These controls together enforce the append-only invariant at the database level, not
only in application code.

The database shall maintain a **non-unique index** on `(source_id, normalized_sha256)` for
query performance. No uniqueness constraint shall be enforced on this pair.

---

### REQ-14 — Capture status rules

The system shall assign the `status` field of a `Capture` row using the following rules,
evaluated in order:

| Condition | `status` | Hash / path fields | Notes |
|-----------|----------|--------------------|-------|
| No `RawCapture` produced (unhandled exception before or during fetch) | `internal_error` | all null | Fallback values per REQ-13 column notes |
| `status_code = -1` and `error_reason` in `{"robots_forbidden", "robots_unavailable"}` | `robots_forbidden` | all null | robots_unavailable = 5xx/unreachable |
| `status_code = -1` and `error_reason` in `{"invalid_url", "ssrf_blocked"}` | `blocked` | all null | URL rejected before or during fetch |
| `status_code = 0` (any `error_reason`) | `fetch_error` | all null | Timeout, network error, redirect limit |
| `status_code` in 100–199 | `fetch_error` | all null | 1xx not a final response |
| `status_code` in 300–399 | `fetch_error` | all null | 3xx not resolved |
| `status_code` in 400–499 | `http_error` | all null | Client error |
| `status_code` in 500–599 | `http_error` | all null | Server error |
| `status_code` in 200–299 | `ok` | all populated | Successful fetch |
| Any other value | `fetch_error` | all null | Unexpected sentinel |

The `internal_error` status for a **blob-write failure** (steps 7–8 of REQ-17) is assigned
by the **capture service**, not derived from the `RawCapture`. When a blob write fails after
a successful 2xx fetch, the `status_code` stored on the error row is the real HTTP status
code from `RawCapture.status_code` (e.g. `200`), and `status` is set to `"internal_error"`
by the service. This distinguishes a storage failure from an HTTP failure at the same
`status_code` value.

For every status other than `ok`, `raw_sha256`, `normalized_sha256`, `normalizer_version`,
and `storage_path` shall be null, and `truncated` and `normalization_fallback` shall be
`False`.

---

### REQ-15 — Idempotency and append-only Capture semantics

The blob store deduplicates file content; the `Capture` table records history. These two
concerns are independent.

Storing the same content (same `normalized_sha256`) for the same source a second time shall
produce **one blob file** (the existing file is reused by `BlobStore.put`) and a **new
`Capture` row** marked `status = "ok"`. The new row records that the content was confirmed
unchanged at a later point in time.

If a source's content changes A → B → A, three `Capture` rows shall exist for that source
(one per fetch), and the blob for A shall be stored exactly once.

Two different sources fetching identical content shall each produce their own `Capture` rows
(with the same `normalized_sha256`) but only one shared blob file.

The capture service shall never skip inserting a `Capture` row on the basis that the content
has not changed.

---

### REQ-16 — Source database model

The system shall define a `Source` **database model** (ORM-mapped) with the following
fields, mapping to and from the `Source` domain object defined in REQ-02:

| Field            | Type           | Constraints |
|------------------|----------------|-------------|
| `id`             | UUID           | Primary key, generated on insert. |
| `url`            | `text`         | Not null; the URL being tracked. |
| `label`          | `text`         | Nullable; optional human-readable name. |
| `created_at`     | `timestamptz`  | Not null; set on insert, never updated. |
| `watch_schedule` | `text`         | Nullable; cron expression or interval string. |

The `url` field shall be stored exactly as submitted.

The database shall enforce a **foreign key constraint** from `Capture.source_id` to
`Source.id` with `ON DELETE RESTRICT`, so a `Source` row cannot be deleted while any
`Capture` row references it.

---

### REQ-17 — End-to-end capture flow

When the capture service is invoked with a `Source` domain object, the system shall execute
the following steps in order:

1. Call `FetchPort.fetch(source)` to obtain a `RawCapture`. The adapter always returns a
   value and never raises.
2. Determine `status` per REQ-14.
3. If `status != "ok"`, record an error `Capture` row via
   `CaptureRepository.insert_in_new_transaction` per REQ-18 and stop.
4. Compute `raw_sha256` from `RawCapture.raw_body`.
5. Call `Normalizer.normalize(raw)` to obtain a `NormalizerResult`. (The normalizer never
   raises; if it falls back, `normalization_fallback = True` is recorded on the row.)
6. Compute `normalized_sha256` from `NormalizerResult.normalized_body`.
7. Call `BlobStore.put(raw_sha256, raw_body)` to store the raw blob; use the returned
   storage key as the raw blob reference.
8. Call `BlobStore.put(normalized_sha256, normalized_body)` to store the normalized blob;
   use the returned storage key as `storage_path` on the `CaptureRecord`.
9. Insert a new `Capture` row with `status = "ok"` and all hash/path fields populated
   by calling `CaptureRepository.insert(capture_record)`.

**Ordering:** blob writes (steps 7–8) happen before the database insert (step 9). If any
step from 7 onward fails, the failure is handled uniformly per REQ-18: the capture service
catches the exception, builds an `internal_error` record, and records it via
`CaptureRepository.insert_in_new_transaction`. Any blobs already written in steps 7–8 that
have no corresponding `Capture` row are **orphan blobs**; they are acceptable because they
are content-addressed and harmless. Orphan blobs may be reclaimed by a future
garbage-collection process (defined in a later spec) but their presence does not affect
correctness.

If `NormalizerResult.normalization_fallback` is `True`, the capture proceeds normally;
the flag is recorded on the `Capture` row. A capture with `normalization_fallback = True`
**is** certifiable provided all other eligibility conditions in REQ-19 are met; the fallback
indicates a normalization limitation, not a data integrity failure.

If an **unhandled exception** is raised anywhere in steps 1–9 (e.g. by a bug in the capture
service itself, not by an adapter), the capture service shall catch it using
`except Exception` (not `BaseException`, to allow `KeyboardInterrupt` and `SystemExit` to
propagate), log a critical error with full context, and proceed to REQ-18 to record an
`internal_error` row using the fallback field values defined in REQ-13. The service shall
return the error `CaptureRecord` after recording it; it shall re-raise only if the
error-row insert itself fails (per REQ-18).

---

### REQ-18 — Error Capture row transaction isolation

When any step in REQ-17 results in a failure, the system shall record a `Capture` row
documenting the failure using `CaptureRepository.insert_in_new_transaction`. This ensures
the error record commits even if any outer transaction has rolled back.

The system shall log the error with full context (source id, `requested_url`, adapter,
`fetched_at`, status, `error_reason`, exception type and message if applicable) **before**
attempting to insert the error row.

If the error-row insert itself fails (e.g. the database is unreachable), the system shall
log a **critical** error with the original error details and the insert-failure details, and
then re-raise the **original** error. It shall not raise the insert failure in place of the
original.

---

### REQ-19 — Certificate eligibility

A `Capture` row is eligible for certificate generation if and only if all of the following
hold:

- `status = "ok"`
- `truncated = False`
- `raw_sha256` is not null
- `normalized_sha256` is not null
- `storage_path` is not null

A capture with `normalization_fallback = True` **is** eligible provided the above conditions
are met; the certificate shall include a note that normalization fell back to raw content.

A capture with `truncated = True` is **never** eligible, regardless of other fields.

The certificate service (defined in a later spec) shall reject any request to certify a
`Capture` that does not meet all eligibility conditions and return a structured error
identifying which condition failed.

---

### REQ-20 — Database append-only enforcement

The database shall enforce the append-only constraint on the `capture` table at two levels:

1. **Privilege revocation:** The application database role used at runtime shall be granted
   `INSERT` and `SELECT` on `capture`, but not `UPDATE` or `DELETE`.

2. **Trigger:** A `BEFORE UPDATE OR DELETE` trigger on `capture` shall raise an exception
   for any attempted modification or deletion, providing defense in depth in case the
   application role is inadvertently granted broader privileges.

The same append-only enforcement (privilege revocation + trigger) shall apply to the
`source` table with the following **specific column rules**:

- `DELETE` on `source` shall be blocked entirely (revoke `DELETE` privilege; trigger raises
  on any `DELETE` attempt).
- `UPDATE` on `source` is **permitted**, but only for the mutable fields `label` and
  `watch_schedule`. The trigger shall raise an error if an `UPDATE` statement causes any
  of `id`, `url`, or `created_at` to take a value different from its current value
  (i.e. `NEW.col IS DISTINCT FROM OLD.col`). An `UPDATE` that sets those columns to their
  existing values shall be permitted. `SELECT` and `INSERT` are unrestricted for the
  application role.

---

### REQ-21 — Property-based test properties

The test suite shall include Hypothesis-driven property tests verifying all of the following.

**Interface-level properties** use in-memory fakes for all storage and network dependencies;
no real network request or real filesystem access is made.

**File-level properties** use a real `FileBlobStore` instance against a temporary directory
(created and torn down per test run via `tmp_path` or equivalent).

| ID  | Level     | Property | Statement |
|-----|-----------|----------|-----------|
| P1  | Interface | **Hashing matches hashlib** | For any byte sequence `b`: `sha256(b) == hashlib.sha256(b).hexdigest()`. |
| P2  | File      | **Idempotent blob storage** | Calling `FileBlobStore.put(h, data)` twice with the same `(h, data)` results in exactly one file on disk with the correct contents. |
| P3  | Interface | **New Capture row per fetch** | Invoking the capture service twice for the same source and identical content, using in-memory fakes for `BlobStore` and `CaptureRepository`, produces two rows in the fake repository and **no new blobs on the second call** (the fake `BlobStore` already holds the hash from the first call). |
| P4  | Interface | **Normalization is idempotent** | Tested over generated well-formed HTML inputs produced by a Hypothesis strategy that constructs `RawCapture` objects using `hypothesis.strategies.builds` to assemble `<html>`, `<head>`, and `<body>` elements with text, links, and paragraphs (not `text()` filtered by regex). For any such input `r`: `normalize(wrap(normalize(r).normalized_body, "text/html", charset=None)).normalized_body == normalize(r).normalized_body`, where `wrap(b, ct, charset)` constructs a `RawCapture` with `raw_body = b`, `content_type = ct`, and `charset = charset`. The HTML parser is **BeautifulSoup with `html.parser`**; the test pins the BeautifulSoup version. |
| P5  | Interface | **JSON key order does not change the hash** | For any JSON-serializable dict `d` (generated by Hypothesis), any permutation of its top-level keys produces the same `normalized_sha256`. |
| P6  | Interface | **Normalizer never raises on arbitrary bytes** | For any byte sequence `b` and any `content_type` string (including unknown and malformed types), `normalize(RawCapture(raw_body=b, content_type=ct, charset=None, ...))` returns a `NormalizerResult` without raising an exception. |
| P7  | File      | **Round-trip integrity** | For any byte sequence `b`, after `FileBlobStore.put(sha256(b), b)`: `FileBlobStore.get(sha256(b)) == b`. |
| P8  | File      | **Content-addressing** | For any byte sequence `b` stored via `FileBlobStore.put(sha256(b), b)`: `sha256(FileBlobStore.get(sha256(b))) == sha256(b)`. |
| P9  | File      | **Mismatched put raises** | Calling `FileBlobStore.put(h, data)` where `sha256(data) != h` raises `BlobHashMismatchError` and writes no file. |
| P10 | File      | **Concurrent puts leave one intact file** | Simulating two concurrent `FileBlobStore.put(h, data)` calls (via threads) for the same `(h, data)` results in exactly one file on disk with contents equal to `data`, and no partial writes. |
