"""SPEC §3.1: never implement IMAP, POP3, Gmail API, Microsoft Graph or any mail OAuth."""

from __future__ import annotations

import ast
import subprocess
import sys

import pytest

from khugjil.settings import Settings
from tests.invariants.helpers import SRC_DIR, locked_packages

pytestmark = pytest.mark.invariant(1)

FORBIDDEN_PACKAGES = {
    "imapclient",
    "imaplib2",
    "aioimaplib",
    "imap-tools",
    "google-api-python-client",
    "google-auth-oauthlib",
    "gmail-api",
    "msal",
    "msgraph-sdk",
    "msgraph-core",
    "o365",
    "exchangelib",
    "mailbox-oauth",
}
FORBIDDEN_MODULES = {
    "imaplib",
    "poplib",
    "imapclient",
    "aioimaplib",
    "imap_tools",
    "googleapiclient",
    "google_auth_oauthlib",
    "msal",
    "msgraph",
    "O365",
    "exchangelib",
}


def test_lockfile_has_no_mailbox_clients() -> None:
    assert locked_packages().isdisjoint(FORBIDDEN_PACKAGES)


def test_source_never_imports_mailbox_modules() -> None:
    offenders = []
    for path in SRC_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            offenders += [f"{path.name}:{n}" for n in names if n.split(".")[0] in FORBIDDEN_MODULES]
    assert offenders == []


def test_importing_every_module_loads_no_mailbox_module() -> None:
    code = (
        "import importlib, pkgutil, sys, khugjil\n"
        "for m in pkgutil.walk_packages(khugjil.__path__, 'khugjil.'):\n"
        "    importlib.import_module(m.name)\n"
        f"bad = sorted(set(sys.modules) & set({sorted(FORBIDDEN_MODULES)!r}))\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == ""


def test_no_mailbox_settings_exist() -> None:
    for name in Settings.model_fields:
        assert not any(word in name for word in ("imap", "pop3", "oauth", "gmail", "graph"))
