"""Rule-based detection of prompt injection and toxic language (part 6b; model-based detection comes later).

These rules catch KNOWN phrasing. They do not understand meaning, so paraphrases, other languages and a determined
attacker can get past them; the prompt-level defences and access filtering stay in place underneath. Text is normalised
first (case, look-alike letters, zero-width characters, spaced-out letters) so simple obfuscation does not hide a phrase.
"""
import re
import unicodedata

SENSITIVITY = ("low", "medium", "high")
# an injection question is blocked when its score reaches this
INJECTION_THRESHOLD = {"low": 3.0, "medium": 2.0, "high": 1.0}
# toxicity tiers that are active at each sensitivity
TOXICITY_TIERS = {"low": ("threat",), "medium": ("threat", "profanity"), "high": ("threat", "profanity", "insult")}

_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i", "|": "i"})
_HOMOGLYPH = str.maketrans({"а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "і": "i", "ѕ": "s",
                            "ο": "o", "ν": "v", "α": "a", "ε": "e", "ι": "i", "κ": "k"})
_SPACED = re.compile(r"\b(?:[a-z][ .\-_*]){3,}[a-z]\b")


def _squash_spaced(text: str) -> str:
    return _SPACED.sub(lambda m: re.sub(r"[ .\-_*]", "", m.group(0)), text)


def normalise(text: str) -> tuple[str, str]:
    """(plain, deobfuscated): lower-cased and tidied, and the same with look-alike characters, leetspeak and spaced letters undone."""
    t = unicodedata.normalize("NFKC", text or "")
    t = "".join(c for c in t if unicodedata.category(c) not in ("Cf", "Cc") or c in "\n\t")       # zero-width and control characters
    plain = re.sub(r"[ \t]+", " ", t.lower().translate(_HOMOGLYPH)).strip()
    deob = _squash_spaced(plain)
    deob = re.sub(r"(?<=[a-z])[0-9@$!|]|[0-9@$!|](?=[a-z])", lambda m: m.group(0).translate(_LEET), deob)   # sh1t -> shit (next to a letter only)
    deob = re.sub(r"([a-z])\1{2,}", r"\1\1", deob)                                                   # fuuuuck -> fuuck
    return plain, deob


# ---- prompt injection ------------------------------------------------------------------------------------------------
_W = r"(?:\w+\s+){0,4}"          # up to four filler words
INJECTION_RULES = [
    (3.0, r"\b(?:ignore|disregard|forget|override|bypass|skip)\b\s+" + _W + r"(?:instructions?|rules?|prompts?|guidelines?|directives?|restrictions?|safeguards?)\b"),
    (3.0, r"\b(?:reveal|show|print|repeat|display|output|leak|expose|tell me|give me|what (?:is|are))\b\s+" + _W + r"(?:system|initial|original|hidden|secret|internal|base)\s+(?:prompt|instructions?|message|rules)\b"),
    (3.0, r"\byour\s+(?:system|initial|original|hidden|secret)\s+(?:prompt|instructions?)\b"),
    (3.0, r"\b(?:do anything now|developer mode|jailbreak(?:en|ed)?|dan mode|god mode)\b"),
    (3.0, r"\byou\s+(?:are|will be)\s+(?:now\s+)?(?:dan|unrestricted|unfiltered|jailbroken|free of|no longer bound)\b"),
    (3.0, r"\b(?:pretend|act|behave|respond)\b\s+" + _W + r"(?:with(?:out)?|no)\s+(?:any\s+)?(?:rules|restrictions|limits|limitations|filters|guidelines|safeguards)\b"),
    (3.0, r"(?:<\|?\s*(?:im_start|im_end|system|endoftext)\s*\|?>|\[/?inst\]|<<sys>>|#{2,}\s*(?:system|instruction)s?\b)"),
    (1.5, r"(?:^|\n)\s*(?:system|assistant|developer)\s*[:>\]]"),
    (2.0, r"\bfrom now on\b\s+" + _W + r"you\s+(?:will|must|are|should|shall)\b"),
    (2.0, r"\b(?:new|updated|real|actual)\s+instructions?\s*(?:are|:|follow)"),
    (2.0, r"\b(?:stop being|no longer be)\b\s+" + _W + r"(?:assistant|chatbot|bot|an ai)\b"),
    (2.0, r"\b(?:pretend|imagine)\b\s+(?:to be|you are|that you are|you're)\s+" + _W + r"(?:hacker|evil|malicious|different ai|another ai)\b"),
    (2.0, r"\bwithout\s+(?:any\s+)?(?:restrictions|filters|censorship|safety)\b"),
    (1.0, r"\b(?:system prompt|prompt injection|jailbreak)\b"),
    (1.0, r"\b(?:decode|translate|convert)\b\s+" + _W + r"(?:base64|hex|rot13|binary)\b"),
    (1.0, r"\broleplay\b"),
]
_INJECTION = [(w, re.compile(p, re.I)) for w, p in INJECTION_RULES]


