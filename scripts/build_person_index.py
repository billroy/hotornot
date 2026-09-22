"""Build the deterministic news-pump person-name lookup asset."""

from __future__ import annotations

import csv
import gzip
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "names" / "person_2025_update.csv"
OUTPUT = ROOT / "names" / "person_index.json.gz"


def normalize_person_key(value: str) -> str:
    parts = re.findall(r"[^\W_]+", value.casefold(), re.UNICODE)
    normalized = []
    initials = []
    for part in parts:
        if len(part) == 1 and part.isalpha():
            initials.append(part)
            continue
        if initials:
            normalized.append("".join(initials))
            initials = []
        normalized.append(part)
    if initials:
        normalized.append("".join(initials))
    return " ".join(normalized)


def build_index() -> dict[str, str]:
    people: dict[str, tuple[float, str]] = {}
    with SOURCE.open(encoding="utf-8", newline="") as source_file:
        for row in csv.DictReader(source_file):
            if row["is_group"].strip().upper() == "TRUE":
                continue
            canonical_name = " ".join(row["name"].split())
            # Single-token entries collide heavily with surnames, places, and
            # ordinary headline words. Require an explicit multi-part name.
            if len(canonical_name.split()) < 2:
                continue
            key = normalize_person_key(canonical_name)
            if not key:
                continue
            score = float(row["hpi"] or 0)
            if key not in people or score > people[key][0]:
                people[key] = (score, canonical_name)
    return {key: value[1] for key, value in sorted(people.items())}


def main() -> None:
    payload = json.dumps(build_index(), ensure_ascii=False, separators=(",", ":")).encode()
    with OUTPUT.open("wb") as output_file:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output_file, compresslevel=9, mtime=0) as gzip_file:
            gzip_file.write(payload)


if __name__ == "__main__":
    main()
