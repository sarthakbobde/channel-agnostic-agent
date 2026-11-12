"""Runtime-editable settings that used to be .env lines (top-k, score threshold, history, chunking).

One row in `global_settings`, created on first read from the .env values (so an upgrade keeps
behaving exactly as before). Readers call `current()`; a 2-second cache means an admin's change
reaches every reader within seconds, with no restart. If the database cannot be read, the .env
values are used and a warning is logged, so answering never stops because of this table.
"""
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass

from sqlalchemy.exc import IntegrityError

from app.config import settings as env
from app.db import Database, GlobalSettings, get_db

log = logging.getLogger(__name__)
CACHE_SECONDS = 2.0


@dataclass(frozen=True)
class Snapshot:
    top_k: int
    score_threshold: float
    recent_turns: int
    chunk_size: int
    chunk_overlap: int
    max_retention_days: int
    prompt_max_chars: int
    summarise_after_turns: int
    retention_job_enabled: bool
    export_enabled: bool
    chat_rate_per_minute: int
    retrieval_strategy: str
    hybrid_keyword_weight: float
    diversity: float
    accepted_languages: tuple = ()
    document_languages: tuple = ()
    default_language: str = "en"
    language_confidence: float = 0.5
    catalog_languages: tuple = ()
    summary_enabled: bool = False
    summary_max_input_tokens: int = 6000
    glossary_mode: str = "off"
    glossary_max_terms: int = 5
    glossary_max_aliases: int = 3
    suggestions_enabled: bool = False
    suggestions_requested: int = 3
    suggestions_shown: int = 2
    suggestions_dedup_threshold: float = 0.9
    suggestions_mode: str = "same_call"
    suggestions_max_searches: int = 3
    suggestions_payload_days: int = 14
    feedback_enabled: bool = False
    feedback_comment_max_chars: int = 1000
    feedback_payload_days: int = 14
    feedback_masking: str = "always"
    admin_agent_mode: str = "off"
    ticket_assignment: str = "department"
    ticket_related_window_days: int = 0
    ticket_retention_days: int = 0
    ticket_alert_channels: tuple = ("admin_screen",)
    ticket_alert_events: tuple = ("assigned", "approval", "queue_released")
    feedback_processing_mode: str = "off"
    feedback_intent_enabled: bool = True
    feedback_verify_enabled: bool = True
    feedback_replay_daily_cap: int = 50
    feedback_retry_limit: int = 3
    feedback_review_claim_minutes: int = 30
    feedback_review_age_days: int = 7
    feedback_agent_max_steps: int = 6
    feedback_agent_timeout_seconds: int = 60
    feedback_agent_token_ceiling: int = 6000
    admin_agent_interval_minutes: int = 5
    admin_agent_daily_fix_cap: int = 5
    admin_agent_timeout_minutes: int = 30
    admin_agent_setting_keys: tuple = ("retrieval_strategy", "hybrid_keyword_weight", "diversity", "top_k", "score_threshold", "glossary_mode")
    admin_agent_max_steps: int = 8
    admin_agent_token_ceiling: int = 8000
    admin_agent_run_seconds: int = 120


ADMIN_AGENT_KEYS = {"retrieval_strategy": "Retrieval strategy", "hybrid_keyword_weight": "Keyword weight", "diversity": "Diversity", "top_k": "Passages per question",
                    "score_threshold": "Score threshold", "glossary_mode": "Glossary mode"}
PROCESSING_MODES = {"off": "Off", "review_all": "Review all (every negative goes to a person)", "auto": "Automatic (a confirmed replay becomes a ticket)"}
AGENT_MODES = {"off": "Off", "shadow": "Shadow (diagnose and record, change nothing)", "live": "Live"}
ALERT_CHANNELS = {"admin_screen": "Admin screen", "email": "Email"}
ALERT_EVENTS = {"assigned": "A ticket is assigned to a person or group", "approval": "A proposal is waiting for approval",
                "evidence_added": "New evidence is added to an open ticket", "queue_released": "The agent's queue is released to people"}
