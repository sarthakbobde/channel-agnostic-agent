"""Phase 12a and 12b: every stored file encrypted, parsing never leaves plain copies, and the personal-data engine
(Admin-defined types, sensitive keywords, tokens and the vault)."""
import dataclasses
import io

import pytest
from cryptography.fernet import Fernet

from app import guardrails, pii
from app.knowledge import chunkers, documents, filestore
from app.secretstore import SecretError

SECRET = b"Ravi Kumar, salary 12,40,000, ravi.kumar@corp.com"


@pytest.fixture
def store(tmp_path):
    return filestore.FileStore(filestore.LocalBackend(str(tmp_path)), Fernet.generate_key().decode(), str(tmp_path))


@pytest.fixture
def docs(tmp_path, monkeypatch):
    """documents.py pointed at a fresh folder with its own key."""
    cfg = dataclasses.replace(documents.settings, docs_dir=str(tmp_path / "files"), file_store="local",
                              secret_key=Fernet.generate_key().decode())
    monkeypatch.setattr(documents, "settings", cfg)
    return tmp_path / "files"


def _all_bytes(folder) -> bytes:
    return b"".join(p.read_bytes() for p in folder.rglob("*") if p.is_file())


def _docx(text: str) -> bytes:
    from docx import Document
    buf = io.BytesIO()
    d = Document()
    d.add_paragraph(text)
    d.save(buf)
    return buf.getvalue()


def _pptx(text: str) -> bytes:
    from pptx import Presentation
    buf = io.BytesIO()
    p = Presentation()
    p.slides.add_slide(p.slide_layouts[5]).shapes.title.text = text
    p.save(buf)
    return buf.getvalue()


# ---- 12a: encrypted storage ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("name,data", [
    ("policy.txt", SECRET), ("policy.md", b"# Pay\n\n" + SECRET), ("policy.docx", _docx(SECRET.decode())),
    ("deck.pptx", _pptx(SECRET.decode())), ("photo.png", b"\x89PNG\r\n" + SECRET), ("sheet.xlsx", b"PK\x03\x04" + SECRET),
], ids=["txt", "md", "docx", "pptx", "png", "xlsx"])
def test_every_file_type_is_stored_encrypted(store, tmp_path, name, data):
    store.put(f"1/2/{name}", data)
    raw = (tmp_path / "1" / "2" / name).read_bytes()
    assert raw.startswith(filestore.MAGIC)
    assert b"Ravi" not in raw and b"salary" not in raw and b"corp.com" not in raw
    assert store.get(f"1/2/{name}") == data


def test_the_wrong_key_cannot_read_a_file(store, tmp_path):
    store.put("1/2/a.txt", SECRET)
    other = filestore.FileStore(store.backend, Fernet.generate_key().decode(), str(tmp_path))
    with pytest.raises(SecretError):
        other.get("1/2/a.txt")


def test_nothing_is_stored_without_a_master_key(tmp_path):
    with pytest.raises(ValueError, match="APP_SECRET_KEY"):
        filestore.FileStore(filestore.LocalBackend(str(tmp_path)), "", str(tmp_path)).put("1/2/a.txt", SECRET)
    assert not any(p.is_file() for p in tmp_path.rglob("*"))


def test_keys_cannot_escape_the_folder(store):
    with pytest.raises(ValueError):
        store.put("../outside.txt", SECRET)
    with pytest.raises(ValueError):
        store.put(".work/x.txt", SECRET)


def test_plain_copies_are_deleted_after_use_even_when_parsing_fails(store):
    store.put("1/2/a.txt", SECRET)
    with store.plain_copy("1/2/a.txt") as p:
        assert p.read_bytes() == SECRET
    assert not any(store.work.rglob("*.txt"))
    with pytest.raises(RuntimeError):
        with store.plain_copy("1/2/a.txt"):
            raise RuntimeError("parser crashed")
    assert not any(store.work.rglob("*.txt"))


def test_leftovers_from_a_crash_are_wiped_at_start(store):
    (store.work / "abc").mkdir(parents=True)
    (store.work / "abc" / "a.pdf").write_bytes(SECRET)
    assert store.wipe_work() == 1
    assert not store.work.exists()


