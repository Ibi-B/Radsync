"""GitHub repository M3U discovery adapter for IPRD and radio-playlists."""
import aiohttp

from sources.m3u import parse_m3u
from sources.normalize import as_records


async def fetch_repository_playlists(
    session: aiohttp.ClientSession,
    repo_api_url: str,
    raw_base_url: str,
):
    async with session.get(repo_api_url, headers={"User-Agent": "Radsync/1.0", "Accept": "application/vnd.github+json"}) as response:
        response.raise_for_status()
        payload = await response.json(content_type=None)

    tree = as_records(payload, ("tree", "items", "data"))
    playlist_paths = []
    for item in tree:
        path = item.get("path") or item.get("name")
        kind = item.get("type") or item.get("kind")
        if path and (kind in (None, "blob", "file")) and str(path).lower().endswith((".m3u", ".m3u8")):
            playlist_paths.append(str(path))

    playlists = []
    for path in sorted(set(playlist_paths))[:100]:
        playlist_url = raw_base_url.rstrip("/") + "/" + path.lstrip("/")
        try:
            async with session.get(playlist_url, headers={"User-Agent": "Radsync/1.0"}) as response:
                if response.status != 200:
                    continue
                playlists.extend(parse_m3u(await response.text(), base_url=playlist_url))
        except aiohttp.ClientError:
            continue

    return playlists
