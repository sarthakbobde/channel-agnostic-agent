"""Switch on the Release 2 features in the UAT admin center, through its API, for testing in the web chat.

Run from the repository root (the admin password goes from Secret Manager straight into the environment, never printed):
    CAA_ADMIN_PASSWORD="$(gcloud secrets versions access latest --secret=caa-uat-admin-password --project=sysfore-nexus)" \
      python deploy/gcp-uat/setup-release2.py [--kb "HR Policies"] [--department HR]

Safe to run again: each step is skipped when it is already done. What it does:
  1. Hindi: approves the built-in Hindi message drafts, adds Hindi as a message language, accepts English and Hindi.
  2. The HR knowledge base: switches it to the local multilingual model, rebuilds the index, then sets the 0.75 threshold.
  3. The HR department's glossary: imports glossaries/hr.csv, publishes it, and switches query expansion on.
  4. Suggested follow-up questions: on for the Web UI channel and for the HR knowledge base.
Every change is recorded in the admin center's audit log under the admin account.
"""
import argparse
import os
import sys
import time
from pathlib import Path

import httpx

URL = os.getenv("CAA_ADMIN_URL", "https://caa-uat-app-251420217705.asia-south1.run.app").rstrip("/") + "/admin/api"
EMAIL = os.getenv("CAA_ADMIN_EMAIL", "agent-admin@sysfore.in")
E5 = "local-multilingual"


def step(text: str) -> None:
    print(f"- {text}", flush=True)


class Admin:
    def __init__(self, password: str):
        self.c = httpx.Client(timeout=120, follow_redirects=False)
        r = self.c.post(URL + "/auth/login", json={"email": EMAIL, "password": password})
        if r.status_code != 200:
            sys.exit(f"Sign-in failed (HTTP {r.status_code}). Check the account and the secret.")
        self.csrf = r.json()["csrf"]

    def call(self, method: str, path: str, body=None, ok=(200, 201, 202)):
        r = self.c.request(method, URL + path, json=body, headers={"X-CSRF-Token": self.csrf} if method != "GET" else {})
        if r.status_code not in ok:
            detail = ""
            try:
                detail = r.json().get("detail", "")
            except ValueError:
                pass
            sys.exit(f"{method} {path} failed (HTTP {r.status_code}): {detail}")
        return r.json() if r.content else {}


def pick(items: list[dict], wanted: str | None, what: str) -> dict:
    if wanted:
        found = [x for x in items if x["name"].lower() == wanted.lower()]
    else:
        found = [x for x in items if "hr" in x["name"].lower().replace("-", " ").split()] or (items if len(items) == 1 else [])
    if len(found) != 1:
        names = ", ".join(repr(x["name"]) for x in items)
        sys.exit(f"Could not tell which {what} to use. Run again with --{what.split()[-1]} NAME. Choices: {names}")
    return found[0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", help="name of the HR knowledge base")
    ap.add_argument("--department", help="name of the HR department")
    args = ap.parse_args()
    password = os.getenv("CAA_ADMIN_PASSWORD", "")
    if not password:
        sys.exit("Set CAA_ADMIN_PASSWORD from Secret Manager (see the top of this file).")
    a = Admin(password)
    print(f"Signed in to {URL} as {EMAIL}")

    # 1. Hindi -----------------------------------------------------------------------------------------------------
    cat = a.call("GET", "/catalog")
    hi = next((l for l in cat["languages"] if l["code"] == "hi"), None)
    if not (hi and hi["complete"]):
        r = a.call("POST", "/catalog/hi/approve-all")
        step(f"Hindi messages: {r['approved']} drafts approved")
    else:
        step("Hindi messages: already approved")
    a.call("PUT", "/settings", {"values": {"catalog_languages": ["hi"], "accepted_languages": ["en", "hi"],
                                           "suggestions_dedup_threshold": 0.95}})
    step("Message languages: hi; questions accepted in: en, hi; suggestion repeat cut-off 0.95 (measured on the policies)")

    # 2. The HR knowledge base on the multilingual model -------------------------------------------------------------
    kb = pick(a.call("GET", "/kbs")["kbs"], args.kb, "knowledge base kb")
    step(f"Knowledge base: {kb['name']} (id {kb['id']}), model in use: {kb.get('active_embedding') or 'built-in MiniLM'}")
    if kb.get("active_embedding") != E5:
        if kb.get("embedding") != E5:
            a.call("PUT", f"/kbs/{kb['id']}", {"embedding": E5})
            step("Embedding model set to the local multilingual model (e5-small)")
        r = a.call("POST", f"/kbs/{kb['id']}/index/rebuild", ok=(200, 202, 409))
        step("Index rebuild started" if "detail" not in r else "Index rebuild already running")
        end = time.time() + 45 * 60
        while time.time() < end:
            time.sleep(15)
            now = next(x for x in a.call("GET", "/kbs")["kbs"] if x["id"] == kb["id"])
            if now.get("active_embedding") == E5:
                step("Rebuild finished: the multilingual index is now answering")
                break
            print("  ...rebuilding", flush=True)
        else:
            sys.exit("The rebuild has not finished after 45 minutes. Check KB configuration, then run this again.")
    a.call("PUT", f"/scoped-settings/kb/{kb['id']}", {"values": {"score_threshold": 0.75, "suggestions_enabled": True}})
    step("Match threshold 0.75 and suggested questions on, for this knowledge base")

    # 3. The HR department's glossary ------------------------------------------------------------------------------------
    departments = a.call("GET", "/departments")["departments"]
    owner = [d for d in departments if d["id"] == kb.get("department_id")]
    # by default, the department that owns the knowledge base: its glossary is the one that applies to it
    dept = pick(departments, args.department, "department") if args.department or not owner else owner[0]
    g = a.call("GET", f"/glossaries/department/{dept['id']}")
    csv_text = (Path(__file__).resolve().parents[2] / "glossaries" / "hr.csv").read_text(encoding="utf-8")
    wanted = {line.split(",", 1)[0].strip().lower() for line in csv_text.splitlines()[1:] if line.strip()}
    have = {t["canonical"].strip().lower() for t in (g["published"] or {}).get("terms", [])}
    if g["published"] and have == wanted:
        step(f"Glossary of {dept['name']}: already published and up to date (v{g['published']['version']}, {len(have)} terms)")
    else:
        g = a.call("POST", f"/glossaries/department/{dept['id']}/import", {"csv": csv_text})
        if g["draft"]["conflicts"]:
            sys.exit("The glossary has conflicts; open Input > Glossary to resolve them.")
        g = a.call("POST", f"/glossaries/versions/{g['draft']['id']}/publish")
        step(f"Glossary of {dept['name']}: {len(g['published']['terms'])} terms imported and published")
    a.call("PUT", f"/scoped-settings/department/{dept['id']}", {"values": {"glossary_mode": "query"}})
    step(f"Glossary expansion on for the {dept['name']} department")

    # 4. Suggested questions in the web chat -----------------------------------------------------------------------------
    a.call("PUT", "/scoped-settings/channel/web", {"values": {"suggestions_enabled": True}})
    step("Suggested questions on for the Web UI channel")
    print("Done. Try it in the web chat: https://caa-uat-web-251420217705.asia-south1.run.app")


if __name__ == "__main__":
    main()
