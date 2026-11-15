"""Icecast Yellow Pages adapter with namespace- and field-name-tolerant parsing."""
import re
import xml.etree.ElementTree as ET

import aiohttp


async def fetch_directory(session: aiohttp.ClientSession, url: str):
    async with session.get(url, headers={"User-Agent": "Radsync/1.0"}) as response:
        response.raise_for_status()
        return await response.text()


def _local_name(tag):
    return tag.rsplit("}", 1)[-1].casefold().replace("-", "_")


def _child_text(entry, *names):
    accepted = {name.casefold().replace("-", "_") for name in names}
    for node in entry.iter():
        if _local_name(node.tag) in accepted and node.text and node.text.strip():
            return node.text.strip()
    return ""


def parse_directory(xml_text: str):
    root = ET.fromstring(xml_text)
    stations = []
    for entry in root.iter():
        if _local_name(entry.tag) not in {"entry", "station", "server"}:
            continue
        name = _child_text(entry, "server_name", "name", "title")
        stream_url = _child_text(entry, "listen_url", "listenurl", "stream_url", "url")
        if not name or not stream_url:
            continue
        genre = _child_text(entry, "genre", "genres", "category")
        bitrate = _child_text(entry, "bitrate", "bit_rate", "kbps")
        stations.append({
            "name": name,
            "stream_url": stream_url,
            "genres": [item.strip() for item in re.split(r"[,;]", genre) if item.strip()],
            "codec": _child_text(entry, "codec", "server_type", "content_type"),
            "bitrate": int(bitrate) if bitrate.isdigit() else 0,
            "country": _child_text(entry, "country", "server_country"),
            "country_code": _child_text(entry, "country_code", "countrycode"),
            "language": _child_text(entry, "language", "server_language"),
        })
    return stations
