# External provider-state storage

## Goal and safety contract

## Storage adapter interface

The refresh pipeline uses the `SnapshotStore` protocol from `sources/snapshot_store.py`. Its storage-neutral operations are `save_snapshot`, `load_snapshot`, `verify_snapshot`, and `prune_snapshots`. `LocalFileSnapshotStore` implements the protocol for offline tests and the initial Git-state bootstrap.

The local adapter verifies snapshots with streaming JSON parsing. Its prune operation is deliberately non-destructive by default and only targets incomplete temporary files; it never prunes a valid last-good provider snapshot. External object publication and restore are orchestrated by the S3-compatible archive service, with small manifests as the durable pointers.

Radsync stores provider last-good snapshots as immutable, versioned objects in a private S3-compatible object store. The radsync-state branch stores only small manifests and diagnostic pointers. The public Pages catalogue and its BeastED contract are unchanged.

The migration is two-phase:

1. A provider snapshot is serialized as a .jsonl.zst object with a header, one station record per line, and a footer.
2. The object is uploaded under a content-addressed key, downloaded again, SHA-256 checked, streamed into SQLite, and checked against the header, footer, station count, stream count, and provider-specific anomaly bounds.
3. Only after every object in the run has passed verification are the small manifests written and the local legacy JSON snapshots removed from the state worktree.
4. The workflow commits the manifests and JSON deletions to radsync-state. If upload, download, checksum, decompression, staging, validation, or Git push fails, the remote last-good manifests and snapshot objects are not replaced.

The initial migration imports the existing state/sources/*.json snapshots. It does not synthesize or repair missing provider provenance. In particular, IPRD observations without historical playlist path, entry index, EXTINF, original title/URL, and ingestion time remain legacy-incomplete-provenance.

## Object format

Provider keys are content-addressed:

    <prefix>/providers/<provider>/sha256/<compressed-sha256>.jsonl.zst

The object contains:

- Header: archive and provider-snapshot schema versions, provider ID, snapshot timestamp, station count, stream count, and anomaly-bound configuration.
- Station records: one complete station snapshot object per JSONL record, preserving all station fields, streams, source records, playlist provenance, and provenance status.
- Footer: station count, stream count, and SHA-256 of the canonical station-record sequence.

The manifest stored in Git at manifests/providers/<provider>.json contains the object key, compressed-byte SHA-256, compressed size, schema/format, station/stream counts, snapshot time, and anomaly bounds.

Large refresh reports are compressed and stored at a content-addressed report key. Their small pointers live at manifests/reports/<run-id>.json; the normal GitHub Actions refresh-report artifact is still uploaded separately for convenient inspection.

## SQLite verification staging

The restore path downloads to a temporary file, verifies the compressed object's SHA-256, and streams Zstandard decompression line-by-line into a temporary SQLite database. It does not load the full compressed JSONL archive into memory.

staged_stations retains the full station JSON and station-level provenance status. staged_streams carries:

- provider and station key;
- stream index;
- normalized URL and SHA-256 URL hash;
- codec and bitrate;
- source-record/provider ID;
- playlist path and zero-based entry index;
- provenance status (fresh, restored-last-good, or legacy-incomplete-provenance).

The snapshot is promoted to the local last-good JSON file only after all external provider objects in the restore batch have passed verification. The SQLite staging database is temporary and is not committed to Git.

## External report retrieval

A report pointer at `manifests/reports/<run-id>.json` can be used to retrieve the compressed report after its GitHub Actions artifact expires:

```bash
python -m sources.state_cli fetch-external-report \
  --state-dir state \
  --run-id 43910570174 \
  --output artifacts/source-report-43910570174.json
```

The command checks the object key is under the configured report prefix, downloads and verifies the compressed SHA-256 before decompression, validates the resulting JSON stream, and atomically writes the requested output file. A mismatch or malformed report leaves any existing output untouched.

## Provider-specific anomaly bounds

config/sources.json declares bounds independently for each provider. The gate evaluates station count and stream count separately:

- minStationCount: absolute floor for stations;
- maxStationDropRatio: maximum allowed decrease from that provider's previous verified snapshot;
- minStreamCount: absolute floor for streams;
- maxStreamDropRatio: maximum allowed decrease from that provider's previous verified snapshot;
- minRecordProductionCoverage: minimum share of retrieved playlists producing at least one usable named HTTP(S) stream, where the provider exposes playlist-production coverage.

The current absolute floors are conservative starting values derived from the controlled refresh evidence, not universal assumptions about all future provider data. Change them only after reviewing a provider-specific baseline and its expected update behavior. A failed bound restores that provider's last-good snapshot and records the reason; it does not discard the snapshot or weaken station identity checks.

## GitHub Actions configuration

Use a private S3-compatible bucket. Configure these repository Actions variables:

- RADSYNC_S3_BUCKET — required bucket name.
- RADSYNC_S3_ENDPOINT — optional endpoint URL; set for S3-compatible services, omit for standard AWS S3.
- RADSYNC_S3_REGION — optional region; defaults to us-east-1.
- RADSYNC_S3_PREFIX — optional key prefix; defaults to radsync/state.

Configure these repository Actions secrets:

- RADSYNC_S3_ACCESS_KEY_ID
- RADSYNC_S3_SECRET_ACCESS_KEY

The credentials need permission to read, write, and inspect objects under the configured prefix. Do not make the bucket public. The repository is public, so do not place credentials, signed object URLs, or raw provider secrets in manifests or logs.

Objects are stored under content-addressed keys and the workflow never intentionally overwrites an existing key. For stronger account-level immutability, enable bucket versioning and apply a bucket policy that denies object deletion and replacement for the Radsync prefix.

## Workflow behavior

- If no external manifests exist, restore-external leaves the existing Git-hosted last-good snapshots unchanged. This is the bootstrap path.
- If external manifests exist but the object store is not configured or an object cannot be verified, restore fails closed before provider collection.
- When the bucket is configured, publish-external uploads and verifies all available local provider snapshots, writes small manifests, uploads the run report, and then removes local provider JSON files from the state worktree.
- If the bucket is not configured and no external manifests exist, the workflow warns and skips state advancement. It does not retry the known-failing oversized Git snapshot push.
- If external manifests exist but the bucket is no longer configured, persistence fails rather than reverting to stale Git JSON.

The first successful external publish is the migration point. Until the bucket variables and secrets are configured, provider-state migration is not complete. The workflow can still run against the legacy state branch, but it will explicitly leave durable provider state unchanged.
