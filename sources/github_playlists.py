"""GitHub repository M3U discovery adapter for IPRD and radio-playlists."""
from urllib.parse import urlsplit

import aiohttp

from sources.m3u import parse_m3u
from sources.normalize import as_records


async def fetch_repository_playlists(
    session: aiohttp.ClientSession, repo_api_url: str, raw_base_url: str,
):
    async with session.get(repo_api_url, headers={"User-Agent": "Radsync/1.0", "Accept": "application/vnd.github+json"}) as response:
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

    raw_parts = [part for part in urlsplit(raw_base_url).path.split("/") if part]
    repository_path = "/".join(raw_parts[:2])[:200]
    playlists = []
    for path in sorted(set(playlist_paths)):
        playlist_url = raw_base_url.rstrip("/") + "/" + path.lstrip("/")
        async with session.get(playlist_url, headers={"User-Agent": "Radsync/1.0"}) as response:
            response.raise_for_status()
            records = parse_m3u(await response.text(), base_url=playlist_url)
        for record in records:
            record["playlist_path"] = path
            record["playlist_url"] = playlist_url
            record["playlist_repository"] = repository_path
            record["source_url"] = playlist_url
            record["original_title"] = record.get("name", "")
            record["original_url"] = record.get("stream_url", "")
            record["raw_metadata"] = record.get("attributes", {})
        playlists.extend(records)
    return playlists
