"""Tests never call Groq: the key is removed for every test, and the narration cache
lives in memory unless a test passes its own."""
import pytest


@pytest.fixture(autouse=True)
def _no_groq(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("NARRATION_CACHE_PATH", ":memory:")