def test_plain_files_from_before_are_encrypted_once(store, tmp_path):
    (tmp_path / "3" / "4").mkdir(parents=True)
    (tmp_path / "3" / "4" / "old.pdf").write_bytes(SECRET)
    assert store.get("3/4/old.pdf") == SECRET                 # still readable before the migration ran
    assert store.plain_keys() == ["3/4/old.pdf"]
    assert store.encrypt_plain() == 1
    assert store.encrypt_plain() == 0                          # repeatable
    assert store.plain_keys() == [] and store.get("3/4/old.pdf") == SECRET
    assert b"salary" not in _all_bytes(tmp_path)


def test_rotation_moves_every_file_to_the_new_key(store, tmp_path):
    store.put("1/2/a.txt", SECRET)
    old = store._keys
    new = Fernet.generate_key().decode()
    both = filestore.FileStore(store.backend, f"{new},{old}", str(tmp_path))
    assert both.rotate() == {"total": 1, "rotated": 1}
    assert filestore.FileStore(store.backend, new, str(tmp_path)).get("1/2/a.txt") == SECRET


def test_rotation_changes_nothing_if_a_file_cannot_be_read(store, tmp_path):
    store.put("1/2/a.txt", SECRET)
    store.backend.write("1/2/b.txt", filestore.MAGIC + Fernet(Fernet.generate_key()).encrypt(b"x"))
    before = store.backend.read("1/2/a.txt")
    with pytest.raises(SecretError):
        filestore.FileStore(store.backend, f"{Fernet.generate_key().decode()},{store._keys}", str(tmp_path)).rotate()
    assert store.backend.read("1/2/a.txt") == before


class MemoryBackend(filestore.Backend):
    """Stands in for a blob container: a flat key space."""

    def __init__(self):
        self.blobs = {}

    def read(self, key):
        return self.blobs[key]

    def write(self, key, data):
        self.blobs[key] = data

    def exists(self, key):
        return key in self.blobs

    def delete(self, key):
        self.blobs.pop(key, None)

    def keys(self, prefix=""):
        return iter(sorted(k for k in self.blobs if k.startswith(prefix)))


def test_a_blob_backend_only_ever_holds_ciphertext(tmp_path):
    blob = MemoryBackend()
    s = filestore.FileStore(blob, Fernet.generate_key().decode(), str(tmp_path))
    s.put("1/2/a.txt", SECRET)
    s.put("1/3/b.txt", SECRET)
    assert all(v.startswith(filestore.MAGIC) and b"salary" not in v for v in blob.blobs.values())
    s.move_prefix("1/3/", "2/3/")
    assert sorted(blob.blobs) == ["1/2/a.txt", "2/3/b.txt"] and s.get("2/3/b.txt") == SECRET
    s.delete_prefix("1/")
    assert sorted(blob.blobs) == ["2/3/b.txt"]
    with s.plain_copy("2/3/b.txt") as p:
        assert p.read_bytes() == SECRET
    assert not any(s.work.rglob("*.txt"))


def test_unknown_file_store_is_refused():
    with pytest.raises(RuntimeError, match="Unknown FILE_STORE"):
        filestore.build(dataclasses.replace(documents.settings, file_store="ftp"))


def test_indexing_parses_an_encrypted_original_and_leaves_no_plain_copy(docs):
    d = {"id": 7, "kb_id": 3, "name": "pay.txt", "doc_key": "doc-7"}
    documents._write(d, SECRET)
    assert documents.stored(d) and b"salary" not in _all_bytes(docs)
    ids, texts, metas = documents.prepare(d, {"id": 3}, chunkers.build("recursive", 900, 150), "HR")
    assert ids == ["doc-7::0"] and "salary" in texts[0] and metas[0]["source"] == "pay.txt"
    assert not [p for p in docs.rglob("*") if p.is_file() and filestore.WORK in p.parts]
    assert b"salary" not in _all_bytes(docs)


