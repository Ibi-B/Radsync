# External provider-state storage

## Goal and safety contract

The refresh pipeline uses the storage-neutral \`SnapshotStore\` protocol from \`sources/snapshot_store.py\`. Its operations are \`save_snapshot\`, \`load_snapshot\`, \`verify_snapshot\`, and \`prune_snapshots\`. \`LocalFileSnapshotStore\` remains the offline adapter and initial Git-state bootstrap.

The external S3-compatible implementation stores verified provider snapshots while the \`radsync-state\` Git branch keeps small manifests as durable pointers. The public Pages catalogue, canonical station identity, matching, UUID rules, ranking, ambiguity handling, and publication safeguards are outside this storage migration.

The storage adapter verifies a candidate before changing committed pointers:

1. Serialize a provider snapshot as JSONL/Zstandard with a header, one station record per line, and a footer.
2. Validate the local archive and provider-specific anomaly bounds.
3. Upload content-addressed objects using the atomic S3 \`If-None-Match: *\` create-only condition.
4. Download each destination object again, verify compressed SHA-256, and stream it into SQLite to validate its header, footer, canonical record digest, provider identity, and station/stream counts.
5. Write the new Git manifests only after all provider and diagnostic objects pass verification.
6. Push the manifest commit to \`radsync-state\`.
7. Only after that push, finalize rotation by removing superseded source objects whose verified replacements are already referenced by the committed manifest.

If upload, download, checksum, decompression, staging, anomaly validation, or Git push fails, the current and previous manifest pointers are not advanced. A failed candidate does not replace the last-good snapshot. Cleanup is idempotent and happens only after the new manifest commit is remote.

The initial migration imports existing \`state/sources/*.json\` snapshots. It does not synthesize or repair missing provider provenance. In particular, IPRD observations without historical playlist path, entry index, raw EXTINF, original title/URL, and ingestion time remain legacy-incomplete-provenance.

## Retention-aware object layout

The default state prefix is \`radsync/state\`. Provider objects are stored under four distinct prefixes:

\`\`\`text
radsync/state/current/<provider>/sha256/<digest>/active.jsonl.zst
radsync/state/previous/<provider>/sha256/<digest>/previous.jsonl.zst
radsync/state/archive/<provider>/<timestamp>-<digest>.jsonl.zst
radsync/diagnostics/refresh-report-<run>-<digest>.json.zst
radsync/diagnostics/identity-ambiguity-<run>-<digest>.json.zst
\`\`\`

The digest directory is intentional. It keeps every object immutable and allows uploads to continue using \`If-None-Match: *\`. The small manifest in Git identifies the active object; fixed mutable object names are not used.

The manifest at \`manifests/providers/<provider>.json\` stores the current descriptor at its top level and an optional \`previous\` descriptor. Both descriptors include object key, compressed SHA-256, compressed size, station/stream counts, snapshot timestamp, and anomaly bounds. The manifest can also carry a temporary \`rotationCleanup\` list. That list is honored only after the Git pointer commit has been pushed.

### Promotion sequence

- First successful publish: verify the candidate, then point \`current/<provider>/\` at it. There is no previous version yet.
- Second successful publish: verify the candidate, copy and re-verify the prior current object under \`previous/<provider>/\`, then update the manifest. The prior current is not removed until the manifest push succeeds.
- Third and subsequent successful publishes: copy and verify the outgoing previous object under \`archive/<provider>/\`; copy and verify current under \`previous/<provider>/\`; then publish the new current pointer. After the Git manifest push, finalization removes the superseded source keys.
- If current is missing or corrupt but previous verifies, restore previous as the local last-good snapshot. A later successful publish keeps that verified previous version as the new previous until a valid current can be rotated.
- If neither current nor previous verifies, restore fails closed and does not promote any local provider file.

The archive copy is verified before the previous pointer can advance. Finalization checks that the replacement object exists with the expected SHA-256 metadata and that the replacement is still referenced by the committed manifest before deleting the superseded object. An interrupted finalization is retried on the next refresh.

## Retention policy

Configure lifecycle rules only after the retention-aware layout is merged and reviewed:

| Prefix | Policy |
|---|---|
| \`radsync/state/current/\` | No lifecycle expiration |
| \`radsync/state/previous/\` | No lifecycle expiration |
| \`radsync/state/archive/\` | Expire after 90 days |
| \`radsync/diagnostics/\` | Expire after 75 days (within the 60–90-day policy) |

Do not create a bucket-wide or \`radsync/state/\`-wide expiration rule. Such a rule could delete a current or previous-good object when no successful refresh has occurred for longer than the retention window.

The optional \`prune-external-archive\` command defaults to a dry run and refuses an age below 90 days. When explicitly invoked with \`--apply\`, it lists and deletes objects only beneath \`archive/\`. It never lists or deletes objects from \`current/\` or \`previous/\`. R2 lifecycle expiration should remain the normal archive cleanup mechanism.

## Object format and verification

Each provider object contains:

- Header: archive and provider-snapshot schema versions, provider ID, snapshot timestamp, station count, stream count, and anomaly-bound configuration.
- Station records: one complete station snapshot object per JSONL record, preserving all station fields, streams, source records, playlist provenance, and provenance status.
- Footer: station count, stream count, and SHA-256 of the canonical station-record sequence.

The restore path downloads to a temporary file, verifies compressed SHA-256 before decompression, and streams Zstandard JSONL records into a temporary SQLite database. It does not load the full compressed archive into memory.

\`staged_stations\` retains full station JSON and station-level provenance status. \`staged_streams\` carries provider and station key, stream index, normalized URL and URL hash, codec, bitrate, provider/source record ID, playlist path, zero-based entry index, and provenance status.

All providers are staged before any local last-good JSON file is promoted. A corrupt or missing current object can fall back to a separately verified previous object. If the whole provider batch cannot be staged, the local last-good files remain untouched.

## Diagnostics storage and retrieval

Refresh reports and identity-ambiguity diagnostics are compressed and stored below the separate diagnostics prefix. Their small pointers remain in Git at:

- \`manifests/reports/<run-id>.json\` for refresh reports;
- \`manifests/diagnostics/<run-id>.json\` for identity-ambiguity diagnostics.

The normal GitHub Actions artifacts remain available for convenient short-term inspection. The external report can be retrieved after its Actions artifact expires:

\`\`\`bash
python -m sources.state_cli fetch-external-report \\
  --state-dir state \\
  --run-id 43910570174 \\
  --output artifacts/source-report-43910570174.json
\`\`\`

Retrieval checks the pointer prefix, downloads and verifies compressed SHA-256 before decompression, validates the resulting JSON stream, and atomically writes the output. A mismatch or malformed report leaves any existing output untouched.

## Provider-specific anomaly bounds

\`config/sources.json\` declares bounds independently for each provider. The gate evaluates station count and stream count separately:

- \`minStationCount\`: absolute floor for stations;
- \`maxStationDropRatio\`: maximum allowed decrease from that provider's previous verified snapshot;
- \`minStreamCount\`: absolute floor for streams;
- \`maxStreamDropRatio\`: maximum allowed decrease from that provider's previous verified snapshot;
- \`minRecordProductionCoverage\`: minimum share of retrieved playlists producing at least one usable named HTTP(S) stream, where the provider exposes playlist-production coverage.

The current absolute floors are conservative starting values derived from controlled refresh evidence, not universal assumptions about all future provider data. Change them only after reviewing a provider-specific baseline and its expected update behavior. A failed bound preserves last-good snapshot data and does not weaken station identity checks.

## GitHub Actions configuration

Use a private S3-compatible bucket. Configure these repository Actions variables:

- \`RADSYNC_S3_BUCKET\` — required bucket name.
- \`RADSYNC_S3_ENDPOINT\` — optional endpoint URL; set for S3-compatible services, omit for standard AWS S3.
- \`RADSYNC_S3_REGION\` — optional region; defaults to \`us-east-1\`.
- \`RADSYNC_S3_PREFIX\` — optional state prefix; defaults to \`radsync/state\`.
- \`RADSYNC_DIAGNOSTICS_PREFIX\` — optional diagnostics prefix; defaults to \`radsync/diagnostics\`.

Configure these repository Actions secrets:

- \`RADSYNC_S3_ACCESS_KEY_ID\`
- \`RADSYNC_S3_SECRET_ACCESS_KEY\`

Credentials must be scoped to the single private Radsync bucket and only to the operations required by the workflow. Do not make the bucket public. The repository is public, so do not place credentials, signed object URLs, or raw provider secrets in manifests or logs.

Provider and diagnostic uploads use content-addressed keys and the atomic S3 \`If-None-Match: *\` create-only condition. A pre-existing object is accepted only after its stored SHA-256 metadata and byte length match the expected content.

## Cloudflare R2 configuration — after this PR

R2 is the primary target for the first live migration:

- Bucket: \`radsync-state-store\`
- Location: Automatic, or Europe if preferred
- Storage class: Standard
- Access: Private
- API token: Object Read & Write, restricted to this bucket
- Endpoint: \`https://<ACCOUNT_ID>.r2.cloudflarestorage.com\`
- Region: \`auto\`
- Bucket Lock: Off

Configure only these lifecycle rules:

1. Prefix \`radsync/state/archive/\`: expire after 90 days.
2. Prefix \`radsync/diagnostics/\`: expire after 75 days.

Do not apply expiration to \`current/\` or \`previous/\`, and do not enable Bucket Lock. Do not configure R2 or add GitHub repository variables/secrets as part of this code change.

## Workflow behavior

- If no external provider manifests exist, restore leaves the Git-hosted last-good snapshots unchanged. This is the bootstrap path.
- If manifests exist but the bucket is not configured, restore fails closed.
- If current is missing or fails integrity/semantic checks, restore tries previous. If both fail, restore exits before promoting any local provider files.
- Publication stages and verifies all provider versions and diagnostics before writing manifest pointers.
- The workflow pushes the new manifest pointers before finalizing rotation. Finalization is safe to retry and verifies replacements before deleting superseded source keys.
- The refresh report is not committed to Git. Small report and ambiguity pointers are committed instead.
- If the bucket is not configured and no external manifests exist, the workflow warns and leaves the durable provider state unchanged rather than retrying oversized Git snapshot pushes.

Merging this code does not prove live provider-state persistence. The first controlled R2 migration must still verify real upload, download, SHA-256, header/footer, SQLite restore, current/previous fallback, diagnostics retrieval, and the absence of oversized provider files/refresh reports in Git.
