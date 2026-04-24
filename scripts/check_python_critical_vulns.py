#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


GHSA_ALIAS_PATTERN = re.compile(r"^GHSA-[23456789cfghjmpqrvwx]{4}-[23456789cfghjmpqrvwx]{4}-[23456789cfghjmpqrvwx]{4}$")
SEVERITY_PATTERN = re.compile(r">\s*(Critical|High|Moderate|Low) severity\s*<", re.IGNORECASE)


@dataclass(frozen=True)
class CriticalFinding:
    requirements_file: Path
    package: str
    version: str
    advisory: str
    severity: str
    url: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run pip-audit for one or more requirements files and fail only when "
            "a vulnerability resolves to critical severity."
        )
    )
    parser.add_argument("requirements_files", nargs="+", type=Path)
    return parser.parse_args()


def run_pip_audit(requirements_file: Path) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "pip_audit",
        "--progress-spinner",
        "off",
        "-r",
        str(requirements_file),
        "-f",
        "json",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode not in (0, 1):
        raise RuntimeError(
            f"pip-audit failed for {requirements_file} with exit code "
            f"{result.returncode}.\n{result.stderr.strip()}"
        )

    stdout = result.stdout.strip()
    if not stdout:
        raise RuntimeError(
            f"pip-audit produced no JSON output for {requirements_file}.\n"
            f"{result.stderr.strip()}"
        )

    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"pip-audit returned invalid JSON for {requirements_file}: {exc}"
        ) from exc


def find_ghsa_alias(vulnerability: dict[str, Any]) -> str | None:
    candidates = [vulnerability.get("id"), *(vulnerability.get("aliases") or [])]
    for candidate in candidates:
        if isinstance(candidate, str) and GHSA_ALIAS_PATTERN.match(candidate):
            return candidate
    return None


def fetch_github_advisory_severity(ghsa_alias: str) -> str | None:
    request = urllib.request.Request(
        f"https://github.com/advisories/{ghsa_alias}",
        headers={"User-Agent": "OpenCuria-CI-Security-Audit"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            html = response.read().decode("utf-8", "ignore")
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Unable to fetch GitHub advisory {ghsa_alias}: {exc}"
        ) from exc

    match = SEVERITY_PATTERN.search(html)
    if not match:
        return None
    return match.group(1).lower()


def collect_critical_findings(requirements_file: Path) -> tuple[list[CriticalFinding], int]:
    audit_report = run_pip_audit(requirements_file)
    findings: list[CriticalFinding] = []
    vulnerability_count = 0

    for dependency in audit_report.get("dependencies", []):
        package_name = dependency.get("name", "<unknown>")
        package_version = dependency.get("version", "<unknown>")
        for vulnerability in dependency.get("vulns", []):
            vulnerability_count += 1
            ghsa_alias = find_ghsa_alias(vulnerability)
            if not ghsa_alias:
                continue

            severity = fetch_github_advisory_severity(ghsa_alias)
            if severity != "critical":
                continue

            findings.append(
                CriticalFinding(
                    requirements_file=requirements_file,
                    package=package_name,
                    version=package_version,
                    advisory=ghsa_alias,
                    severity=severity,
                    url=f"https://github.com/advisories/{ghsa_alias}",
                )
            )

    return findings, vulnerability_count


def main() -> int:
    args = parse_args()
    critical_findings: list[CriticalFinding] = []
    scanned_vulnerabilities = 0

    for requirements_file in args.requirements_files:
        findings, vulnerability_count = collect_critical_findings(requirements_file)
        critical_findings.extend(findings)
        scanned_vulnerabilities += vulnerability_count

    if critical_findings:
        print("Critical Python vulnerabilities detected:")
        for finding in critical_findings:
            print(
                f"- {finding.requirements_file}: {finding.package}=={finding.version} "
                f"({finding.advisory}, {finding.severity}) {finding.url}"
            )
        return 1

    print(
        "No critical Python vulnerabilities detected "
        f"across {len(args.requirements_files)} requirement file(s). "
        f"Observed {scanned_vulnerabilities} total known vulnerability finding(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