def injection_score(text: str, custom=()) -> float:
    """Total weight of the rules that match. Custom phrases (set by an Admin) always weigh 3, so they block at every sensitivity."""
    plain, deob = normalise(text)
    score = 0.0
    for weight, pat in _INJECTION:
        if pat.search(plain) or pat.search(deob):
            score += weight
    for phrase in custom:
        p, d = normalise(phrase)
        if p and (p in plain or d in deob):
            score += 3.0
    return score


def is_injection(text: str, sensitivity: str = "medium", custom=()) -> bool:
    return injection_score(text, custom) >= INJECTION_THRESHOLD.get(sensitivity, 2.0)


# ---- toxic language ---------------------------------------------------------------------------------------------------
# Conservative on purpose: common profanity, threats and plain insults. Slurs are left out; an Admin can add what their workplace needs.
_THREATS = [
    r"\b(?:i(?:'ll| will| am going to|'m going to|'m gonna| am gonna| gonna))\s+(?:kill|murder|hurt|stab|shoot|beat|destroy|ruin)\s+(?:you|him|her|them|everyone|everybody|all of you)\b",
    r"\b(?:i hope|hope) (?:you|he|she|they) (?:die|dies|get hurt|suffer)s?\b",
    r"\b(?:kill yourself|go die|you should die|you deserve to die)\b",
    r"\bi(?:'ll| will) (?:find|hunt) you\b",
]
_PROFANITY = [r"fuck\w*", r"motherfuck\w*", r"shit\w*", r"bullshit", r"asshole\w*", r"bastard\w*", r"bitch\w*",
              r"dickhead\w*", r"piss off", r"arsehole\w*", r"wanker\w*", r"prick"]
_INSULTS = [r"idiot\w*", r"moron\w*", r"stupid", r"dumb", r"loser\w*", r"useless", r"pathetic", r"jerk\w*", r"imbecile\w*"]
_TIER_PATTERNS = {
    "threat": [re.compile(p, re.I) for p in _THREATS],
    "profanity": [re.compile(r"\b" + p + r"\b", re.I) for p in _PROFANITY],
    "insult": [re.compile(r"\b" + p + r"\b", re.I) for p in _INSULTS],
}


def toxicity_hit(text: str, sensitivity: str = "medium", custom_words=(), allow_words=()) -> bool:
    plain, deob = normalise(text)
    allow = {w.strip().lower() for w in allow_words if w.strip()}
    for tier in TOXICITY_TIERS.get(sensitivity, TOXICITY_TIERS["medium"]):
        for pat in _TIER_PATTERNS[tier]:
            for variant in (plain, deob):
                for m in pat.finditer(variant):
                    if m.group(0).strip().lower() not in allow:
                        return True
    for w in custom_words:
        w = w.strip()
        if not w:
            continue
        p, d = normalise(w)
        pat = re.compile(r"\b" + re.escape(p) + r"\b", re.I)
        if pat.search(plain) or pat.search(deob):
            if p not in allow:
                return True
    return False
