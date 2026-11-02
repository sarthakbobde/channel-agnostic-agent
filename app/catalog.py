"""The message catalogue (D29): every system text people see, per language.

`t(key, lang)` gives the text in the person's language when that entry is Approved, else in the knowledge base's default
language, else in English. English is seeded from the texts below, which are byte-identical to the ones the code used
before. The English "blocked" text stays the Blocked Reply Text on the Guardrails screen, so there is one place to edit it.

Other languages are switched on in Global Settings (Message languages). An AI-drafted translation is a Draft and is never
used until an Admin approves it; an Admin's own edit is approved by saving it. A language can be accepted for questions
only when every one of its entries is approved (D39).
"""
import logging
import threading
import time

from sqlalchemy import select

from app import language
from app.db import Database, MessageCatalogEntry, get_db

log = logging.getLogger(__name__)
CACHE_SECONDS = 2.0
APPROVED, DRAFT = "Approved", "Draft"

# key -> (English text, what it is for). Placeholders in {braces} must be kept in every language.
TEXTS = {
    "not_found": ("I couldn't find this in the knowledge base.", "The answer when the documents do not answer the question."),
    "blocked": ("I can't help with that request. If you think this is a mistake, contact an administrator.",
                "A question or answer stopped by the guardrails. The English text is the Blocked Reply Text on the Guardrails screen."),
    "friendly_error": ("Sorry, I can't process that right now. Please try again in a moment.", "The guardrail layer itself failed."),
    "error": ("Sorry, something went wrong while answering. Please try again.", "A chat app question failed."),
    "slow_down": ("You are asking too quickly. Please wait a moment and try again.", "The chat apps' rate limit."),
    "slow_down_wait": ("You are asking too quickly. Please wait {seconds} seconds and try again.", "The Web UI's rate limit."),
    "denied": ("Your access to this assistant has been turned off. Please contact an administrator.", "A deactivated person asks."),
    "provider_refused": ("The AI service refused to process this message. Please rephrase your question and try again.",
                         "The AI provider's own content filter refused the question."),
    "gchat_timeout": ("That took too long. Please try again, or ask a shorter question.", "Google Chat's 25-second limit was reached."),
    "language_not_supported": ("Sorry, I can only answer questions in {languages}.",
                               "A question in a language that is not accepted (D39). {languages} lists the accepted ones."),
    "sources": ("Sources", "The label before the list of sources in the chat apps."),
    "web_session_ended": ("Your session has ended. Sign out (top right) and sign in again.", "Web UI: the sign-in expired."),
    "web_try_again": ("Please try again.", "Web UI: a request was refused without a reason."),
    "web_error": ("Something went wrong on our side (error {status}). Please try again.", "Web UI: the server failed."),
    "suggested": ("Suggested questions", "The heading above suggested follow-up questions."),
    "suggestion_invalid": ("This button is not valid.", "A suggestion button whose signature does not check out."),
    "suggestion_expired": ("This suggestion has expired. Type the question instead.", "A suggestion button older than its expiry."),
    "suggestion_not_yours": ("Only the person who asked can use these suggestions.", "Someone else clicked a suggestion button."),
}
KEYS = tuple(TEXTS)

# Hindi, written by Claude (not yet reviewed by a native speaker). Seeded as Drafts: never used until an Admin approves them.
HINDI = {
    "not_found": "मुझे यह जानकारी ज्ञानकोष में नहीं मिली।",
    "blocked": "मैं इस अनुरोध में मदद नहीं कर सकता। अगर आपको लगता है कि यह गलती है, तो किसी व्यवस्थापक से संपर्क करें।",
    "friendly_error": "क्षमा करें, मैं अभी इसे संसाधित नहीं कर सकता। कृपया थोड़ी देर बाद फिर से कोशिश करें।",
    "error": "क्षमा करें, उत्तर देते समय कुछ गड़बड़ हो गई। कृपया फिर से कोशिश करें।",
    "slow_down": "आप बहुत जल्दी-जल्दी पूछ रहे हैं। कृपया थोड़ा रुककर फिर से कोशिश करें।",
    "slow_down_wait": "आप बहुत जल्दी-जल्दी पूछ रहे हैं। कृपया {seconds} सेकंड रुककर फिर से कोशिश करें।",
    "denied": "इस सहायक तक आपकी पहुँच बंद कर दी गई है। कृपया किसी व्यवस्थापक से संपर्क करें।",
    "provider_refused": "AI सेवा ने इस संदेश को संसाधित करने से मना कर दिया। कृपया अपना प्रश्न दूसरे शब्दों में लिखकर फिर से कोशिश करें।",
    "gchat_timeout": "इसमें बहुत समय लग गया। कृपया फिर से कोशिश करें, या छोटा प्रश्न पूछें।",
    "language_not_supported": "क्षमा करें, मैं केवल इन भाषाओं में प्रश्नों के उत्तर दे सकता हूँ: {languages}।",
    "sources": "स्रोत",
    "web_session_ended": "आपका सत्र समाप्त हो गया है। साइन आउट करें (ऊपर दाईं ओर) और फिर से साइन इन करें।",
    "web_try_again": "कृपया फिर से कोशिश करें।",
    "web_error": "हमारी ओर से कुछ गड़बड़ हो गई (त्रुटि {status})। कृपया फिर से कोशिश करें।",
    "suggested": "सुझाए गए प्रश्न",
    "suggestion_invalid": "यह बटन मान्य नहीं है।",
    "suggestion_expired": "इस सुझाव की समय-सीमा समाप्त हो गई है। कृपया प्रश्न खुद लिखें।",
    "suggestion_not_yours": "इन सुझावों का उपयोग केवल वही व्यक्ति कर सकता है जिसने प्रश्न पूछा था।",
}
DRAFTS = {"hi": HINDI}

