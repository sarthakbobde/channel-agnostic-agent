import argparse
from pathlib import Path
from app.knowledge import chunkers, kbs, metadata
from app.knowledge.parsers import SUPPORTED, load_pages


def ingest(directory: str, kb_id: int | None = None) -> None:
    """Index a folder into a knowledge base (the default one unless `kb_id` is given)."""
    kb = (kbs.get_kb(kbs.get_db(), kb_id) if kb_id else None) or kbs.ensure_default()
    store = kbs.store_for(kb)
    from app.knowledge import versions
    chunker = versions.active_chunker(kbs.get_db(), kb)     # same settings as the active index

    department = kbs.department_name(kbs.get_db(), kb["department_id"])
    root = Path(directory)
    files = [f for f in sorted(root.rglob("*")) if f.suffix.lower() in SUPPORTED]
    if not files:
        print(f"No {', '.join(sorted(SUPPORTED))} files found in {root}")
        return

    total_chunks = 0
    for f in files:
        doc_id = f.relative_to(root).as_posix()
        ids, docs, metas = [], [], []
        n = 0
        for page, text in load_pages(f):
            for chunk in chunker.split(text):
                ids.append(f"{doc_id}::{n}")          # deterministic ID
                docs.append(chunk)
                metas.append(metadata.build(source=f.name, doc_id=doc_id, page=page, chunk=n,
                                            kb_id=kb["id"], department=department))
                n += 1

        store.delete_by_document(doc_id)              # remove old version of this file
        store.add(ids, docs, metas)
        total_chunks += len(ids)
        print(f"  {f.name}: {len(ids)} chunks")

    print(f"\nIndexed {len(files)} docs, {total_chunks} chunks into '{kb['name']}'. "
          f"Collection total: {store.count()}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("directory", nargs="?", default="data/docs")
    ap.add_argument("--kb", type=int, default=None, help="knowledge base id (default: the default one)")
    args = ap.parse_args()
    ingest(args.directory, args.kb)
