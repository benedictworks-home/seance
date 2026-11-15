# Séance — Product Steering

## Problem

Web pages and official statistics disappear or get silently revised. Students, journalists, and
analysts cite a figure, and later the page is gone or the number has changed, with no proof of
what it originally said.

## What Séance does

Séance captures sources over time, detects exactly what changed, resurrects dead pages from public
web archives, and issues a citation certificate proving what a source said and when.

## Users

- Students citing statistics or government data in academic work.
- Journalists fact-checking claims against primary sources.
- Analysts tracking policy documents, pricing pages, or regulatory filings over time.

## Halloween theme: "Talk to the dead web"

Every interaction uses séance metaphors:

- **Summon** — submit a URL or JSON endpoint; Séance captures a snapshot.
- **Watch** — the source is re-captured on a schedule; every change is detected and shown as a diff.
- **Resurrect** — if a source is dead (404/410/timeout), look up the most recent surviving version
  in public web archives and rebuild it, clearly labeled as a "ghost version" with its origin and
  capture date.
- **Certify** — produce a citation certificate: source, capture time, SHA-256 content hash,
  hash-chain link to the previous version, and a link to the stored evidence.

## Scope tiers

| Tier   | Features |
|--------|----------|
| MUST   | Capture + snapshot store, diff engine, REST API, timeline + diff UI, certificate page |
| SHOULD | Archive resurrection adapter, scheduled re-capture |
| COULD  | Stellar anchor, table/number structured diff |

## Hackathon context

Built during **Kiroween 2026**, category: Resurrection.
