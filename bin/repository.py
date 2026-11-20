#!/usr/bin/env python3
"""Inspect the current Git candidates without modifying the workspace or index.

This is a bounded preparation check, not a credential audit of Git history.
Only candidate working-tree bytes are inspected. Git metadata for an uninitialised
workspace is created in a temporary directory and removed on exit.
"""
import argparse
from bisect import bisect_left
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile


MAX_FILE_BYTES = 5 * 1024 * 1024
SCAN_CHUNK_BYTES = 1024 * 1024
SCAN_OVERLAP_BYTES = 64 * 1024
REVIEWED_FILES_PATH = "docs/workspace/gitlab-reviewed-files.json"
REVIEWED_FILES_SCHEMA = "data-workspace.gitlab-reviewed-files.v1"
CONTROL_FILES = {
    "agent.json", "policy.json", "accounts.json", "roles.json", "runtime.json",
    "storage.json", "sources.json", "engineering.json",
    "README.md", "HISTORY.md", "MIGRATION.md",
}
CONTROL_ORIGINALS = {".control/backups/policy-20260919-pre-codex-supervision.json"}
LOCAL_PARTS = {
    ".secrets", ".state", ".runtime", ".toolchain", ".agents",
    ".claude", ".foundry", ".cache", "node_modules", ".venv", "venv",
    "__pycache__", ".playwright-mcp", ".npm", ".pnpm-store", "local-environment", "worker-target",
    "developer-toolchain",
}
SKILL_ENV_TEMPLATE = ".agents/skills/document-granular-decompose/assets/config.example.env"
SKILL_REQUIREMENTS_LOCK = ".agents/skills/academic-paper-download/requirements.lock"
LOCAL_ROOTS = {"tmp"}
LOCAL_FILES = {".runtime.initialize.json"}
PRIVATE_FILE_NAMES = {"session.json", "sessions.json", "private-session.json", "credentials.json", "oauth.json", "auth.json",
                      "connections.local.json", "worker-local-connection.json"}
