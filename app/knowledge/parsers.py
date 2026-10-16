"""Read a file into [(page, text)]. Page is the PDF page or slide number; 0 for formats without pages."""
from pathlib import Path

SUPPORTED = {".pdf", ".docx", ".pptx", ".md", ".txt"}
OLD_FORMATS = {".doc": ".docx", ".ppt": ".pptx"}


def load_pages(path: Path) -> list[tuple[int, str]]:
    ext = path.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        return [(i + 1, p.extract_text() or "") for i, p in enumerate(reader.pages)]
    if ext == ".docx":
        from docx import Document
        doc = Document(str(path))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))
        return [(0, "\n".join(parts))]
    if ext == ".pptx":
        from pptx import Presentation
        out = []
        for i, slide in enumerate(Presentation(str(path)).slides, 1):
            texts = []
            for shape in slide.shapes:
                if shape.has_text_frame and shape.text_frame.text.strip():
                    texts.append(shape.text_frame.text)
                if getattr(shape, "has_table", False) and shape.has_table:
                    for row in shape.table.rows:
                        texts.append(" | ".join(c.text for c in row.cells if c.text.strip()))
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
                texts.append(slide.notes_slide.notes_text_frame.text)
            out.append((i, "\n".join(texts)))
        return out
    if ext in (".md", ".txt"):
        return [(0, path.read_text(encoding="utf-8", errors="ignore"))]
    raise ValueError(f"{ext or 'This'} files are not supported.")
