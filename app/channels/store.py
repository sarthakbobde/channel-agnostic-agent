"""Persistence for channel settings. Secrets are encrypted; every change is audited.

Which settings are secret comes from each adapter's SPEC, so adding a platform needs no
storage code. Secret values are write-only: they are never returned except to the runtime
(`runtime_config`), and never written to the audit log.
"""
import logging
from typing import Callable

from sqlalchemy import select

from app.channels.base import Field
from app.db import AuditLog, Channel, Database, Meta, Secret
from app.secretstore import SecretStore

log = logging.getLogger(__name__)
OWNER = "channel"          # secrets.owner_type for channel credentials


class ChannelStore:
    def __init__(self, db: Database, secrets: SecretStore,
                 spec_lookup: Callable[[str], tuple[Field, ...]]):
        self.db = db
        self.secrets = secrets
        self._spec_lookup = spec_lookup

    # ---- helpers ----------------------------------------------------------
    def _spec(self, typ: str) -> dict[str, Field]:
        try:
            return {f.name: f for f in self._spec_lookup(typ)}
        except KeyError:
            raise ValueError(f"unknown channel type {typ!r}") from None

    @staticmethod
    def _audit(s, actor: str, action: str, target: str = "", detail: str = "") -> None:
        s.add(AuditLog(actor=actor, action=action, target=target, detail=detail))

    # ---- write ------------------------------------------------------------
    def save(self, typ: str, *, enabled: bool, values: dict, actor: str) -> None:
        """Create or update a channel.

        For a secret field, an empty or missing value keeps the stored one; a new value
        replaces it. For other fields, None removes the setting.
        """
        spec = self._spec(typ)
        unknown = sorted(set(values) - set(spec))
        if unknown:
            raise ValueError(f"unknown settings for {typ}: {', '.join(unknown)}")
        with self.db.session() as s:
            ch = s.scalar(select(Channel).where(Channel.type == typ))
            created = ch is None
            if created:
                ch = Channel(type=typ, config={})
                s.add(ch)
            cfg = dict(ch.config or {})
            changed = []
            for name, val in values.items():
                if spec[name].secret:
                    if val in (None, ""):
                        continue                                    # keep what is stored
                    row = s.scalar(select(Secret).where(Secret.owner_type == OWNER, Secret.owner_id == typ, Secret.name == name))
                    token = self.secrets.encrypt(str(val))
                    if row is None:
                        s.add(Secret(owner_type=OWNER, owner_id=typ, name=name, ciphertext=token))
                    else:
                        row.ciphertext = token
                    self._audit(s, actor, "secret.replace", typ, f"{name} replaced")
                else:
                    if val is None:
                        cfg.pop(name, None)
                    else:
                        cfg[name] = val
                    changed.append(name)
            was_enabled = None if created else ch.enabled
            ch.config, ch.enabled = cfg, bool(enabled)
            detail = ("created; " if created else "") + (f"settings: {', '.join(changed)}" if changed else "no setting changes")
            self._audit(s, actor, "channel.create" if created else "channel.update", typ, detail)
            if was_enabled is not None and was_enabled != bool(enabled):
                self._audit(s, actor, "channel.enable" if enabled else "channel.disable", typ)

    def delete(self, typ: str, *, actor: str) -> bool:
        with self.db.session() as s:
            ch = s.scalar(select(Channel).where(Channel.type == typ))
            if ch is None:
                return False
            for row in s.scalars(select(Secret).where(Secret.owner_type == OWNER, Secret.owner_id == typ)):
                s.delete(row)
            s.delete(ch)
            self._audit(s, actor, "channel.delete", typ)
            return True

    def record(self, actor: str, action: str, target: str = "", detail: str = "") -> None:
        with self.db.session() as s:
            self._audit(s, actor, action, target, detail)

    # ---- read -------------------------------------------------------------
    def runtime_config(self, typ: str) -> tuple[bool, dict] | None:
        """(enabled, full config including decrypted secrets) for the running adapter."""
        with self.db.session() as s:
            ch = s.scalar(select(Channel).where(Channel.type == typ))
            if ch is None:
                return None
            cfg = dict(ch.config or {})
            for row in s.scalars(select(Secret).where(Secret.owner_type == OWNER, Secret.owner_id == typ)):
                cfg[row.name] = self.secrets.decrypt(row.ciphertext)
            return ch.enabled, cfg

    def merged_config(self, typ: str, values: dict) -> dict:
        """What the full config would be after applying `values`, without saving anything.

        Same rules as save(): an empty secret keeps the stored one; None removes a setting.
        """
        spec = self._spec(typ)
        unknown = sorted(set(values) - set(spec))
        if unknown:
            raise ValueError(f"unknown settings for {typ}: {', '.join(unknown)}")
        found = self.runtime_config(typ)
        cfg = dict(found[1]) if found else {}
        for name, val in values.items():
            if spec[name].secret:
                if val not in (None, ""):
                    cfg[name] = str(val)
            elif val is None:
                cfg.pop(name, None)
            else:
                cfg[name] = val
        return cfg

    def types(self) -> list[str]:
        with self.db.session() as s:
            return list(s.scalars(select(Channel.type).order_by(Channel.type)))

    def public_view(self, typ: str) -> dict | None:
        """Safe for the admin UI: secret values are replaced by an is_set flag."""
        spec = self._spec(typ)
        with self.db.session() as s:
            ch = s.scalar(select(Channel).where(Channel.type == typ))
            if ch is None:
                return None
            set_secrets = set(s.scalars(select(Secret.name).where(Secret.owner_type == OWNER, Secret.owner_id == typ)))
            fields = []
            for f in spec.values():
                item = {"name": f.name, "label": f.label, "kind": f.kind, "secret": f.secret,
                        "required": f.required, "help": f.help}
                if f.secret:
                    item["is_set"] = f.name in set_secrets
                else:
                    item["value"] = (ch.config or {}).get(f.name, f.default)
                fields.append(item)
            return {"type": typ, "enabled": ch.enabled, "updated_at": ch.updated_at, "fields": fields}

    def recent_audit(self, limit: int = 100) -> list[dict]:
        with self.db.session() as s:
            rows = s.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))
            return [{"ts": r.ts, "actor": r.actor, "action": r.action,
                     "target": r.target, "detail": r.detail} for r in rows]

    def search_audit(self, *, actor: str = "", action: str = "", target: str = "", date_from=None, date_to=None,
                     limit: int = 50, offset: int = 0) -> dict:
        """Filtered, paged audit rows (newest first) plus the lists of people and actions to filter by."""
        from sqlalchemy import func
        q = select(AuditLog)
        if actor:
            q = q.where(func.lower(AuditLog.actor).like("%" + actor.lower().replace("%", "") + "%"))
        if action:
            q = q.where(AuditLog.action.like(action.replace("%", "") + "%"))
        if target:
            q = q.where(func.lower(AuditLog.target).like("%" + target.lower().replace("%", "") + "%"))
        if date_from is not None:
            q = q.where(AuditLog.ts >= date_from)
        if date_to is not None:
            q = q.where(AuditLog.ts < date_to)
        with self.db.session() as s:
            total = s.scalar(select(func.count()).select_from(q.subquery())) or 0
            rows = s.scalars(q.order_by(AuditLog.id.desc()).limit(limit).offset(offset))
            out = [{"id": r.id, "ts": r.ts, "actor": r.actor, "action": r.action, "target": r.target, "detail": r.detail} for r in rows]
            actors = [a for (a,) in s.execute(select(AuditLog.actor).distinct().order_by(AuditLog.actor)).all()]
            actions = [a for (a,) in s.execute(select(AuditLog.action).distinct().order_by(AuditLog.action)).all()]
        return {"total": total, "rows": out, "actors": actors, "actions": actions}

    # ---- one-time import --------------------------------------------------
    def import_config_once(self, cfg: dict, *, actor: str = "system") -> list[str]:
        """Seed the database from the legacy channels.yaml (+ environment) on first run.

        Runs once; afterwards the database is the source of truth, so an admin deleting a
        channel is not undone by the next restart.
        """
        with self.db.session() as s:
            if s.get(Meta, "config_imported") is not None:
                return []
        imported = []
        defaults = cfg.get("defaults", {})
        for typ, ccfg in (cfg.get("channels") or {}).items():
            try:
                spec = self._spec(typ)
            except ValueError:
                log.warning("skipping unknown channel %r in channels.yaml", typ)
                continue
            merged = {**defaults, **{k: v for k, v in ccfg.items() if k != "enabled"}}
            values = {k: v for k, v in merged.items() if k in spec and v not in (None, "", [])}
            self.save(typ, enabled=bool(ccfg.get("enabled")), values=values, actor=actor)
            imported.append(typ)
        with self.db.session() as s:
            s.add(Meta(key="config_imported", value="1"))
            self._audit(s, actor, "import", "", "Imported from channels.yaml and environment: "
                        + (", ".join(imported) or "nothing"))
        return imported
