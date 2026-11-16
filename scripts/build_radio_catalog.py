#!/usr/bin/env python3
"""Builds a versioned, atomic multi-source radio catalog.

Duplicate stations are retained as multiple selectable stream variants.
The highest-quality available stream is selected as the default.
"""
import argparse
import asyncio
import hashlib
import json
import re
import unicodedata
import uuid
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
from sources.normalize import as_records, get_value, as_text, as_tags, normalize_record
from sources.state import load_snapshot, save_snapshot

OUTPUT = Path("output")
RADSYNC_NAMESPACE = uuid.UUID("5d0f2f8e-3c52-5c7f-9b4f-6fbb3c7c1a42")


def slug(value: str):
    value = unicodedata.normalize("NFKD", value or "")
    value = re.sub(r"[^\w]+", "-", value.lower()).strip("-")
    return value or "station"



def group_slug(value: str, max_length: int = 96):
    """Create a filesystem-safe, bounded group filename without truncation collisions."""
    base = slug(value)
    if len(base) <= max_length:
        return base
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]
    prefix_length = max_length - len(digest) - 1
    return f"{base[:prefix_length].rstrip('-')}-{digest}"

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
        "sourceStationIds": [str(source_station_id)] if source_station_id not in (None, "") else [],
        "sourceRecords": [],
        "url": str(url or "").strip(),
        "codec": codec,
        "bitrate": bitrate,
        "priority": priority,
        "qualityScore": round(quality, 2),
        "status": "unknown",
    }


def make_identity_evidence(source, record=None, provider_record_id=None, source_url=None,
                           playlist_path=None, entry_index=None, raw_extinf=None,
                           stream_url=None, codec=None, bitrate=None):
    """Keep a small, private evidence record for failed identity diagnostics."""
    record = record if isinstance(record, dict) else {}
    raw_name = get_value(record, "name")
    provider_uuid = (
        record.get("stationuuid") or record.get("stationUuid")
        or record.get("station_uuid") or record.get("uuid")
    )
    tags = get_value(record, "tags")
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.replace(";", ",").split(",") if part.strip()]
    elif isinstance(tags, (list, tuple, set)):
        tags = [as_text(tag) for tag in tags if as_text(tag)]
    else:
        tags = []
    def safe_url(value):
        try:
            parsed = urlsplit(str(value or "").strip())
            if not parsed.scheme or not parsed.netloc:
                return str(value or "")[:500]
            return parsed._replace(query="", fragment="").geturl()[:500]
        except ValueError:
            return str(value or "")[:500]
    return {
        "source": str(source or ""),
        "providerStationUuid": str(provider_uuid or ""),
        "providerRecordId": str(provider_record_id or get_value(record, "id") or ""),
        "sourceUrl": safe_url(source_url),
        "playlistPath": safe_url(playlist_path) if str(playlist_path or "").startswith(("http://", "https://")) else str(playlist_path or "")[:500],
        "entryIndex": entry_index,
        "rawName": str(raw_name or "")[:300],
        "rawExtinf": str(raw_extinf or "")[:500],
        "tags": tags[:50],
        "codec": str(codec or get_value(record, "codec") or "")[:80],
        "bitrate": bitrate if bitrate is not None else get_value(record, "bitrate", 0),
        "streamUrl": safe_url(stream_url or get_value(record, "url")),
        "originalTitle": str(record.get("original_title") or raw_name or "")[:300],
        "originalUrl": safe_url(record.get("original_url") or stream_url or get_value(record, "url")),
        "playlistRepository": str(record.get("playlist_repository") or "")[:200],
        "rawMetadata": record.get("raw_metadata") if isinstance(record.get("raw_metadata"), dict) else {},
        "ingestedAt": datetime.now(timezone.utc).isoformat(),
    }


def _source_ids_for_station(station):
    """Return every provider-qualified ID, accepting legacy scalar sourceIds values."""
    values = {}
    for source, raw_ids in (station.get("sourceIds") or {}).items():
        ids = raw_ids if isinstance(raw_ids, (list, tuple, set)) else [raw_ids]
        for value in ids:
            if value not in (None, ""):
                values.setdefault(str(source), set()).add(str(value))
    for stream in station.get("streams", []):
        source = str(stream.get("source") or "")
        ids = stream.get("sourceStationIds") or (
            [stream.get("sourceStationId")] if stream.get("sourceStationId") else []
        )
        for value in ids:
            if value not in (None, ""):
                values.setdefault(source, set()).add(str(value))
    return {source: sorted(ids) for source, ids in sorted(values.items())}


