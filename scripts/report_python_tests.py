#!/usr/bin/env python3
"""Expose pytest failure identities as GitHub annotations, retaining JUnit details."""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def escape(value: str) -> str:
    """Escape GitHub workflow command data, including embedded control characters."""
    clean = re.sub(r"\x1b\[[0-9;]*m", "", value)
    return clean.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main(path: Path) -> None:
    """Print each failing node and bounded failure text from a pytest JUnit report."""
    if not path.is_file():
        print("::warning::No Python JUnit report was produced.")
        return
    root = ET.parse(path).getroot()
    for case in root.iter("testcase"):
        for failure in case:
            if failure.tag not in {"failure", "error"}:
                continue
            node = f"{case.get('classname', '')}::{case.get('name', '')}"
            detail = (failure.get("message") or failure.text or "Test failed")[:2500]
            print(
                f"::error title=Python test failure::{escape(node)}: {escape(detail)}"
            )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
