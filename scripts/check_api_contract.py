#!/usr/bin/env python3
"""Pin the HTTP contract: regenerate the OpenAPI document and byte-compare.

The committed snapshot `docs/api/openapi.json` is the machine-readable
contract clients build against, so it must never drift silently from the code.

Usage:
    python scripts/check_api_contract.py            # verify (CI + local gate)
    python scripts/check_api_contract.py --write    # refresh the snapshot
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "docs" / "api" / "openapi.json"

def render() -> str:
    """Generate the OpenAPI document from the live application."""
    sys.path.insert(0, str(ROOT / "src"))
    with tempfile.TemporaryDirectory() as tmp:
        # Never touch a real device store or a real terminology database while
        # rendering a static document.
        os.environ["WUWATERM_API_DEVICE_DB_PATH"] = str(Path(tmp) / "devices.db")
        os.environ.setdefault("WUWATERM_DB_PATH", "data/terms.db")
        from wuwaterm_api.app import create_app

        document = create_app().openapi()
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="write the regenerated document to the snapshot path",
    )
    args = parser.parse_args(argv)

    generated = render()
    if args.write:
        SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT.write_text(generated, encoding="utf-8")
        print(f"wrote {SNAPSHOT.relative_to(ROOT).as_posix()}")
        return 0

    if not SNAPSHOT.is_file():
        print(f"missing contract snapshot: {SNAPSHOT}", file=sys.stderr)
        return 1
    committed = SNAPSHOT.read_text(encoding="utf-8")
    if committed != generated:
        print(
            "OpenAPI contract drift: the committed snapshot does not match the "
            "generated document. Run `python scripts/check_api_contract.py "
            "--write` and review the diff before committing.",
            file=sys.stderr,
        )
        return 1
    print("api contract ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
