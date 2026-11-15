#!/usr/bin/env python3
"""Builds a versioned, atomic multi-source radio catalog.

Duplicate stations are retained as multiple selectable stream variants.
The highest-quality available stream is selected as the default.
"""
import argparse
import asyncio
import json
import re
import unicodedata
from urllib.parse import urlsplit
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

from sources.radio_browser import fetch_stations
from sources.airwavedb import fetch_catalog
from sources.icecast import fetch_directory, parse_directory
from sources.iptv_org_streams import fetch_streams
from sources.somafm import fetch_channels
from sources.github_playlists import fetch_repository_playlists
from sources.m3u import parse_m3u
from sources.normalize import as_records, get_value, as_tags, normalize_record
from sources.state import load_snapshot, save_snapshot

OUTPUT = Path("output")


def slug(value: str):
    value = unicodedata.normalize("NFKD", value or "")
    value = re.sub(r"[^\w]+", "-", value.lower()).strip("-")
    return value or "station"


def normalize_name(value: str):
    value = unicodedata.normalize("NFKD", value or "")
    value = re.sub(r"[^\w]+", " ", value.lower())
    return " ".join(value.split())


def valid_stream_url(value):
    try:
        parsed = urlsplit(str(value or "").strip())
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except ValueError:
        return False


def stream_sort_key(stream):
    return (float(stream.get("qualityScore", 0)), int(stream.get("priority", 0)),
            int(stream.get("bitrate", 0) or 0), str(stream.get("url", "")))


def refresh_station_sources(station):
    station["streams"].sort(key=stream_sort_key, reverse=True)
    station["defaultStreamIndex"] = 0 if station["streams"] else None
    station["defaultSource"] = station["streams"][0]["source"] if station["streams"] else None
    grouped = {}
    for index, stream in enumerate(station["streams"]):
        for source in stream.get("sources", [stream["source"]]):
            grouped.setdefault(source, []).append(index)
    station["sources"] = list(grouped)
    station["sourceGroups"] = [
        {"source": source, "streamIndexes": indexes}
        for source, indexes in sorted(grouped.items())
    ]


def normalize_tags(tags):
    if isinstance(tags, str):
        tags = tags.split(",")
    if not isinstance(tags, (list, tuple, set)):
        return []
    normalized = {}
    for tag in tags:
        value = str(tag).strip()
        if value:
            normalized.setdefault(value.casefold(), value)
    return list(normalized.values())


def make_stream(source, url, codec="", bitrate=0, priority=50, source_station_id=None):
    try:
        bitrate = max(0, int(bitrate or 0))
    except (TypeError, ValueError):
        bitrate = 0
    quality = priority + min(bitrate / 10.0, 20.0)
    return {
        "source": source,
        "sources": [source],
        "sourceStationId": source_station_id,
        "sourceStationIds": [source_station_id] if source_station_id else [],
        "url": str(url or "").strip(),
        "codec": codec,
        "bitrate": bitrate,
        "priority": priority,
        "qualityScore": round(quality, 2),
        "status": "unknown",
    }


def make_station(name, country="", country_code="", language="", tags=None, streams=None):
    streams = [stream for stream in (streams or []) if valid_stream_url(stream.get("url"))]
    streams.sort(key=stream_sort_key, reverse=True)
    source_ids = {
        stream["source"]: stream["sourceStationId"]
        for stream in streams if stream.get("sourceStationId")
    }
    station = {
        "stationUuid": f"radsync:{slug(name)}:{slug(country_code or country or 'unknown')}:{slug(language or 'unknown')}",
        "sourceIds": source_ids,
        "name": str(name or "").strip(),
        "normalizedName": normalize_name(name),
        "country": country,
        "countryCode": country_code,
        "language": language,
        "tags": normalize_tags(tags),
        "streams": streams,
        "defaultStreamIndex": 0,
        "defaultSource": streams[0]["source"] if streams else None,
        "sources": [],
        "sourceGroups": [],
        "status": "unknown",
        "lastCheckedAt": None,
        "failureCount": 0,
    }
    refresh_station_sources(station)
    return station


