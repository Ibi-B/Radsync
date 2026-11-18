"""Compare Icecast responses by request profile and exercise Radsync's collection path."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import socket
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from sources.icecast import parse_directory
from scripts.build_radio_catalog import make_station, make_stream, valid_stream_url


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].casefold().replace("-", "_")


def child_text(entry: ET.Element, *names: str) -> str:
    accepted = {name.casefold().replace("-", "_") for name in names}
    for node in entry.iter():
        if local_name(node.tag) in accepted and node.text and node.text.strip():
            return node.text.strip()
    return ""


def safe_prefix(body: bytes, limit: int = 1000) -> str:
    sample = body[:limit].decode("utf-8", errors="replace")
    return re.sub(
        r"""https?://[^<\s"']+""",
        "[STREAM_URL_REDACTED]",
        sample,
        flags=re.IGNORECASE,
    )


async def request_probe(
    session: aiohttp.ClientSession,
    url: str,
    profile: str,
    headers: dict[str, str],
) -> tuple[dict, list[dict]]:
    started = time.monotonic()
    result: dict = {
        "profile": profile,
        "requestedUrl": url,
        "requestHeaders": headers,
    }
    try:
        async with session.get(
            url,
            headers=headers,
            allow_redirects=True,
        ) as response:
            peer = None
            connection = response.connection
            transport = connection.transport if connection else None
            if transport is not None:
                peer = transport.get_extra_info("peername")
            body = await response.read()
            result.update({
                "remotePeer": (
                    {"ip": str(peer[0]), "port": int(peer[1])}
                    if isinstance(peer, tuple) and len(peer) >= 2 else None
                ),
                "httpStatus": response.status,
                "finalUrl": str(response.url),
                "redirectCount": len(response.history),
                "redirectHistory": [
                    {
                        "status": item.status,
                        "url": str(item.url),
                        "location": item.headers.get("Location"),
                    }
                    for item in response.history
                ],
                "contentType": response.headers.get("Content-Type"),
                "contentLengthHeader": response.headers.get("Content-Length"),
                "contentEncoding": response.headers.get("Content-Encoding"),
                "bytesReceived": len(body),
                "bodySha256": hashlib.sha256(body).hexdigest(),
                "elapsedSeconds": round(time.monotonic() - started, 3),
                "responseHeaders": {
                    name: response.headers.get(name)
                    for name in (
                        "Server", "Via", "Age", "Vary", "ETag", "Last-Modified",
                        "Cache-Control", "X-Cache", "CF-Cache-Status", "CF-Ray", "Date",
                    )
                    if response.headers.get(name) is not None
                },
                "sanitizedResponsePrefix": safe_prefix(body),
            })

            if response.status < 200 or response.status >= 300:
                result["httpError"] = f"Unexpected HTTP status {response.status}"
                return result, []

            try:
                # Same charset resolution and strict decoding used by the
                # production fetch_directory() adapter.
                xml_text = await response.text()
            except UnicodeError as error:
                result["decodeErrorType"] = type(error).__name__
                result["decodeError"] = str(error)[:1000]
                return result, []

        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as error:
            result["xmlParseError"] = str(error)
            return result, []

        entries = [
            node for node in root.iter()
            if local_name(node.tag) in {"entry", "station", "server"}
        ]
        names = [
            child_text(node, "server_name", "name", "title")
            for node in entries
        ]
        stream_urls = [
            child_text(node, "listen_url", "listenurl", "stream_url", "url")
            for node in entries
        ]
        schemes: dict[str, int] = {}
        for stream_url in stream_urls:
            if not stream_url:
                scheme = "missing"
            else:
                try:
                    parsed = urlsplit(stream_url)
                    scheme = (
                        parsed.scheme.casefold()
                        if parsed.scheme and parsed.netloc else "invalid"
                    )
                except ValueError:
                    scheme = "invalid"
            schemes[scheme] = schemes.get(scheme, 0) + 1

        # Exercise Radsync's actual XML parser on this response profile.
        records = parse_directory(xml_text)
        result["xml"] = {
            "rootTag": root.tag,
            "entryCount": len(entries),
            "entriesWithName": sum(bool(value) for value in names),
            "entriesWithStreamUrl": sum(bool(value) for value in stream_urls),
            "entriesWithBoth": sum(
                bool(name and stream_url)
                for name, stream_url in zip(names, stream_urls)
            ),
            "streamUrlSchemes": schemes,
        }
        result["radsyncParserRecords"] = len(records)
        return result, records

    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
        result.update({
            "requestErrorType": type(error).__name__,
            "requestError": str(error)[:1000],
            "elapsedSeconds": round(time.monotonic() - started, 3),
        })
        return result, []


