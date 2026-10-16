"""LLM providers: failure handling and fallback, the store rules, hot reload, the .env seed,
adapters (HTTP mocked) and the admin API. No network and no real keys."""
import dataclasses

import httpx
import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app import alerts, llm
from app.admin import api as admin_api
from app.admin import providers_api, users
from app.admin.auth import SessionSigner
from app.channels.store import ChannelStore
from app.db import Alert, ModelPrice, UsageRecord
from app.providers import REGISTRY, ProviderAdapter, ProviderError, adapter_class, base, seed
from app.providers.base import API_KEY
from app.providers.store import ProviderStore
from app.secretstore import FernetSecretStore, generate_key

KEY = "sk-super-secret-key-12345"


# ---- fakes -------------------------------------------------------------------------------------
class HTTPError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status_code = status


class Msg:
    def __init__(self, text_, inp=None, out=None):
        self.content = text_
        self.usage_metadata = {"input_tokens": inp, "output_tokens": out} if inp is not None else None


class Fake:
    """A scripted chat model: each call takes the next behaviour (repeating the last)."""

    def __init__(self, *behaviours):
        self.behaviours, self.calls = list(behaviours), 0

    def _next(self):
        b = self.behaviours[min(self.calls, len(self.behaviours) - 1)]
        self.calls += 1
        if isinstance(b, BaseException):
            raise b
        return b() if callable(b) else b

    def invoke(self, messages, **kw):
        return self._next()

    def stream(self, messages, **kw):
        yield from self._next()


def chain(db, pref, fb=None):
    slots = [llm.Slot(1, "pref", "openai", "m-pref", pref)]
    if fb is not None:
        slots.append(llm.Slot(2, "fb", "anthropic", "m-fb", fb))
    return llm.ModelChain(slots, db)


def open_alerts(db):
    return {a["key"] for a in alerts.active(db)}


# ---- which failures trigger the fallback --------------------------------------------------------
@pytest.mark.parametrize("exc", [HTTPError(402), HTTPError(429), HTTPError(500), HTTPError(503),
                                 httpx.ReadTimeout("slow"), httpx.ConnectError("down"), TimeoutError()])
def test_these_failures_fall_back(exc):
    assert llm.should_fall_back(exc) is True


@pytest.mark.parametrize("exc", [HTTPError(400), HTTPError(401), HTTPError(403), HTTPError(404), HTTPError(422),
                                 ValueError("bad"), RuntimeError("boom")])
def test_these_failures_do_not_fall_back(exc):
    assert llm.should_fall_back(exc) is False


def test_status_is_found_through_wrapped_errors():
    inner = HTTPError(402)
    outer = RuntimeError("wrapped")
    outer.__cause__ = inner
    assert llm.status_of(outer) == 402 and llm.should_fall_back(outer)


# ---- the model chain ----------------------------------------------------------------------------------
def usage_rows(db):
    with db.session() as s:
        return [(u.provider, u.model, u.purpose, u.status, u.input_tokens, u.output_tokens)
                for u in s.scalars(select(UsageRecord).order_by(UsageRecord.id))]


def test_success_on_the_preferred_model_records_one_usage_row(new_db):
    db = new_db()
    pref, fb = Fake(Msg("hello", 10, 4)), Fake(Msg("never"))
    out = chain(db, pref, fb).invoke(["hi"], purpose="router", department="hr")
    assert out.content == "hello" and fb.calls == 0
    assert usage_rows(db) == [("pref", "m-pref", "router", "ok", 10, 4)]
    with db.session() as s:
        assert s.scalar(select(UsageRecord.department)) == "hr"
    assert open_alerts(db) == set()


@pytest.mark.parametrize("exc, why", [(HTTPError(402), "credit exhausted"), (HTTPError(429), "rate limited"),
                                      (HTTPError(500), "server error"), (httpx.ReadTimeout("t"), "timed out")])
def test_the_fallback_answers_and_an_alert_is_raised(new_db, exc, why):
    db = new_db()
    out = chain(db, Fake(exc), Fake(Msg("from fallback", 5, 2))).invoke(["hi"])
    assert out.content == "from fallback"
    assert [(p, s) for p, _, _, s, _, _ in usage_rows(db)] == [("pref", "failed"), ("fb", "ok")]
    assert "fallback_used" in open_alerts(db)
    message = next(a["message"] for a in alerts.active(db) if a["key"] == "fallback_used")
    assert "pref/m-pref" in message and "fb/m-fb" in message


