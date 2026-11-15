#!/usr/bin/env python3
"""Builds a versioned, atomic radio catalog from configured sources."""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from sources.radio_browser import fetch_stations
from sources.iptv_org import fetch_playlist, parse_m3u
from sources.icecast import fetch_directory

OUTPUT = Path("output")


def normalize_radio_browser(raw):
    return {
        "stationUuid": raw.get("stationuuid") or raw.get("id"),
        "name": raw.get("name", "").strip(),
        "country": raw.get("country") or "",
        "countryCode": raw.get("countrycode") or "",
        "language": raw.get("language") or "",
        "tags": [tag.strip() for tag in (raw.get("tags") or "").split(",") if tag.strip()],
        "codec": raw.get("codec") or "",
        "bitrate": raw.get("bitrate") or 0,
        "streamUrl": raw.get("url_resolved") or raw.get("url") or "",
        "logoUrl": raw.get("favicon") or "",
        "source": "radio-browser",
        "status": "unknown",
    }


def normalize_iptv_org(raw):
    return {
        "stationUuid": f"iptv-org:{raw.get('name', '')}:{raw.get('stream_url', '')}",
        "name": raw.get("name", "").strip(),
        "country": "",
        "countryCode": "",
        "language": "",
        "tags": [],
        "codec": "",
        "bitrate": 0,
        "streamUrl": raw.get("stream_url", ""),
        "logoUrl": "",
        "source": "iptv-org",
        "status": "unknown",
    }


def normalize_icecast(raw):
    return {
        "stationUuid": f"icecast:{raw.get('name', '')}:{raw.get('stream_url', '')}",
        "name": raw.get("name", "").strip(),
        "country": "",
        "countryCode": "",
        "language": "",
        "tags": raw.get("genres", []),
        "codec": raw.get("codec", ""),
        "bitrate": raw.get("bitrate", 0),
        "streamUrl": raw.get("stream_url", ""),
        "logoUrl": "",
        "source": "icecast",
        "status": "unknown",
    }


async def main():
    version = int(datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"))
    version_dir = OUTPUT / f"v{version}"
    (version_dir / "genres").mkdir(parents=True, exist_ok=True)
    (version_dir / "countries").mkdir(parents=True, exist_ok=True)
    (version_dir / "languages").mkdir(parents=True, exist_ok=True)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
        radio_browser = await fetch_stations(session)
        iptv_org = parse_m3u(await fetch_playlist(session))
        await fetch_directory(session)

    stations = [normalize_radio_browser(item) for item in radio_browser]
    stations += [normalize_iptv_org(item) for item in iptv_org]

    stations = [station for station in stations if station["name"] and station["streamUrl"]]
    unique = {}
    for station in stations:
        key = station["stationUuid"] or f"{station['name']}|{station['streamUrl']}"
        unique[key] = station
    stations = list(unique.values())

    genre_groups = {}
    country_groups = {}
    language_groups = {}

    for station in stations:
        for tag in station["tags"] or ["uncategorized"]:
            genre_groups.setdefault(tag.lower(), []).append(station)
        country = station["countryCode"] or "unknown"
        country_groups.setdefault(country.lower(), []).append(station)
        language = station["language"] or "unknown"
        language_groups.setdefault(language.lower(), []).append(station)

    def write_group(directory, groups):
        items = []
        for group_id, group_stations in sorted(groups.items()):
            path = directory / f"{group_id}.json"
            path.write_text(json.dumps({"id": group_id, "version": version, "stations": group_stations}, ensure_ascii=False), encoding="utf-8")
            items.append({"id": group_id, "count": len(group_stations), "url": f"{directory.name}/{group_id}.json"})
        return items

    genres = write_group(version_dir / "genres", genre_groups)
    countries = write_group(version_dir / "countries", country_groups)
    languages = write_group(version_dir / "languages", language_groups)

    master = {
        "version": version,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "stationCount": len(stations),
        "groups": {
            "genres": {"url": "groups/genres.json", "items": genres},
            "countries": {"url": "groups/countries.json", "items": countries},
            "languages": {"url": "groups/languages.json", "items": languages},
        },
    }
    (version_dir / "master-index.json").write_text(json.dumps(master, ensure_ascii=False), encoding="utf-8")

    groups_dir = version_dir / "groups"
    groups_dir.mkdir(exist_ok=True)
    (groups_dir / "genres.json").write_text(json.dumps({"type": "genres", "version": version, "items": genres}, ensure_ascii=False), encoding="utf-8")
    (groups_dir / "countries.json").write_text(json.dumps({"type": "countries", "version": version, "items": countries}, ensure_ascii=False), encoding="utf-8")
    (groups_dir / "languages.json").write_text(json.dumps({"type": "languages", "version": version, "items": languages}, ensure_ascii=False), encoding="utf-8")

    current = OUTPUT / "current"
    current.mkdir(exist_ok=True)
    pointer = {"version": version, "masterIndex": f"v{version}/master-index.json"}
    (current / "master-index.json").write_text(json.dumps(pointer, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
