import os, sys, tempfile, time, dataclasses, types
sys.path.insert(0, os.getcwd())
tmp = tempfile.mkdtemp()
import chromadb
from sqlalchemy import select
from app import retriever, global_settings, config, groups, identity, chat_service, rag, memory
from app.admin import users
from app.channels import core
from app.channels.base import Envelope
from app.db import Database, Department, DepartmentEditor
from app.knowledge import kbs, documents, versions, access
from langchain_core.messages import AIMessage

db = Database(os.path.join(tmp, "t.db")); db.migrate()
client = chromadb.PersistentClient(path=os.path.join(tmp, "chroma"))
retriever.get_client = lambda: client
kbs.get_db = documents.get_db = global_settings.get_db = versions.get_db = identity.get_db = lambda: db
import app.db as appdb
appdb.get_db = lambda: db
cfg = dataclasses.replace(config.settings, docs_dir=os.path.join(tmp, "files"), chat_dir=os.path.join(tmp, "chats"))
documents.settings = cfg
memory.settings = cfg
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg)
import app.ratelimit as _rl; _rl.allow = lambda key, limit, now=None: (True, 0)     # the per-person question limit is tested in tests/test_hardening.py


# ---- a spy standing in for the LLM: records EVERYTHING it is sent
sent = []


class Spy:
    def __init__(self, reply): self.reply = reply
    def invoke(self, messages, **kw):
        sent.append("\n".join(str(m.content) for m in messages)); return AIMessage(content=self.reply)
    def stream(self, messages, **kw):
        sent.append("\n".join(str(m.content) for m in messages)); yield AIMessage(content="Answer based on context [1].")


import langchain_core.messages as lm
chat_service.get_chat_model = lambda: Spy("ROUTER")
rag.get_chat_model = lambda: Spy("")


def ask(email, text="What is the executive bonus pool?", kb_ids=None):
    sent.clear()
    kb_ids = kb_ids if kb_ids is not None else (HRKB["id"],)
    r = core.handle(Envelope("slack", "t", "r", f"th-{time.time()}", "U", text, tuple(kb_ids), email))
    return r, "\n".join(sent)


# ---- data
default = kbs.ensure_default(db)
with db.session() as s:
    for n in ("HR", "IT"):
        s.add(Department(name=n, retention_days=90, banned_topics="", is_default=False))
with db.session() as s:
    hr = s.scalar(select(Department.id).where(Department.name == "HR")); itd = s.scalar(select(Department.id).where(Department.name == "IT"))
amy = users.create_user(db, "amy@sysfore.com", "Amy", "viewer", None)
bob = users.create_user(db, "bob@sysfore.com", "Bob", "viewer", None)
carl = users.create_user(db, "carl@sysfore.com", "Carl", "viewer", None)
dave = users.create_user(db, "dave@sysfore.com", "Dave", "viewer", None)
ed = users.create_user(db, "ed@sysfore.com", "Ed", "editor", hr)
ed2 = users.create_user(db, "ed2@sysfore.com", "Ed2", "editor", itd)
boss = users.create_user(db, "boss@sysfore.com", "Boss", "admin", None)
with db.session() as s:
    s.add(DepartmentEditor(department_id=hr, user_id=ed["id"])); s.add(DepartmentEditor(department_id=itd, user_id=ed2["id"]))
users.update_user(db, dave["id"], status="Deactivated")
lead = groups.create(db, "Leadership", "", hr, [amy["id"]])
hrkb = kbs.create_kb(db, "HR docs", "", hr)
HRKB = hrkb
other = kbs.create_kb(db, "Other", "", itd)


def put(kb, name, text, vis="everyone", users_=(), groups_=()):
    d = documents.add(db, kb["id"], name, text.encode(), "boss@sysfore.com", {"visibility": vis, "user_ids": list(users_), "group_ids": list(groups_)})
    end = time.time() + 60
    while documents.get(db, d["id"])["status"] not in ("Indexed", "Failed") and time.time() < end:
        time.sleep(0.2)
    assert documents.get(db, d["id"])["status"] == "Indexed", documents.get(db, d["id"])
    return d


pub = put(hrkb, "public.txt", "Annual leave is 18 days for every employee.")
bonus = put(hrkb, "bonus.txt", "The executive bonus pool for 2026 is XYZZY-SECRET and reserved for leadership.", "groups", groups_=[lead["id"]])
ppl = put(hrkb, "relocation.txt", "Bob's executive relocation package is PLUGH-SECRET dollars.", "people", users_=[bob["id"]])
oth = put(other, "other.txt", "Parking is free on Fridays. The executive parking bay is level two.")
NOANS = rag.FALLBACK

# ---- acceptance: outside the group -> no answer, and the LLM never saw the text
r, seen = ask("carl@sysfore.com")
ok(r["answer"] == NOANS and "XYZZY" not in seen and "PLUGH" not in seen, "user outside the group: no-answer reply; spy proves restricted text never reached the LLM")
ok(all(s.get("source") not in ("bonus.txt", "relocation.txt") for s in r["sources"]), "restricted documents are absent from the sources list")
r, seen = ask("amy@sysfore.com")
ok("XYZZY" in seen and r["sources"] and r["sources"][0]["source"] == "bonus.txt", "a member of the group gets the passage (sanity check on the spy)")
r, seen = ask("bob@sysfore.com", "What is the executive relocation package?")
ok("PLUGH" in seen and "XYZZY" not in seen, "a selected person sees their document but not the group's")
r, seen = ask("dave@sysfore.com")
ok(r["answer"] == identity.DENIED_REPLY and seen == "", "deactivated user: refused and the LLM is never called")
r, seen = ask("ed2@sysfore.com")
ok("XYZZY" not in seen and "PLUGH" not in seen, "an Editor of another department gets nothing restricted")
r, seen = ask("ed@sysfore.com")
ok("XYZZY" in seen, "the Editor of the owning department sees their department's documents")
r, seen = ask("boss@sysfore.com")
ok("XYZZY" in seen, "an Admin sees everything")
for who in ("stranger@x.com", ""):
    r, seen = ask(who)
    ok("XYZZY" not in seen and "PLUGH" not in seen and r["answer"] == NOANS, f"unknown sender ({who or 'no email'}) sees only Everyone documents")