def test_a_402_or_a_rejected_key_raises_a_provider_failing_alert(new_db):
    db = new_db()
    chain(db, Fake(HTTPError(402)), Fake(Msg("ok"))).invoke(["hi"])
    msg = next(a["message"] for a in alerts.active(db) if a["key"] == "provider_failing:pref")
    assert "credit exhausted" in msg and "Add credit" in msg
    c = chain(db, Fake(HTTPError(401)))
    with pytest.raises(HTTPError):
        c.invoke(["hi"])
    assert "key rejected" in next(a["message"] for a in alerts.active(db) if a["key"] == "provider_failing:pref")


def test_a_rejected_request_is_not_retried_on_the_fallback(new_db):
    db = new_db()
    fb = Fake(Msg("should not be used"))
    with pytest.raises(HTTPError) as e:
        chain(db, Fake(HTTPError(400)), fb).invoke(["hi"])
    assert e.value.status_code == 400 and fb.calls == 0
    assert "fallback_used" not in open_alerts(db) and "llm_unavailable" not in open_alerts(db)


def test_if_both_models_fail_the_last_error_is_raised_and_an_alert_is_raised(new_db):
    db = new_db()
    with pytest.raises(HTTPError) as e:
        chain(db, Fake(HTTPError(402)), Fake(HTTPError(503))).invoke(["hi"])
    assert e.value.status_code == 503
    assert "llm_unavailable" in open_alerts(db)
    assert [s for *_, s, _, _ in usage_rows(db)] == ["failed", "failed"]


def test_with_no_fallback_a_failure_raises_and_alerts(new_db):
    db = new_db()
    with pytest.raises(HTTPError):
        chain(db, Fake(HTTPError(500))).invoke(["hi"])
    assert "llm_unavailable" in open_alerts(db)


def test_alerts_clear_themselves_when_the_provider_works_again(new_db):
    db = new_db()
    pref = Fake(HTTPError(402), Msg("back", 1, 1))
    c = chain(db, pref, Fake(Msg("fb")))
    c.invoke(["hi"])
    assert {"fallback_used", "provider_failing:pref"} <= open_alerts(db)
    assert c.invoke(["hi"]).content == "back"
    assert open_alerts(db) == set()


def test_streaming_collects_the_answer_so_a_midstream_failure_can_still_fall_back(new_db):
    db = new_db()

    def dies_midway():
        yield Msg("part")
        raise HTTPError(500)

    out = list(chain(db, Fake(dies_midway), Fake([Msg("full "), Msg("answer", 7, 3)])).stream(["hi"], purpose="answer"))
    assert [m.content for m in out] == ["full ", "answer"]            # nothing from the failed attempt leaks out
    assert [(p, s) for p, _, _, s, _, _ in usage_rows(db)] == [("pref", "failed"), ("fb", "ok")]


def test_streamed_usage_takes_the_largest_value_per_field(new_db):
    db = new_db()
    chunks = [Msg("a", 12, 1), Msg("b", None), Msg("c", 12, 9)]       # cumulative or partial: both end at 12 and 9
    list(chain(db, Fake(chunks)).stream(["hi"]))
    assert usage_rows(db) == [("pref", "m-pref", "answer", "ok", 12, 9)]


def test_cost_is_filled_in_only_for_models_with_a_price(new_db):
    db = new_db()
    with db.session() as s:
        s.add(ModelPrice(provider_type="openai", model="m-pref", input_per_1k=0.5, output_per_1k=1.5))
    chain(db, Fake(Msg("x", 2000, 1000))).invoke(["hi"])
    chain(db, Fake(HTTPError(500)), Fake(Msg("y", 100, 100))).invoke(["hi"])          # fallback model has no price
    with db.session() as s:
        costs = [u.cost for u in s.scalars(select(UsageRecord).order_by(UsageRecord.id))]
    assert costs[0] == pytest.approx(2.5) and costs[1] is None and costs[2] is None


def test_a_failure_while_recording_usage_never_breaks_the_answer(new_db, monkeypatch):
    db = new_db()
    monkeypatch.setattr("app.usage.UsageRecord", None)               # makes the insert blow up
    assert chain(db, Fake(Msg("fine", 1, 1))).invoke(["hi"]).content == "fine"


