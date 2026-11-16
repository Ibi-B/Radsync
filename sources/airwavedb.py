"""AirwaveDB source adapter."""
import aiohttp

async def fetch_catalog(session: aiohttp.ClientSession, stations_url: str, streams_url: str):
    async with session.get(stations_url, headers={"User-Agent": "Radsync/1.0"}) as response:
        response.raise_for_status()
        stations = await response.json(content_type=None)

    async with session.get(streams_url, headers={"User-Agent": "Radsync/1.0"}) as response:
        response.raise_for_status()
        streams = await response.json(content_type=None)

    return stations, streams