LIST_CHOICES = {"ticket_alert_channels": ALERT_CHANNELS, "ticket_alert_events": ALERT_EVENTS, "admin_agent_setting_keys": ADMIN_AGENT_KEYS}
FEEDBACK_MASKING = {"always": "Always (recommended)", "follow_switch": "Only while personal-data masking is on"}
SUGGESTION_MODES = {"same_call": "In the same AI call as the answer", "second_call": "In a second AI call (for models that cannot follow the format)"}
GLOSSARY_MODES = {"off": "Off", "query": "Expand questions (recommended)", "index": "Also add stable terms to passages when indexing"}
LIST_FIELDS = ("accepted_languages", "document_languages", "catalog_languages", "ticket_alert_channels", "ticket_alert_events", "admin_agent_setting_keys")

STRATEGIES = {
    "similarity": "Similarity (meaning)",
    "keyword": "Keyword (exact words)",
    "hybrid": "Hybrid (meaning + keywords)",
    "mmr": "Similarity with variety",
}


# What the admin screen shows and what is accepted. `integer` fields reject fractions.
SPEC = (
    {"name": "retrieval_strategy", "label": "How passages are found", "kind": "choice", "choices": list(STRATEGIES), "choice_labels": STRATEGIES,
     "help": "Similarity finds passages with the same meaning (best for ordinary questions). Keyword finds the exact words (best for codes, names and numbers). Hybrid does both and merges the results. Similarity with variety avoids sending near-identical passages. Default similarity."},
    {"name": "hybrid_keyword_weight", "show_when": {"field": "retrieval_strategy", "values": ["hybrid"]}, "label": "Hybrid: weight of keywords", "min": 0.0, "max": 1.0, "step": 0.05, "integer": False,
     "help": "Only used by Hybrid. From 0 to 1: 0 follows meaning only, 1 follows keywords only, 0.5 weighs them equally. Default 0.5."},
    {"name": "diversity", "show_when": {"field": "retrieval_strategy", "values": ["mmr"]}, "label": "Variety between passages", "min": 0.0, "max": 1.0, "step": 0.05, "integer": False,
     "help": "Only used by Similarity with variety. From 0 to 1: higher skips passages that repeat each other. Default 0.3."},
    {"name": "top_k", "label": "Passages used per question", "min": 1, "max": 50, "step": 1, "integer": True,
     "help": "How many of the best-matching passages from your documents are given to the AI model with each question. More gives it more to work with but costs more. Default 5."},
    {"name": "score_threshold", "label": "How closely a passage must match", "min": 0.0, "max": 1.0, "step": 0.01, "integer": False,
     "help": "From 0 to 1. Passages that match less well than this are ignored. If nothing matches well enough, the assistant says it could not find the answer. Default 0.35."},
    {"name": "recent_turns", "label": "Recent question-and-answer pairs the AI model remembers", "min": 1, "max": 50, "step": 1, "integer": True,
     "help": "How many of the latest question-and-answer pairs the AI model sees with each new question. Default 6."},
    {"name": "chunk_size", "label": "Passage size (characters)", "min": 100, "max": 8000, "step": 50, "integer": True,
     "help": "How long each piece of a document is when it is split for searching. Applies the next time documents are indexed. Default 900."},
    {"name": "chunk_overlap", "label": "Overlap between passages (characters)", "min": 0, "max": 7999, "step": 10, "integer": True,
     "help": "How much neighbouring passages share, so a sentence at the edge is not lost. Must be smaller than the passage size. Default 150."},
    {"name": "max_retention_days", "label": "Longest time conversations may be kept (days)", "min": 1, "max": 3650, "step": 1, "integer": True,
     "help": "No department can keep conversations longer than this. Each department chooses its own period, up to this. Default 365."},
    {"name": "prompt_max_chars", "label": "Longest department prompt (characters)", "min": 200, "max": 20000, "step": 100, "integer": True,
     "help": "A department prompt longer than this is rejected when it is saved. Default 4000."},
    {"name": "summarise_after_turns", "label": "Summarise a long chat after (question-answer pairs)", "min": 4, "max": 100, "step": 1, "integer": True,
     "help": "When this many pairs have not been summarised yet, the older ones are summarised in the background so the AI model keeps the gist. The most recent messages (see above) are always kept word for word. Default 12."},
    {"name": "chat_rate_per_minute", "label": "Questions per person per minute", "min": 0, "max": 600, "step": 1, "integer": True,
     "help": "Slows down anyone who asks more than this many questions in a minute, in the Web UI, the API and the chat apps. 0 means no limit. Default 20."},
    {"name": "retention_job_enabled", "label": "Delete expired conversations every day", "kind": "bool",
     "help": "A daily job deletes conversations that are older than their department's keep-for period. Every run is recorded in the audit log."},
    {"name": "export_enabled", "label": "Allow exporting conversations", "kind": "bool",
     "help": "Off by default. Turn it on only after the privacy sign-off. An export needs a written reason, only Admins can do it, and every export is recorded in the audit log."},
    {"name": "accepted_languages", "label": "Languages questions may be asked in", "kind": "list",
     "help": "Two-letter codes, comma-separated (for example en, hi). Empty accepts every language. A question in another language gets a polite reply that names these, and nothing is searched. A language can be added only when all its system messages are approved (Message catalogue)."},
    {"name": "default_language", "label": "Default language", "kind": "text",
     "help": "Two-letter code. Used only when the language of a question cannot be told and the conversation has no earlier language. Default en."},
    {"name": "document_languages", "label": "Languages of the documents", "kind": "list",
     "help": "Two-letter codes, comma-separated. Empty means detected from the documents. A question in a language the documents are not written in is searched by meaning only, not by keywords."},
    {"name": "language_confidence", "label": "How sure language detection must be", "min": 0.05, "max": 1.0, "step": 0.05, "integer": False,
     "help": "From 0 to 1. Below this, the question is answered in the conversation's last language, or else the default language. Default 0.5."},
    {"name": "catalog_languages", "label": "Message languages", "kind": "list",
     "help": "Two-letter codes, comma-separated, of the languages that get their own system messages besides English. Add one here, then write or draft its messages in the Message catalogue."},
    {"name": "summary_enabled", "label": "Write a summary of each document", "kind": "bool",
     "help": "Off by default. When a document is indexed, the preferred AI model writes a 2 to 3 sentence summary in the document's language, shown in the documents table. Whole document text is sent to that model. Display only: never used to answer."},
    {"name": "glossary_mode", "label": "Glossary", "kind": "choice", "choices": list(GLOSSARY_MODES), "choice_labels": GLOSSARY_MODES,
     "help": "Off by default. Expand questions: a question that uses a term or one of its aliases is also searched with the other forms (only the search changes, never the question or the answer). Also add stable terms: terms marked stable are added to passages when they are indexed, which needs a re-index after each change."},
    {"name": "glossary_max_terms", "label": "Glossary: most terms applied per question", "min": 1, "max": 20, "step": 1, "integer": True,
     "help": "How many glossary terms one question can be expanded with. Default 5."},
    {"name": "glossary_max_aliases", "label": "Glossary: most forms added per term", "min": 1, "max": 10, "step": 1, "integer": True,
     "help": "How many other forms of one term are added. Default 3."},
    {"name": "suggestions_enabled", "label": "Suggest follow-up questions", "kind": "bool",
     "help": "Off by default. After an answer from the documents, up to two follow-up questions are shown as buttons, each checked to be new, answerable for the person asking and within the guardrails. Switch it on per channel and per knowledge base in Scoped settings: both must be on."},
    {"name": "suggestions_requested", "label": "Suggestions: how many the AI model writes", "min": 1, "max": 5, "step": 1, "integer": True,
     "help": "Candidates asked for with each answer. Default 3."},
    {"name": "suggestions_shown", "label": "Suggestions: most shown", "min": 1, "max": 3, "step": 1, "integer": True,
     "help": "The best-supported candidates that pass every check are shown, up to this many. Default 2."},
    {"name": "suggestions_dedup_threshold", "label": "Suggestions: how alike counts as a repeat", "min": 0.5, "max": 1.0, "step": 0.01, "integer": False,
     "help": "From 0 to 1. A candidate at least this similar in meaning to an earlier question or suggestion is dropped. Default 0.9."},
    {"name": "suggestions_mode", "label": "Suggestions: how they are written", "kind": "choice", "choices": list(SUGGESTION_MODES), "choice_labels": SUGGESTION_MODES,
     "help": "The same call adds a short block to the answer (cheaper). A second call asks for them separately, for models that do not follow the block format."},
    {"name": "suggestions_max_searches", "label": "Suggestions: most checking searches per answer", "min": 1, "max": 10, "step": 1, "integer": True,
     "help": "Each candidate is checked with one search under the asker's permissions. Candidates beyond this are not checked or shown. Default 3."},
    {"name": "suggestions_payload_days", "label": "Suggestions: buttons work for (days)", "min": 1, "max": 90, "step": 1, "integer": True,
     "help": "After this, a suggestion in an old conversation shows as plain text instead of a button. Default 14."},
    {"name": "summary_max_input_tokens", "label": "Longest text read for a summary (tokens)", "min": 500, "max": 50000, "step": 500, "integer": True,
     "help": "A longer document is summarised section by section, and only this much of it is read. A token is about four characters. Default 6000."},
    {"name": "feedback_enabled", "label": "Let people rate answers", "kind": "bool",
     "help": "Off by default. Adds rating buttons under each answer in the chat apps and the Web UI. A rating can carry a reason and a comment; comments are masked before they are stored. Switch it on or off per channel, knowledge base or department in Scoped settings."},
    {"name": "feedback_comment_max_chars", "label": "Feedback: longest comment (characters)", "min": 50, "max": 4000, "step": 50, "integer": True,
     "help": "A longer comment is cut to this length before it is masked and stored. Default 1000."},
    {"name": "feedback_payload_days", "label": "Feedback: rating buttons work for (days)", "min": 1, "max": 90, "step": 1, "integer": True,
     "help": "After this, the rating buttons of an old answer stop working. Default 14."},
    {"name": "feedback_masking", "label": "Feedback: when comments are masked", "kind": "choice", "choices": list(FEEDBACK_MASKING), "choice_labels": FEEDBACK_MASKING,
     "help": "Comments, and later the summaries and ticket evidence, are read by more people and kept longer than conversations, so they are masked even when question masking is off. Choose the second option only if comments must follow the question-masking switch. Default always."},
    {"name": "admin_agent_mode", "label": "Autonomous admin agent", "kind": "choice", "choices": list(AGENT_MODES), "choice_labels": AGENT_MODES,
     "help": "Off by default. Live lets tickets go to the agent's queue (the agent itself is added in a later phase). While it is off, a ticket that a rule would send to the agent goes to the person or group instead. Switching from Live to Off releases every ticket still waiting in the agent's queue to people."},
    {"name": "ticket_assignment", "label": "Who receives new tickets", "kind": "text",
     "help": "department (the default: the department's Editors and Admins see it and take it), autonomous (the agent's queue, only while the agent is Live), user:<id> or group:<id>. Set it per knowledge base or department under Scoped settings."},
    {"name": "ticket_related_window_days", "label": "Related tickets: look back (days)", "min": 0, "max": 3650, "step": 1, "integer": True,
     "help": "A new ticket is linked to an earlier closed ticket for the same person, knowledge base and document if that one was closed within this many days. 0 means no limit. Default 0."},
    {"name": "ticket_retention_days", "label": "Tickets: keep closed tickets for (days)", "min": 0, "max": 3650, "step": 1, "integer": True,
     "help": "Closed tickets older than this are retired together with their events (hidden from screens and reports); open tickets never are. 0 keeps them for ever. Default 0."},
    {"name": "ticket_alert_channels", "label": "Ticket alerts: where they go", "kind": "list",
     "help": "Comma-separated: admin_screen, email. An alert holds the ticket number and a link, never the evidence. Default admin_screen."},
    {"name": "feedback_processing_mode", "label": "Feedback processing", "kind": "choice", "choices": list(PROCESSING_MODES), "choice_labels": PROCESSING_MODES,
     "help": "Off by default. Review all: the agents read each negative rating and replay the question, then a person decides every one. Automatic: a replay that confirms the problem becomes a ticket with no person involved; everything doubtful still goes to a person. Start with Review all, compare the agents with your reviewers, then switch. Can be set per knowledge base or department under Scoped settings."},
    {"name": "feedback_intent_enabled", "label": "Feedback: intent agent", "kind": "bool",
     "help": "A switch for this agent alone. While it is off, every negative feedback goes to a person. Default on, but nothing runs while Feedback processing is Off."},
    {"name": "feedback_verify_enabled", "label": "Feedback: verification agent (replay)", "kind": "bool",
     "help": "A switch for this agent alone. While it is off, nothing is replayed and every negative feedback goes to a person. Default on, but nothing runs while Feedback processing is Off."},
    {"name": "feedback_replay_daily_cap", "label": "Feedback: replays per day", "min": 0, "max": 100000, "step": 10, "integer": True,
     "help": "A replay asks the model again, so it costs money. After this many in a day (UTC), the rest go to a person. 0 means no replays. Default 50."},
    {"name": "feedback_retry_limit", "label": "Feedback: tries before a person is asked", "min": 1, "max": 10, "step": 1, "integer": True,
     "help": "A step that keeps failing is tried again with a wait; after this many tries the item goes to a person, with the reason. Default 3."},
    {"name": "feedback_review_claim_minutes", "label": "Review: how long a reviewer holds an item (minutes)", "min": 1, "max": 1440, "step": 5, "integer": True,
     "help": "A reviewer who opens an item holds it for this long, so two people do not decide the same one. Default 30."},
    {"name": "feedback_review_age_days", "label": "Review: mark items older than (days)", "min": 1, "max": 365, "step": 1, "integer": True,
     "help": "An item waiting for a person this long is marked as old in the queue. Default 7."},
    {"name": "feedback_agent_max_steps", "label": "Agents: most steps in one run", "min": 1, "max": 20, "step": 1, "integer": True,
     "help": "A run that needs more steps than this stops and the item goes to a person. Default 6."},
    {"name": "feedback_agent_timeout_seconds", "label": "Agents: longest run (seconds)", "min": 5, "max": 600, "step": 5, "integer": True,
     "help": "A run that takes longer stops and the item goes to a person. Default 60."},
    {"name": "feedback_agent_token_ceiling", "label": "Agents: most tokens in one run", "min": 500, "max": 100000, "step": 500, "integer": True,
     "help": "A run that would read more than this stops and the item goes to a person. Default 6000."},
    {"name": "admin_agent_interval_minutes", "label": "Admin agent: how often it looks for tickets (minutes)", "min": 1, "max": 1440, "step": 1, "integer": True,
     "help": "How often the agent checks its queue. Default 5."},
    {"name": "admin_agent_daily_fix_cap", "label": "Admin agent: fixes per knowledge base per day", "min": 0, "max": 1000, "step": 1, "integer": True,
     "help": "After this many fixes (re-index, glossary term) on one knowledge base in a day (UTC), the agent hands the rest to a person. 0 means no fixes. Default 5."},
    {"name": "admin_agent_timeout_minutes", "label": "Admin agent: hand a stuck ticket to a person after (minutes)", "min": 1, "max": 1440, "step": 1, "integer": True,
     "help": "A ticket the agent has held this long without finishing (a crashed run) is handed to a person. Default 30."},
    {"name": "admin_agent_setting_keys", "label": "Admin agent: settings it may propose changing", "kind": "list",
     "help": "Comma-separated knowledge-base setting names. The agent only PROPOSES a change; a person approves it. A key not on this list is refused. Default: retrieval_strategy, hybrid_keyword_weight, diversity, top_k, score_threshold, glossary_mode."},
    {"name": "admin_agent_max_steps", "label": "Admin agent: most steps in one run", "min": 1, "max": 30, "step": 1, "integer": True,
     "help": "A run that needs more steps stops and the ticket goes to a person. Default 8."},
    {"name": "admin_agent_token_ceiling", "label": "Admin agent: most tokens in one run", "min": 1000, "max": 200000, "step": 1000, "integer": True,
     "help": "A run that would read more than this stops and the ticket goes to a person. Default 8000."},
    {"name": "admin_agent_run_seconds", "label": "Admin agent: longest run (seconds)", "min": 10, "max": 900, "step": 10, "integer": True,
     "help": "A run that takes longer stops and the ticket goes to a person. Default 120."},
    {"name": "ticket_alert_events", "label": "Ticket alerts: which events", "kind": "list",
     "help": "Comma-separated: assigned, approval, evidence_added, queue_released. Default assigned, approval, queue_released."},
)
_BY_NAME = {f["name"]: f for f in SPEC}

