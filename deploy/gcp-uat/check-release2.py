"""Read-only check of the Release 2 settings in UAT: which knowledge bases the web chat answers from, whether suggestions
are on for each, and what happened to every suggestion candidate. Changes nothing. Run like setup-release2.py:
    CAA_ADMIN_PASSWORD="$(gcloud secrets versions access latest --secret=caa-uat-admin-password --project=sysfore-nexus)" \
      python deploy/gcp-uat/check-release2.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
setup = __import__("setup-release2")


def value(fields: list[dict], key: str):
    f = next((x for x in fields if x["key"] == key), None)
    return None if f is None else (f["value"], "set here" if f["overridden"] else "from " + f["inherited_from"])


def main() -> None:
    password = os.getenv("CAA_ADMIN_PASSWORD", "")
    if not password:
        sys.exit("Set CAA_ADMIN_PASSWORD from Secret Manager.")
    a = setup.Admin(password)
    web = a.call("GET", "/channels/web")
    bound = next((f.get("value") or [] for f in web.get("fields", []) if f["name"] == "kb_ids"), [])
    kbs = a.call("GET", "/kbs")["kbs"]
    default = next((k for k in kbs if k.get("is_default")), None)
    used = [k for k in kbs if k["id"] in bound] if bound else ([default] if default else [])
    print("Web chat answers from:", ", ".join(f"{k['name']} (id {k['id']})" for k in used) or "nothing found",
          "(bound in Channels)" if bound else "(the default knowledge base: none is bound to the Web UI)")
    print("Suggestions for the Web UI channel:", value(a.call("GET", "/scoped-settings/channel/web")["fields"], "suggestions_enabled"))
    for k in kbs:
        f = a.call("GET", f"/scoped-settings/kb/{k['id']}")["fields"]
        print(f"  KB {k['name']} (id {k['id']}{', default' if k.get('is_default') else ''}): suggestions {value(f, 'suggestions_enabled')}, "
              f"threshold {value(f, 'score_threshold')}, glossary {value(f, 'glossary_mode')}, model {k.get('active_embedding') or 'built-in'}, "
              f"department id {k.get('department_id')}")
    r = a.call("GET", "/reports/suggestions")
    print("Suggestion candidates so far:", {o["name"]: o["count"] for o in r["outcomes"]} or "none stored",
          "| replies without a usable block:", r["no_block"])
    for row in r["rows"]:
        print(f"  shown in {row['channel']} / {row['kb']}: {row['shown']} shown, {row['clicked']} clicked")
    g = a.call("GET", "/settings")["fields"]
    print("Global settings:", {f["name"]: f["value"] for f in g if f["name"].startswith("suggestions_")})


if __name__ == "__main__":
    main()