def test_usage_never_stores_error_text_only_the_error_type_and_status(new_db):
    db = new_db()
    with pytest.raises(HTTPError):
        chain(db, Fake(HTTPError(400))).invoke(["hi"])
    with db.session() as s:
        assert s.scalar(select(UsageRecord.error)) == "HTTPError HTTP 400"


# ---- the store: encryption, approval, model settings -----------------------------------------------
class FakeProvider(ProviderAdapter):
    type, title = "fake", "Fake provider"
    SPEC = (API_KEY,)
    built: list = []

    def list_models(self):
        if self.cfg["api_key"] == "bad":
            raise ProviderError("The provider rejected the API key (HTTP 401).")
        return ["m1", "m2"]

    def build(self, model, timeout):
        FakeProvider.built.append((model, timeout))
        return Fake(Msg(f"answer from {model}", 3, 2))


@pytest.fixture
def fake_registry(monkeypatch):
    monkeypatch.setitem(REGISTRY, "fake", FakeProvider)
    FakeProvider.built = []
    return REGISTRY


@pytest.fixture
def pstore(new_db, fake_registry):
    return ProviderStore(new_db(), FernetSecretStore(generate_key()))


def make_provider(store, name="main", key=KEY, models=("m1", "m2"), type_="fake"):
    if type_ not in store.approved():
        store.set_approved(sorted(set(store.approved()) | {type_}), "admin")
    return store.create(type_, name, {"api_key": key}, list(models), "admin")


def test_providers_need_an_approved_type_first(pstore):
    with pytest.raises(ValueError, match="not an approved provider"):
        pstore.create("fake", "x", {"api_key": KEY}, ["m1"], "a")
    assert pstore.approved() == []                                   # nothing is approved by default (D15)


def test_the_key_is_encrypted_never_returned_and_never_audited(pstore, db_text):
    pid = make_provider(pstore)
    dump = db_text(pstore.db)
    assert KEY not in dump and "gAAAA" in dump
    view = pstore.get(pid)
    assert KEY not in repr(view) and {f["name"]: f for f in view["fields"]}["api_key"]["is_set"] is True
    assert KEY not in repr(pstore.list_all())
    info, cfg = pstore.runtime(pid)
    assert cfg["api_key"] == KEY and info["name"] == "main"
    with pstore.db.session() as s:
        assert all(KEY not in str(r) for r in s.execute(text("select * from audit_log")).all())


def test_names_are_unique_and_a_blank_key_keeps_the_stored_one(pstore):
    pid = make_provider(pstore)
    with pytest.raises(ValueError, match="already exists"):
        pstore.create("fake", "main", {"api_key": KEY}, ["m1"], "a")
    pstore.update(pid, "renamed", {"api_key": ""}, ["m1", "m2", "m3"], "a")
    assert pstore.runtime(pid)[1]["api_key"] == KEY and pstore.get(pid)["name"] == "renamed"
    assert pstore.get(pid)["available_models"] == ["m1", "m2", "m3"]


def test_unknown_settings_are_rejected(pstore):
    make_provider(pstore)
    with pytest.raises(ValueError, match="Unknown settings"):
        pstore.merged("fake", {"nonsense": 1})


def test_approval_cannot_be_withdrawn_while_a_provider_of_that_type_exists(pstore):
    make_provider(pstore)
    with pytest.raises(ValueError, match="Cannot remove approval"):
        pstore.set_approved([], "a")
    with pytest.raises(ValueError, match="Unknown provider type"):
        pstore.set_approved(["nope"], "a")


def test_model_settings_rules(pstore):
    a = make_provider(pstore, "a")
    b = make_provider(pstore, "b")
    ok = pstore.set_model_settings({"provider_id": a, "model": "m1"}, {"provider_id": b, "model": "m2"}, 30, "admin")
    assert ok == []                                                  # different providers: no warning
    same = pstore.set_model_settings({"provider_id": a, "model": "m1"}, {"provider_id": a, "model": "m2"}, 30, "admin")
    assert len(same) == 1 and "same provider" in same[0]
    with pytest.raises(ValueError, match="different model"):
        pstore.set_model_settings({"provider_id": a, "model": "m1"}, {"provider_id": a, "model": "m1"}, 30, "a")
    with pytest.raises(ValueError, match="choose one of the models"):
        pstore.set_model_settings({"provider_id": a, "model": "ghost"}, None, 30, "a")
    with pytest.raises(ValueError, match="choose a provider"):
        pstore.set_model_settings({"provider_id": 999, "model": "m1"}, None, 30, "a")
    for bad in (4, 301, True, "30"):
        with pytest.raises(ValueError, match="timeout"):
            pstore.set_model_settings({"provider_id": a, "model": "m1"}, None, bad, "a")
    assert pstore.model_settings()["fallback"]["provider_id"] == a   # failed attempts changed nothing