_cache: dict[int, tuple[float, Snapshot]] = {}
_lock = threading.Lock()
_warned_at = 0.0


def defaults() -> Snapshot:
    """The .env values (also the first-run seed)."""
    return Snapshot(top_k=env.top_k, score_threshold=env.score_threshold, recent_turns=env.memory_max_turns,
                    chunk_size=env.chunk_size, chunk_overlap=env.chunk_overlap,
                    max_retention_days=env.max_retention_days, prompt_max_chars=env.prompt_max_chars,
                    summarise_after_turns=env.summarise_after_turns, retention_job_enabled=True, export_enabled=False,
                    chat_rate_per_minute=20, retrieval_strategy="similarity", hybrid_keyword_weight=0.5, diversity=0.3,
                    accepted_languages=(), document_languages=(), default_language="en", language_confidence=0.5, catalog_languages=(),
                    summary_enabled=False, summary_max_input_tokens=6000, glossary_mode="off", glossary_max_terms=5, glossary_max_aliases=3,
                    suggestions_enabled=False, suggestions_requested=3, suggestions_shown=2, suggestions_dedup_threshold=0.9,
                    suggestions_mode="same_call", suggestions_max_searches=3, suggestions_payload_days=14,
                    feedback_enabled=False, feedback_comment_max_chars=1000, feedback_payload_days=14, feedback_masking="always",
                    admin_agent_mode="off", ticket_assignment="department", ticket_related_window_days=0, ticket_retention_days=0,
                    ticket_alert_channels=("admin_screen",), ticket_alert_events=("assigned", "approval", "queue_released"),
                    feedback_processing_mode="off", feedback_intent_enabled=True, feedback_verify_enabled=True, feedback_replay_daily_cap=50,
                    feedback_retry_limit=3, feedback_review_claim_minutes=30, feedback_review_age_days=7, feedback_agent_max_steps=6,
                    feedback_agent_timeout_seconds=60, feedback_agent_token_ceiling=6000,
                    admin_agent_interval_minutes=5, admin_agent_daily_fix_cap=5, admin_agent_timeout_minutes=30,
                    admin_agent_setting_keys=("retrieval_strategy", "hybrid_keyword_weight", "diversity", "top_k", "score_threshold", "glossary_mode"),
                    admin_agent_max_steps=8, admin_agent_token_ceiling=8000, admin_agent_run_seconds=120)


