"""IPTV-org radio playlist source adapter."""
import aiohttp

IPTV_ORG_RADIO_URL = "https://iptv-org.github.io/iptv/radio.m3u"

async def fetch_playlist(session: aiohttp.ClientSession):
    async with session.get(IPTV_ORG_RADIO_URL, headers={"User-Agent": "Radsync/0.1"}) as response:
        response.raise_for_status()
        return await response.text()


def parse_m3u(text: str):
    stations = []
    current = {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF:"):
            metadata = line.split(",", 1)[-1]
            current = {"name": metadata, "attributes": {}}
        elif line and not line.startswith("#"):
            if current:
                current["stream_url"] = line
                stations.append(current)
                current = {}
    return stations