def make_station(name, country="", country_code="", language="", tags=None, streams=None,
                 identity_evidence=None):
    streams = [stream for stream in (streams or []) if valid_stream_url(stream.get("url"))]
    streams.sort(key=stream_sort_key, reverse=True)
    evidence = list(identity_evidence or [
        make_identity_evidence(
            stream.get("source"), provider_record_id=stream.get("sourceStationId"),
            stream_url=stream.get("url"), codec=stream.get("codec"),
            bitrate=stream.get("bitrate"),
        )
        for stream in streams
    ])
    source_ids = _source_ids_for_station({"streams": streams})
    for stream in streams:
        safe_stream_url = make_identity_evidence(
            stream.get("source"), stream_url=stream.get("url")
        )["streamUrl"]
        matching = [
            item for item in evidence
            if item.get("source") == stream.get("source")
            and (not item.get("streamUrl") or item.get("streamUrl") == safe_stream_url)
        ]
        stream["sourceRecords"] = matching or stream.get("sourceRecords", [])
    station = {
        # Assigned after reconciliation, once canonical groups are known.
        "stationUuid": None,
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
        "_identityEvidence": evidence,
    }
    refresh_station_sources(station)
    return station


def merge_station(target, incoming):
    existing_urls = {stream["url"] for stream in target["streams"]}
    evidence = target.setdefault("_identityEvidence", [])
    known_evidence = {json.dumps(item, sort_keys=True, ensure_ascii=False) for item in evidence}
    for item in incoming.get("_identityEvidence", []):
        marker = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if marker not in known_evidence:
            evidence.append(item)
            known_evidence.add(marker)

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
            known_records = {json.dumps(item, sort_keys=True, ensure_ascii=False) for item in existing.get("sourceRecords", [])}
            for item in stream.get("sourceRecords", []):
                marker = json.dumps(item, sort_keys=True, ensure_ascii=False)
                if marker not in known_records:
                    existing.setdefault("sourceRecords", []).append(item)
                    known_records.add(marker)
            if stream_sort_key(stream) > stream_sort_key(existing):
                for field in ("source", "priority", "qualityScore", "codec", "bitrate"):
                    existing[field] = stream.get(field)

    merged_ids = _source_ids_for_station(target)
    incoming_ids = _source_ids_for_station(incoming)
    for source, ids in incoming_ids.items():
        merged_ids[source] = sorted(set(merged_ids.get(source, []) + ids))
    target["sourceIds"] = merged_ids
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
    if not compatible:
        return None

    # Prefer an exact provider identity when one is available. A source may
    # legitimately publish several formats/URLs for the same station under
    # separate provider records; treating every same-name candidate as
    # ambiguous prevents those variants from being reconciled. Do not use a
    # stream URL as identity: URLs are variants, not station identifiers.
    incoming_ids = {
        (source, source_id)
        for source, ids in _source_ids_for_station(incoming).items()
        for source_id in ids
    }
    if incoming_ids:
        exact_matches = [
            station for station in compatible
            if incoming_ids.intersection(
                (source, source_id)
                for source, ids in _source_ids_for_station(station).items()
                for source_id in ids
            )
        ]
        if len(exact_matches) == 1:
            return exact_matches[0]
        if len(exact_matches) > 1:
            return None

    return compatible[0] if len(compatible) == 1 else None


def _canonical_identity_base(station):
    values = (
        normalize_name(station.get("name", "")),
        str(station.get("countryCode") or "").strip().casefold(),
        str(station.get("country") or "").strip().casefold(),
        str(station.get("language") or "").strip().casefold(),
    )
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def _canonical_identity_disambiguator(station):
    provider_ids = sorted(
        f"{stream.get('source', '')}:{source_id}"
        for stream in station.get("streams", [])
        for source_id in stream.get("sourceStationIds", [])
        if source_id
    )
    stream_provenance = sorted(
        f"{stream.get('source', '')}:{stream.get('url', '')}"
        for stream in station.get("streams", [])
    )
    return json.dumps({"providerIds": provider_ids, "streams": stream_provenance}, ensure_ascii=False, separators=(",", ":"))