def _snapshot(row: GlobalSettings) -> Snapshot:
    return Snapshot(row.top_k, row.score_threshold, row.recent_turns, row.chunk_size, row.chunk_overlap,
                    row.max_retention_days, row.prompt_max_chars, row.summarise_after_turns, row.retention_job_enabled,
                    row.export_enabled, row.chat_rate_per_minute, row.retrieval_strategy, row.hybrid_keyword_weight, row.diversity,
                    tuple(row.accepted_languages or ()), tuple(row.document_languages or ()), row.default_language or "en",
                    row.language_confidence if row.language_confidence is not None else 0.5, tuple(row.catalog_languages or ()),
                    bool(row.summary_enabled), row.summary_max_input_tokens or 6000,
                    row.glossary_mode or "off", row.glossary_max_terms or 5, row.glossary_max_aliases or 3,
                    bool(row.suggestions_enabled), row.suggestions_requested or 3, row.suggestions_shown or 2,
                    row.suggestions_dedup_threshold or 0.9, row.suggestions_mode or "same_call", row.suggestions_max_searches or 3,
                    row.suggestions_payload_days or 14,
                    bool(row.feedback_enabled), row.feedback_comment_max_chars or 1000, row.feedback_payload_days or 14,
                    row.feedback_masking or "always", row.admin_agent_mode or "off", row.ticket_assignment or "department",
                    row.ticket_related_window_days or 0, row.ticket_retention_days or 0,
                    tuple(row.ticket_alert_channels or ("admin_screen",)), tuple(row.ticket_alert_events if row.ticket_alert_events is not None else ("assigned", "approval", "queue_released")),
                    row.feedback_processing_mode or "off", row.feedback_intent_enabled is not False, row.feedback_verify_enabled is not False,
                    50 if row.feedback_replay_daily_cap is None else row.feedback_replay_daily_cap, row.feedback_retry_limit or 3,
                    row.feedback_review_claim_minutes or 30, row.feedback_review_age_days or 7, row.feedback_agent_max_steps or 6,
                    row.feedback_agent_timeout_seconds or 60, row.feedback_agent_token_ceiling or 6000,
                    row.admin_agent_interval_minutes or 5, 5 if row.admin_agent_daily_fix_cap is None else row.admin_agent_daily_fix_cap,
                    row.admin_agent_timeout_minutes or 30,
                    tuple(row.admin_agent_setting_keys if row.admin_agent_setting_keys is not None else ("retrieval_strategy", "hybrid_keyword_weight", "diversity", "top_k", "score_threshold", "glossary_mode")),
                    row.admin_agent_max_steps or 8, row.admin_agent_token_ceiling or 8000, row.admin_agent_run_seconds or 120)