def test_a_provider_in_use_cannot_be_deleted(pstore):
    a = make_provider(pstore, "a")
    b = make_provider(pstore, "b")
    pstore.set_model_settings({"provider_id": a, "model": "m1"}, {"provider_id": b, "model": "m1"}, 30, "admin")
    for pid in (a, b):
        with pytest.raises(ValueError, match="used by the model settings"):
            pstore.delete(pid, "admin")
    pstore.set_model_settings({"provider_id": a, "model": "m1"}, None, 30, "admin")
    pstore.delete(b, "admin")
    assert [p["name"] for p in pstore.list_all()] == ["a"]
    with pstore.db.session() as s:
        assert s.execute(text("select count(*) from secrets where owner_id = :i"), {"i": str(b)}).scalar() == 0


# ---- the runtime: built from the database, reloaded without a restart ----------------------------------
@pytest.fixture
def runtime(pstore, monkeypatch):
    monkeypatch.setattr(llm, "_dependencies", lambda: (pstore.db, pstore.secrets))
    monkeypatch.setattr(llm, "CACHE_SECONDS", 0)
    llm.invalidate()
    yield pstore
    llm.invalidate()


def test_the_models_come_from_the_database_and_follow_settings_changes(runtime):
    a = make_provider(runtime, "a")
    b = make_provider(runtime, "b")
    runtime.set_model_settings({"provider_id": a, "model": "m1"}, {"provider_id": b, "model": "m2"}, 45, "admin")
    c = llm.get_chat_model()
    assert [(s.provider_name, s.model) for s in c.slots] == [("a", "m1"), ("b", "m2")]
    assert c.invoke(["hi"]).content == "answer from m1"
    assert FakeProvider.built[:2] == [("m1", 45), ("m2", 45)]        # the saved timeout reaches the model
    runtime.set_model_settings({"provider_id": b, "model": "m2"}, None, 45, "admin")      # no restart in between
    c2 = llm.get_chat_model()
    assert [(s.provider_name, s.model) for s in c2.slots] == [("b", "m2")]
    assert c2.invoke(["hi"]).content == "answer from m2"


def test_nothing_configured_and_no_env_model_gives_a_clear_error(runtime, monkeypatch):
    monkeypatch.setattr(llm, "settings", dataclasses.replace(llm.settings, api_key="", llm_model=""))
    with pytest.raises(RuntimeError, match="Providers screen"):
        llm.get_chat_model()


def test_without_providers_the_env_model_is_used(runtime, monkeypatch):
    monkeypatch.setattr(llm, "settings", dataclasses.replace(llm.settings, api_key="k", llm_model="env-model",
                                                             base_url="http://localhost:1/v1"))
    c = llm.get_chat_model()
    assert [(s.provider_name, s.model) for s in c.slots] == [("env", "env-model")]


# ---- the .env seed --------------------------------------------------------------------------------------
def test_the_env_model_becomes_a_provider_once(pstore, monkeypatch):
    monkeypatch.setattr(seed, "settings", dataclasses.replace(seed.settings, api_key=KEY, llm_model="gemini-x",
                                                              base_url="https://example.test/v1"))
    assert seed.seed_from_env(pstore) is True
    [p] = pstore.list_all()
    assert p["name"] == seed.SEED_NAME and p["type"] == "openai_compatible" and p["status"] == "Not tested"
    assert "openai_compatible" in pstore.approved()
    assert pstore.model_settings()["preferred"] == {"provider_id": p["id"], "model": "gemini-x"}
    assert pstore.runtime(p["id"])[1]["api_key"] == KEY
    assert seed.seed_from_env(pstore) is False                       # runs once
    assert len(pstore.list_all()) == 1