DEPENDENCY_LOCK_FILES = {"Cargo.lock", "yarn.lock", "uv.lock", "poetry.lock", "Pipfile.lock", "bun.lock"}
NATIVE_SUFFIX = re.compile(r"(?:\.(?:sqlite3?|db)(?:-.*)?|\.(?:lock|pid)|\.session\.json)$", re.IGNORECASE)
TOKEN_PATTERNS = (
    ("private-key", re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    ("github-token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("gitlab-token", re.compile(rb"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("signed-jwt", re.compile(rb"\beyJ[A-Za-z0-9_-]{10,2048}\.eyJ[A-Za-z0-9_-]{10,8192}\.[A-Za-z0-9_-]{32,1024}\b")),
    ("aws-access-key", re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("api-secret", re.compile(rb"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{40,}\b")),
    ("supabase-secret", re.compile(rb"\bsb_secret_[A-Za-z0-9_-]{20,}\b")),
    ("slack-token", re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("stripe-secret", re.compile(rb"\bsk_live_[A-Za-z0-9]{20,}\b")),
    ("google-api-key", re.compile(rb"\bAIza[A-Za-z0-9_-]{35}\b")),
)
TOKEN_PREFIXES = {
    "private-key": (b"-----BEGIN ",),
    "github-token": (b"ghp_", b"gho_", b"ghu_", b"ghs_", b"ghr_", b"github_pat_"),
    "gitlab-token": (b"glpat-",),
    "signed-jwt": (b"eyJ",),
    "aws-access-key": (b"AKIA", b"ASIA"),
    "api-secret": (b"sk-",),
    "supabase-secret": (b"sb_secret_",),
    "slack-token": (b"xox",),
    "stripe-secret": (b"sk_live_",),
    "google-api-key": (b"AIza",),
}


class InspectionError(Exception):
    """A safe, fixed diagnostic; never include raw command output."""


def git_environment():
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_ATTR_NOSYSTEM="1", LC_ALL="C")
    return env


def git_run(prefix, arguments, data=None, accepted=(0,)):
    try:
        result = subprocess.run(prefix + arguments, input=data, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=git_environment(), check=False)
    except OSError as error:
        raise InspectionError("Git could not be executed") from error
    if result.returncode not in accepted:
        raise InspectionError("Git could not enumerate the selected working tree")
    return result.stdout


@contextmanager
def repository_git(root, isolated=False):
    base = ["git", "--no-optional-locks", "-c", "core.excludesFile=" + os.devnull,
            "-c", "core.fsmonitor=false"]
    if not isolated and os.path.lexists(root / ".git"):
        prefix = base + ["-C", str(root)]
        top = git_run(prefix, ["rev-parse", "--show-toplevel"]).decode("utf-8", "surrogateescape").strip()
        if Path(top).resolve() != root:
            raise InspectionError("The selected directory must be the repository root")
        yield prefix, "existing"
    else:
        with tempfile.TemporaryDirectory(prefix="workspace-repository-check-") as temporary:
            gitdir = str(Path(temporary) / "repository.git")
            git_run(base, ["init", "--bare", "--template=", "--quiet", gitdir])
            yield base + ["-C", str(root), "--git-dir=" + gitdir, "--work-tree=" + str(root)], "temporary"


def paths_from_git(data):
    return {os.fsdecode(path) for path in data.split(b"\0") if path}


def enumerate_candidates(root):
    with repository_git(root) as (prefix, mode):
        tracked = paths_from_git(git_run(prefix, ["ls-files", "--cached", "--full-name", "-z"]))
        untracked = paths_from_git(git_run(prefix, ["ls-files", "--others", "--exclude-standard", "--full-name", "-z"]))
        ignored_tracked = set()
        if tracked:
            data = b"".join(os.fsencode(path) + b"\0" for path in sorted(tracked))
            ignored_tracked = paths_from_git(git_run(
                prefix, ["check-ignore", "--no-index", "--stdin", "-z"], data, (0, 1)))
        candidates = sorted(tracked | untracked)
        selected = set(candidates) - ignored_tracked
        portable_attribute_directories = {}

        def attributes_are_portable(relative):
            # Git reads ignored working-tree attributes too. Do not admit a
            # large file using rules that will be absent from another checkout.
            portable = True
            for directory in reversed(PurePosixPath(relative).parents):
                if directory not in portable_attribute_directories:
                    attribute = (directory / ".gitattributes").as_posix()
                    portable_attribute_directories[directory] = portable and (
                        attribute in selected or not os.path.lexists(root / attribute))
                portable = portable_attribute_directories[directory]
            return portable

        lfs = set()
        if candidates:
            data = b"".join(os.fsencode(path) + b"\0" for path in candidates)
            # Only committed-capable working-tree attributes establish LFS intent;
            # local .git/info/attributes must not admit a large file on their own.
            with repository_git(root, isolated=True) as (attribute_prefix, _):
                attributes = git_run(attribute_prefix, ["check-attr", "-z", "--stdin", "filter"], data).split(b"\0")
            for index in range(0, len(attributes) - 2, 3):
                relative = os.fsdecode(attributes[index])
                if (attributes[index + 1:index + 3] == [b"filter", b"lfs"]
                        and attributes_are_portable(relative)):
                    lfs.add(relative)
        return candidates, tracked, ignored_tracked, mode, lfs


def reviewed_environment_template(relative):
    parts = PurePosixPath(relative).parts
    return (len(parts) > 1 and parts[-1] == ".env.example"
            and not any(part == ".env" or part.startswith(".env.") for part in parts[:-1]))


def forbidden_path(relative, reviewed_template=False):
    parts = PurePosixPath(relative).parts
    if not parts or relative.startswith("/") or ".." in parts or ".git" in parts:
        return "invalid-path"
    if relative != SKILL_ENV_TEMPLATE and any(part.endswith(".env") for part in parts):
        return "environment-file"
    if any(part == ".env" or part.startswith(".env.") for part in parts):
        if relative != ".env.example" and not (reviewed_template and reviewed_environment_template(relative)):
            return "environment-file"
    if ".control" in parts:
        if relative not in CONTROL_ORIGINALS and (len(parts) != 2 or parts[0] != ".control" or parts[1] not in CONTROL_FILES):
            return "local-state"
    local_parts = parts[1:] if len(parts) > 2 and parts[:2] == (".agents", "skills") else parts
    if any(part in LOCAL_PARTS for part in local_parts) or parts[0] in LOCAL_ROOTS or relative in LOCAL_FILES:
        return "local-state"
    directories = parts[:-1]
    if ("pydeps" in directories or parts[-1].startswith("~$")
            or any(parent.startswith("toolkit-") and child == "unpacked"
                   for parent, child in zip(directories, directories[1:]))
            or any((parent == "runtime" and child.startswith("python-"))
                   or (parent, child) == ("extracted", "runtime")
                   for parent, child in zip(parts, parts[1:]))
            or any(parent.startswith("foundry-managed-adoption-")
                   and child in {"native-input", "retained-original-native"}
                   for parent, child in zip(parts, parts[1:]))
            or any(parent == "components" and leaf == "root"
                   for parent, _, leaf in zip(parts, parts[1:], parts[2:]))
            or any((parent, child, leaf) == ("home", "cache", "pnpm")
                   or (parent.startswith("foundry-managed-adoption-")
                       and (child, leaf) == ("cache", "components"))
                   for parent, child, leaf in zip(directories, directories[1:], directories[2:]))):
        return "local-state"
    if any(part.lower() in PRIVATE_FILE_NAMES
           or (NATIVE_SUFFIX.search(part) and part not in DEPENDENCY_LOCK_FILES
               and relative != SKILL_REQUIREMENTS_LOCK)
           or part.lower().endswith((".pem", ".key")) for part in parts):
        return "local-state"
    return None


def category(relative):
    first = PurePosixPath(relative).parts[0]
    if first in {"bin", "tests", "docs", "roles", "workflows", "templates"}:
        return first
    return "root" if "/" not in relative else "other"


def candidate_stat(root, relative):
    """Identify links without following them, including linked ancestors."""
    current = root
    parts = PurePosixPath(relative).parts
    for part in parts:
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            return info, "symlink"
    if not stat.S_ISREG(info.st_mode):
        return info, "not-regular-file"
    return info, None


def portable_material_link(root, relative, candidates, selected):
    """Allow a relative leaf alias only when its actual material is selected too.

    Directory aliases need a selected regular descendant. Link bytes are retained
    by Git; the target is inspected through its own normal candidate path.
    """
    # Control/template files retain their regular-file invariants; aliases are
    # for material custody, not a way around their path-specific checks.
    if relative in {".env.example", ".gitignore", ".gitattributes"} or relative.startswith(".control/"):
        return False
    link = root / relative
    # A tracked path can have an ancestor changed to a link after enumeration.
    # Such a path never qualifies as a leaf alias.
    ancestor = root
    for part in PurePosixPath(relative).parts[:-1]:
        ancestor = ancestor / part
        if ancestor.is_symlink():
            return False
    raw = os.readlink(link)
    if os.path.isabs(raw):
        return False
    lexical_target = Path(os.path.normpath(link.parent / raw))
    if not lexical_target.is_relative_to(root):
        return False
    target_relative = lexical_target.relative_to(root).as_posix()
    if forbidden_path(target_relative):
        return False
    target_info, problem = candidate_stat(root, target_relative)
    # Relative aliases to another alias or through a linked directory are refused.
    if problem == "symlink":
        return False
    target = lexical_target.resolve(strict=True)
    if not target.is_relative_to(root):
        return False
    if stat.S_ISREG(target_info.st_mode):
        return target_relative in selected
    if not stat.S_ISDIR(target_info.st_mode):
        return False
    prefix = target_relative.rstrip("/") + "/"
    index = bisect_left(candidates, prefix)
    for candidate_index in range(index, len(candidates)):
        candidate = candidates[candidate_index]
        if not candidate.startswith(prefix):
            break
        if candidate not in selected or forbidden_path(candidate):
            continue
        _, descendant_problem = candidate_stat(root, candidate)
        if descendant_problem is None:
            return True
    return False


def empty_environment_template(data):
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return False
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not re.fullmatch(r"(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*\s*=\s*(?:\"\"|'')?\s*(?:#.*)?", line):
            return False
    return True


def scan_secrets(candidate, digest=None):
    """Stream complete bytes with enough overlap for bounded JWT matches.

    Diagnostics contain only pattern names and line numbers, never matched bytes.
    """
    found = {}
    carry = b""
    lines_read = 0
    bytes_read = 0
    while True:
        chunk = candidate.read(SCAN_CHUNK_BYTES)
        if not chunk:
            return found, bytes_read
        if digest is not None:
            digest.update(chunk)
        data = carry + chunk
        start_line = lines_read - carry.count(b"\n")
        for code, expression in TOKEN_PATTERNS:
            if code not in found and any(prefix in data for prefix in TOKEN_PREFIXES[code]):
                match = expression.search(data)
                if match:
                    found[code] = start_line + data.count(b"\n", 0, match.start()) + 1
        lines_read += chunk.count(b"\n")
        bytes_read += len(chunk)
        carry = data[-SCAN_OVERLAP_BYTES:]


def load_reviewed_files(root, selected, tracked):
    """Load only an indexed, portable manifest; never let it grant path access."""
    path = root / REVIEWED_FILES_PATH
    if not os.path.lexists(path):
        return {}, None
    if REVIEWED_FILES_PATH not in selected or REVIEWED_FILES_PATH not in tracked:
        return {}, "reviewed-manifest-not-indexed"

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        info, problem = candidate_stat(root, REVIEWED_FILES_PATH)
        if problem or info.st_size > MAX_FILE_BYTES:
            return {}, "reviewed-manifest-invalid"
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as candidate:
            opened = os.fstat(candidate.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                return {}, "reviewed-manifest-invalid"
            data = candidate.read(MAX_FILE_BYTES + 1)
            finished = os.fstat(candidate.fileno())
            if (len(data) > MAX_FILE_BYTES or finished.st_size != len(data)
                    or (finished.st_size, finished.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns)):
                return {}, "reviewed-manifest-invalid"
        manifest = json.loads(data, object_pairs_hook=unique_object)
        if (not isinstance(manifest, dict) or set(manifest) != {"schema", "files"}
                or manifest["schema"] != REVIEWED_FILES_SCHEMA or not isinstance(manifest["files"], dict)):
            return {}, "reviewed-manifest-invalid"
        token_codes = {code for code, _ in TOKEN_PATTERNS}
        for relative, entry in manifest["files"].items():
            if (not isinstance(relative, str) or not relative or relative.startswith("/")
                    or PurePosixPath(relative).as_posix() != relative
                    or ".." in PurePosixPath(relative).parts
                    or not isinstance(entry, dict) or set(entry) != {"sha256", "allow", "reason"}
                    or not isinstance(entry["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
                    or not isinstance(entry["reason"], str) or not entry["reason"].strip()
                    or not isinstance(entry["allow"], list) or not entry["allow"]
                    or not all(isinstance(code, str) for code in entry["allow"])):
                return {}, "reviewed-manifest-invalid"
            codes = set(entry["allow"])
            if (len(codes) != len(entry["allow"]) or codes - token_codes - {"environment-file"}
                    or ("environment-file" in codes and not reviewed_environment_template(relative))):
                return {}, "reviewed-manifest-invalid"
        return manifest["files"], None
    except (OSError, RuntimeError, ValueError, UnicodeError):
        return {}, "reviewed-manifest-invalid"


def inspect_candidate(relative, root, candidates, selected, lfs, reviewed, ignored_tracked):
    """Inspect one path; the parent retains all enumeration and admission work."""
    issues = []
    total_bytes = 0

    def issue(relative, code, line=None):
        record = {"path": relative, "code": code}
        if line is not None:
            record["line"] = line
        issues.append(record)

    if relative in ignored_tracked:
        issue(relative, "tracked-but-ignored")
    review = reviewed.get(relative)
    approved_codes = set(review["allow"]) if review else set()
    forbidden = forbidden_path(relative, reviewed_template="environment-file" in approved_codes)
    if forbidden:
        issue(relative, forbidden)
        return total_bytes, issues  # Never open environment secrets or native runtime state.
    try:
        info, problem = candidate_stat(root, relative)
        if problem:
            if problem == "symlink" and not review:
                try:
                    portable = portable_material_link(root, relative, candidates, selected)
                except (OSError, RuntimeError):
                    portable = False
                if portable:
                    return total_bytes, issues
            issue(relative, problem)
            return total_bytes, issues
        total_bytes += info.st_size
        if info.st_size > MAX_FILE_BYTES and relative not in lfs:
            issue(relative, "file-too-large")
            return total_bytes, issues
        # O_NOFOLLOW also prevents a final-component symlink swapped after lstat.
        descriptor = os.open(root / relative, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as candidate:
            opened = os.fstat(candidate.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                issue(relative, "file-changed-during-check")
                return total_bytes, issues
            if relative in {".env.example", SKILL_ENV_TEMPLATE}:
                template = candidate.read(MAX_FILE_BYTES + 1)
                if relative == SKILL_ENV_TEMPLATE:
                    template = template.replace(
                        b"UNSTRUCTURED_AUTH_TOKEN=replace_with_fastapi_bearer_token",
                        b"UNSTRUCTURED_AUTH_TOKEN=",
                    ).replace(
                        b"UNSTRUCTURED_API_BASE_URL=https://your-unstructured-host:7770",
                        b"UNSTRUCTURED_API_BASE_URL=",
                    )
                if not empty_environment_template(template):
                    issue(relative, "nonempty-environment-template")
                candidate.seek(0)
            digest = hashlib.sha256() if review else None
            findings, bytes_read = scan_secrets(candidate, digest)
            finished = os.fstat(candidate.fileno())
            if (finished.st_size != bytes_read or finished.st_size != opened.st_size
                    or finished.st_mtime_ns != opened.st_mtime_ns):
                issue(relative, "file-changed-during-check")
    except (OSError, RuntimeError):
        issue(relative, "file-unavailable")
        return total_bytes, issues
    if review and digest.hexdigest() != review["sha256"]:
        issue(relative, "reviewed-file-mismatch")
        approved_codes = set()
    for code, line in findings.items():
        if code not in approved_codes:
            issue(relative, code, line)

    return total_bytes, issues


_WORKER_STATE = None


def initialize_inspection_worker(root, candidates, selected, lfs, reviewed, ignored_tracked):
    global _WORKER_STATE
    _WORKER_STATE = (root, candidates, selected, lfs, reviewed, ignored_tracked)


def inspect_worker_candidate(relative):
    return inspect_candidate(relative, *_WORKER_STATE)


def inspect(root, include_files=False, workers=1):
    if not 1 <= workers <= 8:
        raise InspectionError("Worker count must be between 1 and 8")
    candidates, tracked, ignored_tracked, mode, lfs = enumerate_candidates(root)
    selected = set(candidates) - ignored_tracked
    issues = []
    counts = Counter(category(relative) for relative in candidates)
    total_bytes = 0
    reviewed, manifest_problem = load_reviewed_files(root, selected, tracked)
    if manifest_problem:
        issues.append({"path": REVIEWED_FILES_PATH, "code": manifest_problem})
    state = (root, candidates, selected, lfs, reviewed, ignored_tracked)

    def collect(results):
        nonlocal total_bytes
        for inspected_bytes, candidate_issues in results:
            total_bytes += inspected_bytes
            issues.extend(candidate_issues)

    if workers == 1:
        collect(inspect_candidate(relative, *state) for relative in candidates)
    else:
        try:
            with ProcessPoolExecutor(max_workers=workers, initializer=initialize_inspection_worker,
                                     initargs=state) as executor:
                # map retains candidate order; chunks batch only path names.
                collect(executor.map(inspect_worker_candidate, candidates, chunksize=32))
        except (OSError, RuntimeError) as error:
            raise InspectionError("Parallel working-tree inspection could not complete") from error

    result = {
        "schema": "data-workspace.repository-check.v1",
        "ok": not issues,
        "git_metadata": mode,
        "scope": "current-working-tree-candidates; no-history-or-staged-content-audit",
        "file_count": len(candidates),
        "tracked_count": len(tracked),
        "inspected_bytes": total_bytes,
        "categories": dict(sorted(counts.items())),
        "issues": issues,
    }
    if include_files:
        result["files"] = candidates
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    default_root = Path(__file__).resolve().parents[1]
    parser.add_argument("--root", type=Path, default=default_root)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("check", "files"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--root", type=Path, default=argparse.SUPPRESS)
        subparser.add_argument("--workers", type=int, choices=range(1, 9), default=1)
    arguments = parser.parse_args(argv)
    root = arguments.root.resolve()
    try:
        if not root.is_dir():
            raise InspectionError("The selected root is not a directory")
        result = inspect(root, include_files=arguments.command == "files", workers=arguments.workers)
    except InspectionError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=True, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