def _read(db: Database) -> tuple[Snapshot, GlobalSettings]:
    for _ in range(2):
        with db.session() as s:
            row = s.get(GlobalSettings, 1)
            if row is not None:
                return _snapshot(row), row
            try:
                s.add(GlobalSettings(id=1, updated_by="system (seeded from .env)", **_columns(asdict(defaults()))))
                s.flush()
            except IntegrityError:           # another process seeded it first
                s.rollback()
    raise RuntimeError("could not read or seed global settings")


def _columns(values: dict) -> dict:
    """Snapshot values as stored: the language lists are JSON lists."""
    return {k: list(v) if k in LIST_FIELDS else v for k, v in values.items()}


def _languages(f: dict, raw, check_catalog: bool = True) -> list[str]:
    from app import language
    if not isinstance(raw, (list, tuple)) or not all(isinstance(x, str) for x in raw):
        raise ValueError(f"{f['label']} must be a list of two-letter language codes.")
    codes = list(dict.fromkeys(x.strip().lower() for x in raw if x.strip()))
    bad = [c for c in codes if not language.valid(c)]
    if bad:
        raise ValueError(f"{f['label']}: not a language code: " + ", ".join(bad) + ". Use two-letter codes such as en or hi.")
    if f["name"] == "accepted_languages" and check_catalog:
        from app import catalog
        missing = [c for c in codes if not catalog.complete(c)]
        if missing:
            raise ValueError("These languages cannot be accepted yet, because not all their system messages are approved "
                             "(Message catalogue): " + ", ".join(missing) + ".")
    return codes


