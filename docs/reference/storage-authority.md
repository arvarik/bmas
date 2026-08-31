# Storage authority: the runtime journal boundary

The runtime journal is the one append-only replay authority for
Foundation runtime transactions. This page documents the single-host
SQLite boundary that the code enforces.

## The single-host rule

Keep every journal transaction participant on one host. The database
file, the write-ahead log (`-wal`), and the shared-memory file
(`-shm`) must live on one local filesystem.

These operational facts drive the rule:

- SQLite permits one WAL writer at a time.
- Long readers can delay checkpoints.
- WAL pressure can increase read cost and disk use.
- Network filesystems do not support the required WAL sharing model.
  Their lock implementations are unreliable, and the shared-memory
  index cannot work across hosts.

The readiness check in `daemon/src/journal_backup.py` enforces the
rule. A known network filesystem blocks journal writers. The check
cannot prove every mount type, so an unknown storage type requires an
explicit operator confirmation.

## Durability settings

The journal connection commits with `journal_mode=WAL` and
`synchronous=FULL`. On macOS the connection also sets `fullfsync`,
because a plain `fsync` does not flush the disk cache there. An
acknowledged journal append survives an application crash and a power
loss.

The readiness check also blocks unsupported SQLite versions and a
database that fails the SQLite quick check.

## Immutability

The journal table rejects every update and delete through database
triggers. Task deletion does not cascade into the journal; a task
tombstone records the deletion instead. Mutable delivery state lives
in separate tables (`journal_delivery`, `run_queue`, and the dispatch
outboxes) and never touches an authority row.

One privileged chain-compaction migration can remove journal rows when
policy requires deletion. The migration needs two distinct approvers,
starts a new chain epoch, and retains an erasure manifest. A later
replay reports `redacted_by_policy` for the removed content.

## Backup

Use the SQLite Online Backup API for a live snapshot. Never copy only
the live main database file: the WAL is persistent database state. An
offline physical copy must retain the required WAL or the database
must close cleanly first.

Every backup writes one manifest with the database snapshot digest,
the schema version, the highest journal cursor, each active chain
head, the referenced artifact digests, the tool versions, the times,
and the verification result. The backup publishes only after the
database snapshot and the artifact set both pass verification. An
incomplete backup stays staged.

## Restore

Restore into an isolated location. The restore verifies the manifest
digests, replays every retained chain from the journal, opens every
replay-critical artifact, and reports the recovery point cursor and
the recovery time. Run this restore test on a schedule; a backup that
never restored is not a backup.