def _identity_diagnostic_record(station):
    evidence = station.get("_identityEvidence") or []
    if not evidence:
        evidence = [
            make_identity_evidence(
                stream.get("source"), provider_record_id=stream.get("sourceStationId"),
                stream_url=stream.get("url"), codec=stream.get("codec"),
                bitrate=stream.get("bitrate"),
            )
            for stream in station.get("streams", [])
        ]
    return {
        "stationName": station.get("name", ""),
        "normalizedName": normalize_name(station.get("name", "")),
        "identityInputs": {
            "normalizedName": normalize_name(station.get("name", "")),
            "countryCode": str(station.get("countryCode") or "").strip().casefold(),
            "country": str(station.get("country") or "").strip().casefold(),
            "language": str(station.get("language") or "").strip().casefold(),
        },
        "country": station.get("country", ""),
        "countryCode": station.get("countryCode", ""),
        "language": station.get("language", ""),
        "providerIdentities": sorted({
            f"{item.get('source', '')}:{item.get('providerStationUuid') or item.get('providerRecordId')}"
            for item in evidence
            if item.get("providerStationUuid") or item.get("providerRecordId")
        }),
        "records": evidence,
        "streams": [
            {
                "source": stream.get("source", ""),
                "sourceStationIds": stream.get("sourceStationIds", []),
                "codec": stream.get("codec", ""),
                "bitrate": stream.get("bitrate", 0),
                "streamUrl": make_identity_evidence(
                    stream.get("source"), stream_url=stream.get("url")
                )["streamUrl"],
            }
            for stream in station.get("streams", [])
        ],
    }


