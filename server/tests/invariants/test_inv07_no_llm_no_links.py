"""SPEC §3.7: no LLM in the processing path, no link following."""

from __future__ import annotations

import ast

import pytest

from tests.fixtures.emails import Mailer
from tests.invariants.helpers import SRC_DIR, evaluate, locked_packages

pytestmark = pytest.mark.invariant(7)

LLM_PACKAGES = {
    "openai",
    "anthropic",
    "langchain",
    "langchain-core",
    "langchain-openai",
    "llama-index",
    "llama-cpp-python",
    "transformers",
    "cohere",
    "mistralai",
    "google-generativeai",
    "google-genai",
    "ollama",
    "litellm",
    "vllm",
}
NETWORK_MODULES = {"httpx", "requests", "urllib", "aiohttp", "socket", "http", "websockets"}


def test_no_llm_sdk_in_lockfile() -> None:
    assert locked_packages().isdisjoint(LLM_PACKAGES)


def test_ingest_package_imports_no_network_clients() -> None:
    offenders = []
    for path in (SRC_DIR / "ingest").rglob("*.py"):
        if path.name == "cf_hook.py":  # receives HTTP, never fetches
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            offenders += [f"{path.name}:{m}" for m in mods if m.split(".")[0] in NETWORK_MODULES]
    assert offenders == []


def test_links_and_images_are_never_fetched(mailer: Mailer) -> None:
    from khugjil.ingest.checks import Accepted

    mailer.lookups.clear()
    r = evaluate(mailer.bank_mail(links=True), mailer)
    assert isinstance(r, Accepted)
    # Sockets are disabled by pytest-socket, so any fetch would raise. The only external
    # lookups allowed are DKIM key records.
    assert all(b"._domainkey." in name for name in mailer.lookups)
    assert "tracker.example" not in repr(r)
