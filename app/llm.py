"""Minimal LLM helper (Google Gemini REST API over httpx). Returns None when unconfigured or on any
failure, so callers always have a deterministic fallback. The model is swappable via LLM_MODEL."""
from __future__ import annotations

import json
import logging
import re

import httpx

from .config import get_settings

log = logging.getLogger("priceback.llm")

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def complete(system: str, user: str, max_tokens: int = 600, json_mode: bool = False) -> str | None:
    s = get_settings()
    if not s.gemini_api_key:
        return None
    gen_config: dict = {"maxOutputTokens": max_tokens, "temperature": 0}
    if json_mode:
        gen_config["responseMimeType"] = "application/json"
    try:
        r = httpx.post(
            GEMINI_URL.format(model=s.llm_model),
            headers={"x-goog-api-key": s.gemini_api_key, "content-type": "application/json"},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": gen_config,
            },
            timeout=30,
        )
        r.raise_for_status()
        candidates = r.json().get("candidates") or []
        if not candidates:
            return None
        parts = (candidates[0].get("content") or {}).get("parts") or []
        # skip any "thought" parts from thinking models; keep only the answer text
        text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        return text or None
    except Exception as e:  # noqa: BLE001 - any failure -> deterministic fallback
        log.warning("LLM call failed, using fallback: %s", e)
        return None


def extract_json(text: str | None) -> dict | None:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None
