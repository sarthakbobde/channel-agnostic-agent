"""Languages (D19, D21): clean-up of incoming text, detection behind an adapter, and the language a turn is answered in.

Normalization is clean-up only (NFKC, whitespace, zero-width characters): nothing is ever translated. Detection runs locally
(fastText's lid.176 model, about 1 MB, chosen in the Phase 2 spike) on masked text. When it is unsure, the turn falls back to
the conversation's last language, then the knowledge base's default language. If the model file is missing, every
detection is "unsure", so the fallback decides and answering never stops because of it.
"""
import contextlib
import contextvars
import logging
import re
import threading
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# ISO 639-1 codes the screens offer, with their English and own names. fastText knows more; any two-letter code it returns works.
NAMES = {
    "en": ("English", "English"), "hi": ("Hindi", "हिन्दी"), "mr": ("Marathi", "मराठी"), "bn": ("Bengali", "বাংলা"),
    "ta": ("Tamil", "தமிழ்"), "te": ("Telugu", "తెలుగు"), "kn": ("Kannada", "ಕನ್ನಡ"), "ml": ("Malayalam", "മലയാളം"),
    "gu": ("Gujarati", "ગુજરાતી"), "pa": ("Punjabi", "ਪੰਜਾਬੀ"), "or": ("Odia", "ଓଡ଼ିଆ"), "ur": ("Urdu", "اردو"),
    "ar": ("Arabic", "العربية"), "fr": ("French", "Français"), "de": ("German", "Deutsch"), "es": ("Spanish", "Español"),
    "pt": ("Portuguese", "Português"), "it": ("Italian", "Italiano"), "nl": ("Dutch", "Nederlands"), "ru": ("Russian", "Русский"),
    "ja": ("Japanese", "日本語"), "zh": ("Chinese", "中文"), "ko": ("Korean", "한국어"), "id": ("Indonesian", "Bahasa Indonesia"),
    "ms": ("Malay", "Bahasa Melayu"), "th": ("Thai", "ไทย"), "vi": ("Vietnamese", "Tiếng Việt"), "tr": ("Turkish", "Türkçe"),
    "pl": ("Polish", "Polski"), "sw": ("Swahili", "Kiswahili"),
}
CODE = re.compile(r"^[a-z]{2,3}$")
# A question is turned away for its language (D39) only when detection is at least this sure. Below it, an unaccepted
# language is treated as unsure and falls back (D19): Hindi and Marathi share a script and short questions get mixed up.
REJECT_CONFIDENCE = 0.8
MODEL_FILE = "lid.176.ftz"
MODEL_SHA256 = "8f3472cfe8738a7b6099e8e999c3cbfae0dcd15696aac7d7738a8039db603e83"
MODEL_URL = "https://dl.fbaipublicfiles.com/fasttext/supervised-models/lid.176.ftz"
_ZERO_WIDTH = dict.fromkeys(map(ord, "​⁠﻿­"), None)   # ZWJ/ZWNJ are kept: Indic scripts need them
_PLACEHOLDER = re.compile(r"<[A-Z_]{2,}>")                                 # masking labels such as <EMAIL_ADDRESS>
_CITATION = re.compile(r"\[\d+(?:\s*,\s*\d+)*\]|\[\[[A-Z_]+\]\]")


def name(code: str) -> str:
    return NAMES.get(code, (code.upper() if code else "", ""))[0]


def native(code: str) -> str:
    """The language's name in itself, for a reply that lists languages to someone who may not read the reply's language."""
    return NAMES.get(code, ("", code.upper()))[1]


def valid(code) -> bool:
    return isinstance(code, str) and bool(CODE.match(code))


