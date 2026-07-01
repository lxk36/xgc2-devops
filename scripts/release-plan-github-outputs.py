#!/usr/bin/env python3
"""Write GitHub Actions matrix outputs for a release plan."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--max-layers", type=int, default=8)
    args = parser.parse_args()

    with Path(args.plan).open("r", encoding="utf-8") as handle:
        plan: dict[str, Any] = json.load(handle)
    layers = plan.get("layers", [])
    if not isinstance(layers, list):
        raise ValueError("release plan layers must be a list")
    if len(layers) > args.max_layers:
        raise ValueError(
            f"release plan has {len(layers)} layers, but workflow supports {args.max_layers}"
        )

    print(f"layer_count={len(layers)}")
    for index in range(1, args.max_layers + 1):
        layer = layers[index - 1] if index <= len(layers) else []
        if not isinstance(layer, list):
            raise ValueError(f"release plan layer {index} must be a list")
        include = [
            {
                "product_id": str(item["id"]),
                "repository": str(item["repository"]),
                "ref": str(item["ref"]),
                "workflow": str(item["workflow"]),
            }
            for item in layer
        ]
        print(f"layer_{index}_count={len(include)}")
        print(
            f"layer_{index}_matrix="
            f"{json.dumps({'include': include}, separators=(',', ':'))}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
