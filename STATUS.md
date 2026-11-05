# Status

**Public snapshot:** 2026-11-05, branch `wip/r1-02.2-r1` (commit `ae48e76`) of the private development repository.

This is a research / hackathon prototype. **It is not a medical device**, does not provide validated seizure detection, and must not be used for real health monitoring or emergency alerts.

## Not production-ready

- Independent review status: **NOT READY FOR R2**.
- **P0-1** (per-device FIFO causing incorrect events) is fixed.
- **P0-2** (writer exclusivity across processes) is unresolved and blocks merging to `main`.
- **P1-A** (crash after all stages are final but before `complete_job`, at max attempts) remains open in this snapshot; a fix exists only in a private worktree.
- **P1-B / P1-C** (APK ordering by timestamp; decisions failure downgraded to warning) still need an explicit accept/implement decision.

## What is included

- FastAPI backend and agent (`guardian/`)
- Android client source (`android/`)
- Tests (`tests/`), scripts (`scripts/`) and documentation (`docs/`)

## What is not included

- `.env`, API keys, pairing credentials
- SQLite databases, real health data, raw datasets
- Logs, model weights, build artifacts

## Running

See the quick start in [README.md](README.md). The default service binds `127.0.0.1`; do not expose it to the public internet. See [SECURITY.md](SECURITY.md).
