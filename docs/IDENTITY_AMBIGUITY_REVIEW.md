# Review of the 12 original identity-ambiguity groups

## Source and limits

This review is grounded in `radsync-state/diagnostics/identity-ambiguity.json`, generated **2026-11-15T23:21:15.992814+00:00** for catalogue version **20261115232041180000**. The full source records are preserved in [`tests/fixtures/identity_ambiguity_groups.json`](../tests/fixtures/identity_ambiguity_groups.json) as regression fixtures.

This diagnostic predates the source-preservation PR. It contains no provider station IDs, no playlist repository/file path, no entry index, no raw EXTINF, and no original playlist metadata. Country, country code, and language are blank throughout. Consequently, it can identify repeated observations and endpoint patterns, but it cannot prove canonical station identity. The PR's new provenance fields cannot retroactively fill gaps in this historical snapshot.

“Distinct endpoints” below means distinct exact URL strings in the diagnostic. It is not a claim that URL variants have been normalized or that different URLs are different stations.

## Group-by-group evidence review

| Group | Candidate records | Exact URL strings | Providers | Recommended station action | Evidence-based assessment |
|---|---:|---:|---|---|
| Atlantica Oldies | 2 | 1 | IPRD | Needs manual evidence | Same provider and exact endpoint repeated. Likely duplicate source observations; retain both provenance observations. Canonical station merge still requires source-entry evidence. |
| Magic Radio | 9 | 7 | Icecast, IPRD | Needs manual evidence | Several distinct stream endpoints/formats plus two repeated IPRD endpoint observations. Preserve all seven URL variants; do not collapse the station records solely by name. |
| Radio Panamericana | 3 | 2 | IPRD, Alon Drilich | Needs manual evidence | IPRD endpoint repeated; the second provider has a different Zeno URL. Keep both endpoints; relationship between the two provider records remains unproven. |
| J-Club Powerplay HipHop | 3 | 1 | IPRD, Alon Drilich | Needs manual evidence | Same exact endpoint across providers, with a repeated Alon observation. Endpoint equivalence is strong; canonical station equivalence still needs upstream identity/provenance. |
| Capital Radio | 4 | 2 | IPRD, Alon Drilich | Needs manual evidence | Two exact URLs are cross-listed across providers; one IPRD URL repeats. “Capital Radio” is generic and there are no provider IDs or metadata to prove identity. Preserve both endpoints and defer station merge. |
| Radio Regenbogen Guns N' Roses | 2 | 1 | IPRD | Needs manual evidence | Same provider and exact endpoint repeated. Likely duplicate source observations; preserve both source records until playlist entry provenance confirms their origin. |
| Top FM | 2 | 1 | IPRD | Needs manual evidence | Same provider and exact endpoint repeated. Likely duplicate source observations; retain provenance and do not alter canonical identity yet. |
| Delta FM | 3 | 2 | IPRD, Alon Drilich | Needs manual evidence | IPRD endpoint repeats; Alon supplies a different endpoint. Preserve both endpoints; same-name cross-provider identity is not established. |
| Métis FM | 2 | 1 | IPRD | Needs manual evidence | Same provider and exact endpoint repeated. Likely duplicate source observations; retain both provenance records. |
| Radio Radio | 3 | 2 | IPRD | Needs manual evidence | One IPRD endpoint repeats and another distinct endpoint exists. Preserve both endpoint alternatives; do not treat the second URL as a new station or as a confirmed variant without provider evidence. |
| GHERLA | 3 | 1 | IPRD, Alon Drilich | Needs manual evidence | Same exact endpoint appears under both providers, with a repeated IPRD observation. Strong endpoint overlap, but no station IDs or metadata establish the cross-provider relationship. |
| J-Pop Powerplay | 3 | 2 | IPRD, Alon Drilich | Needs manual evidence | Alon endpoint repeats. IPRD and Alon URLs share host/port/path but differ by HTTP/HTTPS; preserve both URL variants until endpoint equivalence and station identity are confirmed. |

