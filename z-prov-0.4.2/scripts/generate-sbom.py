#!/usr/bin/env python3
from __future__ import annotations

import importlib.metadata
import json
import platform
import sys
from pathlib import Path

import z_prov


def main() -> None:
    components = []
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if not name:
            continue
        normalized = name.lower().replace("_", "-")
        components.append({
            "type": "library",
            "name": name,
            "version": distribution.version,
            "purl": f"pkg:pypi/{normalized}@{distribution.version}",
        })
    document = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": "z-prov",
                "version": z_prov.__version__,
            },
            "properties": [
                {"name": "python.version", "value": platform.python_version()},
            ],
        },
        "components": sorted(components, key=lambda item: item["name"].lower()),
    }
    destination = Path(sys.argv[1] if len(sys.argv) > 1 else "dist/sbom.cdx.json")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(destination)


if __name__ == "__main__":
    main()
