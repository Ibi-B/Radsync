"""Radio-Browser source adapter."""
import aiohttp

async def fetch_stations(session: aiohttp.ClientSession, url: str):
    async with session.get(url, headers={"User-Agent": "Radsync/1.0"}) as response:
        response.raise_for_status()
        return await response.json(content_type=None)