def test_the_seed_does_nothing_without_an_env_model_or_when_providers_exist(pstore, monkeypatch):
    monkeypatch.setattr(seed, "settings", dataclasses.replace(seed.settings, api_key="", llm_model=""))
    assert seed.seed_from_env(pstore) is False and pstore.list_all() == []
    monkeypatch.setattr(seed, "settings", dataclasses.replace(seed.settings, api_key=KEY, llm_model="m"))
    make_provider(pstore, "existing")
    assert seed.seed_from_env(pstore) is False and [p["name"] for p in pstore.list_all()] == ["existing"]


# ---- adapters (HTTP mocked) ----------------------------------------------------------------------------
class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body, self.ok = status, body, 200 <= status < 300

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


@pytest.fixture
def http(monkeypatch):
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, headers))
        r = http.responses[0] if len(http.responses) == 1 else http.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    http.responses = []
    http.calls = calls
    monkeypatch.setattr(base.requests, "get", fake_get)
    return http


def test_openai_style_lists_models_and_strips_the_models_prefix(http):
    http.responses = [Resp(200, {"data": [{"id": "models/gemini-b"}, {"id": "gemini-a"}, {"id": "gemini-a"}]})]
    models = adapter_class("openai_compatible")({"api_key": KEY, "endpoint": "https://h.test/v1/"}).list_models()
    assert models == ["gemini-a", "gemini-b"]
    url, headers = http.calls[0]
    assert url == "https://h.test/v1/models" and headers == {"Authorization": f"Bearer {KEY}"}


def test_an_endpoint_that_cannot_list_models_uses_the_manual_list_or_says_so(http):
    http.responses = [Resp(404)]
    assert adapter_class("openai_compatible")({"api_key": KEY, "endpoint": "https://h.test", "manual_models": ["x"]}).list_models() == ["x"]
    with pytest.raises(ProviderError, match="Enter the model names"):
        adapter_class("openai_compatible")({"api_key": KEY, "endpoint": "https://h.test"}).list_models()


@pytest.mark.parametrize("status, expected", [(401, "rejected the API key"), (403, "rejected the API key"),
                                              (402, "no credit"), (429, "rate limiting"), (500, "HTTP 500")])
def test_provider_errors_become_safe_messages_that_never_contain_the_key(http, status, expected):
    http.responses = [Resp(status, {"error": {"message": f"Incorrect API key provided: {KEY}"}})]
    with pytest.raises(ProviderError) as e:
        adapter_class("openai")({"api_key": KEY}).list_models()
    assert expected in str(e.value) and KEY not in str(e.value) and "Incorrect API key" not in str(e.value)


def test_an_unreachable_provider_and_a_non_json_answer_are_reported_safely(http):
    http.responses = [requests.ConnectionError(f"failed for key {KEY}")]
    with pytest.raises(ProviderError) as e:
        adapter_class("openai")({"api_key": KEY}).list_models()
    assert "Could not reach" in str(e.value) and KEY not in str(e.value)
    http.responses = [Resp(200, None)]
    with pytest.raises(ProviderError, match="not JSON"):
        adapter_class("openai")({"api_key": KEY}).list_models()


