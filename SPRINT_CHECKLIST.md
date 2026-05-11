# Sprint Checklist (Mini Board)
Last updated: 2026-05-05

Priority labels: P0 = critical, P1 = high, P2 = nice-to-have.
Status values: Not Started, In Progress, Partial, Done, Blocked.

## Week 1 Goal: Reliability and Data Quality Hardening
| Task | Priority | Estimate | Status | Deliverable | Acceptance Criteria | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| Task 1: Add retry/backoff policy to extraction and transient storage failures | P0 | 1.5 days | Done ✅ | retry utility + integrated usage in extract/storage call sites | deterministic tests proving retry count, delay pattern, and fail-fast behavior | `retry.py` complete with exponential backoff, RETRY_MAX_ATTEMPTS=4, configurable delays. @run_with_retry decorator used in extract.py (_get_response) and storage.py (all R2 ops). Tests: test_retry.py + test_extract.py retry tests. Retryable HTTP status (408, 429, 5xx) and transient S3 errors properly classified. |
| Task 2: Add transform validation gates | P0 | 1.5 days | Done ✅ | required-field checks, timestamp sanity checks, basic value range checks | invalid records are counted/logged and safely skipped or quarantined by policy | `validators/engine.py` complete with schema (required columns), row_count (min/max), null_thresholds, uniqueness checks. `validation_rules.json` configured for current + forecast. staging_transform.py calls validation_engine.run_validations(). Tests: test_validators.py covers all checks. Config flag VALIDATION_FAIL_ON_ERROR controls behavior. |
| Task 3: Improve run metadata logging | P0 | 1 day | In Progress | per-run summary (cities processed, rows staged/loaded, failures, duration) | clear end-of-run structured log line for each data type | orchestrator.py logs per-step (extract, stage, load) but no summary aggregation yet. Needs: track row counts per stage, aggregate across data types, log end-of-run summary. |
| Task 4: Tests for all new reliability/validation behavior | P0 | 1 day | Done ✅ | unit tests for retries, validation branches, and logging summaries | green CI and local test pass | Retry tests (test_retry.py): 3 deterministic tests covering retry loop, exhaustion, fail-fast. Validation tests (test_validators.py): schema, row_count, null, uniqueness checks. Extract tests: retry behavior on 5xx and timeouts. All tests passing. Missing: aggregation summary tests. |

## Week 2 Goal: Observability + Integration Confidence
| Task | Priority | Estimate | Status | Deliverable | Acceptance Criteria | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| Task 5: Add integration test path with real Postgres (container or dedicated test DB) | P1 | 2 days | Not Started | at least one end-to-end test for current and one for forecast | test verifies raw -> staging -> load with idempotent re-run behavior | All tests currently mock Postgres (see conftest.py). Need: Docker Postgres container setup in CI or test fixture, e2e test for current+forecast ETL flow, idempotency verification (re-run same raw data, no duplicates). Recommend: pytest-postgresql plugin or Docker container in conftest. |
| Task 6: Add lightweight freshness/anomaly checks | P1 | 1 day | Not Started | script/checks for stale city data and abnormal row-count deltas | checks can run in CI/manual mode and return actionable output | Need: Query analytics tables for (1) last_updated timestamp per city vs. current time (alert if >2h stale), (2) row count per city vs. historical baseline (alert if 0 or >10x baseline). Can be CLI tool or query set for dashboards. |
| Task 7: Small dashboard starter for portfolio demonstration | P2 | 1 day | Not Started | minimal metrics view (ingestion counts, freshness, failure counts) | one reproducible screenshot/report query set for portfolio README | Need: 3-5 SQL queries or Python script that produces CSV/table: (1) rows/city/day, (2) last refresh timestamp/city, (3) error counts by type/week. Matplotlib or plain SQL output acceptable. |
| Task 8: Docs and architecture decision log polish | P2 | 1 day | Partial | update docs with final behavior and trade-offs | docs match implementation, no stale command paths, clear scope boundaries | SRS.txt and SYSTEM ARCHITECTURE.txt updated 2026-03-27. Validation rules documented. Retry logic documented. Missing: end-of-sprint polish (final checklist of completed features, roadmap for next phases). |

## End-of-Week-2 Milestone (Portfolio Checkpoint)
**Target Status: 75-80% Complete** (up from ~65% today)

✅ **Completed (Week 1 + prior)**:
- Reliable extraction with 3x exponential backoff retry
- Data validation gates (required fields, uniqueness, null thresholds)
- Idempotent loads (ON CONFLICT DO NOTHING + DB constraints)
- GitHub Actions schedules for current (1h) and forecast (3h)
- Unit tests for extract, transform, load, storage (80+ tests)
- Lakehouse architecture (raw → staging → analytics)
- Local + R2 storage backends

⚠️ **In Progress**:
- Task 3 (Metrics): Log per-run summary with row counts

❌ **Not Started (Week 2 blockers)**:
- Task 5 (Integration Tests): Real Postgres e2e tests
- Task 6 (Freshness/Anomaly): Stale data and outlier detection
- Task 7 (Dashboard): Portfolio-ready metrics view

## Current Completion Estimate (as of 2026-05-05)

| Phase | Requirements | Complete | % | Blockers |
| --- | --- | --- | --- | --- |
| Phase 1: Core ETL | FR-1,2,3,4,6,7,8,11,12,13,14,15,16 | 12/13 | **92%** | None (Task 3 summary logging pending) |
| Phase 2: Multi-Source | FR-9,10,17,18 | 4/4 | **100%** | None (alerts deferred by design) |
| Phase 3: Lakehouse | FR-19,20 (High-water marks, partitioning, R2 support) | 3/3 | **100%** | None |
| Phase 4: Production | FR-5 (Retry ✅), FR-10 (Validation ✅), FR-16 (Metrics 🟠), Integration tests (❌), Anomaly detection (❌), Alerting (❌) | 5/10 | **50%** | Integration tests, freshness/anomaly checks, alerting |

**Overall Weighted Average: ~68%** (up from 61% at assessment start)

**Narrative for Next 2 Weeks**:
- Week 1 hardening (retry, validation) mostly complete; finish Task 3 (metrics summary)
- Week 2 focus: Integration tests (Task 5) for credibility, freshness/anomaly detection (Task 6) for reliability
- Dashboard (Task 7) is portfolio showcase, not blocking functionality
- By end of Week 2: System will be production-minded (reliable, validated, tested, observable)
