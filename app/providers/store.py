"""Persistence for LLM providers, the approved-provider list and the model settings.

The API key lives encrypted in `secrets` (owner_type='provider'); it is write-only: it is never
returned except to the runtime (`runtime`) and never written to the audit log.
"""
from typing import Any

from sqlalchemy import delete, select

from app import global_settings
from app.db import AuditLog, Database, GlobalSettings, LLMProvider, ModelSettings, Secret
from app.providers import REGISTRY, adapter_class
from app.secretstore import SecretStore

OWNER = "provider"
COLUMNS = ("endpoint", "deployment", "api_version", "manual_models")      # non-secret settings stored as columns
MIN_TIMEOUT, MAX_TIMEOUT = 5, 300


class ProviderStore:
    def __init__(self, db: Database, secrets: SecretStore):
        self.db, self.secrets = db, secrets

    # ---- helpers ------------------------------------------------------------------------
    @staticmethod
    def _audit(s, actor: str, action: str, target: str = "", detail: str = "") -> None:
        s.add(AuditLog(actor=actor, action=action, target=target, detail=detail))

    def _has_key(self, s, pid: int) -> bool:
        return s.scalar(select(Secret.id).where(Secret.owner_type == OWNER, Secret.owner_id == str(pid),
                                                Secret.name == "api_key")) is not None

    def _view(self, s, p: LLMProvider) -> dict:
        cls = adapter_class(p.type)
        has_key = self._has_key(s, p.id)
        fields = []
        for f in cls.SPEC:
            item = {"name": f.name, "label": f.label, "kind": f.kind, "secret": f.secret,
                    "required": f.required, "help": f.help}
            if f.choices:
                item["choices"] = list(f.choices)
            if f.secret:
                item["is_set"] = has_key
            else:
                value = getattr(p, f.name, None) if f.name in COLUMNS else None
                item["value"] = value if value is not None else ([] if f.kind == "list" else f.default)
            fields.append(item)
        return {"id": p.id, "name": p.name, "type": p.type, "title": cls.title, "warning": cls.warning,
                "status": p.status, "status_detail": p.status_detail,
                "available_models": list(p.available_models or []), "fields": fields,
                "updated_at": p.updated_at}

    # ---- providers ----------------------------------------------------------------------
    def list_all(self) -> "list[dict]":
        with self.db.session() as s:
            return [self._view(s, p) for p in s.scalars(select(LLMProvider).order_by(LLMProvider.name))]

    def get(self, pid: int) -> dict | None:
        with self.db.session() as s:
            p = s.get(LLMProvider, pid)
            return self._view(s, p) if p else None

    def runtime(self, pid: int) -> tuple[dict, dict] | None:
        """(provider summary, full config including the decrypted key) for building a model."""
        with self.db.session() as s:
            p = s.get(LLMProvider, pid)
            if p is None:
                return None
            cfg = {c: getattr(p, c) for c in COLUMNS}
            row = s.scalar(select(Secret).where(Secret.owner_type == OWNER, Secret.owner_id == str(pid),
                                                Secret.name == "api_key"))
            if row is not None:
                cfg["api_key"] = self.secrets.decrypt(row.ciphertext)
            return {"id": p.id, "name": p.name, "type": p.type}, cfg

    def merged(self, type_: str, values: dict, pid: int | None = None) -> dict:
        """The full config after applying `values` on top of what is stored (nothing is saved).
        An empty api_key keeps the stored one; None removes a non-secret setting."""
        spec = {f.name: f for f in adapter_class(type_).SPEC}
        unknown = sorted(set(values) - set(spec))
        if unknown:
            raise ValueError("Unknown settings: " + ", ".join(unknown))
        cfg: dict[str, Any] = {}
        if pid is not None:
            found = self.runtime(pid)
            if found is None:
                raise ValueError("No such provider.")
            cfg = dict(found[1])
        for name, val in values.items():
            if spec[name].secret:
                if val not in (None, ""):
                    cfg[name] = str(val)
            elif val is None or val == "":
                cfg.pop(name, None)
            else:
                cfg[name] = val
        return cfg

    def _apply(self, s, p: LLMProvider, cfg: dict, values: dict, actor: str, pid_audit: str) -> None:
        for c in COLUMNS:
            if c in values:
                setattr(p, c, cfg.get(c) if c != "manual_models" else list(cfg.get(c) or []))
        if values.get("api_key"):
            token = self.secrets.encrypt(str(values["api_key"]))
            row = s.scalar(select(Secret).where(Secret.owner_type == OWNER, Secret.owner_id == str(p.id),
                                                Secret.name == "api_key"))
            if row is None:
                s.add(Secret(owner_type=OWNER, owner_id=str(p.id), name="api_key", ciphertext=token))
            else:
                row.ciphertext = token
            self._audit(s, actor, "secret.replace", pid_audit, "api_key replaced")

    def create(self, type_: str, name: str, values: dict, models: list[str], actor: str) -> int:
        adapter_class(type_)
        name = (name or "").strip()
        if not name:
            raise ValueError("Give the provider a name.")
        if type_ not in self.approved():
            raise ValueError(f"{adapter_class(type_).title} is not an approved provider. Approve it first.")
        cfg = self.merged(type_, values)
        with self.db.session() as s:
            if s.scalar(select(LLMProvider.id).where(LLMProvider.name == name)):
                raise ValueError(f"A provider named {name!r} already exists.")
            p = LLMProvider(name=name, type=type_, manual_models=[], available_models=list(models),
                            status="Connected", status_detail="")
            s.add(p)
            s.flush()
            self._apply(s, p, cfg, {**{k: cfg.get(k) for k in COLUMNS}, "api_key": cfg.get("api_key")}, actor,
                        f"{type_}:{name}")
            self._audit(s, actor, "provider.create", name, f"type {type_}")
            return p.id

    def update(self, pid: int, name: str | None, values: dict, models: list[str], actor: str) -> None:
        with self.db.session() as s:
            p = s.get(LLMProvider, pid)
            if p is None:
                raise ValueError("No such provider.")
            cfg = self.merged(p.type, values, pid)
            if name is not None and name.strip() and name.strip() != p.name:
                if s.scalar(select(LLMProvider.id).where(LLMProvider.name == name.strip(), LLMProvider.id != pid)):
                    raise ValueError(f"A provider named {name.strip()!r} already exists.")
                p.name = name.strip()
            apply_values = {k: cfg.get(k) for k in COLUMNS if k in values}
            if values.get("api_key"):
                apply_values["api_key"] = values["api_key"]
            self._apply(s, p, cfg, apply_values, actor, p.name)
            p.available_models, p.status, p.status_detail = list(models), "Connected", ""
            self._audit(s, actor, "provider.update", p.name)

    def set_status(self, pid: int, status: str, detail: str = "", models: list[str] | None = None,
                   actor: str | None = None) -> None:
        with self.db.session() as s:
            p = s.get(LLMProvider, pid)
            if p is None:
                return
            p.status, p.status_detail = status, detail[:300]
            if models is not None:
                p.available_models = list(models)
            if actor:
                self._audit(s, actor, "provider.test", p.name, f"{status}: {detail[:200]}" if detail else status)

    def delete(self, pid: int, actor: str) -> None:
        with self.db.session() as s:
            p = s.get(LLMProvider, pid)
            if p is None:
                raise ValueError("No such provider.")
            ms = s.get(ModelSettings, 1)
            if ms and pid in (ms.preferred_provider_id, ms.fallback_provider_id):
                raise ValueError("This provider is used by the model settings. Choose other models first.")
            s.execute(delete(Secret).where(Secret.owner_type == OWNER, Secret.owner_id == str(pid)))
            s.delete(p)
            self._audit(s, actor, "provider.delete", p.name)

    # ---- approved provider types (D15) ----------------------------------------------------
    def approved(self) -> list[str]:
        global_settings.current(self.db)                       # makes sure the settings row exists
        with self.db.session() as s:
            return list(s.get(GlobalSettings, 1).approved_providers or [])

    def set_approved(self, types: list[str], actor: str) -> list[str]:
        unknown = sorted(set(types) - set(REGISTRY))
        if unknown:
            raise ValueError("Unknown provider type: " + ", ".join(unknown))
        global_settings.current(self.db)
        with self.db.session() as s:
            in_use = {p.type for p in s.scalars(select(LLMProvider))}
            blocked = sorted(in_use - set(types))
            if blocked:
                names = ", ".join(adapter_class(t).title for t in blocked)
                raise ValueError(f"Cannot remove approval while providers of that type exist: {names}.")
            row = s.get(GlobalSettings, 1)
            before = set(row.approved_providers or [])
            row.approved_providers = sorted(set(types))
            self._audit(s, actor, "providers.approval", "global",
                        f"approved: {', '.join(sorted(types)) or 'none'} (was: {', '.join(sorted(before)) or 'none'})")
        return sorted(set(types))

    # ---- model settings -----------------------------------------------------------------
    def model_settings(self) -> dict | None:
        with self.db.session() as s:
            ms = s.get(ModelSettings, 1)
            if ms is None:
                return None
            return {"preferred": {"provider_id": ms.preferred_provider_id, "model": ms.preferred_model},
                    "fallback": ({"provider_id": ms.fallback_provider_id, "model": ms.fallback_model}
                                 if ms.fallback_provider_id else None),
                    "request_timeout": ms.request_timeout, "updated_at": ms.updated_at,
                    "updated_by": ms.updated_by}

    def _check_choice(self, s, label: str, choice: dict) -> LLMProvider:
        p = s.get(LLMProvider, choice.get("provider_id"))
        if p is None:
            raise ValueError(f"{label}: choose a provider.")
        if p.type not in self.approved():
            raise ValueError(f"{label}: {adapter_class(p.type).title} is not an approved provider.")
        model = (choice.get("model") or "").strip()
        known = set(p.available_models or []) | set(p.manual_models or [])
        if not model or model not in known:
            raise ValueError(f"{label}: choose one of the models listed for {p.name}.")
        return p

    def set_model_settings(self, preferred: dict, fallback: dict | None, timeout: int, actor: str) -> list[str]:
        """Validate and save. Returns warnings (for example 'same provider'); raises ValueError otherwise."""
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not MIN_TIMEOUT <= timeout <= MAX_TIMEOUT:
            raise ValueError(f"Request timeout must be a whole number between {MIN_TIMEOUT} and {MAX_TIMEOUT} seconds.")
        warnings = []
        with self.db.session() as s:
            p1 = self._check_choice(s, "Preferred model", preferred)
            p2 = None
            if fallback and fallback.get("provider_id"):
                p2 = self._check_choice(s, "Fallback model", fallback)
                if (p1.id, preferred["model"]) == (p2.id, fallback["model"]):
                    raise ValueError("The fallback must be a different model from the preferred one.")
                if p1.id == p2.id:
                    warnings.append("The fallback is on the same provider as the preferred model, so an outage, "
                                    "expired key or exhausted credit there would stop both. A different provider is safer.")
            ms = s.get(ModelSettings, 1) or ModelSettings(id=1)
            ms.preferred_provider_id, ms.preferred_model = p1.id, preferred["model"]
            ms.fallback_provider_id, ms.fallback_model = (p2.id, fallback["model"]) if p2 else (None, "")
            ms.request_timeout, ms.updated_by = timeout, actor
            s.merge(ms)
            self._audit(s, actor, "model_settings.update", "global",
                        f"preferred {p1.name}/{preferred['model']}; fallback "
                        + (f"{p2.name}/{fallback['model']}" if p2 else "none") + f"; timeout {timeout}s")
        return warnings
