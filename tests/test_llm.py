"""Gemini LLM helper: request shape and fallback behaviour. httpx.post is stubbed; no real network."""
import httpx

from app import llm


class _Resp:
    def __init__(self, status: int, body: dict):
        self.status_code = status
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("POST", "https://example.test")
            raise httpx.HTTPStatusError("error", request=req, response=httpx.Response(self.status_code, request=req))

    def json(self):
        return self._body


def test_no_key_returns_none_without_calling_api(monkeypatch):
    called = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: called.append(1))
    assert llm.complete("sys", "hi") is None
    assert called == []


def test_request_shape_and_thought_parts_skipped(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "gemini-test-model")
    seen = {}

    def fake_post(url, *, headers, json, timeout):
        seen.update(url=url, headers=headers, body=json)
        return _Resp(200, {"candidates": [{"content": {"parts": [
            {"text": "thinking about it...", "thought": True},
            {"text": '{"window_days": 30}'},
        ]}}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    out = llm.complete("SYSTEM PROMPT", "user text", max_tokens=800, json_mode=True)

    assert out == '{"window_days": 30}'
    assert seen["url"].endswith("/models/gemini-test-model:generateContent")
    assert seen["headers"]["x-goog-api-key"] == "test-key"
    body = seen["body"]
    assert body["systemInstruction"] == {"parts": [{"text": "SYSTEM PROMPT"}]}
    assert body["contents"] == [{"role": "user", "parts": [{"text": "user text"}]}]
    assert body["generationConfig"]["maxOutputTokens"] == 800
    assert body["generationConfig"]["responseMimeType"] == "application/json"

    # without json_mode no responseMimeType is sent
    llm.complete("s", "u", max_tokens=400)
    assert "responseMimeType" not in seen["body"]["generationConfig"]


def test_empty_candidates_or_http_429_returns_none(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(200, {"candidates": []}))
    assert llm.complete("s", "u") is None
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(429, {"error": {"code": 429}}))
    assert llm.complete("s", "u") is None
