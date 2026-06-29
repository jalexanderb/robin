"""
RobinHealth: blob storage for uploaded bill files.

Real, working filesystem I/O by default -- not a sketch -- backed by
STORAGE_DIR (env var; defaults to blob_storage/ alongside pipeline/).
This sandbox's network is locked to package registries + GitHub +
Anthropic's API (checked directly: no cloud object storage endpoint --
S3, GCS, Azure Blob -- is reachable from here, same as the LLM-hosting
and CMS-rate-data domains earlier), so a literal cloud backend can't
actually be exercised in this environment the way it could in a real
deployment.

The interface is deliberately tiny -- save bytes, get back an opaque
key; give back a key, get the bytes -- so swapping this module's
internals for a real S3/GCS/Azure client later doesn't ripple through
callers (api.py only ever calls save()/load(), never touches a path
directly).

save() writes content-addressed by a hash of the bytes, not a random
UUID: re-uploading the same bill twice produces the same key and writes
the file at most once, rather than silently duplicating storage for
identical content.
"""

from __future__ import annotations

import hashlib
import os


_EXTENSION_BY_CONTENT_TYPE = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
}


def _storage_dir() -> str:
    path = os.environ.get(
        "STORAGE_DIR",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "blob_storage"),
    )
    os.makedirs(path, exist_ok=True)
    return path


def save(data: bytes, content_type: str) -> str:
    """
    Write `data` to storage and return its storage_key. Content-addressed
    (sha256 of the bytes) -- saving identical content twice returns the
    same key and writes the file at most once.
    """
    digest = hashlib.sha256(data).hexdigest()
    ext = _EXTENSION_BY_CONTENT_TYPE.get(content_type, "")
    key = digest + ext
    path = os.path.join(_storage_dir(), key)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)
    return key


def load(storage_key: str) -> bytes:
    """Read back the bytes for a storage_key returned by save(). Raises FileNotFoundError if it doesn't exist."""
    # os.path.basename strips any path separators a caller might pass in
    # storage_key -- this function should never be able to read outside
    # _storage_dir(), regardless of what string it's handed.
    path = os.path.join(_storage_dir(), os.path.basename(storage_key))
    with open(path, "rb") as f:
        return f.read()


def exists(storage_key: str) -> bool:
    path = os.path.join(_storage_dir(), os.path.basename(storage_key))
    return os.path.isfile(path)


def delete(storage_key: str) -> bool:
    """
    Delete the blob for a storage_key (used by data-deletion / retention).
    Returns True if a file was removed, False if it didn't exist. os.path.basename
    guards against path traversal, exactly like load()/exists().
    """
    if not storage_key:
        return False
    path = os.path.join(_storage_dir(), os.path.basename(storage_key))
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
