"""Icecast directory source adapter."""
import xml.etree.ElementTree as ET

import aiohttp

ICECAST_URL = "https://dir.xiph.org/yp.xml"


async def fetch_directory(session: aiohttp.ClientSession):
    async with session.get(ICECAST_URL, headers={"User-Agent": "Radsync/0.1"}) as response:
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

        genres = [genre.strip() for genre in text("genre").split(",") if genre.strip()]
        bitrate_text = text("bitrate")
        bitrate = int(bitrate_text) if bitrate_text.isdigit() else 0

        stations.append({
            "name": name,
            "stream_url": stream_url,
            "genres": genres,
            "codec": text("codec") or "",
            "bitrate": bitrate,
        })

    return stations