def test_anthropic_and_google_listing(http):
    http.responses = [Resp(200, {"data": [{"id": "claude-b"}, {"id": "claude-a"}]})]
    assert adapter_class("anthropic")({"api_key": KEY}).list_models() == ["claude-a", "claude-b"]
    assert http.calls[0][1] == {"x-api-key": KEY, "anthropic-version": "2023-06-01"}
    http.responses = [Resp(200, {"models": [
        {"name": "models/gemini-chat", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/embedding-1", "supportedGenerationMethods": ["embedContent"]}]})]
    assert adapter_class("google")({"api_key": KEY}).list_models() == ["gemini-chat"]
    assert http.calls[1][1] == {"x-goog-api-key": KEY} and KEY not in http.calls[1][0]    # key in a header, not the URL


def test_azure_proves_the_deployment_with_a_test_call(monkeypatch):
    cfg = {"api_key": KEY, "endpoint": "https://r.openai.azure.com", "deployment": "dep", "api_version": "2024-06-01"}
    adapter = adapter_class("azure_openai")(cfg)
    monkeypatch.setattr(adapter, "build", lambda model, timeout: Fake(Msg("OK")))
    assert adapter.list_models() == ["dep"]
    monkeypatch.setattr(adapter, "build", lambda model, timeout: Fake(HTTPError(401)))
    with pytest.raises(ProviderError) as e:
        adapter.list_models()
    assert "refused the test call" in str(e.value) and KEY not in str(e.value)


def test_required_settings_are_checked():
    assert adapter_class("openai").validate({}) == ["API key is required"]
    assert adapter_class("openai_compatible").validate({"api_key": "k"}) == ["Endpoint is required"]
    assert len(adapter_class("azure_openai").validate({"api_key": "k"})) == 3
    assert adapter_class("anthropic").validate({"api_key": "k"}) == []


def test_every_provider_builds_the_right_model_without_network():
    from langchain_anthropic import ChatAnthropic
    from langchain_google_genai import ChatGoogleGenerativeAI
    from langchain_openai import AzureChatOpenAI, ChatOpenAI
    k = {"api_key": KEY}
    for type_, cfg, cls in [
        ("openai", k, ChatOpenAI), ("xai", k, ChatOpenAI), ("deepseek", k, ChatOpenAI),
        ("openai_compatible", {**k, "endpoint": "https://h.test/v1"}, ChatOpenAI),
        ("azure_openai", {**k, "endpoint": "https://r.openai.azure.com", "deployment": "d", "api_version": "2024-06-01"}, AzureChatOpenAI),
        ("anthropic", k, ChatAnthropic), ("google", k, ChatGoogleGenerativeAI),
    ]:
        model = adapter_class(type_)(cfg).build("some-model", 30)
        assert isinstance(model, cls), type_
    openai_model = adapter_class("xai")(k).build("grok", 30)
    assert str(openai_model.openai_api_base).startswith("https://api.x.ai/v1") and openai_model.max_retries == 0


def test_grok_and_deepseek_carry_a_data_handling_warning():
    assert "xAI" in adapter_class("xai").warning and "DeepSeek" in adapter_class("deepseek").warning
    assert adapter_class("openai").warning == "" and adapter_class("anthropic").warning == ""


# ---- admin API -----------------------------------------------------------------------------------------------
EMAIL, PASSWORD = "boss@sysfore.com", "correct horse battery"


@pytest.fixture
def papi(new_db, fake_registry):
    db = new_db()
    secrets = FernetSecretStore(generate_key())
    ctx = admin_api.AdminContext(db=db, store=ChannelStore(db, secrets, lambda t: ()), manager=None,
                                 signer=SessionSigner(generate_key()), domain="sysfore.com", allow_remote=True)
    users.create_admin(db, EMAIL, PASSWORD, domain="sysfore.com")
    app = FastAPI()
    app.include_router(admin_api.router)
    app.include_router(providers_api.router)
    app.dependency_overrides[admin_api.get_ctx] = lambda: ctx
    c = TestClient(app, base_url="http://localhost")
    r = c.post("/admin/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    return ctx, c, {"X-CSRF-Token": r.json()["csrf"]}


def test_provider_routes_need_sign_in_and_csrf(papi):
    ctx, c, h = papi
    anon = TestClient(c.app, base_url="http://localhost")
    for method, path in [("get", "/providers"), ("post", "/providers"), ("put", "/providers/approval"),
                         ("get", "/model-settings"), ("put", "/model-settings"), ("get", "/alerts")]:
        assert getattr(anon, method)("/admin/api" + path, **({"json": {}} if method in ("post", "put") else {})).status_code == 401, path
    assert c.put("/admin/api/providers/approval", json={"approved": ["fake"]}).status_code == 403     # no CSRF


def test_full_provider_flow_through_the_api(papi):
    ctx, c, h = papi
    listing = c.get("/admin/api/providers").json()
    assert {t["type"] for t in listing["types"]} >= {"anthropic", "google", "openai", "azure_openai", "xai", "deepseek", "openai_compatible"}
    assert listing["approved"] == [] and listing["providers"] == []
    grok = next(t for t in listing["types"] if t["type"] == "xai")
    assert "xAI" in grok["warning"] and grok["approved"] is False
    # not approved yet
    r = c.post("/admin/api/providers", headers=h, json={"type": "fake", "name": "main", "values": {"api_key": KEY}})
    assert r.status_code == 422 and "not an approved provider" in r.json()["detail"]
    assert c.put("/admin/api/providers/approval", headers=h, json={"approved": ["fake"]}).json() == {"approved": ["fake"]}
    # a failing test stores nothing
    bad = c.post("/admin/api/providers", headers=h, json={"type": "fake", "name": "main", "values": {"api_key": "bad"}})
    assert bad.status_code == 400 and "rejected the API key" in bad.json()["detail"]
    assert c.get("/admin/api/providers").json()["providers"] == []
    # a good one is created, tested, and the key never comes back
    ok = c.post("/admin/api/providers", headers=h, json={"type": "fake", "name": "main", "values": {"api_key": KEY}})
    assert ok.status_code == 201 and ok.json()["available_models"] == ["m1", "m2"] and ok.json()["status"] == "Connected"
    assert KEY not in ok.text and KEY not in c.get("/admin/api/providers").text
    pid = ok.json()["id"]
    # dry-run test and re-test
    assert c.post("/admin/api/providers/test", headers=h, json={"type": "fake", "values": {"api_key": KEY}}).json()["ok"] is True
    assert c.post("/admin/api/providers/test", headers=h, json={"type": "fake", "values": {"api_key": "bad"}}).status_code == 400
    assert c.post(f"/admin/api/providers/{pid}/test", headers=h).json()["ok"] is True
    # an edit with a blank key keeps the key
    up = c.put(f"/admin/api/providers/{pid}", headers=h, json={"name": "primary", "values": {"api_key": ""}})
    assert up.status_code == 200 and up.json()["name"] == "primary"
    assert ProviderStore(ctx.db, ctx.store.secrets).runtime(pid)[1]["api_key"] == KEY
    # model settings
    ms = c.put("/admin/api/model-settings", headers=h, json={"preferred": {"provider_id": pid, "model": "m1"},
                                                              "fallback": None, "request_timeout": 40})
    assert ms.status_code == 200 and ms.json()["preferred"]["model"] == "m1" and ms.json()["warnings"] == []
    assert c.put("/admin/api/model-settings", headers=h, json={"preferred": {"provider_id": pid, "model": "zzz"},
                                                                "request_timeout": 40}).status_code == 422
    assert c.delete(f"/admin/api/providers/{pid}", headers=h).status_code == 409                # in use
    assert c.put("/admin/api/providers/approval", headers=h, json={"approved": []}).status_code == 422
    audit = [e for e in ctx.store.recent_audit(200)]
    assert {"provider.create", "provider.update", "model_settings.update", "providers.approval", "secret.replace"} <= {e["action"] for e in audit}
    assert all(KEY not in str(e) for e in audit)


def test_a_failed_retest_marks_the_provider_failed(papi, monkeypatch):
    ctx, c, h = papi
    store = ProviderStore(ctx.db, ctx.store.secrets)
    pid = make_provider(store)
    monkeypatch.setattr(FakeProvider, "list_models", lambda self: (_ for _ in ()).throw(ProviderError("The provider says the account has no credit left (HTTP 402).")))
    r = c.post(f"/admin/api/providers/{pid}/test", headers=h).json()
    assert r["ok"] is False and "no credit" in r["message"]
    assert store.get(pid)["status"] == "Failed" and "no credit" in store.get(pid)["status_detail"]


def test_alerts_can_be_listed_and_resolved(papi):
    ctx, c, h = papi
    alerts.raise_alert(ctx.db, "k1", "provider_failing", "OpenAI: credit exhausted", "error")
    alerts.raise_alert(ctx.db, "k1", "provider_failing", "OpenAI: credit exhausted again", "error")      # same key: one row
    listed = c.get("/admin/api/alerts").json()
    assert len(listed) == 1 and listed[0]["count"] == 2 and listed[0]["severity"] == "error"
    assert c.post(f"/admin/api/alerts/{listed[0]['id']}/resolve", headers=h).status_code == 200
    assert c.get("/admin/api/alerts").json() == []
    assert c.post(f"/admin/api/alerts/{listed[0]['id']}/resolve", headers=h).status_code == 404
    assert any(e["action"] == "alert.resolve" for e in ctx.store.recent_audit())


def test_a_passing_connection_test_does_not_clear_a_credit_alert(papi):
    """Listing models needs no credit, so passing it must not hide 'credit exhausted'."""
    ctx, c, h = papi
    store = ProviderStore(ctx.db, ctx.store.secrets)
    pid = make_provider(store, "main")
    alerts.raise_alert(ctx.db, "provider_failing:main", "provider_failing", "main: credit exhausted (HTTP 402)", "error")
    assert c.post(f"/admin/api/providers/{pid}/test", headers=h).json()["ok"] is True
    assert "provider_failing:main" in open_alerts(ctx.db)