def test_replacing_a_document_drops_the_old_file(docs):
    d = {"id": 8, "kb_id": 3, "name": "a.txt", "doc_key": "doc-8"}
    documents._write(d, b"one")
    documents._write({**d, "name": "b.txt"}, b"two")
    assert documents.files().keys(documents.doc_prefix(3, 8)) == ["3/8/b.txt"]


def test_secured_chat_backups_can_still_be_imported(tmp_path):
    key = Fernet.generate_key().decode()
    f = tmp_path / "web-1-abc.txt"
    f.write_bytes(b'{"role": "user", "content": "hi"}\n')
    assert filestore.secure_folder(str(tmp_path), key) == 1
    assert filestore.secure_folder(str(tmp_path), key) == 0
    assert f.read_bytes().startswith(filestore.MAGIC)
    assert filestore.read_maybe_encrypted(f, key).startswith(b'{"role"')


# ---- 12b: sensitive keywords --------------------------------------------------------------------------------------
def _masked(text, keywords=guardrails.DEFAULT_KEYWORDS):
    out = text
    for a, b in sorted(pii.keyword_spans(text, keywords), reverse=True):
        out = out[:a] + "<V>" + out[b:]
    return out


@pytest.mark.parametrize("text,expected", [
    ("Salary: 12,40,000 per year. Next.", "Salary: <V>. Next."),
    ("Her salary is 12 lakh", "Her salary is <V>"),
    ("Name | Ravi\nSalary | 12,40,000", "Name | Ravi\nSalary | <V>"),
    ("DOB 12/03/1990 and joined", "DOB <V> and joined"),
    ("Aadhaar No: 1234 5678 9012", "Aadhaar No: <V>"),
    ("Diagnosis: type 2 diabetes; reviewed", "Diagnosis: <V>; reviewed"),
    ("Bank account number - 0012 3456 789", "Bank account number - <V>"),
    ("CTC of ₹18,00,000 offered", "CTC of <V> offered"),
])
def test_values_after_sensitive_keywords_are_found(text, expected):
    assert _masked(text) == expected


@pytest.mark.parametrize("text", [
    "What is my salary?", "Salary of employees is reviewed annually.", "PAN-India rollout starts in May.",
    "Medical leave is available.", "Read the passport policy before travelling.",
])
def test_ordinary_sentences_with_a_keyword_are_left_alone(text):
    assert _masked(text) == text


def test_a_keyword_value_never_runs_on():
    text = "Diagnosis: " + "word " * 100
    (a, b), = pii.keyword_spans(text, ["Diagnosis"])
    assert b - a <= pii.MAX_VALUE


def test_keywords_off_means_no_keyword_masking():
    snap = dataclasses.replace(guardrails.Snapshot(*[None] * 18), keywords_enabled=False, sensitive_keywords=("Salary",))
    assert snap.keywords == ()


# ---- 12b: Admin-defined types -------------------------------------------------------------------------------------
@pytest.mark.parametrize("name,pattern,msg", [
    ("x", r"\d+", "capital letters"), ("EMAIL_ADDRESS", r"\d+", "built-in"), ("EMP_ID", "", "between 1 and 300"),
    ("EMP_ID", "(", "not a valid regular expression"), ("EMP_ID", "a*", "matches empty text"),
])
def test_bad_custom_types_are_refused(name, pattern, msg):
    with pytest.raises(ValueError, match=msg):
        pii.check_custom(name, pattern, [], 60)


def test_a_custom_type_name_is_normalised():
    assert pii.check_custom("employee id", r"EMP-\d{5}", [" Employee ", "staff"], 70) == \
        {"name": "EMPLOYEE_ID", "pattern": r"EMP-\d{5}", "context": ["employee", "staff"], "confidence": 70}


def test_try_pattern_shows_matches():
    assert pii.try_pattern(r"EMP-\d{5}", "EMP-12345 and EMP-99999, not EMP-1") == ["EMP-12345", "EMP-99999"]


