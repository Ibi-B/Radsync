"""Shared M3U parsing helpers."""


def parse_m3u(text: str):
    stations = []
    current = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("#EXTINF:"):
            metadata = line.split(",", 1)[1] if "," in line else ""
            attributes = {}

            for part in line.split(" "):
                if "=" in part and not part.startswith("#EXTINF:"):
                    key, value = part.split("=", 1)
                    attributes[key.strip()] = value.strip().strip('"')

            current = {
                "name": metadata.strip(),
                "attributes": attributes,
            }
        elif line.startswith("#"):
            continue
        elif current is not None:
            current["stream_url"] = line
            stations.append(current)
            current = None

    return stations
