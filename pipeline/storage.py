"""
RobinHealth: blob storage for uploaded bill/EOB files and generated letters.

Two backends behind one tiny interface (save bytes -> opaque key; key -> bytes;
exists; delete). The backend is chosen by STORAGE_BACKEND:

  local (default)
    Filesystem under STORAGE_DIR (defaults to blob_storage/ alongside
    pipeline/). Fine for development and tests; on an ephemeral host (e.g.
    Railway) it is NOT durable and is not encrypted by us.

  s3
    Any S3-compatible object store (AWS S3, Cloudflare R2, Backblaze B2, GCS via
    its S3 API, MinIO). Objects are written with server-side encryption at rest.
    This is the production-grade option (durable + encrypted). Configure via:
        S3_BUCKET            (required) bucket name
        AWS_REGION           region, e.g. us-east-1
        S3_ENDPOINT_URL      optional, for non-AWS S3 (R2/B2/MinIO)
        S3_SSE               server-side encryption: "AES256" (default) or "aws:kms"
        S3_SSE_KMS_KEY_ID    KMS key id/arn, used when S3_SSE == "aws:kms"
    Credentials come from the standard boto3 chain (AWS_ACCESS_KEY_ID /
    AWS_SECRET_ACCESS_KEY env, or an instance/role).

Both backends are content-addressed: the key is sha256(bytes) + extension, so
saving identical content twice yields the same key and stores it at most once
(no silent duplication). Path traversal is prevented by reducing any key to its
basename before use, so a key can never reach outside the store.

boto3 is imported lazily, only when the s3 backend is actually used, so the
default (local) path -- and the unit tests -- need no AWS dependency installed.
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


def _backend() -> str:
    return os.environ.get("STORAGE_BACKEND", "local").strip().lower()


def _key_for(data: bytes, content_type: str) -> str:
    """Content-addressed key: sha256 of the bytes + a content-type extension."""
    digest = hashlib.sha256(data).hexdigest()
    return digest + _EXTENSION_BY_CONTENT_TYPE.get(content_type, "")


def _safe_key(storage_key: str) -> str:
    """Reduce a key to a basename so it can never escape the store (works for
    both a filesystem path and a flat object-store namespace)."""
    return os.path.basename(storage_key)


# ============================================================
# Local filesystem backend
# ============================================================

def _storage_dir() -> str:
    path = os.environ.get(
        "STORAGE_DIR",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "blob_storage"),
    )
    os.makedirs(path, exist_ok=True)
    return path


def _local_save(data: bytes, key: str) -> str:
    path = os.path.join(_storage_dir(), key)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(data)
    return key


def _local_load(key: str) -> bytes:
    with open(os.path.join(_storage_dir(), _safe_key(key)), "rb") as f:
        return f.read()


def _local_exists(key: str) -> bool:
    return os.path.isfile(os.path.join(_storage_dir(), _safe_key(key)))


def _local_delete(key: str) -> bool:
    try:
        os.remove(os.path.join(_storage_dir(), _safe_key(key)))
        return True
    except FileNotFoundError:
        return False


# ============================================================
# S3-compatible backend (encrypted at rest)
# ============================================================

_S3_CLIENT = None  # cached boto3 client


def _reset_s3_client() -> None:
    """Test hook: drop the cached client so a new one is built (e.g. under a
    fresh moto mock or after changing env)."""
    global _S3_CLIENT
    _S3_CLIENT = None


def _s3_client():
    global _S3_CLIENT
    if _S3_CLIENT is None:
        import boto3  # lazy: only needed when the s3 backend is selected
        _S3_CLIENT = boto3.client(
            "s3",
            region_name=os.environ.get("AWS_REGION") or None,
            endpoint_url=os.environ.get("S3_ENDPOINT_URL") or None,
        )
    return _S3_CLIENT


def _s3_bucket() -> str:
    bucket = os.environ.get("S3_BUCKET")
    if not bucket:
        raise RuntimeError("STORAGE_BACKEND=s3 but S3_BUCKET is not set")
    return bucket


def _s3_sse_args() -> dict:
    sse = os.environ.get("S3_SSE", "AES256").strip()
    args = {"ServerSideEncryption": sse}
    if sse == "aws:kms":
        kms = os.environ.get("S3_SSE_KMS_KEY_ID")
        if kms:
            args["SSEKMSKeyId"] = kms
    return args


def _s3_save(data: bytes, key: str) -> str:
    # Content-addressed: skip the upload if this exact object already exists.
    if not _s3_exists(key):
        _s3_client().put_object(
            Bucket=_s3_bucket(), Key=key, Body=data, **_s3_sse_args()
        )
    return key


def _s3_load(key: str) -> bytes:
    from botocore.exceptions import ClientError
    try:
        resp = _s3_client().get_object(Bucket=_s3_bucket(), Key=_safe_key(key))
    except ClientError as exc:
        # Normalize a missing object to FileNotFoundError so callers (and the
        # /letters endpoint) keep their existing 404 behavior across backends.
        code = exc.response.get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404", "NotFound"):
            raise FileNotFoundError(key) from exc
        raise
    return resp["Body"].read()


def _s3_exists(key: str) -> bool:
    from botocore.exceptions import ClientError
    try:
        _s3_client().head_object(Bucket=_s3_bucket(), Key=_safe_key(key))
        return True
    except ClientError:
        return False


def _s3_delete(key: str) -> bool:
    existed = _s3_exists(key)
    if existed:
        _s3_client().delete_object(Bucket=_s3_bucket(), Key=_safe_key(key))
    return existed


# ============================================================
# Public interface (backend-dispatching)
# ============================================================

def save(data: bytes, content_type: str) -> str:
    """
    Write `data` and return its content-addressed storage_key. Saving identical
    content twice returns the same key and stores it at most once.
    """
    key = _key_for(data, content_type)
    return _s3_save(data, key) if _backend() == "s3" else _local_save(data, key)


def load(storage_key: str) -> bytes:
    """Read back the bytes for a storage_key. Raises FileNotFoundError if absent."""
    return _s3_load(storage_key) if _backend() == "s3" else _local_load(storage_key)


def exists(storage_key: str) -> bool:
    return _s3_exists(storage_key) if _backend() == "s3" else _local_exists(storage_key)


def delete(storage_key: str) -> bool:
    """
    Delete the blob for a storage_key (data-deletion / retention). Returns True
    if something was removed, False if it didn't exist. Path traversal is
    prevented by _safe_key, exactly as for load()/exists().
    """
    if not storage_key:
        return False
    return _s3_delete(storage_key) if _backend() == "s3" else _local_delete(storage_key)
