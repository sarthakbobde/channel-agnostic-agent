"""One contract for every vector store: the same checks run against Chroma, PostgreSQL + pgvector and Azure AI Search.

pgvector runs only when TEST_PGVECTOR_URL points at a PostgreSQL server that has the pgvector extension, for example
    docker run -d --name caa-pgvector -e POSTGRES_PASSWORD=caa-test -p 55433:5432 pgvector/pgvector:pg16
    TEST_PGVECTOR_URL=postgresql://postgres:caa-test@localhost:55433/postgres python -m pytest tests/test_store_contract.py
Azure AI Search runs against an in-memory stand-in for its REST API (the real service needs an Azure account).
"""
import hashlib
import json
import os
import re
import uuid

import numpy as np
import pytest

from app.knowledge import storeadapters
from app.knowledge.stores import ChromaStore

PG_URL = os.getenv("TEST_PGVECTOR_URL", "")


class FakeEmbedder:
    """Bag-of-words vectors: similar words give similar vectors, no model needed."""
    builtin = False

    def _vec(self, text):
        v = np.zeros(64)
        for w in re.findall(r"\w+", text.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1
        n = np.linalg.norm(v)
        return (v / n if n else v).tolist()

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


# ---- an in-memory stand-in for Azure AI Search ------------------------------------------------------------------------------
class FakeAzure:
    KEY = "az-test-key-123456"

    def __init__(self):
        self.indexes: dict[str, dict] = {}

    def __call__(self, method, url, headers, payload=None):
        if headers.get("api-key") != self.KEY:
            return 403, {"error": "forbidden"}
        path, _, query = url.partition("?")
        path = path.split("search.windows.net", 1)[-1] if "search.windows.net" in path else re.sub(r"^https?://[^/]+", "", path)
        m = re.match(r"^/indexes(?:/([^/]+))?(?:/(.*))?$", path)
        name, rest = m.group(1), m.group(2)
        if name is None:
            return 200, {"value": [{"name": n} for n in self.indexes]}
        if rest is None:
            if method == "GET":
                return (200, {"name": name}) if name in self.indexes else (404, None)
            if method == "PUT":
                self.indexes.setdefault(name, {})
                return 201, {"name": name}
            if method == "DELETE":
                self.indexes.pop(name, None)
                return 204, None
        if name not in self.indexes:
            return 404, None
        docs = self.indexes[name]
        if rest == "docs/$count":
            return 200, len(docs)
        if rest == "stats":
            return 200, {"documentCount": len(docs), "storageSize": 1234 * len(docs)}
        if rest == "docs/index":
            for a in payload["value"]:
                act = a["@search.action"]
                if act == "delete":
                    docs.pop(a["id"], None)
                elif act == "mergeOrUpload":
                    docs[a["id"]] = {k: v for k, v in a.items() if k != "@search.action"}
                elif act == "merge" and a["id"] in docs:
                    docs[a["id"]].update({k: v for k, v in a.items() if k != "@search.action"})
            return 200, {"value": [{"key": a["id"], "status": True} for a in payload["value"]]}
        if rest == "docs/search":
            rows = [d for d in docs.values() if self._match(d, payload.get("filter"))]
            if payload.get("vectorQueries"):
                q = np.array(payload["vectorQueries"][0]["vector"])
                scored = []
                for d in rows:
                    v = np.array(d["embedding"])
                    cos = float(q @ v / ((np.linalg.norm(q) * np.linalg.norm(v)) or 1))
                    scored.append((d, (1 + cos) / 2))
                scored.sort(key=lambda x: -x[1])
                return 200, {"value": [{**{k: d[k] for k in ("id", "content", "metadata")}, "@search.score": s} for d, s in scored[:payload["top"]]]}
            rows.sort(key=lambda d: d["id"])
            skip, top = payload.get("skip", 0), payload.get("top", 50)
            return 200, {"value": [{k: d[k] for k in ("id", "doc_id", "metadata")} for d in rows[skip:skip + top]]}
        return 400, None

    @staticmethod
    def _match(doc, flt):
        if not flt:
            return True
        ok = True
        for clause in flt.split(" and "):
            m = re.match(r"^search\.in\((\w+), '(.*)', '\|'\)$", clause)
            if m:
                ok = ok and doc[m.group(1)] in m.group(2).replace("''", "'").split("|")
                continue
            vals = [v.replace("''", "'") for v in re.findall(r"doc_id eq '((?:[^']|'')*)'", clause)]
            ok = ok and doc["doc_id"] in vals
        return ok


@pytest.fixture(params=["chroma", "pgvector", "azure"])
def store(request, tmp_path, monkeypatch):
    emb = FakeEmbedder()
    name = "t" + uuid.uuid4().hex[:8]
    if request.param == "chroma":
        import chromadb
        client = chromadb.PersistentClient(path=str(tmp_path / "chroma"))
        s = ChromaStore(lambda: client.get_or_create_collection(name, metadata={"hnsw:space": "cosine"}),
                        lambda: client.delete_collection(name), embeddings=emb)
    elif request.param == "pgvector":
        if not PG_URL:
            pytest.skip("set TEST_PGVECTOR_URL to run against PostgreSQL + pgvector")
        s = storeadapters.PgVectorStore(name, {"schema": "public"}, {"dsn": PG_URL}, emb)
    else:
        fake = FakeAzure()
        monkeypatch.setattr(storeadapters, "_http", fake)
        s = storeadapters.AzureSearchStore(name, {"endpoint": "https://svc.search.windows.net", "index_name": "caa"}, {"api_key": FakeAzure.KEY}, emb)
    yield s
    try:
        s.delete()
    except Exception:
        pass


DOCS = {
    "leave": ("Employees get eighteen days of annual leave every year.", {"source": "leave.txt", "page": 1}),
    "cafe": ("The cafeteria serves lunch from noon and the coffee machine is upstairs.", {"source": "cafe.txt", "page": 2}),
    "vpn": ("Reset the VPN token by raising a ticket with the IT service desk.", {"source": "vpn.txt", "page": 3}),
}


def fill(store):
    ids, texts, metas = [], [], []
    for key, (text, meta) in DOCS.items():
        ids.append(f"{key}::0")
        texts.append(text)
        metas.append({**meta, "doc_id": key, "chunk": 0, "kb_id": 1, "department": "HR", "heading_path": "", "allowed_groups": "", "allowed_people": ""})
    store.add(ids, texts, metas)


def test_add_search_and_ranking(store):
    fill(store)
    hits = store.search("how many days of annual leave", 3)
    assert [h["metadata"]["doc_id"] for h in hits][0] == "leave" and len(hits) == 3
    assert hits[0]["score"] > hits[-1]["score"] and all(set(h) >= {"id", "text", "metadata", "score"} for h in hits)
    assert hits[0]["metadata"]["source"] == "leave.txt" and hits[0]["metadata"]["page"] == 1


def test_adding_the_same_id_again_replaces_it(store):
    fill(store)
    fill(store)
    assert store.count() == 3


def test_filters_equality_and_in(store):
    fill(store)
    assert {h["metadata"]["doc_id"] for h in store.search("lunch", 5, {"doc_id": "cafe"})} == {"cafe"}
    assert {h["metadata"]["doc_id"] for h in store.search("lunch", 5, {"doc_id": {"in": ["cafe", "vpn"]}})} == {"cafe", "vpn"}
    assert store.search("lunch", 5, {"doc_id": {"in": ["nothing-here"]}}) == []
    assert store.search("lunch", 5, {"doc_id": {"in": []}}) == []


def test_the_filter_is_applied_inside_the_search_not_after(store):
    """With top-k 1 and the best match filtered out, the next best allowed passage must still come back."""
    fill(store)
    hits = store.search("annual leave days", 1, {"doc_id": {"in": ["cafe", "vpn"]}})
    assert len(hits) == 1 and hits[0]["metadata"]["doc_id"] in ("cafe", "vpn")


def test_awkward_values_are_handled_as_data(store):
    store.add(["a::0"], ["quoted text here"], [{"doc_id": "it's; DROP TABLE x;--", "source": "s", "page": 0}])
    assert len(store.search("quoted", 3, {"doc_id": "it's; DROP TABLE x;--"})) == 1
    assert len(store.search("quoted", 3, {"doc_id": {"in": ["it's; DROP TABLE x;--", "b"]}})) == 1
    assert store.count() == 1


def test_delete_by_document(store):
    fill(store)
    store.delete_by_document("vpn")
    assert store.count() == 2 and store.search("VPN token", 3, {"doc_id": "vpn"}) == []
    store.delete_by_document("not-there")
    assert store.count() == 2


def test_update_metadata_in_place_changes_access_without_re_embedding(store):
    fill(store)
    before = store.search("annual leave", 1)[0]
    assert store.update_metadata({"allowed_groups": "|7|"}, document="leave") == 1
    after = store.search("annual leave", 1)[0]
    assert after["metadata"]["allowed_groups"] == "|7|" and after["metadata"]["source"] == "leave.txt"      # merged, not replaced
    assert after["score"] == before["score"] and store.count() == 3                                       # same vector, same chunk
    assert store.update_metadata({"allowed_people": "|3|"}, ids=["cafe::0", "vpn::0"]) == 2
    assert {h["metadata"]["allowed_people"] for h in store.search("lunch", 5, {"doc_id": {"in": ["cafe", "vpn"]}})} == {"|3|"}
    assert store.update_metadata({"x": 1}, document="not-there") == 0
    with pytest.raises(ValueError):
        store.update_metadata({"x": 1})


def test_replace_metadata_per_id(store):
    fill(store)
    store.replace_metadata(["leave::0", "cafe::0"], [
        {"source": "leave.txt", "doc_id": "leave", "page": 9, "chunk": 0, "kb_id": 2, "department": "IT", "heading_path": "A > B", "allowed_groups": "", "allowed_people": ""},
        {"source": "cafe.txt", "doc_id": "cafe-new", "page": 1, "chunk": 0, "kb_id": 2, "department": "IT", "heading_path": "", "allowed_groups": "", "allowed_people": ""}])
    h = store.search("annual leave", 1)[0]
    assert h["metadata"]["page"] == 9 and h["metadata"]["heading_path"] == "A > B"
    assert len(store.search("lunch", 3, {"doc_id": "cafe-new"})) == 1 and store.search("lunch", 3, {"doc_id": "cafe"}) == []


def test_count_stats_health_and_paging(store):
    assert store.count() == 0 and store.search("anything", 3) == []
    fill(store)
    assert store.count() == 3
    stats = store.stats()
    assert stats["chunks"] == 3 and stats["documents"] == 3
    ok, message = store.health()
    assert ok is True
    ids = [i for page, _ in store.iter_metadata(batch=2) for i in page]
    assert sorted(ids) == ["cafe::0", "leave::0", "vpn::0"]
    assert all(m["doc_id"] for _, metas in store.iter_metadata(batch=2) for m in metas)


def test_delete_removes_the_whole_index(store):
    fill(store)
    store.delete()
    assert store.count() == 0 and store.search("leave", 3) == []
    store.delete()                                                        # deleting twice is harmless
    fill(store)                                                           # and the index can be created again
    assert store.count() == 3


# ---- adapter configuration, tests and secrets ------------------------------------------------------------------------------------
def test_pgvector_validation_and_failed_connection_hides_the_password():
    assert storeadapters.PgVectorStore.validate({"schema": "bad schema;"}, {}) != []
    ok, message = storeadapters.PgVectorStore.test({}, {"dsn": "postgresql://user:SuperSecret99@127.0.0.1:1/db"})
    assert ok is False and "SuperSecret99" not in message
    ok, message = storeadapters.PgVectorStore.test({}, {})
    assert ok is False and "connection string" in message.lower()


@pytest.mark.skipif(not PG_URL, reason="set TEST_PGVECTOR_URL")
def test_pgvector_connection_test_reports_the_extension():
    ok, message = storeadapters.PgVectorStore.test({}, {"dsn": PG_URL})
    assert ok is True and "pgvector" in message


def test_azure_validation_and_test(monkeypatch):
    fake = FakeAzure()
    monkeypatch.setattr(storeadapters, "_http", fake)
    cfg = {"endpoint": "https://svc.search.windows.net", "index_name": "caa"}
    assert storeadapters.AzureSearchStore.validate({"endpoint": "http://nope", "index_name": "Bad Name"}, {}) != []
    ok, message = storeadapters.AzureSearchStore.test(cfg, {"api_key": FakeAzure.KEY})
    assert ok is True
    ok, message = storeadapters.AzureSearchStore.test(cfg, {"api_key": "wrong-key-000000"})
    assert ok is False and "wrong-key-000000" not in message and "rejected" in message


def test_azure_creates_the_index_on_first_write_and_names_it_per_version(monkeypatch):
    fake = FakeAzure()
    monkeypatch.setattr(storeadapters, "_http", fake)
    s = storeadapters.AzureSearchStore("kb_1_v2", {"endpoint": "https://svc.search.windows.net", "index_name": "caa"}, {"api_key": FakeAzure.KEY}, FakeEmbedder())
    assert fake.indexes == {}
    fill(s)
    assert list(fake.indexes) == ["caa-kb-1-v2"]