**Disposition for all 12 groups: needs manual evidence for canonical station identity.** Some repeated same-provider/same-URL observations are likely redundant source records, but the old diagnostic lacks playlist path and entry index, so even those cannot yet be proven to be duplicate playlist entries. Once provenance is available, consolidate only exact duplicate endpoint observations while retaining all source-record references. Do not collapse distinct URLs, formats, or provider observations.

## What can be concluded now

- All 12 groups contain at least one repeated source/endpoint fingerprint.
- The evidence strongly suggests redundant **source observations** exist, but that is not the same as proving duplicate **station identities**.
- Magic Radio, Radio Panamericana, Capital Radio, Delta FM, Radio Radio, and J-Pop Powerplay have multiple exact URL strings that must remain represented.
- J-Club Powerplay HipHop and GHERLA have cross-provider observations of the same exact URL. The historical diagnostic has no country, language, call-sign, or website evidence, so these records still do not meet the current cross-provider confidence gate.
- No group has usable provider station IDs or playlist-entry provenance in this historical diagnostic. No canonical merge should be authorized from this file alone.

## Catalog identity policy

An unresolved identity collision is **not a reason to block the catalogue build or discard a record**.

- Merge source observations into alternate streams when either the provider-qualified station ID matches or a high-confidence cross-provider match is established by exact normalized name, compatible geography/language, and a shared stream endpoint, official website, or call sign. Names, genres, or URLs in isolation are not sufficient.
- When provider IDs or playlist-entry provenance distinguish aggregates, use that stable evidence to generate separate deterministic station IDs.
- When two aggregates remain completely indistinguishable, retain both with unique occurrence-qualified IDs and emit a diagnostic. This preserves the source record count without claiming the two records are the same station.
- The occurrence suffix is a deterministic fallback for otherwise identical rows; without an upstream ID or entry key, no system can reliably tell which physical station an identical row represents after the upstream order changes. The fallback preserves both rows rather than silently merging or deleting either one.

The intended trade-off is explicit: **a possible duplicate is safer than losing a potentially different station**. Strong cross-provider evidence can establish a canonical station even when provider IDs differ; otherwise, preserve the candidates for later review.

## Automatic matching gate

Station matching uses a confidence hierarchy:

1. An exact provider-qualified identity key `(provider, provider station ID)` is the strongest match.
2. Cross-provider records can also be reconciled when the normalized station name matches, country/language metadata is compatible, and there is a strong independent identity anchor. Anchors include the same normalized stream endpoint, call sign, or official website.
3. When stream URLs differ, matching call sign or official website plus matching geography/language can establish a strong cross-provider match. This allows multiple bitrate, codec, and server variants to remain streams under one station.
4. A shared URL is not sufficient when the known call signs conflict. Country/language contradictions also block matching.
5. Matching names, country, language, genres, or provider IDs from different providers alone are not sufficient. Weak or tied candidate matches remain separate and are recorded for review.

Provider IDs are scoped to their provider; different IDs across providers do not prove different stations. Stream URLs, codecs, and bitrates describe stream offerings, not canonical station identity. Query tokens and URL fragments are ignored when comparing stream endpoint identity, but the original stream URLs are retained for playback. Where evidence is inconclusive, preserve each aggregate with its own station ID rather than silently merging or dropping it.

## Required next evidence

After PR #10's provenance changes are deployed to the state-refresh path, capture a new diagnostic containing provider IDs, playlist repository/path, entry index, raw EXTINF, original title/URL, and source timestamps. Compare each repeated observation by its actual playlist file and entry index. Only then classify an observation as an exact duplicate playlist entry, a distinct stream variant, or an unresolved station candidate.

## Reconciliation disposition

**Do not implement automatic station merges for any of these 12 groups yet.** Keep each unresolved aggregate as a separate catalogue station with a unique ID. Preserve all endpoint alternatives and source records. The canonical collision guard now assigns occurrence-qualified IDs and records a diagnostic instead of failing the build; it does not merge or delete indistinguishable candidates.
