"""Phase-by-phase feature checks, run as pytest tests.

Each script in tests/checks/ builds its own throwaway database and vector store, drives the real code (admin API through
a test client, the answering core with a stand-in for the language model) and prints one PASS or FAIL line per behaviour.
A script passes here when it exits cleanly, prints no FAIL and prints at least the number of PASS lines listed below
(so a check that silently stops early is caught). Every script runs with its own temporary data folders, so none of them
can touch the real admin database, chats or documents.

The vector-store checks against a real PostgreSQL (pgvector) are in tests/test_store_contract.py.
"""
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
CHECKS = pathlib.Path(__file__).resolve().parent / "checks"
# script: (what it covers, minimum PASS lines)
EXPECTED = {
    "check_2a": ("users, departments, roles, sign-in and the role-to-route map", 22),
    "check_3a": ("knowledge bases and documents: upload, status, replace, delete", 21),
    "check_3b": ("chunk metadata, parsers, tables and code kept whole", 32),
    "check_3c": ("documents and knowledge bases through the admin API and Editor scope", 32),
    "check_4a": ("groups and document visibility", 28),
    "check_4b": ("access enforced inside the search; the model never sees restricted text", 27),
    "check_5": ("prompts: versions, publish, rollback, department prompts, preview", 34),
    "check_6a": ("guardrails: length, banned topics, personal-data masking, output checks", 31),
    "check_6b": ("guardrails: injection, toxicity, prompt-leak checks", 33),
    "check_7a": ("conversation memory in the database", 30),
    "check_7b": ("summaries, retention job and audited export", 34),
    "check_8a": ("chunking strategies and chunk preview", 33),
    "check_8b": ("embedding models per knowledge base and versioned rebuilds", 32),
    "check_9a": ("usage, cost and prices", 24),
    "check_9b": ("alerts, channel health and notifications", 26),
    "check_9c": ("audit search and reports", 18),
    "scenario_admin": ("Admin journey: providers, people, knowledge base, Slack, prompt, a question, usage and cost, reports, audit", 25),
    "scenario_editor_viewer": ("Editor over HTTPS, restricted document, department prompt; Viewer access, blocked replies, masking", 25),
    "scenario_resilience_governance": ("model fallback and alerts, bad channel credential, store outage, retention, audited export", 29),
}


EXTRA_ENV = {"check_6a": {"GUARDRAILS_ENABLED": "true"}, "check_6b": {"GUARDRAILS_ENABLED": "true"}}   # these load the detection models
# The scenario scripts set up their own data folders and settings (see tests/checks/_scenario.py).


def minimum_pass(name: str) -> int:
    return EXPECTED[name][1]


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_feature_check(name, tmp_path):
    script = CHECKS / f"{name}.py"
    if name == "check_8b" and not (ROOT / "models").exists():
        pytest.skip("the local multilingual model files are not on this machine")
    env = {**os.environ, **EXTRA_ENV.get(name, {}), "ADMIN_DB_PATH": str(tmp_path / "admin.db"), "CHAT_DIR": str(tmp_path / "chats"),
           "CHROMA_PATH": str(tmp_path / "chroma"), "DOCS_DIR": str(tmp_path / "files"), "DATABASE_URL": ""}
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=900)
    lines = out.stdout.splitlines()
    failed = [x for x in lines if x.startswith("FAIL")]
    passed = [x for x in lines if x.startswith("PASS")]
    assert out.returncode == 0, out.stderr[-1500:] + "\n" + "\n".join(lines[-10:])
    assert not failed, "\n".join(failed)
    assert len(passed) >= minimum_pass(name), f"only {len(passed)} checks ran"
