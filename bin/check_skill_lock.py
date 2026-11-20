"""Verify the Skill files shipped with this checkout."""

import hashlib
import json
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    lock = json.loads((root / "skills-lock.json").read_text())
    count = 0
    for skill in lock["skills"]:
        directory = root / skill["path"]
        for entry in skill["files"]:
            path = directory / entry["path"]
            if not path.is_file():
                raise SystemExit(f"Missing locked Skill file: {path.relative_to(root)}")
            contents = path.read_bytes()
            if len(contents) != entry["bytes"] or hashlib.sha256(contents).hexdigest() != entry["sha256"]:
                raise SystemExit(f"Changed locked Skill file: {path.relative_to(root)}")
            count += 1
    print(f"Verified {len(lock['skills'])} Skills and {count} file hashes.")


if __name__ == "__main__":
    main()
