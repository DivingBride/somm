#!/usr/bin/env python3
"""
build_context.py — Assemble and print the full somm system prompt.

Usage:
    python build_context.py --path "../Wine Pairings/somm"
    python build_context.py  # uses SOMM_DATA_PATH env var
"""

import argparse
import os
import sys
from pathlib import Path

TABLE_FILES = [
    "Table_1___molecules_to_aromas_t.md",
    "Table_2___molecules_to_aromas_t.md",
    "Table_3___Master_Pairing.md",
    "Table_4___Cooking_Transformatio.md",
    "Table_5___wines_to_aromas.md",
    "Table_6___Physiological_Effects.md",
]

DEPLOYMENT_NOTE = """
---
DEPLOYMENT NOTE: This instance runs as a web application. The screenshot files
referenced in this skill are pre-loaded and passed as image inputs alongside
each user message when relevant. You do not need to read files from any path —
the relevant screenshots are already included in this conversation if applicable.
The reference tables above replace the file-based tables. Answer from this
assembled context only.
---
"""


def assemble_system_prompt(somm_path: Path) -> str:
    parts = []

    # 1. SKILL.md
    skill_file = somm_path / "SKILL.md"
    parts.append(skill_file.read_text(encoding="utf-8"))

    # 2. Deployment note
    parts.append(DEPLOYMENT_NOTE)

    # 3. Reference tables
    parts.append("\n\n---\n\n## REFERENCE TABLES\n\n")
    refs_dir = somm_path / "references"
    for fname in TABLE_FILES:
        fpath = refs_dir / fname
        parts.append(f"### {fname}\n\n")
        parts.append(fpath.read_text(encoding="utf-8"))
        parts.append("\n\n")

    # 4. Book citations
    parts.append("\n\n---\n\n## BOOK CITATIONS\n\n")
    parts.append((refs_dir / "book_citations.md").read_text(encoding="utf-8"))

    # 5. Screenshot index
    parts.append("\n\n---\n\n## SCREENSHOT INDEX\n\n")
    parts.append((somm_path / "screenshots" / "INDEX.md").read_text(encoding="utf-8"))

    # 6. Screenshot note
    parts.append(
        "\n\nThe screenshots themselves are PNG image files located alongside this index. "
        "When a user asks a question related to a chapter that has ✅ screenshots, load and "
        "include the relevant screenshot images in the API call as vision inputs."
    )

    return "".join(parts)


def validate_files(somm_path: Path) -> list[str]:
    missing = []
    required = (
        [somm_path / "SKILL.md"]
        + [somm_path / "references" / f for f in TABLE_FILES]
        + [somm_path / "references" / "book_citations.md"]
        + [somm_path / "screenshots" / "INDEX.md"]
    )
    for f in required:
        if not f.exists():
            missing.append(str(f))
    return missing


def main():
    parser = argparse.ArgumentParser(description="Assemble the somm system prompt.")
    parser.add_argument("--path", help="Path to somm data directory")
    args = parser.parse_args()

    raw_path = args.path or os.environ.get("SOMM_DATA_PATH")
    if not raw_path:
        print("ERROR: Provide --path or set SOMM_DATA_PATH env var.", file=sys.stderr)
        sys.exit(1)

    somm_path = Path(raw_path).resolve()
    print(f"Resolved path: {somm_path}", file=sys.stderr)

    missing = validate_files(somm_path)
    if missing:
        print("ERROR: Missing required files:", file=sys.stderr)
        for f in missing:
            print(f"  - {f}", file=sys.stderr)
        sys.exit(1)

    prompt = assemble_system_prompt(somm_path)

    char_count = len(prompt)
    token_estimate = char_count // 4

    print(prompt)
    print(f"\n\n--- STATS ---", file=sys.stderr)
    print(f"Characters: {char_count:,}", file=sys.stderr)
    print(f"Estimated tokens: {token_estimate:,}", file=sys.stderr)


if __name__ == "__main__":
    main()
