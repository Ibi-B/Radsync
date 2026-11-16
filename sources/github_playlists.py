"""GitHub repository M3U discovery adapter for IPRD and radio-playlists."""
import asyncio
from urllib.parse import urlsplit

import aiohttp

from sources.m3u import parse_m3u
from sources.normalize import as_records


MIN_PLAYLIST_COVERAGE = 0.80


async def fetch_repository_playlists(
    session: aiohttp.ClientSession,
    repo_api_url: str,
    raw_base_url: str,
    report=None,
):
    """Fetch every discovered playlist; tolerate isolated failures with explicit coverage."""
    metrics = report if report is not None else {}
    metrics.clear()
    metrics.update({
        "discovered": 0,
        "processed": 0,
        "retrieved": 0,
        "succeeded": 0,
        "failed": 0,
        "skipped": 0,
        "parsedSuccessfully": 0,
        "validEmpty": 0,
        "unparseable": 0,
        "recordsProduced": 0,
        "failedPlaylists": [],
        "skippedPlaylists": [],
        "validEmptyPlaylists": [],
        "minimumCoverageRatio": MIN_PLAYLIST_COVERAGE,
        "minimumParseCoverageRatio": MIN_PLAYLIST_COVERAGE,
        "coverageRatio": 0.0,
        "parseCoverageRatio": 0.0,
        "recordProductionCoverageRatio": 0.0,
    })

    # Failure to retrieve or decode the repository tree is systemic: never use
    # an incomplete listing as the basis for replacing a provider snapshot.
    async with session.get(
        repo_api_url,
        headers={"User-Agent": "Radsync/1.0", "Accept": "application/vnd.github+json"},
    ) as response:
        response.raise_for_status()
        payload = await response.json(content_type=None)

    if isinstance(payload, dict) and payload.get("truncated") is True:
        raise ValueError("GitHub playlist tree is truncated; refusing incomplete provider snapshot")

    tree = as_records(payload, ("tree", "items", "data"))
    playlist_paths = []
    for item in tree:
        path = item.get("path") or item.get("name")
        kind = item.get("type") or item.get("kind")
        if path and kind in (None, "blob", "file") and str(path).lower().endswith((".m3u", ".m3u8")):
            playlist_paths.append(str(path))

    playlist_paths = sorted(set(playlist_paths))
    metrics["discovered"] = len(playlist_paths)
    if not playlist_paths:
        raise ValueError("GitHub repository tree contained no discoverable playlists")

    raw_parts = [part for part in urlsplit(raw_base_url).path.split("/") if part]
    repository_path = "/".join(raw_parts[:2])[:200]
    playlists = []

    for path in playlist_paths:
        metrics["processed"] += 1
        playlist_url = raw_base_url.rstrip("/") + "/" + path.lstrip("/")
        try:
            async with session.get(
                playlist_url, headers={"User-Agent": "Radsync/1.0"}
            ) as response:
                response.raise_for_status()
                text = await response.text()
            records = parse_m3u(text, base_url=playlist_url)
        except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeError, ValueError) as error:
            status = getattr(error, "status", None)
            metrics["failed"] += 1
            metrics["failedPlaylists"].append({
                "path": path,
                "error": str(error)[:500],
                "status": status,
            })
            # Authentication, authorization and rate limiting are provider-wide
            # conditions, not isolated missing playlist files.
            if status in (401, 403, 429):
                raise RuntimeError(
                    f"Systemic GitHub playlist access failure ({status}) at {path}"
                ) from error
            continue

        metrics["retrieved"] += 1
        if not records:
            # M3U has no universal marker that distinguishes an intentional
            # empty playlist from truncation or a parser regression. Preserve
            # last-good records until an upstream-specific marker is defined.
            metrics["skipped"] += 1
            metrics["unparseable"] += 1
            metrics["skippedPlaylists"].append({
                "path": path,
                "classification": "unparseable",
                "reason": "No named stream records; the current M3U format provides no explicit valid-empty marker",
            })
            continue

        metrics["succeeded"] += 1
        metrics["parsedSuccessfully"] += 1
        metrics["recordsProduced"] += len(records)
        for record in records:
            record["playlist_path"] = path
            record["playlist_url"] = playlist_url
            record["playlist_repository"] = repository_path
            record["source_url"] = playlist_url
            record["original_title"] = record.get("name", "")
            record["original_url"] = record.get("stream_url", "")
            record["raw_metadata"] = record.get("attributes", {})
        playlists.extend(records)

    metrics["coverageRatio"] = round(
        (metrics["discovered"] - metrics["failed"]) / metrics["discovered"], 4
    )
    metrics["parseCoverageRatio"] = round(
        metrics["parsedSuccessfully"] / metrics["discovered"], 4
    )
    metrics["recordProductionCoverageRatio"] = round(
        metrics["succeeded"] / metrics["discovered"], 4
    )
    if metrics["retrieved"] == 0:
        raise ValueError("No GitHub playlists could be retrieved; protecting last-good snapshot")
    if metrics["coverageRatio"] < MIN_PLAYLIST_COVERAGE:
        raise ValueError(
            "GitHub playlist retrieval coverage collapsed: "
            f"{metrics['coverageRatio']:.1%} retrieved, "
            f"{MIN_PLAYLIST_COVERAGE:.0%} required"
        )
    if metrics["parseCoverageRatio"] < MIN_PLAYLIST_COVERAGE:
        raise ValueError(
            "GitHub playlist parse coverage collapsed: "
            f"{metrics['parseCoverageRatio']:.1%} produced named records, "
            f"{MIN_PLAYLIST_COVERAGE:.0%} required"
        )

    return playlists