def assign_canonical_station_ids(stations, diagnostic_path=None, catalog_version=None):
    """Assign deterministic UUIDv5 identities; write diagnostics before failing closed."""
    by_base = {}
    for station in stations:
        by_base.setdefault(_canonical_identity_base(station), []).append(station)

    ambiguous_groups = []
    for base, collisions in by_base.items():
        if len(collisions) == 1:
            collisions[0]["stationUuid"] = str(uuid.uuid5(RADSYNC_NAMESPACE, base))
            continue

        fingerprints = {}
        for station in sorted(collisions, key=_canonical_identity_disambiguator):
            fingerprint = _canonical_identity_disambiguator(station)
            fingerprints.setdefault(fingerprint, []).append(station)
            station["stationUuid"] = str(uuid.uuid5(RADSYNC_NAMESPACE, f"{base}|{fingerprint}"))

        duplicates = [group for group in fingerprints.values() if len(group) > 1]
        if duplicates:
            records = [_identity_diagnostic_record(station) for station in collisions]
            identities = [
                set(record["providerIdentities"]) for record in records
            ]
            repeated_provider_identity = any(
                left.intersection(right)
                for index, left in enumerate(identities)
                for right in identities[index + 1:]
            )
            ambiguous_groups.append({
                "groupFingerprint": hashlib.sha256(base.encode("utf-8")).hexdigest(),
                "canonicalIdentity": json.loads(base),
                "classification": "likely_duplicate" if repeated_provider_identity else "ambiguous",
                "reason": "Multiple separate aggregates have identical canonical metadata and provider/stream provenance.",
                "duplicateFingerprintCount": len(duplicates),
                "records": records,
            })

    if ambiguous_groups:
        report = {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "catalogVersion": catalog_version,
            "ambiguousGroups": ambiguous_groups,
        }
        if diagnostic_path is not None:
            path = Path(diagnostic_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        raise ValueError(
            "Cannot assign stable distinct station identities: separate canonical stations "
            "have identical metadata and provider provenance"
        )

    if diagnostic_path is not None:
        Path(diagnostic_path).unlink(missing_ok=True)

    identities = [station.get("stationUuid") for station in stations]
    if any(not identity for identity in identities) or len(identities) != len(set(identities)):
        raise ValueError("Canonical station identity assignment produced a missing or duplicate ID")
    return stations


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
            restored_stations = snapshot["stations"]
            for station in restored_stations:
                for stream in station.get("streams", []):
                    try:
                        bitrate = max(0, int(stream.get("bitrate", 0) or 0))
                    except (TypeError, ValueError):
                        bitrate = 0
                    stream["priority"] = priority
                    stream["qualityScore"] = round(priority + min(bitrate / 10.0, 20.0), 2)
                refresh_station_sources(station)
            return restored_stations, {
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
    diagnostics_path = Path(state_dir) / "diagnostics" / "identity-ambiguity.json"
    diagnostics_path.unlink(missing_ok=True)

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
                "id": get_value(item, "stationuuid") or get_value(item, "stationUuid") or get_value(item, "id"),
            }
            normalized = normalize_record(record)
            if normalized:
                result.append(make_station(
                    normalized["name"], normalized["country"], normalized["countryCode"],
                    normalized["language"], normalized["tags"],
                    [make_stream("radio-browser", normalized["url"], normalized["codec"],
                                 normalized["bitrate"], priority, normalized["sourceStationId"])],
                    identity_evidence=[make_identity_evidence(
                        "radio-browser", item, normalized["sourceStationId"], source_config["url"],
                        stream_url=normalized["url"], codec=normalized["codec"], bitrate=normalized["bitrate"],
                    )],
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
                    as_text(get_value(station, "name", "")),
                    as_text(get_value(station, "country", "")),
                    as_text(get_value(station, "country_code", "")),
                    as_text(get_value(station, "language", "")),
                    as_tags(get_value(station, "tags") or []),
                    streams,
                    identity_evidence=[make_identity_evidence(
                        "airwavedb", station, station_id, source_config["stationsUrl"],
                        stream_url=stream.get("url"), codec=get_value(stream, "codec"),
                        bitrate=get_value(stream, "bitrate", 0),
                    ) for stream in streams_by_station.get(str(station_id), [])],
                ))
        return result

    async def load_icecast(session, source_config, priority):
        raw = parse_directory(await fetch_directory(session, source_config["url"]))
        return [
            make_station(
                item["name"],
                country=item.get("country", ""),
                country_code=item.get("country_code", ""),
                language=item.get("language", ""),
                tags=item["genres"],
                streams=[make_stream("icecast", item["stream_url"], item["codec"], item["bitrate"], priority)],
            )
            for item in raw
        ]

    async def load_m3u(session, source_id, url, priority):
        async with session.get(url, headers={"User-Agent": "Radsync/1.0"}) as response:
            response.raise_for_status()
            raw = parse_m3u(await response.text())
        result = []
        for item in raw:
            evidence_record = {
                **item, "id": item.get("station_id"),
                "original_title": item.get("name"),
                "original_url": item.get("stream_url"),
                "raw_metadata": item.get("attributes", {}),
            }
            evidence = make_identity_evidence(
                source_id, evidence_record, item.get("station_id"), url,
                playlist_path=url, entry_index=item.get("entry_index"),
                raw_extinf=item.get("raw_extinf"), stream_url=item.get("stream_url"),
                codec=item.get("attributes", {}).get("tvg-audio"),
            )
            result.append(make_station(
                item["name"], country=item.get("country", ""),
                country_code=item.get("attributes", {}).get("tvg-country-code", ""),
                language=item.get("language", ""),
                tags=item.get("genres") or [source_id],
                streams=[make_stream(
                    source_id, item["stream_url"],
                    item.get("attributes", {}).get("tvg-audio", ""),
                    priority=priority, source_station_id=item.get("station_id"),
                )],
                identity_evidence=[evidence],
            ))
        return result

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
                    identity_evidence=[make_identity_evidence(
                        "iptv-org-streams", item, normalized["sourceStationId"], source_config["url"],
                        stream_url=normalized["url"], codec=normalized["codec"], bitrate=normalized["bitrate"],
                    )],
                ))
        return result

    async def load_somafm(session, source_config, priority):
        raw = await fetch_channels(session, source_config["url"])
        channels = as_records(raw, ("channels", "data", "results", "items"))
        result = []
        for channel in channels:
            playlist_payload = (
                channel.get("playlists") or channel.get("streams")
                or channel.get("urls") or channel.get("playlist") or []
            )
            if isinstance(playlist_payload, dict):
                playlists = [
                    {"format": key, "url": value}
                    for key, value in playlist_payload.items()
                    if isinstance(value, str)
                ] or as_records(playlist_payload)
            elif isinstance(playlist_payload, list) and (
                not playlist_payload or isinstance(playlist_payload[0], str)
            ):
                playlists = [{"url": item} for item in playlist_payload if isinstance(item, str)]
            else:
                playlists = as_records(playlist_payload)
            streams = [
                make_stream("somafm", get_value(item, "url", ""), get_value(item, "codec", ""),
                            get_value(item, "bitrate", 0), priority, get_value(channel, "id"))
                for item in playlists if get_value(item, "url")
            ]
            if streams:
                result.append(make_station(
                    as_text(channel.get("title") or channel.get("name") or ""),
                    tags=as_tags(channel.get("genre", "")),
                    streams=streams,
                ))
        return result

    async def load_repo_playlists(session, source_id, source_config, priority):
        raw = await fetch_repository_playlists(
            session,
            source_config["repoApiUrl"],
            source_config["rawBaseUrl"],
        )
        result = []
        for item in raw:
            evidence_record = {
                **item, "id": item.get("station_id"),
                "original_title": item.get("name"),
                "original_url": item.get("stream_url"),
                "raw_metadata": item.get("attributes", {}),
            }
            evidence = make_identity_evidence(
                source_id, evidence_record, item.get("station_id"),
                item.get("playlist_url"), playlist_path=item.get("playlist_path"),
                entry_index=item.get("entry_index"), raw_extinf=item.get("raw_extinf"),
                stream_url=item.get("stream_url"),
            )
            result.append(make_station(
                item["name"], country=item.get("country", ""),
                language=item.get("language", ""), tags=item.get("genres") or [],
                streams=[make_stream(source_id, item["stream_url"], priority=priority,
                                     source_station_id=item.get("station_id"))],
                identity_evidence=[evidence],
            ))
        return result

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        results = await asyncio.gather(
            load_source(session, "radio-browser", config["radio_browser"], load_radio_browser, state_dir),
            load_source(session, "airwavedb", config["airwavedb"], load_airwavedb, state_dir),
            load_source(session, "icecast", config["icecast"], load_icecast, state_dir),
            load_source(session, "iptv-org-radio", config["iptv_org_radio"], lambda s, c, p: load_m3u(s, "iptv-org-radio", c["url"], p), state_dir),
            load_source(session, "iptv-org-streams", config["iptv_org_streams"], load_iptv_streams, state_dir),
            load_source(session, "pulham-hq", config["pulham_hq"], lambda s, c, p: load_m3u(s, "pulham-hq", c["url"], p), state_dir),
            load_source(session, "iprd", config["iprd"], lambda s, c, p: load_repo_playlists(s, "iprd", c, p), state_dir),
            load_source(session, "alon-drilich", config["alon_drilich"], lambda s, c, p: load_repo_playlists(s, "alon-drilich", c, p), state_dir),
            load_source(session, "somafm", config["somafm"], load_somafm, state_dir),
            load_source(session, "bbc-radio", config["bbc_radio"], lambda s, c, p: load_m3u(s, "bbc-radio", c["url"], p), state_dir),
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
    assign_canonical_station_ids(
        stations, diagnostic_path=diagnostics_path, catalog_version=version
    )
    # Diagnostic provenance is private state only; never leak it into the published schema.
    for station in stations:
        station.pop("_identityEvidence", None)

    def write_group(directory, groups):
        items = []
        for group_id, group_stations in sorted(groups.items()):
            group_id_slug = group_slug(group_id)
            path = directory / f"{group_id_slug}.json"
            path.write_text(json.dumps({"id": group_id_slug, "version": version, "stations": group_stations}, ensure_ascii=False), encoding="utf-8")
            items.append({"id": group_id_slug, "name": group_id, "count": len(group_stations), "url": f"{directory.name}/{group_id_slug}.json"})
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