def test_custom_types_are_added_to_and_removed_from_the_masked_types(new_db):
    db = new_db("pii_types")
    t = pii.save_custom(db, {"name": "EMPLOYEE_ID", "pattern": r"\bEMP-\d{5}\b", "context": [], "confidence": 70}, "admin@x.com")
    assert "EMPLOYEE_ID" in guardrails.current(db).masked_types
    with pytest.raises(LookupError):
        pii.save_custom(db, {"name": "EMPLOYEE_ID", "pattern": r"\d", "context": [], "confidence": 70}, "admin@x.com")
    pii.save_custom(db, {"name": "STAFF_ID", "pattern": r"\bEMP-\d{5}\b", "context": [], "confidence": 70}, "admin@x.com", t["id"])
    types = guardrails.current(db).masked_types
    assert "STAFF_ID" in types and "EMPLOYEE_ID" not in types
    pii.delete_custom(db, t["id"], "admin@x.com")
    assert "STAFF_ID" not in guardrails.current(db).masked_types


def test_the_settings_screen_offers_custom_types(new_db):
    db = new_db("pii_view")
    pii.save_custom(db, {"name": "EMPLOYEE_ID", "pattern": r"EMP-\d{5}", "context": [], "confidence": 70}, "admin@x.com")
    field = next(f for f in guardrails.view(db)["fields"] if f["name"] == "masked_types")
    assert "EMPLOYEE_ID" in field["choices"]
    guardrails.update({"masked_types": ["EMAIL_ADDRESS", "EMPLOYEE_ID"]}, "admin@x.com", db)
    with pytest.raises(ValueError, match="Unknown personal-data type"):
        guardrails.update({"masked_types": ["NOT_A_TYPE"]}, "admin@x.com", db)


def test_new_installs_start_with_the_seed_keywords(new_db):
    snap = guardrails.current(new_db("pii_seed"))
    assert snap.keywords_enabled and "Salary" in snap.sensitive_keywords and snap.pii_threshold == 40


# ---- 12b: tokens and the vault ------------------------------------------------------------------------------------
def _hits(text, *values):
    return [guardrails.Hit(text.index(v), text.index(v) + len(v), t, 1.0) for t, v in values]


def test_the_same_value_gets_the_same_token_across_documents(new_db):
    db = new_db("vault_same")
    a = "Mail ravi@corp.com today"
    b = "Ask RAVI@corp.com"
    ta, ca = pii.tokenise(a, 1, 10, _hits(a, ("EMAIL_ADDRESS", "ravi@corp.com")), db)
    tb, _ = pii.tokenise(b, 1, 11, _hits(b, ("EMAIL_ADDRESS", "RAVI@corp.com")), db)
    token = pii.TOKEN_RE.search(ta).group(0)
    assert ca == {"EMAIL_ADDRESS": 1} and "ravi" not in ta.lower() and token in tb
    assert token.startswith("<EMAIL_ADDRESS_") and len(token) == len("<EMAIL_ADDRESS_>") + 8


def test_tokens_differ_between_knowledge_bases(new_db):
    db = new_db("vault_kb")
    t = "ravi@corp.com"
    one, _ = pii.tokenise(t, 1, 10, _hits(t, ("EMAIL_ADDRESS", t)), db)
    two, _ = pii.tokenise(t, 2, 20, _hits(t, ("EMAIL_ADDRESS", t)), db)
    assert one != two
    assert pii.reveal(one, [2], db) == (one, 0)                 # a token is only revealed from its own knowledge base


def test_reveal_puts_the_real_values_back(new_db):
    db = new_db("vault_reveal")
    t = "Salary: 12,40,000. Mail ravi@corp.com"
    tok, counts = pii.tokenise(t, 1, 10, _hits(t, ("SENSITIVE_VALUE", "12,40,000"), ("EMAIL_ADDRESS", "ravi@corp.com")), db)
    assert counts == {"SENSITIVE_VALUE": 1, "EMAIL_ADDRESS": 1}
    assert pii.reveal(tok, [1], db) == (t, 2)
    assert pii.reveal("<EMAIL_ADDRESS_00000000> stays", [1], db) == ("<EMAIL_ADDRESS_00000000> stays", 0)


