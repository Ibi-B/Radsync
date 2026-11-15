"""Icecast directory source adapter."""
import aiohttp

ICECAST_URL = "https://dir.xiph.org/yp.xml"

async def fetch_directory(session: aiohttp.ClientSession):
    async with session.get(ICECAST_URL, headers={"User-Agent": "Radsync/0.1"}) as response:
        response.raise_for_status()
        return await response.text()
