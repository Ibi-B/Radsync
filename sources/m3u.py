"""Tolerant M3U/M3U8 parser supporting common EXTINF attribute variations."""
import re
from urllib.parse import urljoin


ATTRIBUTE_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')


def parse_m3u(text: str, base_url: str = ""):
    stations = []
    current = None
    entry_index = 0

    for raw_line in (text or "").lstrip("\ufeff").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("#EXTINF:"):
            metadata = line.split(",", 1)[1].strip() if "," in line else ""
            attributes = {key.lower(): value for key, value in ATTRIBUTE_RE.findall(line)}
            duration = line.split(":", 1)[1].split(",", 1)[0].strip()
            current = {
                "entry_index": entry_index, "raw_extinf": line[:500],
                "name": metadata or attributes.get("tvg-name") or attributes.get("name") or "",
                "attributes": attributes, "raw_metadata": dict(attributes),
                "duration": duration, "country": attributes.get("tvg-country", ""),
                "language": attributes.get("tvg-language", ""),
                "genres": [attributes.get("group-title", "")] if attributes.get("group-title") else [],
                "logo": attributes.get("tvg-logo", ""), "station_id": attributes.get("tvg-id", ""),
            }
            entry_index += 1
        elif line.startswith("#"):
            continue
        elif current is not None:
            current["stream_url"] = urljoin(base_url, line) if base_url else line
            current["original_title"] = current.get("name", "")
            current["original_url"] = current["stream_url"]
            stations.append(current)
            current = None
        elif line.startswith(("http://", "https://")):
            stations.append({"name": "", "stream_url": line, "attributes": {}})
    return [station for station in stations if station.get("name") and station.get("stream_url")]