def build_production_stage_report(
    records: list[dict],
    minimum_station_count: int | None,
    priority: int,
) -> dict:
    # Mirrors production load_icecast(): make_stream() then make_station().
    stations = [
        make_station(
            item["name"],
            country=item.get("country", ""),
            country_code=item.get("country_code", ""),
            language=item.get("language", ""),
            tags=item.get("genres", []),
            streams=[
                make_stream(
                    "icecast",
                    item["stream_url"],
                    item.get("codec", ""),
                    item.get("bitrate", 0),
                    priority,
                )
            ],
        )
        for item in records
    ]
    after_make_station = [station for station in stations if station.get("streams")]
    # Mirrors the subsequent load_source() acceptance predicate.
    after_load_source = [
        station for station in after_make_station
        if station.get("name")
        and station.get("streams")
        and any(
            valid_stream_url(stream.get("url"))
            for stream in station.get("streams", [])
        )
    ]
    invalid_urls = sum(
        not valid_stream_url(item.get("stream_url")) for item in records
    )
    report = {
        "recordsBeforeMakeStation": len(records),
        "stationsConstructed": len(stations),
        "stationsWithStreamsAfterMakeStation": len(after_make_station),
        "stationsAcceptedByLoadSourceFilter": len(after_load_source),
        "recordsRejectedForInvalidOrUnsupportedStreamUrl": invalid_urls,
        "codecFilterApplied": False,
        "providerDeduplicationApplied": False,
        "providerLevelDeduplicationApplied": False,
    }
    if minimum_station_count is not None:
        report["configuredMinimumStationCount"] = minimum_station_count
        report["minimumCountGateWouldPass"] = (
            len(after_load_source) >= minimum_station_count
        )
    return report


async def diagnose(url: str, timeout_seconds: int) -> dict:
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    profiles = [
        ("production-radsync", {"User-Agent": "Radsync/1.0"}),
        ("curl-like", {
            "User-Agent": "curl/8.5.0",
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }),
        ("browser-like", {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "application/xml,text/xml,*/*;q=0.8",
        }),
        ("production-no-cache", {
            "User-Agent": "Radsync/1.0",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }),
    ]

    parsed_host = urlsplit(url).hostname
    dns_addresses = []
    dns_error = None
    if parsed_host:
        try:
            dns_addresses = sorted({
                item[4][0]
                for item in socket.getaddrinfo(parsed_host, None, type=socket.SOCK_STREAM)
            })
        except OSError as error:
            dns_error = f"{type(error).__name__}: {str(error)[:500]}"

    report: dict = {
        "configuredUrl": url,
        "dns": {"hostname": parsed_host, "resolvedAddresses": dns_addresses, "error": dns_error},
        "profiles": [],
        "notes": [
            "Read-only diagnostics; no provider snapshots or catalogue output are changed.",
            "The production Icecast collector has no codec filter or provider-level deduplication step.",
            "Only the production-radsync response is used for the production-stage count breakdown.",
        ],
    }
    production_records: list[dict] = []

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for profile, headers in profiles:
            result, records = await request_probe(session, url, profile, headers)
            report["profiles"].append(result)
            if profile == "production-radsync":
                production_records = records

        parsed_url = urlsplit(url)
        if parsed_url.scheme.casefold() == "http":
            https_url = urlunsplit((
                "https", parsed_url.netloc, parsed_url.path,
                parsed_url.query, parsed_url.fragment,
            ))
            result, _ = await request_probe(
                session,
                https_url,
                "https-comparison",
                {"User-Agent": "Radsync/1.0"},
            )
            report["profiles"].append(result)

    config = json.loads(
        Path("config/sources.json").read_text(encoding="utf-8")
    )
    icecast_config = config.get("icecast", {})
    minimum = icecast_config.get("anomalyBounds", {}).get("minStationCount")
    priority = icecast_config.get("priority", 55)

    report["productionCollectionStages"] = build_production_stage_report(
        production_records,
        minimum if isinstance(minimum, int) else None,
        priority,
    )
    report["requestComparisonConclusion"] = {
        "productionProfileBytes": next(
            (
                item.get("bytesReceived")
                for item in report["profiles"]
                if item["profile"] == "production-radsync"
            ),
            None,
        ),
        "profilesWithEntries": [
            item["profile"]
            for item in report["profiles"]
            if item.get("xml", {}).get("entryCount", 0) > 0
        ],
        "profilesWithEmptyDirectory": [
            item["profile"]
            for item in report["profiles"]
            if item.get("xml", {}).get("rootTag") == "directory"
            and item.get("xml", {}).get("entryCount") == 0
        ],
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=None,
        help="Override the Icecast URL from config/sources.json",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=45,
        help="Timeout per request profile in seconds (default: 45)",
    )
    parser.add_argument(
        "--output",
        default="icecast-diagnostic.json",
        help="Path for the JSON diagnostic report",
    )
    args = parser.parse_args()

    config = json.loads(
        Path("config/sources.json").read_text(encoding="utf-8")
    )
    url = args.url or config["icecast"]["url"]

    report = asyncio.run(diagnose(url, args.timeout))
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print("ICECAST_DIAGNOSTIC_JSON:")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"REPORT_FILE: {output_path}")


if __name__ == "__main__":
    main()
