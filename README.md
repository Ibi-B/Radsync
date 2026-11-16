# Radsync

Radsync is BeastED's independent radio catalogue aggregation and publication service. It performs provider collection, format adaptation, station reconciliation, source provenance, stream ranking, and genre/country/language grouping outside the app. BeastED consumes a stable, versioned JSON contract; playback and radio UI remain in BeastED.

## Core data rules

- One canonical station may contain many selectable stream variants.
- Duplicate station entries are reconciled only when their available country/language identity is compatible.
- Every distinct valid HTTP(S) stream URL is retained. When providers publish the same URL, it is stored once while all provider provenance is retained.
- `sources` and `sourceGroups` expose the providers behind the variants; source-group indexes refer to `streams`.
- The highest-ranked stream is at index zero and is selected by `defaultStreamIndex: 0`. `defaultSource` identifies its provider.
- Ranking currently uses configured provider priority plus a bounded bitrate contribution. It does not yet claim that a stream is live; availability-aware ranking is a separate future stage.
- Empty or invalid source responses cannot overwrite that provider's last-good snapshot.

## Provider adaptability

Provider responses are treated as upstream input, not as the app's data contract. Shared helpers in `sources/normalize.py` recognize common variations in:

- response envelopes: arrays, `data`, `results`, `items`, `stations`, `channels`, and keyed record maps
- station names and IDs: `name`, `title`, `stationName`, `station_name`, UUID and ID variants
- stream URLs: `url`, `streamUrl`, `stream_url`, `listen_url`, `url_resolved`, and related variants
- country, language, genre/tag, codec, and bitrate field names
- tags represented as comma/semicolon-delimited strings, arrays, or small objects

M3U/M3U8 parsing retains common EXTINF metadata (station IDs, country, language, genre and logo). Icecast XML parsing tolerates XML namespaces and alternate element names. GitHub playlist discovery tolerates common tree response wrappers.

When a provider changes its format beyond these known variants, update its adapter and add a fixture/regression test there. Do not change the BeastED output contract just to mirror an upstream API. Unknown or ambiguous data should be rejected or reported, not silently misclassified.

## Durable provider state

The scheduled/manual refresh workflow checks out a dedicated `radsync-state` branch into a separate `state/` directory. Each enabled provider has its own versioned snapshot under `state/sources/`.

- Successful, non-empty provider fetches atomically replace only that provider's snapshot.
- A failed or empty provider response reuses only that provider's own last-good snapshot and marks its source report entry `stale`.
- One provider's failure cannot overwrite another provider's snapshot.
- Corrupt or unsupported state is rejected before use.
- The workflow persists snapshots to `radsync-state` with a narrowly scoped `contents: write` permission.
- Snapshot state is not included in the GitHub Pages artifact; only the public catalogue is deployed.

The current release is not advanced if no usable stations can be assembled. The previous Pages deployment therefore remains available when the complete refresh fails.

## Station identity continuity

The refresh workflow can generate an observability-only continuity report comparing the previous deployed station index with the newly built candidate catalogue.