def merge_station(target, incoming):
    existing_urls = {stream["url"] for stream in target["streams"]}

    existing_by_url = {stream["url"]: stream for stream in target["streams"]}
    for stream in incoming["streams"]:
        existing = existing_by_url.get(stream["url"])
        if existing is None:
            target["streams"].append(stream)
            existing_urls.add(stream["url"])
            existing_by_url[stream["url"]] = stream
        else:
            existing["sources"] = sorted(set(existing.get("sources", [existing["source"]]) + stream.get("sources", [stream["source"]])))
            existing["sourceStationIds"] = sorted(set(existing.get("sourceStationIds", []) + stream.get("sourceStationIds", [])))
            if stream_sort_key(stream) > stream_sort_key(existing):
                for field in ("source", "priority", "qualityScore", "codec", "bitrate"):
                    existing[field] = stream.get(field)

    target.setdefault("sourceIds", {}).update(incoming.get("sourceIds", {}))
    target["country"] = target["country"] or incoming["country"]
    target["countryCode"] = target["countryCode"] or incoming["countryCode"]
    target["language"] = target["language"] or incoming["language"]

    for tag in incoming["tags"]:
        if tag not in target["tags"]:
            target["tags"].append(tag)

    refresh_station_sources(target)


def stations_compatible(existing, incoming):
    for field in ("countryCode", "language"):
        left = str(existing.get(field) or "").strip().casefold()
        right = str(incoming.get(field) or "").strip().casefold()
        if left and right and left != right:
            return False
    left = str(existing.get("country") or "").strip().casefold()
    right = str(incoming.get("country") or "").strip().casefold()
    return not (left and right and left != right)


def find_station_match(candidates, incoming):
    compatible = [station for station in candidates if stations_compatible(station, incoming)]
    return compatible[0] if len(compatible) == 1 else None


async def load_source(session, source_id, config, loader, state_dir):
    enabled = config.get("enabled", False)
    priority = config.get("priority", 50)

    if not enabled:
        return [], {
            "source": source_id,
            "enabled": False,
            "status": "skipped",
            "stationCount": 0,
            "error": None,
        }

    try:
        stations = await loader(session, config, priority)
        stations = [
            station for station in stations
            if station.get("name") and station.get("streams")
            and any(valid_stream_url(stream.get("url")) for stream in station.get("streams", []))
        ]
        if not stations:
            raise ValueError("Source returned no usable stations; protecting its last-good snapshot")
        save_snapshot(state_dir, source_id, stations)
        return stations, {
            "source": source_id,
            "enabled": True,
            "status": "ok",
            "stationCount": len(stations),
            "snapshot": "refreshed",
            "error": None,
        }
    except Exception as error:
        snapshot = load_snapshot(state_dir, source_id)
        if snapshot:
            return snapshot["stations"], {
                "source": source_id,
                "enabled": True,
                "status": "stale",
                "stationCount": len(snapshot["stations"]),
                "snapshot": "last-good",
                "snapshotUpdatedAt": snapshot.get("updatedAt"),
                "error": str(error),
            }
        return [], {
            "source": source_id,
            "enabled": True,
            "status": "failed",
            "stationCount": 0,
            "snapshot": "unavailable",
            "error": str(error),
        }