_cache: dict[int, tuple[float, dict]] = {}
_lock = threading.Lock()


def invalidate() -> None:
    with _lock:
        _cache.clear()


def _entries(db: Database | None = None) -> dict:
    """{(key, lang): (text, status)}. If the table cannot be read, English from the code is used."""
    try:
        db = db or get_db()
        now = time.monotonic()
        with _lock:
            hit = _cache.get(id(db))
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
        with db.session() as s:
            out = {(r.key, r.lang): (r.text, r.status) for r in s.scalars(select(MessageCatalogEntry))}
        with _lock:
            _cache[id(db)] = (now, out)
        return out
    except Exception:
        log.warning("message catalogue unavailable; using the English texts", exc_info=True)
        return {}


def _english(key: str, db: Database | None = None) -> str:
    if key == "blocked":
        from app import guardrails
        try:
            return guardrails.current(db).blocked_reply
        except Exception:
            return TEXTS[key][0]
    found = _entries(db).get((key, "en"))
    return found[0] if found and found[1] == APPROVED else TEXTS[key][0]


def _approved(key: str, lang: str, db: Database | None = None) -> str | None:
    if not lang:
        return None
    if lang == "en":
        return _english(key, db)
    found = _entries(db).get((key, lang))
    return found[0] if found and found[1] == APPROVED else None


def t(key: str, lang: str = "", fallback: str = "", db: Database | None = None, **values) -> str:
    """The text for `key` in `lang`, else in `fallback` (the knowledge base default), else in English."""
    text = _approved(key, lang, db) or _approved(key, fallback, db) or _english(key, db)
    for k, v in values.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def text_language(key: str, lang: str, fallback: str = "", db: Database | None = None) -> str:
    """Which language `t` would answer in."""
    return lang if _approved(key, lang, db) else fallback if _approved(key, fallback, db) else "en"


def complete(lang: str, db: Database | None = None) -> bool:
    """Every entry of `lang` is approved, so all its system texts are in that language."""
    if lang == "en":
        return True
    found = _entries(db)
    return all(found.get((k, lang), ("", DRAFT))[1] == APPROVED for k in KEYS)


def bundle(lang: str, fallback: str = "", db: Database | None = None) -> dict:
    """Every text for one language, with the fallbacks applied (the Web UI fetches this)."""
    return {k: t(k, lang, fallback, db) for k in KEYS}


# ---- the screen -----------------------------------------------------------------------------------------------------
def languages(db: Database | None = None) -> list[str]:
    from app import global_settings
    return ["en"] + [l for l in (global_settings.current(db).catalog_languages or []) if l != "en"]


def view(db: Database | None = None) -> dict:
    db = db or get_db()
    with db.session() as s:
        rows = {(r.key, r.lang): r for r in s.scalars(select(MessageCatalogEntry))}
        data = {k: {"text": r.text, "status": r.status, "version": r.version, "source": r.source, "updated_by": r.updated_by,
                    "updated_at": r.updated_at} for k, r in rows.items()}
    langs = languages(db)
    out = []
    for key, (english, purpose) in TEXTS.items():
        entries = {}
        for lang in langs:
            if lang == "en":
                entries["en"] = {**data.get((key, "en"), {"status": APPROVED, "source": "built-in"}), "text": _english(key, db),
                                 "linked": key == "blocked"}
            else:
                entries[lang] = data.get((key, lang)) or {"text": "", "status": "Missing"}
        out.append({"key": key, "purpose": purpose, "default": english, "entries": entries})
    return {"languages": [{"code": l, "name": language.name(l), "complete": complete(l, db)} for l in langs], "keys": out}


