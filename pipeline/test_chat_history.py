"""
Tests for chat conversation memory: api._parse_chat_history (normalization) and
llm_client.complete threading prior turns into the request. No DB/network
(httpx.post mocked). Run with: python3 -m pytest test_chat_history.py
"""

import json
import os
from unittest.mock import MagicMock, patch

import api
import llm_client


# ============================================================
# History parsing / normalization
# ============================================================

def test_parse_history_none_and_bad_input():
    assert api._parse_chat_history(None) == []
    assert api._parse_chat_history("not json") == []
    assert api._parse_chat_history(json.dumps({"not": "a list"})) == []


def test_parse_history_keeps_valid_alternating_turns():
    h = json.dumps([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "q2"},
    ])
    assert api._parse_chat_history(h) == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "q2"},
    ]


def test_parse_history_drops_leading_assistant_turns():
    # Robin's opening greeting(s) are assistant turns; messages must start user.
    h = json.dumps([
        {"role": "assistant", "content": "greeting"},
        {"role": "assistant", "content": "privacy note"},
        {"role": "user", "content": "my question"},
    ])
    out = api._parse_chat_history(h)
    assert out == [{"role": "user", "content": "my question"}]


def test_parse_history_merges_consecutive_same_role():
    h = json.dumps([
        {"role": "user", "content": "part 1"},
        {"role": "user", "content": "part 2"},
        {"role": "assistant", "content": "reply"},
    ])
    out = api._parse_chat_history(h)
    assert out == [
        {"role": "user", "content": "part 1\n\npart 2"},
        {"role": "assistant", "content": "reply"},
    ]


def test_parse_history_skips_invalid_entries():
    h = json.dumps([
        {"role": "user", "content": "ok"},
        {"role": "system", "content": "nope"},   # invalid role
        {"role": "assistant", "content": ""},      # empty
        "not a dict",
    ])
    assert api._parse_chat_history(h) == [{"role": "user", "content": "ok"}]


def test_parse_history_caps_turns_and_length():
    turns = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(30)]
    out = api._parse_chat_history(json.dumps(turns), max_turns=10)
    assert len(out) <= 10
    long = json.dumps([{"role": "user", "content": "x" * 9000}])
    assert len(api._parse_chat_history(long, max_chars=4000)[0]["content"]) == 4000


# ============================================================
# llm_client threads history into the request
# ============================================================

def test_openai_path_inserts_history_between_system_and_user():
    fake = MagicMock()
    fake.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
    hist = [{"role": "user", "content": "earlier q"}, {"role": "assistant", "content": "earlier a"}]
    with patch.dict(os.environ, {"LLM_PROVIDER": "openai_compatible"}), \
         patch("httpx.post", return_value=fake) as mock_post:
        llm_client.complete("current q", system="SYS", max_tokens=50, history=hist)
    body = mock_post.call_args.kwargs["json"]
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    assert body["messages"][-1]["content"] == "current q"


def test_anthropic_path_prepends_history_to_messages():
    fake = MagicMock()
    fake.json.return_value = {"content": [{"type": "text", "text": "ok"}]}
    hist = [{"role": "user", "content": "earlier q"}, {"role": "assistant", "content": "earlier a"}]
    with patch.dict(os.environ, {"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k"}), \
         patch("httpx.post", return_value=fake) as mock_post:
        llm_client.complete("current q", system="SYS", max_tokens=50, history=hist)
    body = mock_post.call_args.kwargs["json"]
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "user"]      # history + current
    assert body["messages"][-1]["content"] == "current q"
    assert body["system"] == "SYS"                       # system stays top-level


def test_no_history_preserves_single_turn_shape():
    fake = MagicMock()
    fake.json.return_value = {"choices": [{"message": {"content": "ok"}}]}
    with patch.dict(os.environ, {"LLM_PROVIDER": "openai_compatible"}), \
         patch("httpx.post", return_value=fake) as mock_post:
        llm_client.complete("just this", max_tokens=50)
    body = mock_post.call_args.kwargs["json"]
    assert [m["role"] for m in body["messages"]] == ["user"]
