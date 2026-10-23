"""Retrieval strategies: similarity, keyword (BM25), hybrid and similarity with variety. A fake store stands in for the vector
index so the ranking logic and the access rules can be checked exactly."""
import dataclasses
import types

import pytest

from app import global_settings, keyword_search, retriever
from app.knowledge import access, kbs, versions

CORPUS = {
    "c1": ("docA", "Form 16 is issued by payroll every April for the previous financial year."),
    "c2": ("docB", "Annual leave entitlement is 18 days per year for every full-time employee."),
    "c3": ("docC", "Form 16 for the board: the secret bonus pool is 4.2 million."),          # restricted
    "c4": ("docD", "Staff holiday allowance: eighteen days of paid time off each year."),
    "c5": ("docE", "Annual leave entitlement is 18 days per year for every full-time employee (copy)."),
}
VECTOR_SCORES = {                                          # what the embedding model would say for each question
    "how many vacation days do i get": {"c4": 0.82, "c2": 0.74, "c5": 0.73, "c1": 0.1, "c3": 0.1},
    "form 16": {"c2": 0.3, "c1": 0.28, "c4": 0.2, "c3": 0.27, "c5": 0.2},
}


class FakeStore:
    name = "fake"

    def __init__(self, listable=True):
        self.listable = listable

    def count(self):
        return len(CORPUS)

    def search(self, query, k, flt=None):
        allowed = set(flt["doc_id"]["in"]) if flt else None
        scores = VECTOR_SCORES.get(query.lower(), {})
        rows = [{"id": i, "text": t, "metadata": {"doc_id": d, "source": d + ".txt"}, "score": scores.get(i, 0.1)}
                for i, (d, t) in CORPUS.items() if allowed is None or d in allowed]
        return sorted(rows, key=lambda r: -r["score"])[:k]

    def iter_texts(self, batch=500):
        if not self.listable:
            raise NotImplementedError("fake")
        yield list(CORPUS), [t for _, t in CORPUS.values()], [{"doc_id": d, "source": d + ".txt"} for d, _ in CORPUS.values()]


ALLOWED = ["docA", "docB", "docD", "docE"]               # docC is restricted for this asker