def test_the_vault_holds_no_plain_values(new_db):
    db = new_db("vault_plain")
    t = "Mail ravi@corp.com"
    pii.tokenise(t, 1, 10, _hits(t, ("EMAIL_ADDRESS", "ravi@corp.com")), db)
    from sqlalchemy import select
    from app.db import PiiVault, Secret
    with db.session() as s:
        row = s.scalar(select(PiiVault))
        assert "ravi" not in row.value_encrypted and "ravi" not in row.value_hash
        assert "ravi" not in "".join(s.scalars(select(Secret.ciphertext)))


def test_a_value_goes_when_its_last_document_goes(new_db):
    db = new_db("vault_release")
    t = "ravi@corp.com"
    tok, _ = pii.tokenise(t, 1, 10, _hits(t, ("EMAIL_ADDRESS", t)), db)
    pii.tokenise(t, 1, 11, _hits(t, ("EMAIL_ADDRESS", t)), db)
    assert pii.counts_for_document(db, 10) == {"EMAIL_ADDRESS": 1}
    assert pii.release_document(db, 10) == 0 and pii.reveal(tok, [1], db)[1] == 1     # document 11 still uses it
    assert pii.release_document(db, 11) == 1 and pii.reveal(tok, [1], db)[1] == 0
    pii.tokenise(t, 1, 12, _hits(t, ("EMAIL_ADDRESS", t)), db)
    assert pii.release_kb(db, 1) == 1 and pii.counts_for_document(db, 12) == {}


# ---- 12b: with the detector (loads the language model) ------------------------------------------------------------
def test_detect_combines_built_in_custom_and_keyword_hits(new_db, monkeypatch):
    db = new_db("pii_detect")
    monkeypatch.setattr(pii, "get_db", lambda: db)
    pii.save_custom(db, {"name": "EMPLOYEE_ID", "pattern": r"\bEMP-\d{5}\b", "context": ["employee"], "confidence": 70}, "admin@x.com")
    snap = guardrails.current(db)
    text = "Employee EMP-12345, mail ravi@corp.com, Salary: 12,40,000."
    masked, counts = guardrails.mask_with(text, snap)
    assert masked == "Employee <EMPLOYEE_ID>, mail <EMAIL_ADDRESS>, Salary: <SENSITIVE_VALUE>."
    assert counts == {"EMPLOYEE_ID": 1, "EMAIL_ADDRESS": 1, "SENSITIVE_VALUE": 1}
    assert guardrails.mask_with(text, snap, allow=("ravi@corp.com",))[0].count("ravi@corp.com") == 1


def test_start_up_encrypts_the_store_and_flags_plain_legacy_folders(docs, tmp_path, monkeypatch, new_db):
    db = new_db("secure_start")
    from sqlalchemy import select
    from app.db import Alert
    chats = tmp_path / "chats"
    chats.mkdir()
    (chats / "web-1-a.txt").write_bytes(b'{"role": "user", "content": "hi"}\n')
    monkeypatch.setattr(documents, "settings", dataclasses.replace(documents.settings, chat_dir=str(chats)))
    monkeypatch.setattr(documents, "LEGACY_DOCS", str(tmp_path / "no-such-folder"))
    (docs / "1" / "2").mkdir(parents=True)
    (docs / "1" / "2" / "old.txt").write_bytes(SECRET)
    (docs / filestore.WORK / "x").mkdir(parents=True)
    (docs / filestore.WORK / "x" / "left.pdf").write_bytes(SECRET)

    def open_alerts():
        with db.session() as s:
            return [a.message for a in s.scalars(select(Alert).where(Alert.key == "files_unencrypted", Alert.resolved.is_(False)))]
    documents.secure_at_start(db)
    assert b"salary" not in _all_bytes(docs) and not (docs / filestore.WORK).exists()
    assert len(open_alerts()) == 1 and "old chat files" in open_alerts()[0] and "1 unencrypted" in open_alerts()[0]
    filestore.secure_folder(str(chats), documents.settings.secret_key)
    documents.secure_at_start(db)
    assert open_alerts() == []
