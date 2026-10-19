"""Vector store adapters beyond Chroma: PostgreSQL + pgvector, and Azure AI Search.

Each has a declarative SPEC (the admin form), `validate()` and `test()` (reachability, login, whether the index exists), and implements the
same VectorStore interface as Chroma, including mandatory metadata filtering (`{"field": value}` / `{"field": {"in": [...]}}`) and
in-place metadata updates. Unlike Chroma these stores do not embed: they are given the knowledge base's embedding model.
Connection strings and keys are secrets: stored encrypted, never returned, never in a message. A store holds one index per index version.
"""
import json
import logging
import re

import httpx

from app.knowledge.stores import ChromaStore, VectorStore
from app.spec import Field

log = logging.getLogger(__name__)


def _scrub(message: str, secrets: dict) -> str:
    for v in (secrets or {}).values():
        if isinstance(v, str) and len(v) >= 4:
            message = message.replace(v, "***")
    return message


def _slug(text: str, sep: str) -> str:
    return re.sub(r"[^a-z0-9]+", sep, text.lower()).strip(sep)[:56]


def vector_text(v) -> str:
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


# ---- PostgreSQL + pgvector ----------------------------------------------------------------------------------------------------
class PgVectorStore(VectorStore):
    name = "pgvector"
    title = "PostgreSQL + pgvector"
    SPEC = (
        Field("dsn", "Connection string", secret=True,
              help="postgresql://user:password@host:5432/database. Leave empty to use the admin database when it is PostgreSQL."),
        Field("schema", "Schema", default="public", help="The schema the tables are created in. Default public."),
    )
    warning = ""

    def __init__(self, collection: str, config: dict, secrets: dict, embedder, drop=None):
        self.config, self.secrets, self.embedder = dict(config or {}), dict(secrets or {}), embedder
        self.table = "caa_" + _slug(collection, "_")
        self.schema = self.config.get("schema") or "public"
        self._dsn = self._resolve_dsn()

    # ---- connection ------------------------------------------------------------------------------------------------------------
    def _resolve_dsn(self) -> str:
        dsn = self.secrets.get("dsn") or ""
        if not dsn:
            from app.config import settings
            dsn = settings.database_url if settings.database_url.startswith("postgres") else ""
        if not dsn:
            raise ValueError("Enter the PostgreSQL connection string (the admin database is not PostgreSQL).")
        return re.sub(r"^postgresql\+\w+://", "postgresql://", dsn)

    def _connect(self):
        import psycopg
        return psycopg.connect(self._dsn, connect_timeout=10)

    def _t(self):
        from psycopg import sql
        return sql.SQL("{}.{}").format(sql.Identifier(self.schema), sql.Identifier(self.table))

    @classmethod
    def validate(cls, config: dict, secrets: dict) -> list[str]:
        errors = []
        schema = (config or {}).get("schema")
        if schema and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", schema):
            errors.append("Schema may contain letters, digits and underscores only")
        return errors

    @classmethod
    def test(cls, config: dict, secrets: dict) -> tuple[bool, str]:
        try:
            store = cls("test", config, secrets, None)
            with store._connect() as conn:
                version = conn.execute("SELECT version()").fetchone()[0].split(",")[0]
                has = conn.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'").fetchone()
            if not has:
                return False, "Connected, but the pgvector extension is not installed on this server."
            return True, f"Connected ({version}). pgvector is available; the tables are created for you."
        except ValueError as e:
            return False, str(e)
        except Exception as e:
            return False, _scrub(f"Could not connect ({type(e).__name__}). Check the connection string, network and login.", secrets)

    # ---- table ------------------------------------------------------------------------------------------------------------------
    def _ensure(self, conn, dim: int) -> None:
        from psycopg import sql
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)))
        conn.execute(sql.SQL("CREATE TABLE IF NOT EXISTS {} (id text PRIMARY KEY, doc_id text NOT NULL, content text NOT NULL, "
                             "metadata jsonb NOT NULL, embedding vector({}) NOT NULL)").format(self._t(), sql.SQL(str(int(dim)))))
        conn.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} USING hnsw (embedding vector_cosine_ops)")
                     .format(sql.Identifier(self.table + "_hnsw"), self._t()))
        conn.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (doc_id)").format(sql.Identifier(self.table + "_doc"), self._t()))

    def _exists(self, conn) -> bool:
        return conn.execute("SELECT to_regclass(%s)", (f'"{self.schema}"."{self.table}"',)).fetchone()[0] is not None

    def _embed_docs(self, texts):
        return self.embedder.embed_documents(texts)

    # ---- interface ----------------------------------------------------------------------------------------------------------------
    def add(self, ids, texts, metadatas):
        if not ids:
            return
        from psycopg import sql
        vectors = self._embed_docs(texts)
        with self._connect() as conn:
            self._ensure(conn, len(vectors[0]))
            stmt = sql.SQL("INSERT INTO {} (id, doc_id, content, metadata, embedding) VALUES (%s, %s, %s, %s::jsonb, %s::vector) "
                           "ON CONFLICT (id) DO UPDATE SET doc_id = EXCLUDED.doc_id, content = EXCLUDED.content, "
                           "metadata = EXCLUDED.metadata, embedding = EXCLUDED.embedding").format(self._t())
            with conn.cursor() as cur:
                cur.executemany(stmt, [(i, str(m.get("doc_id", "")), t, json.dumps(m), vector_text(v)) for i, t, m, v in zip(ids, texts, metadatas, vectors)])

    @staticmethod
    def _where(flt):
        parts, params = [], []
        for key, cond in (flt or {}).items():
            if isinstance(cond, dict):
                if set(cond) != {"in"}:
                    raise ValueError(f"unsupported filter for {key!r}")
                parts.append("metadata ->> %s = ANY(%s)")
                params += [key, [str(x) for x in cond["in"]]]
            else:
                parts.append("metadata ->> %s = %s")
                params += [key, str(cond)]
        return (" WHERE " + " AND ".join(parts)) if parts else "", params

    def search(self, query, k, flt=None):
        from psycopg import sql
        qv = vector_text(self.embedder.embed_query(query))
        where, params = self._where(flt)
        with self._connect() as conn:
            if not self._exists(conn):
                return []
            stmt = sql.SQL("SELECT id, content, metadata, 1 - (embedding <=> %s::vector) AS score FROM {}" + where +
                           " ORDER BY embedding <=> %s::vector LIMIT %s").format(self._t())
            rows = conn.execute(stmt, [qv, *params, qv, int(k)]).fetchall()
        return [{"id": r[0], "text": r[1], "metadata": r[2] or {}, "score": round(float(r[3]), 3)} for r in rows]

    def _run(self, template: str, params, create=False):
        from psycopg import sql
        with self._connect() as conn:
            if not self._exists(conn):
                return None
            cur = conn.execute(sql.SQL(template).format(self._t()), params)
            return cur

    def delete_by_document(self, doc_id):
        cur = self._run("DELETE FROM {} WHERE doc_id = %s", (doc_id,))

    def delete(self):
        from psycopg import sql
        with self._connect() as conn:
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(self._t()))

    def update_metadata(self, patch, *, document=None, ids=None):
        from psycopg import sql
        with self._connect() as conn:
            if not self._exists(conn):
                return 0
            if document is not None:
                cur = conn.execute(sql.SQL("UPDATE {} SET metadata = metadata || %s::jsonb WHERE doc_id = %s").format(self._t()), (json.dumps(patch), document))
            elif ids:
                cur = conn.execute(sql.SQL("UPDATE {} SET metadata = metadata || %s::jsonb WHERE id = ANY(%s)").format(self._t()), (json.dumps(patch), list(ids)))
            else:
                raise ValueError("give a document or ids")
            return cur.rowcount

    def replace_metadata(self, ids, metadatas):
        from psycopg import sql
        with self._connect() as conn:
            if not self._exists(conn) or not ids:
                return
            with conn.cursor() as cur:
                cur.executemany(sql.SQL("UPDATE {} SET metadata = %s::jsonb, doc_id = %s WHERE id = %s").format(self._t()),
                                [(json.dumps(m), str(m.get("doc_id", "")), i) for i, m in zip(ids, metadatas)])

    def count(self):
        from psycopg import sql
        with self._connect() as conn:
            if not self._exists(conn):
                return 0
            return conn.execute(sql.SQL("SELECT count(*) FROM {}").format(self._t())).fetchone()[0]

    def health(self):
        try:
            with self._connect() as conn:
                conn.execute("SELECT 1")
                n = conn.execute(f'SELECT count(*) FROM "{self.schema}"."{self.table}"').fetchone()[0] if self._exists(conn) else 0
            return True, f"{n} chunks"
        except Exception as e:
            return False, _scrub(f"{type(e).__name__}", self.secrets)

    def stats(self):
        from psycopg import sql
        with self._connect() as conn:
            if not self._exists(conn):
                return {"chunks": 0, "documents": 0, "size_bytes": 0}
            n, d = conn.execute(sql.SQL("SELECT count(*), count(DISTINCT doc_id) FROM {}").format(self._t())).fetchone()
            size = conn.execute("SELECT pg_total_relation_size(to_regclass(%s))", (f'"{self.schema}"."{self.table}"',)).fetchone()[0]
        return {"chunks": n, "documents": d, "size_bytes": size}

    def iter_metadata(self, batch=500):
        from psycopg import sql
        offset = 0
        while True:
            with self._connect() as conn:
                if not self._exists(conn):
                    return
                rows = conn.execute(sql.SQL("SELECT id, metadata FROM {} ORDER BY id LIMIT %s OFFSET %s").format(self._t()), (batch, offset)).fetchall()
            if not rows:
                return
            yield [r[0] for r in rows], [r[1] for r in rows]
            offset += len(rows)


