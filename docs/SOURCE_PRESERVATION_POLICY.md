# Radsync source-preservation policy

## Governing principle

Radsync must maximize the preservation of credible station sources while minimizing incorrect identity merges. It must never sacrifice source completeness merely to reduce catalogue size or make publication succeed.

A failed or ambiguous reconciliation preserves the status quo: it must not force a merge, delete an alternative, or publish an unstable identity.

## Identity model

- **Station identity** identifies the editorial/listening entity.
- **Stream identity** identifies a concrete playable endpoint and its format/bitrate.
- **Source record** identifies the upstream observation that described a station or endpoint.
- **Provider identity** identifies the provider-qualified station ID.

These are separate concepts. One station can have multiple provider IDs and many streams. A shared endpoint can have multiple source records.

## Mandatory invariants

1. Never overwrite provider identities. Retain every provider ID per provider.
2. Never lose source provenance. Preserve provider, original record, playlist repository/path, entry index, original title/URL, and raw metadata where available.
3. Never infer station identity from a name alone.
4. Never discard stream alternatives because another stream ranks higher. Ranking selects a default only.
5. Never publish unresolved canonical identity collisions. Keep uncertain candidates separate and preserve the fail-closed guard.
6. Never treat a non-empty provider response as proof of complete provider coverage.
7. Never let stream-health checks delete sources. Health may annotate or rank; it must not change existence.

## Evidence hierarchy

| Evidence | Meaning | Permitted action |
|---|---|---|
| Exact provider-qualified station ID | Strong station-identity evidence | Prefer aggregation when metadata is compatible and the match is unique |
| Same provider and same ID across records | Strong provider continuity | Preserve distinct endpoints; collapse only proven duplicate records |
| Same normalized stream URL | Duplicate-endpoint evidence, not station identity proof | Consolidate endpoint representation only after station aggregation; retain every source record |
| Same normalized name plus compatible country/language/genre | Supporting evidence only | Never merge solely on this basis |
| Same name with conflicting metadata | Conflict | Keep separate |
| Multiple plausible candidates | Ambiguous | Keep separate and diagnose |
| Changed stream URL with stable provider ID | Likely endpoint change | Preserve station continuity; retain new endpoint |
| Changed provider ID or URL without corroboration | Insufficient evidence | Do not force identity continuity |

## Duplicate semantics

- Same provider, same station ID, same endpoint and equivalent metadata: one endpoint/source observation may be consolidated, but provenance remains.
- Same provider and station ID with different endpoints or formats: retain every endpoint.
- Different providers reporting the same station and same endpoint: one endpoint may carry multiple source records.
- Different providers reporting distinct endpoints: retain all endpoints.
- Similar names with unknown relationship: keep separate.
- Same endpoint claimed by conflicting station identities: flag; do not silently merge stations.

## Stage 1 and 2 exit criteria

- sourceIds retains a sorted array of every ID per provider; stream-level IDs remain intact.
- Playlist adapters preserve repository, file path, entry index, raw EXTINF, original title/URL, station ID, and source URL.
- Every published stream exposes its source-record provenance.
- Truncated playlist discovery or failed playlist retrieval cannot replace a provider's last-good snapshot with partial data.
- Regression tests prove multi-ID retention, exact-ID matching, provenance retention on shared endpoints, and M3U metadata retention.
- No identity-matching thresholds or canonical UUID fail-closed rules are weakened in these stages.