Configure the repository variable `RADSYNC_PAGES_URL` with the base URL of the deployed GitHub Pages site (for example, the repository's published Pages URL). When the variable is absent, continuity comparison is explicitly skipped; catalogue validation and normal workflow behavior are unchanged.

Reports are written to the `radsync-state` branch under `identity-continuity/<catalogue-version>.json`. They are not included in the Pages artifact. The report counts retained, changed, removed, added, and ambiguous identities. Provider-qualified source IDs are a high-confidence comparison signal; metadata-only matches are low-confidence suggestions. Ambiguous matches are reported, not guessed.

This is observability only: it never rewrites `stationUuid`, changes aggregation, or changes the published app contract. Review the first report manually before treating continuity statistics as a release acceptance signal.

## Identity ambiguity diagnostics

When canonical UUID assignment encounters separate aggregates with identical canonical metadata and identical provider/stream provenance, the refresh writes `state/diagnostics/identity-ambiguity.json` before failing closed. The report includes the catalogue version, a stable group fingerprint, canonical identity inputs, provisional classification, and whitelisted provider evidence (including M3U entry index/EXTINF where available). URL query strings and fragments are removed from diagnostic URLs to avoid retaining access tokens.

The workflow uploads this report as a GitHub Actions artifact (14-day retention) even when catalogue building fails, and persists it with the private `radsync-state` branch. It is not included in the Pages artifact or BeastED's published JSON contract. A separate `source-report.json` refresh report is atomically written before canonical identity assignment and uploaded as a 30-day Actions artifact even when the identity guard fails. It records provider/snapshot status, ingestion start/end times, playlist discovery and failed/skipped paths, per-station provenance status (`fresh`, `restored-last-good`, or `legacy-incomplete-provenance`), and available source-record ingestion timestamps. Playlist retrieval coverage is successfully downloaded files divided by discovered files; parse coverage is files parsed without error divided by retrieved files; record-production coverage is retrieved files yielding at least one usable named HTTP(S) stream divided by retrieved files. A parsed file with no usable records counts toward parse coverage but not record-production coverage, and remains protected as an incomplete playlist until an upstream-specific valid-empty marker exists. `recordsProduced` is the number of usable station observations, not a coverage ratio. Reporting-only provenance labels stay out of the published station schema. Note: this repository is public, so the state branch and Actions artifacts are not private; the diagnostic contains only whitelisted upstream metadata and URLs with query strings/fragments removed. The report is observational: `likely_duplicate` is a clue for review, not an instruction to merge. No UUID inputs, reconciliation rules, or publication safeguards are relaxed. A successful build clears any stale ambiguity report.

## Published BeastED contract

`output/current/master-index.json` points to an immutable versioned release. The versioned master index exposes `stationsUrl`, station/stream counts and genre/country/language group indexes.

Each station contains canonical identity, provider IDs, descriptive metadata, `streams`, `sources`, `sourceGroups`, `defaultStreamIndex`, and `defaultSource`. BeastED should display one station record, show its source groups as alternatives, and play the default stream unless the user chooses another source.

## Local checks

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m sources.state_cli validate --state-dir state
python scripts/build_radio_catalog.py --state-dir state
python scripts/validate_catalog.py
```

Pull-request validation runs offline regression tests. Scheduled/manual refresh restores provider snapshots, validates state, builds and validates the public catalogue, persists updated provider snapshots, and deploys the Pages artifact.

## Known future enhancement

Live stream probing and health-aware default selection are not yet implemented. Until then, “best” means highest configured ranking score, not proven currently playable. The existing alternatives remain available to BeastED regardless of the default.


## Source preservation and identity policy

Radsync distinguishes canonical station identity, playable stream identity, the upstream source record, and provider-qualified station identity. A station may have several provider IDs from one provider and many distinct stream endpoints. Provider IDs are retained as sorted arrays under sourceIds; stream-level sourceStationIds remains available for compatibility and traceability.

Every published stream carries a sourceRecords array. Each observation retains its provider, provider record ID where available, source/playlist URL, repository and playlist path where applicable, zero-based playlist entry index, original title, safe original URL, raw EXTINF text, raw playlist attributes, and ingestion timestamp. When multiple providers publish the same endpoint, the endpoint may be represented once, but every distinct source-record provenance item is retained.

Duplicate source records may be collapsed only when the endpoint is demonstrably the same. Different URLs, formats, bitrates, and provider observations remain available as stream alternatives. Identical names or URLs alone never prove that two station identities are the same. Name and metadata compatibility are supporting evidence only; exact provider-qualified IDs are stronger evidence. Ambiguous candidates remain separate and the existing canonical identity collision guard continues to fail closed.

GitHub playlist discovery processes every playlist file returned by the repository tree. A truncated tree or failed playlist fetch is treated as an incomplete provider refresh, so the existing last-good snapshot is protected rather than silently replacing it with partial data.

See docs/SOURCE_PRESERVATION_POLICY.md for the governing invariants and evidence rules.


## External provider-state storage

The external snapshot format, SQLite staging schema, provider-specific anomaly bounds, retention-aware `current/`, `previous/`, and `archive/` layout, separate `diagnostics/` prefix, and required GitHub Actions object-store configuration are documented in [docs/EXTERNAL_PROVIDER_STATE.md](docs/EXTERNAL_PROVIDER_STATE.md). The first successful external publish imports existing Git-hosted last-good snapshots; it does not synthesize missing historical provenance. Current and previous objects are protected from lifecycle expiration, archive and diagnostics use separate bounded lifecycle rules, and superseded objects are removed only after the corresponding manifest commit is pushed.
