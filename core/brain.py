"""
core/brain.py — one place that decides what kind of request this is.

THE PROBLEM
    Until now there was no router at all. Every sentence went to the Live model
    with the full tool list, and whether it became a conversation, a single
    command or a ten-step job was left entirely to the model's judgement. That
    is flexible but it is not a design: the same request could be answered as a
    question one minute and start driving the desktop the next, and there was no
    way to say "this is dictation, do not touch the mouse".

WHAT THIS DOES
    A fast, local classifier that labels a request as one of:

        TALK       explanation, question, conversation
        SCREEN     a question about what is visible
        COMMAND    one desktop action ("open Chrome", "turn it down")
        DICTATION  text to be written into a field
        AGENT      multi-step work that should run as a job
        ROUTINE    something that should repeat on a schedule
        CANCEL     stop what is happening

    It is deliberately not a language model. It runs in microseconds, works
    offline, and its mistakes are visible and fixable. The result is a HINT that
    is put in front of the model, not a hard gate — except for two cases where
    the hint becomes enforcement:

        * a CANCEL never reaches a tool;
        * DICTATION is never allowed to trigger desktop automation.

    Both of those are policy, not prediction, and they are the two places where
    guessing wrong is expensive.

CONFIDENCE
    Confidence is a measure of how many independent signals agreed, not a
    probability. Below `LOW_CONFIDENCE` the router says so and the caller should
    treat the request as TALK — the safe default, because talking cannot delete
    a file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from core.screen_context import FOLLOWUP_SECONDS, FRESH_SECONDS, get_screen_context


class Intent(str, Enum):
    TALK = "talk"
    SCREEN = "screen"
    COMMAND = "command"
    DICTATION = "dictation"
    AGENT = "agent"
    ROUTINE = "routine"
    CANCEL = "cancel"


# Below this, the caller should treat the request as TALK.
LOW_CONFIDENCE = 0.45

# Execution level. Talk is the conversation; Agent is sustained work.
class Level(str, Enum):
    TALK = "talk"
    AGENT = "agent"


@dataclass
class Decision:
    intent: Intent
    confidence: float
    needs_screen: bool = False
    reason: str = ""
    level: Level = Level.TALK
    slots: dict = field(default_factory=dict)

    @property
    def is_confident(self) -> bool:
        return self.confidence >= LOW_CONFIDENCE

    def as_hint(self) -> str:
        """The line injected into the turn context for the model."""
        if not self.is_confident:
            return ""
        extra = " (look at the screen before answering)" if self.needs_screen else ""
        return f"[INTENT: {self.intent.value.upper()} — {self.reason}{extra}]"


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------
#
# Each entry is (regex, weight). Weights are added, then normalised against a
# per-intent cap so a long sentence cannot win just by containing many words.
# English and Turkish are both covered because both are used in this house;
# adding a language means adding rows, not changing the algorithm.

_PATTERNS: dict[Intent, list[tuple[str, float]]] = {
    Intent.CANCEL: [
        (r"\b(stop|halt|abort|cancel|never ?mind|forget it)\b", 2.0),
        (r"\b(dur|iptal|vazgeç|bosver|boşver)\b", 2.0),
        (r"^(no|nope|hayır|hayir)$", 1.5),
    ],
    Intent.DICTATION: [
        (r"\b(dictate|dictation|type (this|that|the following)|write (this|that) down)\b", 2.0),
        (r"\b(draft|compose|write) (a|an|my|the)? ?(\w+ ){0,3}(reply|message|email|note|response)\b", 2.2),
        (r"\b(insert|paste) (this|that|the text)\b", 1.5),
        (r"\b(dikte|metni yaz)\b", 1.5),
        (r"\b(cevap yaz|mesaj yaz|yanıt yaz|yanit yaz|yanıtla|yanitla)\b", 1.8),
    ],
    Intent.ROUTINE: [
        (r"\b(every (day|morning|evening|night|week|monday|tuesday|wednesday|thursday|friday|saturday|sunday))\b", 2.5),
        (r"\b(each (day|morning|week))\b", 1.5),
        (r"\b(remind me (every|each|daily|weekly))\b", 2.0),
        (r"\b(daily|weekly|recurring|routine)\b", 1.5),
        (r"\b(her (gün|gun|sabah|akşam|aksam|hafta|pazartesi|salı|sali|çarşamba|carsamba|perşembe|persembe|cuma|cumartesi|pazar))\b", 2.0),
    ],
    Intent.AGENT: [
        (r"\b(build|implement|refactor|migrate|scaffold|set up|setup)\b", 1.5),
        (r"\b(create|make|write|generate) (a|an|me a|me an)? ?(project|app|application|website|script|report|plan|tool)\b", 2.0),
        (r"\b(research|investigate|audit|compare|analy[sz]e|review)\b", 1.2),
        (r"\b(organi[sz]e|clean up|sort|tidy)\b.*\b(files?|folder|downloads?|desktop|pdfs?)\b", 2.0),
        (r"\b(find|collect|gather)\b.*\b(and|then)\b", 1.2),
        (r"\b(then|after that|afterwards|step by step|end to end)\b", 1.0),
        (r"\b(proje|uygulama|site|rapor) (oluştur|olustur|yap|hazırla|hazirla)\b", 2.0),
        (r"\b(araştır|arastir|incele|düzenle|duzenle|temizle|kur)\b", 1.2),
    ],
    Intent.COMMAND: [
        (r"^\s*(open|launch|start|run|close|quit|switch to|focus|maximi[sz]e|minimi[sz]e)\b", 1.6),
        (r"\b(open|launch|start|close|quit)\s+[A-Za-z]", 1.2),
        (r"\b(click|double ?click|right ?click|scroll|drag|type|press)\b", 1.2),
        (r"\b(turn (it )?(up|down|off|on)|volume|brightness|mute|unmute)\b", 1.5),
        (r"\b(switch|change|set) (the )?(tab|window|app|application)\b", 1.2),
        (r"\b(aç|ac|kapat|başlat|baslat|çalıştır|calistir|geç|gec)\b", 1.7),
    ],
    Intent.SCREEN: [
        (r"\b(this|that|these|those)\s+(error|code|function|file|page|screen|window|diagram|chart|image|text|message|line|button|menu|tab)\b", 2.5),
        (r"\b(on (my|the) screen|on screen|right here|what am i (looking at|seeing)|what('s| is) (this|that))\b", 2.5),
        (r"\b(explain|what does) (this|that|it)\b", 2.0),
        (r"\b(which file am i|what website is open|what('s| is) (currently )?(running|open) in my terminal)\b", 2.5),
        (r"\b(what did we discuss|earlier on this screen|before on this screen)\b", 2.0),
        (r"\b(bu (hata\w*|kod\w*|fonksiyon\w*|dosya\w*|sayfa\w*|ekran\w*|pencere\w*|diyagram\w*|grafik\w*|resim\w*|metin\w*|mesaj\w*|satır\w*|satir\w*))\b", 2.2),
        (r"\b(ekranda|burada|ne görüyorsun|ne goruyorsun|bunu açıkla|bunu acikla)\b", 2.0),
    ],
}

# Deictic words mean "look at the screen" even when the intent is TALK.
_DEICTIC = re.compile(
    r"\b(this|that|these|those|here)\b|\b(bu|şu|su|bunu|şunu|sunu|burada)\b",
    re.IGNORECASE,
)

# A short question that only makes sense against something already seen.
_FOLLOWUP = re.compile(
    r"\b(it|that|this|again|the (error|code|file|line|function|problem|issue))\b"
    r"|\b(nasıl|nasil|neden|nerede|düzelt|duzelt)\b",
    re.IGNORECASE,
)

# A request that is only a screen reference ("what's this?") is SCREEN, not TALK.
_SCREEN_ONLY = re.compile(
    r"^\s*(what('s| is) (this|that)\??|bu ne\??|bu nedir\??)\s*$", re.IGNORECASE
)

# Dictation must never be allowed to click or type through automation: the
# whole point is that the text goes into the field the user already chose.
_DICTATION_FORBIDS_AUTOMATION = re.compile(
    r"\b(click|open|launch|switch|scroll|drag|hotkey)\b", re.IGNORECASE
)


class IntentRouter:
    """Fast local classifier. No model calls, no I/O, no threads."""

    def classify(self, text: str) -> Decision:
        text = (text or "").strip()
        if not text:
            return Decision(Intent.TALK, 0.0, reason="empty")

        scores: dict[Intent, float] = {}
        hits: dict[Intent, list[str]] = {}
        for intent, patterns in _PATTERNS.items():
            total = 0.0
            matched: list[str] = []
            for pattern, weight in patterns:
                if re.search(pattern, text, re.IGNORECASE):
                    total += weight
                    matched.append(pattern)
            if total:
                scores[intent] = total
                hits[intent] = matched

        if not scores:
            return Decision(
                Intent.TALK,
                0.55,
                needs_screen=bool(_DEICTIC.search(text)),
                reason="no action signal",
            )

        # Normalise: saturate at 3.0 so extra keywords stop mattering, then map
        # to 0..1. A single strong pattern (2.5) lands at ~0.83.
        intent = max(scores, key=lambda i: scores[i])
        raw = scores[intent]
        confidence = min(1.0, raw / 3.0)
        reason = _reason_for(intent, text)

        if _SCREEN_ONLY.match(text):
            intent, confidence, reason = Intent.SCREEN, 0.9, "asking about the current view"

        needs_screen = intent in (Intent.SCREEN,) or bool(_DEICTIC.search(text))

        # A dictation request that also mentions clicking is contradictory.
        # Treat it as TALK rather than silently doing either.
        if intent is Intent.DICTATION and _DICTATION_FORBIDS_AUTOMATION.search(text):
            intent = Intent.TALK
            confidence = 0.4
            reason = "dictation and automation were both requested"

        level = Level.AGENT if intent in (Intent.AGENT, Intent.ROUTINE) else Level.TALK

        if confidence < LOW_CONFIDENCE:
            level = Level.TALK

        return Decision(
            intent=intent,
            confidence=confidence,
            needs_screen=needs_screen,
            reason=reason,
            level=level,
        )

    def allows_automation(self, decision: Decision) -> bool:
        """Dictation and cancel must never reach a desktop tool."""
        return decision.intent not in (Intent.DICTATION, Intent.CANCEL)


def _reason_for(intent: Intent, text: str) -> str:
    return {
        Intent.CANCEL: "the user is stopping something",
        Intent.DICTATION: "the user wants text written, not an action taken",
        Intent.ROUTINE: "this should repeat on a schedule",
        Intent.AGENT: "this needs several steps",
        Intent.COMMAND: "this is a single desktop action",
        Intent.SCREEN: "the user is asking about what is visible",
        Intent.TALK: "conversation",
    }.get(intent, "conversation")


# ---------------------------------------------------------------------------
# Brain facade
# ---------------------------------------------------------------------------

class Brain:
    """The single object main.py talks to for routing and context assembly.

    It deliberately does not own the model, the microphone or the tool
    registry. Those already exist and work; this is the layer that decides what
    to put in front of them.
    """

    def __init__(self, router: Optional[IntentRouter] = None):
        self.router = router or IntentRouter()
        self.screen = get_screen_context()

    # ── Routing ────────────────────────────────────────────────────────────

    def route(self, text: str) -> Decision:
        decision = self.router.classify(text)
        return self._apply_followup_context(text, decision)

    def _apply_followup_context(self, text: str, decision: Decision) -> Decision:
        """A short follow-up can refer to a screen that is no longer fresh.

        "How can I fix it?" carries no screen words of its own. If a capture was
        taken in the last few minutes, the honest reading is that the question is
        about that capture — and the context block will label its age rather
        than pretend it is live.
        """
        if decision.needs_screen or decision.intent is not Intent.TALK:
            return decision
        if not _FOLLOWUP.search(text):
            return decision
        if self.screen.latest(max_age=FOLLOWUP_SECONDS) is None:
            return decision
        decision.needs_screen = True
        decision.reason = "follow-up about the recent screen"
        return decision

    # ── Context ────────────────────────────────────────────────────────────

    def screen_block(self, text: str = "", fresh_only: bool = False) -> str:
        """The screen context block for this turn, or ''.

        `fresh_only` is for cases where describing an older capture as "now"
        would be a lie. Follow-ups use the default, which labels the age.
        """
        if fresh_only:
            frame = self.screen.latest_fresh()
            if frame is None:
                return ""
            return self.screen.format_block(text, max_age=FRESH_SECONDS)
        return self.screen.format_block(text, max_age=FOLLOWUP_SECONDS)

    def build_context(
        self,
        text: str = "",
        memory_block: str = "",
        include_screen: bool = True,
    ) -> str:
        """Assemble the per-turn context block.

        Order matters: identity and memory first, then the intent hint, then the
        screen. The screen goes last because it is the largest and most
        volatile part, and because it is the part most likely to contain
        untrusted text.
        """
        decision = self.route(text) if text else None
        parts: list[str] = []

        if decision is not None:
            hint = decision.as_hint()
            if hint:
                parts.append(hint)

        if memory_block:
            parts.append(memory_block)

        if include_screen and decision is not None:
            block = self.screen_block(text)
            if block:
                parts.append(block)

        return "\n\n".join(parts)

    # ── Observation intake ─────────────────────────────────────────────────

    def observe(self, observation, source: str = "screen"):
        """Record a captured observation for follow-ups."""
        return self.screen.remember(observation, source=source)

    def forget_screen(self) -> None:
        self.screen.clear()


_brain: Optional[Brain] = None


def get_brain() -> Brain:
    global _brain
    if _brain is None:
        _brain = Brain()
    return _brain
