"""Run the production Icecast fetch/parser/normalization path with safe diagnostics."""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit

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


def safe_prefix(body: bytes, limit: int = 1600) -> str:
    sample = body[:limit].decode("utf-8", errors="replace")
    return re.sub(
        r"""https?://[^<\s"']+""",
        "[STREAM_URL_REDACTED]",
        sample,
        flags=re.IGNORECASE,
    )


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


async def diagnose(url: str, timeout_seconds: int) -> dict:
    report: dict = {
        "requestedUrl": url,
        "fetch": {},
        "xml": {},
        "parser": {},
        "collector": {},
        "notes": [
            "This diagnostic does not modify provider snapshots or catalogue output.",
            "The production Icecast path has no codec filter or provider-level deduplication step.",
        ],
    }
    started = time.monotonic()
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url,
                headers={"User-Agent": "Radsync/1.0"},
                allow_redirects=True,
            ) as response:
                body = await response.read()
                report["fetch"] = {
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
                    "elapsedSeconds": round(time.monotonic() - started, 3),
                    "sanitizedResponsePrefix": safe_prefix(body),
                }

                if response.status < 200 or response.status >= 300:
                    report["fetch"]["httpError"] = f"Unexpected HTTP status {response.status}"
                    return report

        try:
            root = ET.fromstring(body)
        except ET.ParseError as error:
            report["xml"] = {
                "parseError": str(error),
                "bytesReceived": len(body),
            }
            return report

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

        scheme_counts: dict[str, int] = {}
        for stream_url in stream_urls:
            if not stream_url:
                scheme = "missing"
            else:
                try:
                    parsed_url = urlsplit(stream_url)
                    scheme = (
                        parsed_url.scheme.casefold()
                        if parsed_url.scheme and parsed_url.netloc
                        else "invalid"
                    )
                except ValueError:
                    scheme = "invalid"
            scheme_counts[scheme] = scheme_counts.get(scheme, 0) + 1

        report["xml"] = {
            "rootTag": root.tag,
            "entryCount": len(entries),
            "entriesWithName": sum(bool(value) for value in names),
            "entriesWithStreamUrl": sum(bool(value) for value in stream_urls),
            "entriesWithBoth": sum(
                bool(name and stream_url)
                for name, stream_url in zip(names, stream_urls)
            ),
            "streamUrlSchemes": scheme_counts,
        }

        # Exercise Radsync's real parser, not a second parser implementation.
        parsed_records = parse_directory(body.decode("utf-8-sig", errors="replace"))
        report["parser"] = {
            "recordsReturned": len(parsed_records),
        }

        # Reproduce the production load_icecast() conversion exactly:
        # parse_directory() -> make_stream() -> make_station().
        constructed_stations = []
        for item in parsed_records:
            constructed_stations.append(
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
                            55,
                        )
                    ],
                )
            )

        after_make_station = [
            station for station in constructed_stations
            if station.get("streams")
        ]

        # This is the subsequent load_source() acceptance predicate.
        after_load_source_filter = [
            station for station in after_make_station
            if station.get("name")
            and station.get("streams")
            and any(
                valid_stream_url(stream.get("url"))
                for stream in station.get("streams", [])
            )
        ]

        rejected_invalid_urls = 0
        for item in parsed_records:
            if not valid_stream_url(item.get("stream_url")):
                rejected_invalid_urls += 1

        report["collector"] = {
            "recordsBeforeMakeStation": len(parsed_records),
            "stationsConstructed": len(constructed_stations),
            "stationsWithStreamsAfterMakeStation": len(after_make_station),
            "stationsAcceptedByLoadSourceFilter": len(after_load_source_filter),
            "recordsRejectedForInvalidOrUnsupportedStreamUrl": rejected_invalid_urls,
            "codecFilterApplied": False,
            "providerDeduplicationApplied": False,
        }

        config_path = Path("config/sources.json")
        if config_path.exists():
            config = json.loads(config_path.read_text(encoding="utf-8"))
            bounds = config.get("icecast", {}).get("anomalyBounds", {})
            minimum = bounds.get("minStationCount")
            report["collector"]["configuredMinimumStationCount"] = minimum
            report["collector"]["minimumCountGateWouldPass"] = (
                len(after_load_source_filter) >= minimum
                if isinstance(minimum, int) else None
            )

    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
        report["fetch"] = {
            "requestErrorType": type(error).__name__,
            "requestError": str(error)[:1000],
            "elapsedSeconds": round(time.monotonic() - started, 3),
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
        default=90,
        help="Request timeout in seconds (default: 90)",
    )
    parser.add_argument(
        "--output",
        default="icecast-diagnostic.json",
        help="Path for the JSON diagnostic report",
    )
    args = parser.parse_args()

    config = json.loads(Path("config/sources.json").read_text(encoding="utf-8"))
    url = args.url or config["icecast"]["url"]

    report = asyncio.run(diagnose(url, args.timeout))
    write_report(Path(args.output), report)

    print("ICECAST_DIAGNOSTIC_JSON:")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"REPORT_FILE: {args.output}")


if __name__ == "__main__":
    main()
