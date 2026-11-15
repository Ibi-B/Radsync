"""Tolerant normalization helpers for upstream catalogue format drift."""
from collections.abc import Mapping


ALIASES = {
    "name": ("name", "title", "station", "station_name", "stationName", "channel", "channel_name", "server_name"),
    "url": ("url", "stream_url", "streamUrl", "listen_url", "listenUrl", "url_resolved", "urlResolved", "href", "src"),
    "country": ("country", "country_name", "countryName", "nation"),
    "country_code": ("countrycode", "country_code", "countryCode", "country_iso", "countryIso", "iso_3166_1"),
    "language": ("language", "lang", "languages"),
    "tags": ("tags", "tag", "genres", "genre", "categories", "category", "labels"),
    "codec": ("codec", "format", "audio_codec", "audioCodec", "mime", "content_type"),
    "bitrate": ("bitrate", "bit_rate", "bitRate", "kbps", "quality"),
    "id": ("id", "uuid", "stationuuid", "stationUuid", "station_id", "stationId", "channel_id", "channelId"),
}


def get_value(record, field, default=None):
    if not isinstance(record, Mapping):
        return default
    for key in ALIASES.get(field, (field,)):
        value = record.get(key)
        if value not in (None, ""):
            return value
    return default


def as_records(payload, preferred_keys=()):
    """Find record arrays in common API envelopes without assuming one wrapper."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if not isinstance(payload, Mapping):
        return []
    for key in (*preferred_keys, "data", "results", "items", "stations", "channels", "streams", "entries", "records"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
        if isinstance(value, Mapping):
            nested = as_records(value, preferred_keys)
            if nested:
                return nested
    # Some APIs return a single record or a mapping keyed by record ID.
    if get_value(payload, "name") and get_value(payload, "url"):
        return [payload]
    values = [value for value in payload.values() if isinstance(value, Mapping)]
    if values and any(get_value(value, "name") or get_value(value, "url") for value in values):
        return values
    return []


def as_text(value):
    if isinstance(value, Mapping):
        return str(value.get("name") or value.get("title") or value.get("code") or "")
    if isinstance(value, (list, tuple, set)):
        return ", ".join(filter(None, (as_text(item).strip() for item in value)))
    return str(value or "").strip()


def as_tags(value):
    if isinstance(value, str):
        values = value.replace(";", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        values = value
    elif value is None:
        values = []
    else:
        values = [value]
    result = []
    seen = set()
    for item in values:
        label = as_text(item)
        key = label.casefold()
        if label and key not in seen:
            seen.add(key)
            result.append(label)
    return result


def normalize_record(record):
    """Return stable source-neutral fields while preserving original metadata."""
    if not isinstance(record, Mapping):
        return None
    nested_station = record.get("station") if isinstance(record.get("station"), Mapping) else {}
    nested_stream = record.get("stream") if isinstance(record.get("stream"), Mapping) else {}
    name = as_text(get_value(record, "name") or get_value(nested_station, "name"))
    url = as_text(get_value(record, "url") or get_value(nested_stream, "url"))
    if not name or not url:
        return None

    def metadata(field, default=None):
        return get_value(record, field) or get_value(nested_station, field) or default

    return {
        "name": name,
        "url": url,
        "country": as_text(metadata("country")),
        "countryCode": as_text(metadata("country_code")),
        "language": as_text(metadata("language")),
        "tags": as_tags(metadata("tags")),
        "codec": as_text(get_value(record, "codec") or get_value(nested_stream, "codec")),
        "bitrate": get_value(record, "bitrate") or get_value(nested_stream, "bitrate", 0),
        "sourceStationId": as_text(get_value(record, "id") or get_value(nested_station, "id")),
    }