def normalize(text: str) -> str:
    """NFKC, zero-width characters removed, whitespace collapsed (newlines kept). Case is never changed."""
    text = unicodedata.normalize("NFKC", text or "").translate(_ZERO_WIDTH)
    lines = [" ".join(line.split()) for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# ---- the detector -------------------------------------------------------------------------------------------------------
class Detector:
    """Adapter: detect(text) -> (ISO 639-1 code or "", confidence 0..1)."""
    name = "none"

    def detect(self, text: str) -> tuple[str, float]:
        return "", 0.0

    def available(self) -> bool:
        return False


class FastTextDetector(Detector):
    name = "fasttext-lid176"

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._model = None
        self._failed = False
        self._lock = threading.Lock()

    def available(self) -> bool:
        return self._load() is not None

    def _load(self):
        if self._model is None and not self._failed:
            with self._lock:
                if self._model is None and not self._failed:
                    try:
                        import fasttext
                        fasttext.FastText.eprint = lambda *a, **k: None       # it prints a warning on every load
                        self._model = fasttext.load_model(str(self.path))
                    except Exception as e:
                        self._failed = True
                        log.warning("language detection is unavailable (%s); questions fall back to the default language", type(e).__name__)
        return self._model

    def detect(self, text: str) -> tuple[str, float]:
        model = self._load()
        text = " ".join(_CITATION.sub(" ", _PLACEHOLDER.sub(" ", text or "")).split())
        if model is None or len(text) < 2:
            return "", 0.0
        # the C++ binding directly: the Python wrapper of fasttext 0.9.2 breaks on NumPy 2
        prob, label = model.f.predict(text + "\n", 1, 0.0, "strict")[0]
        code = label.replace("__label__", "")
        return (code if valid(code) else ""), min(1.0, float(prob))


_detector: Detector | None = None
_detector_lock = threading.Lock()


def model_path() -> Path:
    from app.config import settings
    return Path(settings.langid_model or Path(settings.models_dir) / MODEL_FILE)


def detector() -> Detector:
    global _detector
    if _detector is None:
        with _detector_lock:
            if _detector is None:
                _detector = FastTextDetector(model_path())
    return _detector


def set_detector(d: Detector | None) -> None:
    """Tests replace the detector."""
    global _detector
    _detector = d


def detect(text: str) -> tuple[str, float]:
    try:
        return detector().detect(text)
    except Exception as e:
        log.warning("language detection failed (%s)", type(e).__name__)
        return "", 0.0


def status() -> str:
    """For the health endpoint: ready | missing."""
    return "ready" if detector().available() else "missing"


def download() -> Path:
    """Fetch the detector model (about 1 MB) and check its hash. For a local install; the image has it built in."""
    import hashlib
    import httpx
    path = model_path()
    data = httpx.get(MODEL_URL, timeout=60, follow_redirects=True).content
    if hashlib.sha256(data).hexdigest() != MODEL_SHA256:
        raise ValueError("The downloaded language model does not match its expected hash.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ---- the language of a turn ------------------------------------------------------------------------------------------
@dataclass
class LanguageContext:
    """How the language of one question was decided. `resolved` is the language the answer is written in."""
    detected: str = ""
    confidence: float = 0.0
    resolved: str = "en"
    source: str = "default"            # detected | conversation | kb_default
    accepted: dict = field(default_factory=dict)      # kb id -> the accepted list checked for it ([] = every language)
    doc_languages: dict = field(default_factory=dict)  # kb id -> its document languages
    answer: str = ""                   # the detected language of the answer
    retried: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


_turn: contextvars.ContextVar["LanguageContext | None"] = contextvars.ContextVar("turn_language", default=None)


@contextlib.contextmanager
def using(ctx: "LanguageContext"):
    """The language of the turn being answered, for retrieval (D35) and generation (D19)."""
    token = _turn.set(ctx)
    try:
        yield
    finally:
        _turn.reset(token)


def current() -> "LanguageContext | None":
    return _turn.get()


def resolve(text: str, *, last: str = "", default: str = "en", threshold: float = 0.5) -> LanguageContext:
    """D19: the question's own language when detection is sure enough; else the conversation's last language; else the
    knowledge base default."""
    code, conf = detect(text)
    if code and conf >= threshold:
        return LanguageContext(code, round(conf, 3), code, "detected")
    if valid(last):
        return LanguageContext(code, round(conf, 3), last, "conversation")
    return LanguageContext(code, round(conf, 3), default if valid(default) else "en", "kb_default")


def of_answer(text: str, threshold: float = 0.5) -> str:
    """The language an answer is written in, or "" when it is too short or unclear to say."""
    if len(_CITATION.sub("", text or "").strip()) < 20:
        return ""
    code, conf = detect(text)
    return code if conf >= threshold else ""


def of_document(texts: list[str], threshold: float = 0.5) -> tuple[list[tuple[str, float]], str]:
    """Per passage (language, confidence), and the document's language: the one covering the most confidently detected text."""
    per = [detect(t) for t in texts]
    weight: dict[str, int] = {}
    for (code, conf), t in zip(per, texts):
        if code and conf >= threshold:
            weight[code] = weight.get(code, 0) + len(t)
    return [(c, round(p, 3)) for c, p in per], (max(weight, key=weight.get) if weight else "")
