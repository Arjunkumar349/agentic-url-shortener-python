# Brownfield Engineering Proposal

## Requirement
Extend an existing URL shortener with analytics without breaking current behavior.

## API proposal
- `POST /api/urls` creates a short link from an HTTP(S) destination.
- `GET /{code}` redirects and records a click.
- `GET /api/urls/{code}/stats` returns creation time, click count, and last access time.

## Reliability assumptions
SQLite provides durable local storage; database transactions keep link creation and click counts consistent. Authentication, expiration, retention, and production availability targets require product decisions.

## Assumptions and open questions
- The existing public API and analytics compatibility requirements are not specified.

## Validation
Generated Python is syntax-checked, JSON is parsed, and integration checks verify required deliverables.
Generated code is not executed automatically. Review and run it in an isolated project environment.