@pytest.fixture
def setup(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(retriever, "_targets", lambda kb_ids: [{"id": 1, "collection": "fake"}])
    monkeypatch.setattr(kbs, "store_for", lambda kb, **k: store)
    monkeypatch.setattr(versions, "active", lambda db, kb_id: {"version": 1})
    monkeypatch.setattr(access, "can_query", lambda db, kb, who: True)
    monkeypatch.setattr(access, "visible_doc_keys", lambda db, kb, who: list(ALLOWED))
    monkeypatch.setattr(access, "permits", lambda meta, who, kb: True)
    monkeypatch.setattr("app.identity.current", lambda: types.SimpleNamespace(group_ids=[], user_id=None))
    keyword_search.clear()

    def use(strategy, **extra):
        snap = dataclasses.replace(global_settings.defaults(), retrieval_strategy=strategy, top_k=3, **extra)
        monkeypatch.setattr(global_settings, "current", lambda db=None: snap)
    return use, store


def ids(hits):
    return [h["source"] for h in hits]


def test_similarity_is_the_default_and_follows_meaning(setup):
    use, _ = setup
    use("similarity")
    hits = retriever.retrieve_scored("how many vacation days do I get")
    assert ids(hits)[0] == "docD.txt" and {h["via"] for h in hits} == {"meaning"}


def test_keyword_finds_an_exact_code_that_meaning_ranks_low(setup):
    use, _ = setup
    use("similarity")
    sim = retriever.retrieve("form 16")
    assert not sim or sim[0]["source"] != "docA.txt" or sim[0]["score"] < 0.35         # meaning alone does not surface it
    use("keyword")
    hits = retriever.retrieve("form 16")
    assert ids(hits) == ["docA.txt"] and hits[0]["via"] == "keywords" and hits[0]["score"] >= 0.35


def test_keyword_search_ignores_filler_and_plurals():
    idx = keyword_search.Index()
    for i, (d, t) in CORPUS.items():
        idx.add(i, t, {"doc_id": d})
    hits = idx.search("what is the annual leave entitlement for the employees", 3, ["docB", "docE"])
    assert [h["id"] for h in hits][0] in ("c2", "c5")


def test_a_weak_single_word_match_is_below_the_default_threshold(setup):
    use, _ = setup
    use("keyword")
    assert retriever.retrieve("what is the capital of france") == []
    assert retriever.retrieve("paid") == [] or retriever.retrieve("paid")[0]["score"] >= 0.35


def test_hybrid_combines_both_lists(setup):
    use, _ = setup
    use("hybrid", hybrid_keyword_weight=0.5)
    hits = retriever.retrieve_scored("form 16")
    assert "docA.txt" in ids(hits)
    assert {h["via"] for h in hits} <= {"meaning", "keywords", "both"}
    assert [h for h in hits if h["source"] == "docA.txt"][0]["via"] == "both"


def test_hybrid_weight_zero_is_pure_meaning_and_one_is_pure_keywords(setup):
    use, _ = setup
    use("hybrid", hybrid_keyword_weight=0.0)
    assert ids(retriever.retrieve_scored("how many vacation days do I get"))[:2] == ["docD.txt", "docB.txt"]
    use("hybrid", hybrid_keyword_weight=1.0)
    assert ids(retriever.retrieve_scored("form 16"))[0] == "docA.txt"


def test_a_restricted_document_never_appears_under_any_strategy(setup):
    use, _ = setup
    for strategy in ("similarity", "keyword", "hybrid", "mmr"):
        use(strategy)
        assert "docC.txt" not in ids(retriever.retrieve_scored("form 16")), strategy


def test_restricted_text_does_not_change_keyword_scores(setup):
    idx = keyword_search.Index()
    for i, (d, t) in CORPUS.items():
        idx.add(i, t, {"doc_id": d})
    with_c = idx.search("form 16", 5, ["docA", "docC"])
    without = idx.search("form 16", 5, ["docA"])
    assert [h["id"] for h in without] == ["c1"]
    solo = keyword_search.Index()
    solo.add("c1", CORPUS["c1"][1], {"doc_id": "docA"})
    assert solo.search("form 16", 5, ["docA"])[0]["raw"] == without[0]["raw"]


def test_variety_drops_a_near_duplicate(setup):
    use, _ = setup
    use("similarity")
    plain = ids(retriever.retrieve_scored("how many vacation days do I get"))
    assert "docB.txt" in plain and "docE.txt" in plain                  # the two copies both come through
    use("mmr", diversity=0.9)
    varied = ids(retriever.retrieve_scored("how many vacation days do I get"))
    assert varied[0] == "docD.txt" and not ({"docB.txt", "docE.txt"} <= set(varied[:2]))


def test_a_store_that_cannot_list_passages_falls_back_to_similarity(setup, caplog):
    use, store = setup
    store.listable = False
    use("keyword")
    hits = retriever.retrieve_scored("how many vacation days do I get")
    assert ids(hits)[0] == "docD.txt"


def test_the_new_settings_are_validated_and_saved():
    base = global_settings.defaults()
    assert global_settings.validate({"retrieval_strategy": "hybrid", "hybrid_keyword_weight": 0.7}, base) == {"retrieval_strategy": "hybrid", "hybrid_keyword_weight": 0.7}
    with pytest.raises(ValueError):
        global_settings.validate({"retrieval_strategy": "magic"}, base)
    with pytest.raises(ValueError):
        global_settings.validate({"diversity": 1.5}, base)
    before, after = global_settings.update({"retrieval_strategy": "keyword"}, "tester")
    assert before.retrieval_strategy == "similarity" and after.retrieval_strategy == "keyword"
    global_settings.update({"retrieval_strategy": "similarity"}, "tester")
    shown = {f["name"]: f for f in global_settings.view()["fields"]}
    assert shown["retrieval_strategy"]["kind"] == "choice" and "hybrid" in shown["retrieval_strategy"]["choices"]


def test_sources_say_how_each_passage_was_found():
    from app import rag
    out = rag.format_sources([{"text": "x", "source": "a.txt", "page": None, "score": 0.5, "via": "keywords"}])
    assert out[0]["via"] == "keywords"