def _choices(f: dict, raw, allowed: dict) -> list[str]:
    if not isinstance(raw, (list, tuple)) or not all(isinstance(x, str) for x in raw):
        raise ValueError(f"{f['label']} must be a list.")
    items = list(dict.fromkeys(x.strip().lower() for x in raw if x.strip()))
    bad = [c for c in items if c not in allowed]
    if bad:
        raise ValueError(f"{f['label']}: not an option: " + ", ".join(bad) + ". Choose from: " + ", ".join(allowed) + ".")
    return items


def invalidate() -> None:
    with _lock:
        _cache.clear()


def current(db: Database | None = None) -> Snapshot:
    global _warned_at
    try:
        db = db or get_db()
        now = time.monotonic()
        with _lock:
            hit = _cache.get(id(db))
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
        snap, _ = _read(db)
        with _lock:
            _cache[id(db)] = (now, snap)
        return snap
    except Exception:
        if time.monotonic() - _warned_at > 60:           # do not flood the log on every question
            _warned_at = time.monotonic()
            log.warning("global settings unavailable; using .env defaults", exc_info=True)
        return defaults()


def validate(values: dict, base: Snapshot, check_catalog: bool = True) -> dict:
    """Clean and check submitted values; returns only the fields to change. Raises ValueError
    with a message an admin can act on."""
    unknown = sorted(set(values) - set(_BY_NAME))
    if unknown:
        raise ValueError("Unknown setting: " + ", ".join(unknown))
    clean = {}
    for name, raw in values.items():
        f = _BY_NAME[name]
        if f.get("kind") == "choice":
            if raw not in f["choices"]:
                raise ValueError(f"{f['label']} must be one of: " + ", ".join(f["choices"]) + ".")
            clean[name] = raw
            continue
        if f.get("kind") == "bool":
            if not isinstance(raw, bool):
                raise ValueError(f"{f['label']} must be on or off.")
            clean[name] = raw
            continue
        if f.get("kind") == "list":
            clean[name] = tuple(_choices(f, raw, LIST_CHOICES[name]) if name in LIST_CHOICES else _languages(f, raw, check_catalog))
            continue
        if name == "ticket_assignment":
            value = raw.strip().lower() if isinstance(raw, str) else raw
            if not (value in ("department", "autonomous") or (isinstance(value, str) and re.fullmatch(r"(user|group):[1-9][0-9]{0,9}", value))):
                raise ValueError(f"{f['label']} must be department, autonomous, user:<number> or group:<number>.")
            clean[name] = value
            continue
        if f.get("kind") == "text":
            from app import language
            code = raw.strip().lower() if isinstance(raw, str) else raw
            if not language.valid(code):
                raise ValueError(f"{f['label']} must be a two-letter language code such as en.")
            clean[name] = code
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"{f['label']} must be a number.")
        if f["integer"]:
            if raw != int(raw):
                raise ValueError(f"{f['label']} must be a whole number.")
            raw = int(raw)
        else:
            raw = float(raw)
        if not f["min"] <= raw <= f["max"]:
            raise ValueError(f"{f['label']} must be between {f['min']} and {f['max']}.")
        clean[name] = raw
    merged = {**asdict(base), **clean}
    if merged["chunk_overlap"] >= merged["chunk_size"]:
        raise ValueError("Overlap must be smaller than the passage size.")
    return clean


