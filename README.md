# Radsync

Radsync is the radio catalogue aggregation and publication service for BeastED. It performs provider collection, normalization, station reconciliation, stream ranking and genre/country/language grouping outside the app. BeastED consumes the published static JSON catalogue; playback remains in BeastED.

## Core behavior

- A station is a canonical catalogue record, not a single stream URL.
- Duplicate station entries from different providers are reconciled when their names and available country/language metadata are compatible.
- Conflicting country or language identities are kept separate rather than being merged by name alone.
- Every distinct HTTP(S) stream URL is retained as a selectable variant.
- If multiple providers advertise the same URL, the URL is stored once while its provider provenance is retained.
- Provider alternatives are exposed through `sources` and `sourceGroups`; each source group points to stream indexes in the station's `streams` array.
- Streams are ranked by configured provider priority plus a bounded bitrate contribution. The highest-ranked stream is at index `0`, with `defaultStreamIndex: 0` and `defaultSource` identifying the automatic default.
- Invalid or empty stream URLs are excluded from published stations.
- Source failures are isolated and reported; a successful provider is not blocked by another provider's failure.

## Published data contract

The `current/master-index.json` pointer identifies an immutable versioned release:

- `masterIndex`: relative path to the version's `master-index.json`
- `version`: release identifier

The versioned master index exposes:

- `stationsUrl`: the complete normalized station catalogue
- `stationCount` and `streamCount`
- `groups.genres`, `groups.countries`, and `groups.languages`: indexes and group-file locations

Each station includes its canonical `stationUuid`, source-specific IDs where available, metadata, `streams`, `defaultStreamIndex`, `defaultSource`, and source-group index references. A client can render one station once, show the provider groups as alternatives, and play the default stream without discarding the alternatives.

## Sources

Sources and their priority are configured in `config/sources.json`. Priority is an explicit ranking input, not a live uptime guarantee. Stream status is currently reported as unknown unless a later availability-checking stage is added.

## Local checks

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python scripts/build_radio_catalog.py
python scripts/validate_catalog.py
```

The pull-request validation workflow runs the offline regression suite. The scheduled/manual refresh workflow builds a fresh catalogue, validates the generated release, and deploys it to GitHub Pages.

## Important limitation

Radsync currently isolates failed sources and refuses to replace the current pointer when the build produces no stations. It does not yet persist a last-good snapshot per provider across workflow runs. That durability mechanism should be added before treating provider outages as fully recoverable.
