"""GitHub repository M3U discovery adapter for IPRD and radio-playlists."""
import aiohttp

from sources.m3u import parse_m3u

async def fetch_repository_playlists(
    session: aiohttp.ClientSession,
    repo_api_url: str,
    raw_base_url: str,
):
    async with session.get(repo_api_url, headers={"User-Agent": "Radsync/1.0"}) as response:
        response.raise_for_status()
        tree = await response.json(content_type=None)

    playlist_paths = [
        item["path"]
        for item in tree.get("tree", [])
        if item.get("type") == "blob"
        and item.get("path", "").lower().endswith((".m3u", ".m3u8"))
    ]

    playlists = []
    for path in playlist_paths[:50]:
        playlist_url = raw_base_url + path
        async with session.get(playlist_url, headers={"User-Agent": "Radsync/1.0"}) as response:
            if response.status != 200:
                continue
            playlists.extend(parse_m3u(await response.text()))

    return playlists