def view(db: Database | None = None) -> dict:
    """The settings screen: field definitions with current values."""
    db = db or get_db()
    snap, row = _read(db)
    values = asdict(snap)
    fields = [{"name": f["name"], "label": f["label"], "kind": "choice", "help": f["help"], "value": values[f["name"]],
               "choices": f["choices"], "choice_labels": f["choice_labels"]}
              if f.get("kind") == "choice" else
              {"name": f["name"], "label": f["label"], "kind": f["kind"], "help": f["help"],
               "value": list(values[f["name"]]) if f["kind"] == "list" else values[f["name"]]}
              if f.get("kind") in ("bool", "list", "text") else
              {"name": f["name"], "label": f["label"], "kind": "number", "help": f["help"],
               "min": f["min"], "max": f["max"], "step": f["step"], "value": values[f["name"]], **({"show_when": f["show_when"]} if f.get("show_when") else {})}
              for f in SPEC]
    return {"fields": fields, "updated_at": row.updated_at, "updated_by": row.updated_by}


def update(values: dict, actor: str, db: Database | None = None) -> tuple[Snapshot, Snapshot]:
    """Apply changes. Returns (before, after). Raises ValueError for invalid input."""
    db = db or get_db()
    before, _ = _read(db)
    clean = validate(values, before)
    if clean:
        with db.session() as s:
            row = s.get(GlobalSettings, 1)
            for k, v in _columns(clean).items():
                setattr(row, k, v)
            row.updated_by = actor
        invalidate()
    after, _ = _read(db)
    return before, after
