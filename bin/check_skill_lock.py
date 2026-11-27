"""Verify the Skill files shipped with this checkout."""

import hashlib
import json
from pathlib import Path


def locked_files(root):
    lock = json.loads((root / "skills-lock.json").read_text())
    entries = [(Path(skill["path"]) / file["path"], file)
               for skill in lock["skills"] for file in skill["files"]]
    entries.extend((Path(file["path"]), file) for file in lock.get("support_files", []))
    for path, entry in entries:
        if path.parts[:2] != (".agents", "skills") or len(path.parts) < 3 or ".." in path.parts:
            raise ValueError(f"Invalid locked Skill path: {path}")
        yield path.as_posix(), entry


def main():
    root = Path(__file__).resolve().parents[1]
    files = list(locked_files(root))
    for relative, entry in files:
        path = root / relative
        if not path.is_file():
            raise SystemExit(f"Missing locked Skill file: {relative}")
        contents = path.read_bytes()
        if len(contents) != entry["bytes"] or hashlib.sha256(contents).hexdigest() != entry["sha256"]:
            raise SystemExit(f"Changed locked Skill file: {relative}")
    print(f"Verified {len(files)} locked Skill and support files.")


if __name__ == "__main__":
    main()
