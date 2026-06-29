"""
Tests for Tier 1 #5 (per-case authorization): the access-token hashing and the
case-access middleware. The middleware is exercised directly (no TestClient /
lifespan / DB) by constructing a Request and a fake call_next, with the DB-backed
verify function patched. Run with: python3 -m pytest test_case_access.py
"""

import asyncio
import hashlib
from unittest.mock import patch

from starlette.requests import Request

import api
import repository


# ============================================================
# Token hashing (pure)
# ============================================================

def test_hash_token_is_sha256_and_deterministic():
    assert repository._hash_token("abc") == hashlib.sha256(b"abc").hexdigest()
    assert repository._hash_token("abc") == repository._hash_token("abc")
    assert repository._hash_token("abc") != repository._hash_token("abd")


# ============================================================
# Middleware
# ============================================================

def _request(path, method="GET", token=None):
    headers = []
    if token is not None:
        headers.append((b"x-case-token", token.encode()))
    scope = {"type": "http", "method": method, "path": path, "headers": headers}
    return Request(scope)


def _run_mw(request):
    """Run the case-access middleware with a sentinel call_next; returns the
    response object (sentinel if it passed through, JSONResponse if blocked)."""
    sentinel = object()

    async def call_next(_req):
        return sentinel

    return asyncio.run(api.case_access_middleware(request, call_next)), sentinel


def test_non_case_path_is_never_blocked():
    resp, sentinel = _run_mw(_request("/health"))
    assert resp is sentinel  # passed straight through, no token check


def test_case_path_blocked_when_token_invalid():
    with patch.object(api.repository, "verify_case_access_token", return_value=False):
        resp, sentinel = _run_mw(_request("/cases/abc-123/full", token="wrong"))
    assert resp is not sentinel
    assert resp.status_code == 403


def test_case_path_allowed_when_token_valid():
    with patch.object(api.repository, "verify_case_access_token", return_value=True):
        resp, sentinel = _run_mw(_request("/cases/abc-123/full", token="right"))
    assert resp is sentinel  # passed through to the handler


def test_options_preflight_bypasses_check():
    # CORS preflight must never be blocked, regardless of token.
    with patch.object(api.repository, "verify_case_access_token", return_value=False) as v:
        resp, sentinel = _run_mw(_request("/cases/abc-123/full", method="OPTIONS"))
    assert resp is sentinel
    v.assert_not_called()


def test_db_error_does_not_block_request():
    # A DB hiccup in the check shouldn't turn into a 403; let the handler deal.
    with patch.object(api.repository, "verify_case_access_token", side_effect=RuntimeError("db down")):
        resp, sentinel = _run_mw(_request("/cases/abc-123/full", token="x"))
    assert resp is sentinel


def test_case_id_is_extracted_from_path():
    captured = {}

    def fake_verify(case_id, token):
        captured["case_id"] = case_id
        captured["token"] = token
        return True

    with patch.object(api.repository, "verify_case_access_token", side_effect=fake_verify):
        _run_mw(_request("/cases/the-case-id/phone-script", token="tok"))
    assert captured["case_id"] == "the-case-id"
    assert captured["token"] == "tok"
