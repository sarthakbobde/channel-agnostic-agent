import argparse
from pathlib import Path
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pypdf import PdfReader
from app.config import settings
from app.retriever import get_client, get_collection

SUPPORTED = {".pdf", ".md", ".txt"}


def load_pages(path: Path) -> list[tuple[int, str]]:
    """Return [(page_number, text)]. Page is 0 for non-PDF files."""
    if path.suffix.lower() == ".pdf":
        reader = PdfReader(str(path))
        return [(i + 1, p.extract_text() or "") for i, p in enumerate(reader.pages)]
    return [(0, path.read_text(encoding="utf-8", errors="ignore"))]


def ingest(directory: str, reset: bool = False) -> None:
    if reset:
        try:
            get_client().delete_collection(settings.collection)
            print("Collection reset.")
        except Exception:
            pass

    col = get_collection()
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size, chunk_overlap=settings.chunk_overlap
    )
    root = Path(directory)
    files = [f for f in sorted(root.rglob("*")) if f.suffix.lower() in SUPPORTED]
    if not files:
        print(f"No .pdf/.md/.txt files found in {root}")
        return

    total_chunks = 0
    for f in files:
        doc_id = f.relative_to(root).as_posix()
        ids, docs, metas = [], [], []
        n = 0
        for page, text in load_pages(f):
            for chunk in splitter.split_text(text):
                ids.append(f"{doc_id}::{n}")          # deterministic ID
                docs.append(chunk)
                metas.append({"source": f.name, "doc_id": doc_id, "page": page})
                n += 1

        col.delete(where={"doc_id": doc_id})          # remove old version of this file
        if ids:
            col.upsert(ids=ids, documents=docs, metadatas=metas)
        total_chunks += len(ids)
        print(f"  {f.name}: {len(ids)} chunks")

    print(f"\nIndexed {len(files)} docs, {total_chunks} chunks. "
          f"Collection total: {col.count()}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("directory", nargs="?", default="data/docs")
    ap.add_argument("--reset", action="store_true", help="wipe the collection first")
    args = ap.parse_args()
    ingest(args.directory, args.reset)