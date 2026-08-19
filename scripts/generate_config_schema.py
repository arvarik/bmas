#!/usr/bin/env python3
"""Generate or verify the public JSON schema for bmas.yaml."""

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "daemon" / "src"))

from config_schema import BmasConfig  # noqa: E402

OUTPUT = REPO_ROOT / "docs" / "reference" / "config.schema.json"


def rendered_schema() -> str:
    """Return the stable formatted JSON schema."""
    return json.dumps(BmasConfig.model_json_schema(), indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = rendered_schema()

    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text() != rendered:
            print("Configuration schema is stale. Run scripts/generate_config_schema.py.")
            return 1
        print("PASS: The generated configuration schema is current.")
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(rendered)
    print(f"Wrote {OUTPUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
