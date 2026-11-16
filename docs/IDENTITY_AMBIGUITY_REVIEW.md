# Review of the 12 original identity-ambiguity groups

## Source and limits

This review is grounded in `radsync-state/diagnostics/identity-ambiguity.json`, generated **2026-11-15T23:21:15.992814+00:00** for catalogue version **20261115232041180000**. The full source records are preserved in [`tests/fixtures/identity_ambiguity_groups.json`](../tests/fixtures/identity_ambiguity_groups.json) as regression fixtures.

This diagnostic predates the source-preservation PR. It contains no provider station IDs, no playlist repository/file path, no entry index, no raw EXTINF, and no original playlist metadata. Country, country code, and language are blank throughout. Consequently, it can identify repeated observations and endpoint patterns, but it cannot prove canonical station identity. The PR's new provenance fields cannot retroactively fill gaps in this historical snapshot.

“Distinct endpoints” below means distinct exact URL strings in the diagnostic. It is not a claim that URL variants have been normalized or that different URLs are different stations.

## Group-by-group evidence review

| Group | Candidate records | Exact URL strings | Providers | Evidence-based assessment |
|---|---:|---:|---|---|
| Atlantica Oldies | 2 | 1 | IPRD | Same provider and exact endpoint repeated. Likely duplicate source observations; retain both provenance observations. Canonical station merge still requires source-entry evidence. |
| Magic Radio | 9 | 7 | Icecast, IPRD | Several distinct stream endpoints/formats plus two repeated IPRD endpoint observations. Preserve all seven URL variants; do not collapse the station records solely by name. |
| Radio Panamericana | 3 | 2 | IPRD, Alon Drilich | IPRD endpoint repeated; the second provider has a different Zeno URL. Keep both endpoints; relationship between the two provider records remains unproven. |
| J-Club Powerplay HipHop | 3 | 1 | IPRD, Alon Drilich | Same exact endpoint across providers, with a repeated Alon observation. Endpoint equivalence is strong; canonical station equivalence still needs upstream identity/provenance. |
| Capital Radio | 4 | 2 | IPRD, Alon Drilich | Two exact URLs are cross-listed across providers; one IPRD URL repeats. “Capital Radio” is generic and there are no provider IDs or metadata to prove identity. Preserve both endpoints and defer station merge. |
| Radio Regenbogen Guns N' Roses | 2 | 1 | IPRD | Same provider and exact endpoint repeated. Likely duplicate source observations; preserve both source records until playlist entry provenance confirms their origin. |
| Top FM | 2 | 1 | IPRD | Same provider and exact endpoint repeated. Likely duplicate source observations; retain provenance and do not alter canonical identity yet. |
| Delta FM | 3 | 2 | IPRD, Alon Drilich | IPRD endpoint repeats; Alon supplies a different endpoint. Preserve both endpoints; same-name cross-provider identity is not established. |
| Métis FM | 2 | 1 | IPRD | Same provider and exact endpoint repeated. Likely duplicate source observations; retain both provenance records. |
| Radio Radio | 3 | 2 | IPRD | One IPRD endpoint repeats and another distinct endpoint exists. Preserve both endpoint alternatives; do not treat the second URL as a new station or as a confirmed variant without provider evidence. |
| GHERLA | 3 | 1 | IPRD, Alon Drilich | Same exact endpoint appears under both providers, with a repeated IPRD observation. Strong endpoint overlap, but no station IDs or metadata establish the cross-provider relationship. |
| J-Pop Powerplay | 3 | 2 | IPRD, Alon Drilich | Alon endpoint repeats. IPRD and Alon URLs share host/port/path but differ by HTTP/HTTPS; preserve both URL variants until endpoint equivalence and station identity are confirmed. |

## What can be concluded now

- All 12 groups contain at least one repeated source/endpoint fingerprint.
- The evidence strongly suggests redundant **source observations** exist, but that is not the same as proving duplicate **station identities**.
- Magic Radio, Radio Panamericana, Capital Radio, Delta FM, Radio Radio, and J-Pop Powerplay have multiple exact URL strings that must remain represented.
- J-Club Powerplay HipHop and GHERLA have cross-provider observations of the same exact URL; this is endpoint evidence, not sufficient station-identity proof.
- No group has usable provider station IDs or playlist-entry provenance in this historical diagnostic. No canonical merge should be authorized from this file alone.

## Required next evidence

After PR #10's provenance changes are deployed to the state-refresh path, capture a new diagnostic containing provider IDs, playlist repository/path, entry index, raw EXTINF, original title/URL, and source timestamps. Compare each repeated observation by its actual playlist file and entry index. Only then classify an observation as an exact duplicate playlist entry, a distinct stream variant, or an unresolved station candidate.

## Reconciliation disposition

**Do not implement automatic station merges for any of these 12 groups yet.** Endpoint-level duplicate observations can be consolidated only when their source-record provenance is retained. Distinct URLs/formats remain alternatives. Groups with insufficient identity evidence stay separate and continue to be blocked by the existing fail-closed canonical collision guard where applicable.