def _check(key: str, lang: str, text: str) -> str:
    if key not in TEXTS:
        raise ValueError("Unknown message.")
    if not language.valid(lang):
        raise ValueError("Unknown language.")
    text = (text or "").strip()
    if not text or len(text) > 1000:
        raise ValueError("The text must be between 1 and 1000 characters.")
    for ph in (p for p in ("{seconds}", "{languages}", "{status}") if p in TEXTS[key][0]):
        if ph not in text:
            raise ValueError(f"Keep {ph} in the text: it is replaced when the message is sent.")
    if key == "blocked" and lang == "en":
        raise ValueError("The English blocked text is the Blocked Reply Text on the Guardrails screen.")
    return text


def save(key: str, lang: str, text: str, actor: str, *, status: str = APPROVED, source: str = "person",
         db: Database | None = None) -> tuple[dict | None, dict]:
    """An Admin's edit is approved by saving it; an AI draft is saved as a Draft. Returns (before, after)."""
    db = db or get_db()
    text = _check(key, lang, text)
    with db.session() as s:
        row = s.scalar(select(MessageCatalogEntry).where(MessageCatalogEntry.key == key, MessageCatalogEntry.lang == lang))
        before = {"text": row.text, "status": row.status} if row else None
        if row is None:
            s.add(MessageCatalogEntry(key=key, lang=lang, text=text, status=status, version=1, source=source, updated_by=actor))
        else:
            row.text, row.status, row.source, row.updated_by = text, status, source, actor
            row.version += 1
    invalidate()
    return before, {"text": text, "status": status}


def approve(key: str, lang: str, actor: str, db: Database | None = None) -> bool:
    db = db or get_db()
    with db.session() as s:
        row = s.scalar(select(MessageCatalogEntry).where(MessageCatalogEntry.key == key, MessageCatalogEntry.lang == lang))
        if row is None or row.status == APPROVED:
            return False
        row.status, row.updated_by = APPROVED, actor
    invalidate()
    return True


DRAFT_PROMPT = """Translate this short message from a workplace assistant into {language}.
Keep any placeholder in curly braces, such as {{seconds}}, exactly as it is. Keep the tone polite and plain.
Reply with only the translation, nothing else."""


def draft_with_ai(lang: str, actor: str, db: Database | None = None) -> int:
    """Draft every Missing or Draft entry of `lang` with the preferred model. Approved entries are never overwritten.
    Returns how many were drafted."""
    from langchain_core.messages import HumanMessage, SystemMessage
    from app.llm import get_chat_model
    db = db or get_db()
    if not language.valid(lang) or lang == "en":
        raise ValueError("Choose a language other than English.")
    found = _entries(db)
    made = 0
    for key in KEYS:
        if found.get((key, lang), ("", DRAFT))[1] == APPROVED:
            continue
        english = t(key, "en", db=db)
        reply = get_chat_model().invoke([SystemMessage(content=DRAFT_PROMPT.format(language=language.name(lang))),
                                         HumanMessage(content=english)], purpose="catalog_draft").content.strip()
        try:
            save(key, lang, reply, actor, status=DRAFT, source="ai-draft", db=db)
            made += 1
        except ValueError:
            log.warning("the AI draft of %s/%s lost a placeholder; left for a person to write", key, lang)
    return made


def seed(db: Database | None = None) -> None:
    """First start: the English texts become Approved entries (built-in), and the built-in translations become Drafts.
    Idempotent: nothing an Admin has saved is ever replaced."""
    db = db or get_db()
    with db.session() as s:
        have = set(s.execute(select(MessageCatalogEntry.key, MessageCatalogEntry.lang)).all())
        for key, (text, _) in TEXTS.items():
            if (key, "en") not in have and key != "blocked":
                s.add(MessageCatalogEntry(key=key, lang="en", text=text, status=APPROVED, source="built-in",
                                          updated_by="system (built-in text)"))
        for lang, texts in DRAFTS.items():
            for key, text in texts.items():
                if (key, lang) not in have:
                    s.add(MessageCatalogEntry(key=key, lang=lang, text=text, status=DRAFT, source="built-in draft",
                                              updated_by="system (built-in draft)"))
    invalidate()


def approve_all(lang: str, actor: str, db: Database | None = None) -> int:
    """Approve every Draft of one language (after someone has read them). Returns how many."""
    db = db or get_db()
    with db.session() as s:
        rows = list(s.scalars(select(MessageCatalogEntry).where(MessageCatalogEntry.lang == lang, MessageCatalogEntry.status == DRAFT)))
        for r in rows:
            r.status, r.updated_by = APPROVED, actor
    invalidate()
    return len(rows)