# ---- Azure AI Search -------------------------------------------------------------------------------------------------------------
API_VERSION = "2024-07-01"
TIMEOUT = 60


class AzureError(RuntimeError):
    pass


def _http(method: str, url: str, headers: dict, payload=None):
    """One HTTP call to the service; returns (status, parsed JSON or None). Replaced in tests."""
    try:
        r = httpx.request(method, url, headers=headers, json=payload, timeout=TIMEOUT)
    except httpx.HTTPError as e:
        raise AzureError(f"Could not reach Azure AI Search ({type(e).__name__}).") from None
    try:
        body = r.json() if r.content else None
    except ValueError:
        body = None
    return r.status_code, body


class AzureSearchStore(VectorStore):
    name = "azure-search"
    title = "Azure AI Search"
    SPEC = (
        Field("endpoint", "Endpoint", required=True, help="For example https://my-service.search.windows.net"),
        Field("index_name", "Index name", required=True, help="Base name; each index version gets its own index with this prefix. Lower-case letters, digits and dashes."),
        Field("api_key", "Admin key", secret=True, required=True, help="Stored encrypted; never shown again."),
    )
    warning = "Document passages leave the company for your Azure AI Search service. Built to the documented REST API and tested against a stand-in, not against a live Azure service."

    def __init__(self, collection: str, config: dict, secrets: dict, embedder, drop=None):
        self.config, self.secrets, self.embedder = dict(config or {}), dict(secrets or {}), embedder
        self.endpoint = (self.config.get("endpoint") or "").rstrip("/")
        self.index = _slug((self.config.get("index_name") or "caa") + "-" + collection, "-")
        self.headers = {"api-key": self.secrets.get("api_key", ""), "Content-Type": "application/json"}

    @classmethod
    def validate(cls, config: dict, secrets: dict) -> list[str]:
        errors = []
        if not re.fullmatch(r"https://[A-Za-z0-9.\-]+(:\d+)?", (config or {}).get("endpoint", "").rstrip("/")):
            errors.append("Endpoint must look like https://my-service.search.windows.net")
        if not re.fullmatch(r"[a-z0-9][a-z0-9\-]{0,40}", (config or {}).get("index_name", "")):
            errors.append("Index name may contain lower-case letters, digits and dashes only")
        return errors

    def _url(self, path: str) -> str:
        return f"{self.endpoint}{path}{'&' if '?' in path else '?'}api-version={API_VERSION}"

    def _call(self, method, path, payload=None, ok=(200, 201, 204, 404)):
        status, body = _http(method, self._url(path), self.headers, payload)
        if status in (401, 403):
            raise ValueError(f"Azure AI Search rejected the key (HTTP {status}).")
        if status == 429 or status >= 500:
            raise AzureError(f"Azure AI Search had a problem (HTTP {status}).")
        if status not in ok:
            raise ValueError(f"Azure AI Search answered HTTP {status}.")
        return status, body

    @classmethod
    def test(cls, config: dict, secrets: dict) -> tuple[bool, str]:
        problems = cls.validate(config, secrets)
        if problems:
            return False, "; ".join(problems)
        try:
            store = cls("test", config, secrets, None)
            status, body = store._call("GET", "/indexes?$select=name")
            names = [i.get("name") for i in (body or {}).get("value", [])]
            return True, f"Connected. {len(names)} index(es) found; the indexes for each version are created for you."
        except (ValueError, AzureError) as e:
            return False, _scrub(str(e), secrets)
        except Exception as e:
            return False, _scrub(type(e).__name__, secrets)

    def _ensure(self, dim: int) -> None:
        status, _ = self._call("GET", f"/indexes/{self.index}?$select=name")
        if status == 200:
            return
        fields = [
            {"name": "id", "type": "Edm.String", "key": True, "filterable": True},
            {"name": "doc_id", "type": "Edm.String", "filterable": True},
            {"name": "content", "type": "Edm.String", "searchable": True},
            {"name": "metadata", "type": "Edm.String", "retrievable": True},
            {"name": "embedding", "type": "Collection(Edm.Single)", "searchable": True, "dimensions": int(dim), "vectorSearchProfile": "caa-profile"},
        ]
        self._call("PUT", f"/indexes/{self.index}", {"name": self.index, "fields": fields, "vectorSearch": {
            "algorithms": [{"name": "caa-hnsw", "kind": "hnsw", "hnswParameters": {"metric": "cosine"}}],
            "profiles": [{"name": "caa-profile", "algorithm": "caa-hnsw"}]}})

    @staticmethod
    def _odata(flt) -> str | None:
        parts = []
        for key, cond in (flt or {}).items():
            if key != "doc_id":
                raise ValueError(f"Azure AI Search can filter on doc_id only, not {key!r}")
            values = [str(x) for x in (cond["in"] if isinstance(cond, dict) else [cond])]
            lit = lambda v: "'" + v.replace("'", "''") + "'"
            if not values:
                return "doc_id eq '\u0000never'"
            parts.append("(" + " or ".join(f"doc_id eq {lit(v)}" for v in values) + ")" if len(values) <= 20 or any("|" in v for v in values)
                         else "search.in(doc_id, '" + "|".join(v.replace("'", "''") for v in values) + "', '|')")
        return " and ".join(parts) or None

    def add(self, ids, texts, metadatas):
        if not ids:
            return
        vectors = self.embedder.embed_documents(texts)
        self._ensure(len(vectors[0]))
        for i in range(0, len(ids), 100):
            docs = [{"@search.action": "mergeOrUpload", "id": ids[j], "doc_id": str(metadatas[j].get("doc_id", "")), "content": texts[j],
                     "metadata": json.dumps(metadatas[j]), "embedding": [float(x) for x in vectors[j]]} for j in range(i, min(i + 100, len(ids)))]
            self._call("POST", f"/indexes/{self.index}/docs/index", {"value": docs})

    def search(self, query, k, flt=None):
        body = {"select": "id,content,metadata", "top": int(k), "vectorQueries": [
            {"kind": "vector", "vector": [float(x) for x in self.embedder.embed_query(query)], "k": int(k), "fields": "embedding"}]}
        f = self._odata(flt)
        if f:
            body["filter"] = f
        status, res = self._call("POST", f"/indexes/{self.index}/docs/search", body)
        if status == 404:
            return []
        return [{"id": r["id"], "text": r["content"], "metadata": json.loads(r.get("metadata") or "{}"),
                 "score": round(2 * float(r["@search.score"]) - 1, 3)} for r in (res or {}).get("value", [])]      # cosine: score = (1 + similarity) / 2

    def _docs(self, flt=None, ids=None):
        out, skip = [], 0
        while True:
            body = {"search": "*", "select": "id,doc_id,metadata", "top": 1000, "skip": skip, "orderby": "id"}
            f = self._odata(flt) if flt else None
            if ids:
                f = "search.in(id, '" + "|".join(i.replace("'", "''") for i in ids) + "', '|')"
            if f:
                body["filter"] = f
            status, res = self._call("POST", f"/indexes/{self.index}/docs/search", body)
            rows = (res or {}).get("value", []) if status != 404 else []
            out += rows
            if len(rows) < 1000:
                return out
            skip += 1000

    def delete_by_document(self, doc_id):
        rows = self._docs({"doc_id": doc_id})
        for i in range(0, len(rows), 100):
            self._call("POST", f"/indexes/{self.index}/docs/index", {"value": [{"@search.action": "delete", "id": r["id"]} for r in rows[i:i + 100]]})

    def delete(self):
        self._call("DELETE", f"/indexes/{self.index}")

    def update_metadata(self, patch, *, document=None, ids=None):
        if document is not None:
            rows = self._docs({"doc_id": document})
        elif ids:
            rows = self._docs(ids=list(ids))
        else:
            raise ValueError("give a document or ids")
        for i in range(0, len(rows), 100):
            self._call("POST", f"/indexes/{self.index}/docs/index", {"value": [
                {"@search.action": "merge", "id": r["id"], "metadata": json.dumps({**json.loads(r.get("metadata") or "{}"), **patch})} for r in rows[i:i + 100]]})
        return len(rows)

    def replace_metadata(self, ids, metadatas):
        for i in range(0, len(ids), 100):
            self._call("POST", f"/indexes/{self.index}/docs/index", {"value": [
                {"@search.action": "merge", "id": ids[j], "doc_id": str(metadatas[j].get("doc_id", "")), "metadata": json.dumps(metadatas[j])}
                for j in range(i, min(i + 100, len(ids)))]})

    def count(self):
        status, body = self._call("GET", f"/indexes/{self.index}/docs/$count")
        return int(body) if status == 200 and isinstance(body, int) else 0

    def health(self):
        try:
            status, body = self._call("GET", f"/indexes/{self.index}/stats")
            return True, f"{(body or {}).get('documentCount', 0)} chunks" if status == 200 else "reachable (no index yet)"
        except Exception as e:
            return False, _scrub(str(e) if isinstance(e, (ValueError, AzureError)) else type(e).__name__, self.secrets)

    def stats(self):
        status, body = self._call("GET", f"/indexes/{self.index}/stats")
        if status != 200:
            return {"chunks": 0, "documents": 0, "size_bytes": 0}
        rows = self._docs()
        return {"chunks": (body or {}).get("documentCount", len(rows)), "documents": len({r.get("doc_id") for r in rows}),
                "size_bytes": (body or {}).get("storageSize", 0)}

    def iter_metadata(self, batch=500):
        rows = self._docs()
        for i in range(0, len(rows), batch):
            part = rows[i:i + batch]
            yield [r["id"] for r in part], [json.loads(r.get("metadata") or "{}") for r in part]


# ---- registry --------------------------------------------------------------------------------------------------------------------------
class ChromaInfo:
    """Chroma is built into the server: nothing to configure."""
    name = "chroma"
    title = "ChromaDB (built in)"
    SPEC = ()
    warning = ""

    @classmethod
    def validate(cls, config, secrets):
        return []

    @classmethod
    def test(cls, config, secrets):
        return True, "ChromaDB runs inside the server; nothing to configure."


STORE_TYPES = {"chroma": ChromaInfo, "pgvector": PgVectorStore, "azure-search": AzureSearchStore}
DEFAULT_STORE = "chroma"


def build_remote(store_type: str, collection: str, config: dict, secrets: dict, embedder, drop=None) -> VectorStore:
    cls = STORE_TYPES.get(store_type)
    if cls is None or store_type == "chroma":
        raise ValueError(f"unknown store {store_type!r}")
    return cls(collection, config, secrets, embedder, drop)
