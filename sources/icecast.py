"""Icecast Yellow Pages source adapter."""
import xml.etree.ElementTree as ET

import aiohttp

async def fetch_directory(session: aiohttp.ClientSession, url: str):
    async with session.get(url, headers={"User-Agent": "Radsync/1.0"}) as response:
        response.raise_for_status()
        return await response.text()


def parse_directory(xml_text: str):
    root = ET.fromstring(xml_text)
    stations = []

    for entry in root.findall(".//entry"):
        def text(tag):
            node = entry.find(tag)
            return node.text.strip() if node is not None and node.text else ""

        name = text("server_name")
        stream_url = text("listen_url")
        if not name or not stream_url:
            continue

        stations.append({
            "name": name,
            "stream_url": stream_url,
            "genres": [genre.strip() for genre in text("genre").split(",") if genre.strip()],
            "codec": text("codec"),
            "bitrate": int(text("bitrate")) if text("bitrate").isdigit() else 0,
        })

    return stations
