# Sprint Checklist (Mini Board)
Last updated: 2026-05-01

Priority labels: P0 = critical, P1 = high, P2 = nice-to-have.
Status values: Not Started, In Progress, Partial, Done.

## Week 1 Goal: Reliability and Data Quality Hardening
| Task | Priority | Estimate | Status | Deliverable | Acceptance Criteria | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| Task 1: Add retry/backoff policy to extraction and transient storage failures | P0 | 1.5 days | Partial | retry utility + integrated usage in extract/storage call sites | deterministic tests proving retry count, delay pattern, and fail-fast behavior | API retries implemented via `run_with_retry` in extract; transient storage writes are logged/skipped but not retried. Retry tests exist. |
| Task 2: Add transform validation gates | P0 | 1.5 days | Partial | required-field checks, timestamp sanity checks, basic value range checks | invalid records are counted/logged and safely skipped or quarantined by policy | Required columns, null thresholds, uniqueness checks implemented; timestamp sanity/range checks missing; no skip/quarantine policy yet. |
| Task 3: Improve run metadata logging | P0 | 1 day | Not Started | per-run summary (cities processed, rows staged/loaded, failures, duration) | clear end-of-run structured log line for each data type |  |
| Task 4: Tests for all new reliability/validation behavior | P0 | 1 day | Partial | unit tests for retries, validation branches, and logging summaries | green CI and local test pass | Retry tests exist; validator tests exist; logging/skip-quarantine tests missing. |

## Week 2 Goal: Observability + Integration Confidence
| Task | Priority | Estimate | Status | Deliverable | Acceptance Criteria | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| Task 5: Add integration test path with real Postgres (container or dedicated test DB) | P1 | 2 days | Not Started | at least one end-to-end test for current and one for forecast | test verifies raw -> staging -> load with idempotent re-run behavior |  |
| Task 6: Add lightweight freshness/anomaly checks | P1 | 1 day | Not Started | script/checks for stale city data and abnormal row-count deltas | checks can run in CI/manual mode and return actionable output |  |
| Task 7: Small dashboard starter for portfolio demonstration | P2 | 1 day | Not Started | minimal metrics view (ingestion counts, freshness, failure counts) | one reproducible screenshot/report query set for portfolio README |  |
| Task 8: Docs and architecture decision log polish | P2 | 1 day | Not Started | update docs with final behavior and trade-offs | docs match implementation, no stale command paths, clear scope boundaries |  |

## End-of-Week-2 Milestone (Portfolio Checkpoint)
Reliable current+forecast pipelines with retry + validation, measurable run health and freshness checks, one integration-tested end-to-end path, clear documented decision trail, and a strong production-minded narrative for recruiters.
