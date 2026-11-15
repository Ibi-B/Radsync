"""Radio-Browser source adapter."""
import aiohttp

RADIO_BROWSER_SERVERS = [
    "https://de1.api.radio-browser.info",
    "https://de2.api.radio-browser.info",
    "https://fi1.api.radio-browser.info",
]

async def fetch_stations(session: aiohttp.ClientSession, limit: int = 10000):
    last_error = None
    for base in RADIO_BROWSER_SERVERS:
        url = f"{base}/json/stations/search?limit={limit}&hidebroken=true"
        try:
            async with session.get(url, headers={"User-Agent": "Radsync/0.1"}) as response:
                response.raise_for_status()
                return await response.json(content_type=None)
        except Exception as error:
            last_error = error
    raise RuntimeError(f"All Radio-Browser servers failed: {last_error}")
