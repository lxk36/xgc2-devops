#!/usr/bin/env python3
"""Regenerate Lichtblick URDF mesh topic keys with showOutlines=false.

Official XGC product layouts (fs150 / scout / mecanum) do NOT set showOutlines
on the foxglove.Urdf layer. Mesh children look up settings at:

    topics["{link}-{visualIndex}-{geometryType}"]

See core-xgc lichtblick_layout.go (lichtblickThreeDTopics / lichtblickUrdfVisual).
"""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path


def mesh_topic_keys(urdf_path: Path) -> list[str]:
    root = ET.parse(urdf_path).getroot()
    keys: list[str] = []
    for link in root.findall("link"):
        name = link.get("name") or ""
        for i, visual in enumerate(link.findall("visual")):
            geom = visual.find("geometry")
            if geom is None:
                continue
            gtype = None
            for child in list(geom):
                tag = child.tag.split("}")[-1]
                if tag in ("mesh", "box", "cylinder", "sphere"):
                    gtype = tag
                    break
            if gtype:
                keys.append(f"{name}-{i}-{gtype}")
    return keys


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--urdf", required=True, type=Path)
    p.add_argument("--layout", required=True, type=Path, help="layout JSON to update in place")
    p.add_argument("--panel", default="3D!b2sim")
    args = p.parse_args()

    keys = mesh_topic_keys(args.urdf)
    data = json.loads(args.layout.read_text(encoding="utf-8"))
    topics = data["configById"][args.panel]["topics"]
    for key in keys:
        entry = topics.get(key) if isinstance(topics.get(key), dict) else {}
        entry = dict(entry)
        entry["showOutlines"] = False
        topics[key] = entry
    args.layout.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(keys)} showOutlines=false keys into {args.layout}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
