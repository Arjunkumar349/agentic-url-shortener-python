# Greenfield Engineering Proposal

## Requirement
Build a Python URL shortener with an API to create short links, redirect to the original URL, and report per-link click analytics. Use persistent storage, validate inputs, and handle short-code collisions safely.

## API proposal
- `POST /api/urls` creates a short link from an HTTP(S) destination.
- `GET /{code}` redirects and records a click.
- `GET /api/urls/{code}/stats` returns creation time, click count, and last access time.

## Reliability assumptions
SQLite provides durable local storage; database transactions keep link creation and click counts consistent. Authentication, expiration, retention, and production availability targets require product decisions.

## Assumptions and open questions
- None identified for this scenario.

## Validation
Generated Python is syntax-checked, JSON is parsed, and integration checks verify required deliverables.
Generated code is not executed automatically. Review and run it in an isolated project environment.