async def main(state_dir=Path("state")):
    version = int(datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f"))
    version_dir = OUTPUT / f"v{version}"

    for directory in ("genres", "countries", "languages", "groups"):
        (version_dir / directory).mkdir(parents=True, exist_ok=True)

    config = json.loads(Path("config/sources.json").read_text(encoding="utf-8"))

    async def load_radio_browser(session, source_config, priority):
        raw = as_records(await fetch_stations(session, source_config["url"]))
        result = []
        for item in raw:
            record = {
                "name": get_value(item, "name"),
                "country": get_value(item, "country"),
                "countryCode": get_value(item, "country_code"),
                "language": get_value(item, "language"),
                "tags": get_value(item, "tags"),
                "url": item.get("url_resolved") or item.get("urlResolved") or get_value(item, "url"),
                "codec": get_value(item, "codec"),
                "bitrate": get_value(item, "bitrate", 0),
                "id": get_value(item, "id"),
            }
            normalized = normalize_record(record)
            if normalized:
                result.append(make_station(
                    normalized["name"], normalized["country"], normalized["countryCode"],
                    normalized["language"], normalized["tags"],
                    [make_stream("radio-browser", normalized["url"], normalized["codec"],
                                 normalized["bitrate"], priority, normalized["sourceStationId"])],
                ))
        return result

    async def load_airwavedb(session, source_config, priority):
        raw_stations, raw_streams = await fetch_catalog(
            session,
            source_config["stationsUrl"],
            source_config["streamsUrl"],
        )
        raw_stations = as_records(raw_stations, ("stations", "data", "items"))
        raw_streams = as_records(raw_streams, ("streams", "data", "items"))
        streams_by_station = {}
        for stream in raw_streams:
            station_id = (
                stream.get("stationId") or stream.get("station_id")
                or stream.get("stationUuid") or stream.get("station_uuid")
                or stream.get("station")
            )
            if isinstance(station_id, dict):
                station_id = get_value(station_id, "id")
            if station_id:
                streams_by_station.setdefault(str(station_id), []).append(stream)

        result = []
        for station in raw_stations:
            station_id = get_value(station, "id")
            streams = [
                make_stream(
                    "airwavedb",
                    get_value(stream, "url") or "",
                    get_value(stream, "codec") or "",
                    get_value(stream, "bitrate") or 0,
                    priority,
                    station_id,
                )
                for stream in streams_by_station.get(str(station_id), [])
            ]
            if streams:
                streams[0]["stationUuid"] = station_id
                result.append(make_station(
                    get_value(station, "name", ""),
                    get_value(station, "country", ""),
                    get_value(station, "country_code", ""),
                    get_value(station, "language", ""),
                    as_tags(get_value(station, "tags") or []),
                    streams,
                ))
        return result

    async def load_icecast(session, source_config, priority):
        raw = parse_directory(await fetch_directory(session, source_config["url"]))
        return [
            make_station(
                item["name"],
                tags=item["genres"],
                streams=[make_stream("icecast", item["stream_url"], item["codec"], item["bitrate"], priority)],
            )
            for item in raw
        ]

    async def load_m3u(session, source_id, url, priority):
        async with session.get(url, headers={"User-Agent": "Radsync/1.0"}) as response:
            response.raise_for_status()
            raw = parse_m3u(await response.text())
        return [
            make_station(
                item["name"],
                country=item.get("country", ""),
                country_code=item.get("attributes", {}).get("tvg-country-code", ""),
                language=item.get("language", ""),
                tags=item.get("genres") or [source_id],
                streams=[make_stream(
                    source_id, item["stream_url"],
                    item.get("attributes", {}).get("tvg-audio", ""),
                    priority=priority,
                    source_station_id=item.get("station_id"),
                )],
            )
            for item in raw
        ]

    async def load_iptv_streams(session, source_config, priority):
        raw = await fetch_streams(session, source_config["url"])
        result = []
        for item in as_records(raw, ("streams", "data", "results", "items")):
            normalized = __import__("sources.normalize", fromlist=["normalize_record"]).normalize_record(item)
            if normalized:
                result.append(make_station(
                    normalized["name"], normalized["country"], normalized["countryCode"],
                    normalized["language"], normalized["tags"],
                    [make_stream("iptv-org-streams", normalized["url"], normalized["codec"],
                                 normalized["bitrate"], priority, normalized["sourceStationId"])],
                ))
        return result

    async def load_somafm(session, source_config, priority):
        raw = await fetch_channels(session, source_config["url"])
        channels = as_records(raw, ("channels", "data", "results", "items"))
        result = []
        for channel in channels:
            playlist_payload = channel.get("playlists") or channel.get("streams") or channel.get("urls") or []
            playlists = as_records(playlist_payload) if not isinstance(playlist_payload, list) or (playlist_payload and isinstance(playlist_payload[0], dict)) else [
                {"url": item} for item in playlist_payload if isinstance(item, str)
            ]
            streams = [
                make_stream("somafm", get_value(item, "url", ""), get_value(item, "codec", ""),
                            get_value(item, "bitrate", 0), priority, get_value(channel, "id"))
                for item in playlists if get_value(item, "url")
            ]
            if streams:
                result.append(make_station(
                    channel.get("title") or channel.get("name") or "",
                    tags=[channel.get("genre", "")] if channel.get("genre") else [],
                    streams=streams,
                ))
        return result

    async def load_repo_playlists(session, source_id, source_config, priority):
        raw = await fetch_repository_playlists(
            session,
            source_config["repoApiUrl"],
            source_config["rawBaseUrl"],
        )
        return [
            make_station(
                item["name"],
                tags=[],
                streams=[make_stream(source_id, item["stream_url"], priority=priority)],
            )
            for item in raw
        ]

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        results = await asyncio.gather(
            load_source(session, "radio-browser", config["radio_browser"], load_radio_browser, state_dir),
            load_source(session, "airwavedb", config["airwavedb"], load_airwavedb, state_dir),
            load_source(session, "icecast", config["icecast"], load_icecast, state_dir),
            load_source(session, "iptv-org-radio", config["iptv_org_radio"], lambda s, c, p: load_m3u(s, "iptv-org-radio", c["url"], p)),
            load_source(session, "iptv-org-streams", config["iptv_org_streams"], load_iptv_streams),
            load_source(session, "pulham-hq", config["pulham_hq"], lambda s, c, p: load_m3u(s, "pulham-hq", c["url"], p)),
            load_source(session, "iprd", config["iprd"], lambda s, c, p: load_repo_playlists(s, "iprd", c, p), state_dir),
            load_source(session, "alon-drilich", config["alon_drilich"], lambda s, c, p: load_repo_playlists(s, "alon-drilich", c, p)),
            load_source(session, "somafm", config["somafm"], load_somafm, state_dir),
            load_source(session, "bbc-radio", config["bbc_radio"], lambda s, c, p: load_m3u(s, "bbc-radio", c["url"], p)),
        )

    stations_by_name = {}
    source_report = []

    for source_stations, report in results:
        source_report.append(report)
        for station in source_stations:
            if not station["name"] or not station["streams"] or not station["normalizedName"]:
                continue
            candidates = stations_by_name.setdefault(station["normalizedName"], [])
            match = find_station_match(candidates, station)
            if match is not None:
                merge_station(match, station)
            else:
                candidates.append(station)

    stations = [station for candidates in stations_by_name.values() for station in candidates]

    def write_group(directory, groups):
        items = []
        for group_id, group_stations in sorted(groups.items()):
            path = directory / f"{slug(group_id)}.json"
            path.write_text(json.dumps({"id": slug(group_id), "version": version, "stations": group_stations}, ensure_ascii=False), encoding="utf-8")
            items.append({"id": slug(group_id), "name": group_id, "count": len(group_stations), "url": f"{directory.name}/{slug(group_id)}.json"})
        return items

    genre_groups = {}
    country_groups = {}
    language_groups = {}

    for station in stations:
        station_genres = {str(tag).strip().casefold() or "uncategorized" for tag in station["tags"]}
        for tag in station_genres or {"uncategorized"}:
            genre_groups.setdefault(tag, []).append(station)
        country_groups.setdefault((station["countryCode"] or "unknown").lower(), []).append(station)
        language_groups.setdefault((station["language"] or "unknown").lower(), []).append(station)

    genres = write_group(version_dir / "genres", genre_groups)
    countries = write_group(version_dir / "countries", country_groups)
    languages = write_group(version_dir / "languages", language_groups)

    master = {
        "version": version,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "stationCount": len(stations),
        "streamCount": sum(len(station["streams"]) for station in stations),
        "groups": {
            "genres": {"url": "groups/genres.json", "items": genres},
            "countries": {"url": "groups/countries.json", "items": countries},
            "languages": {"url": "groups/languages.json", "items": languages},
        },
    }
    (version_dir / "stations.json").write_text(
        json.dumps({"version": version, "stations": stations}, ensure_ascii=False),
        encoding="utf-8",
    )
    master["stationsUrl"] = "stations.json"
    (version_dir / "master-index.json").write_text(json.dumps(master, ensure_ascii=False), encoding="utf-8")

    for group_type, items in (("genres", genres), ("countries", countries), ("languages", languages)):
        (version_dir / "groups" / f"{group_type}.json").write_text(
            json.dumps({"type": group_type, "version": version, "items": items}, ensure_ascii=False),
            encoding="utf-8",
        )

    report = {
        "version": version,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "totalStations": len(stations),
        "totalStreams": master["streamCount"],
        "sources": source_report,
    }
    (version_dir / "source-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if not stations:
        raise RuntimeError("No valid stations were produced; refusing to update current catalog")

    current = OUTPUT / "current"
    current.mkdir(exist_ok=True)
    pointer_tmp = current / "master-index.json.tmp"
    pointer_tmp.write_text(
        json.dumps({"version": version, "masterIndex": f"v{version}/master-index.json"}, ensure_ascii=False),
        encoding="utf-8",
    )
    pointer_tmp.replace(current / "master-index.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the Radsync radio catalogue")
    parser.add_argument("--state-dir", type=Path, default=Path("state"))
    args = parser.parse_args()
    asyncio.run(main(state_dir=args.state_dir))
