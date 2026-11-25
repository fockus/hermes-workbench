# diagnosing-bugs — Workbench companion

## Reconcile a partial CLI success before retrying

1. Separate the write from its verification. Reopen the exact target independently and compare the record ID and intended new value. If it matches, report that the change persisted; retry only the check, not the mutation.
2. For a live SQLite target, connect with `mode=ro`, set `PRAGMA query_only=ON`, and issue `BEGIN` before reading the record and its related run or event count. One snapshot prevents a concurrent change between the two reads from looking like a contradiction.
3. Do not use `immutable=1` for a mutable database or delete its `-wal` or `-shm` files to make a probe pass; committed data may still be in the WAL. If readback now works, distinguish confirmed state from the unknown cause of the earlier open error.

When gateway startup fails but CLI help succeeds, inspect only the matching exit diagnostic for the intended profile. Correlate the time and process, and report the exception and relevant frame rather than raw context. Repeat the failing frame with the same interpreter, sandbox and working directory. Directory listings and socket paths are not proof of a live owner. Stop if a peer is visible but cannot be identified. Keep credential files unreadable and preserve the existing lock and lease boundaries; an unreadable dependency record is not evidence that the shared installation needs repair.

Copyright (c) 2026 Hermes Workbench contributors. MIT; see [LICENSE](../../../LICENSE).