r, seen = ask("carl@sysfore.com", "How many days of annual leave?")
ok("18 days" in seen, "everyone-visible documents still answer for anyone")

# ---- changes take effect on the next question, no re-embedding
ids_before = sorted(i for ids, _ in kbs.store_for(hrkb).iter_metadata() for i in ids)
groups.update(db, lead["id"], member_ids=[amy["id"], carl["id"]])
r, seen = ask("carl@sysfore.com")
ok("XYZZY" in seen, "adding a person to the group takes effect on the next question")
groups.update(db, lead["id"], member_ids=[amy["id"]])
r, seen = ask("carl@sysfore.com")
ok("XYZZY" not in seen and r["answer"] == NOANS, "removing a person from the group takes effect on the next question")
access.set_access(db, bonus["id"], "everyone")
r, seen = ask("carl@sysfore.com")
ok("XYZZY" in seen, "switching a document to Everyone opens it on the next question")
access.set_access(db, bonus["id"], "groups", group_ids=[lead["id"]])
r, seen = ask("carl@sysfore.com")
ok("XYZZY" not in seen, "switching it back to a group closes it on the next question")
ok(sorted(i for ids, _ in kbs.store_for(hrkb).iter_metadata() for i in ids) == ids_before, "none of that re-embedded anything")
users.update_user(db, amy["id"], status="Deactivated")
r, seen = ask("amy@sysfore.com")
ok(r["answer"] == identity.DENIED_REPLY and "XYZZY" not in seen, "a member deactivated later is cut off immediately")
users.update_user(db, amy["id"], status="Active")

# ---- knowledge base permission (D17): both KB permission AND document visibility
access.set_kb_query_groups(db, hrkb["id"], [lead["id"]])
r, seen = ask("bob@sysfore.com", "How many days of annual leave?")
ok(r["answer"] == NOANS and "18 days" not in seen, "KB restricted to a group: a non-member gets nothing, even from Everyone documents")
r, seen = ask("amy@sysfore.com", "How many days of annual leave?")
ok("18 days" in seen, "a member of the KB's group can ask it")
try:
    groups.delete_group(db, lead["id"]); blocked = False
except LookupError:
    blocked = True
ok(blocked, "a group that restricts a KB cannot be deleted (it would open the KB to everyone)")
access.set_kb_query_groups(db, hrkb["id"], [])

# ---- several KBs on one channel, merged by reciprocal rank fusion
r, seen = ask("carl@sysfore.com", "executive parking bay and leave days", kb_ids=[hrkb["id"], other["id"]])
srcs = {s["source"] for s in r["sources"]}
ok("other.txt" in srcs and "public.txt" in srcs and "bonus.txt" not in srcs, f"two KBs merged, each with its own access filter: {sorted(srcs)}")
r, seen = ask("carl@sysfore.com", "executive parking bay", kb_ids=[other["id"]])
ok({s["source"] for s in r["sources"]} == {"other.txt"}, "a channel bound to one KB searches only that KB")

# ---- defence in depth: orphan chunks and chunk-metadata restrictions
col = kbs.store_for(hrkb)._col()
col.upsert(ids=["ghost::0"], documents=["Ghost executive bonus pool GHOST-SECRET"], metadatas=[{"source": "ghost.txt", "doc_id": "ghost", "page": 0, "heading_path": "", "chunk": 0, "kb_id": hrkb["id"], "department": "HR", "allowed_groups": "", "allowed_people": ""}])
for who in ("carl@sysfore.com", "boss@sysfore.com"):
    r, seen = ask(who)
    ok("GHOST-SECRET" not in seen, f"a chunk that belongs to no known document is never returned (even to {who.split('@')[0]})")
kbs.store_for(hrkb).update_metadata({"allowed_groups": "|9999|"}, document=pub["doc_key"])
r, seen = ask("carl@sysfore.com", "How many days of annual leave?")
ok("18 days" not in seen, "a restriction found only in the chunk metadata is still honoured")
kbs.store_for(hrkb).update_metadata({"allowed_groups": ""}, document=pub["doc_key"])

# ---- purge: delete and replace remove chunks immediately
documents.delete(db, bonus["id"])
r, seen = ask("amy@sysfore.com")
ok("XYZZY" not in seen, "deleting a document removes it from answers immediately")
documents.replace(db, ppl["id"], "relocation.txt", b"Relocation packages were revised: contact HR.")
end = time.time() + 30
while documents.get(db, ppl["id"])["status"] != "Indexed" and time.time() < end:
    time.sleep(0.2)
r, seen = ask("bob@sysfore.com", "What is the executive relocation package?")
ok("PLUGH" not in seen, "replacing a document removes the old text from answers")
documents.shutdown()
