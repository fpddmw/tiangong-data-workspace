"""Filesystem primitives shared by native adapters; no production control loop."""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CREDENTIAL_PARTS = frozenset({'.secret', '.secrets', '.ssh', '.aws'})
CREDENTIAL_FILES = frozenset({
    'credentials', 'credentials.json', 'auth.json', 'oauth.json',
    'token.json', 'tokens.json', 'session.json', 'sessions.json',
    'private-session.json', 'editor-session.json', 'manager-session.json',
    'cookies.json', '.npmrc', '.netrc', '.pypirc',
})
PRIVATE_CONTROL_PARTS = frozenset({
    '.control', '.secret', '.secrets', '.private', '.state',
    '.foundry', '.runtime', '.claude', '.codex', '.toolchain',
})


class ProductionError(ValueError):
    pass


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def is_credential_path(path):
    """True when any component names credential or session material."""
    path = Path(path)
    name = path.name.lower()
    return (any(p.lower() in CREDENTIAL_PARTS or p.lower() == '.env' or p.lower().startswith('.env.') for p in path.parts)
            or name in CREDENTIAL_FILES
            or name.endswith(('.pem', '.key', '.p12', '.pfx')))


def safe_path(root, value, *, must_exist=True):
    """Resolve a workspace location and check its actual filesystem boundary."""
    root = Path(root).resolve(strict=True)
    raw = Path(value)
    path = raw if raw.is_absolute() else root / raw
    path = path.resolve()
    if not path.is_relative_to(root):
        raise ProductionError("Resolved path must stay inside this workspace.")
    if must_exist and not path.is_file():
        raise ProductionError("The exact original file is missing or is not a regular file.")
    return path


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
