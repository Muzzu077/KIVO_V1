import copy
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime
from threading import Lock
from pathlib import Path


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR         = get_base_dir()
MEMORY_PATH      = BASE_DIR / "memory" / "long_term.json"
_lock            = Lock()
MAX_VALUE_LENGTH = 380

# ── Why there are two very different numbers here ────────────────────────────
#
# There used to be one: MEMORY_MAX_CHARS = 2200, applied to the whole store. It
# was a *storage* limit, and it existed only because the entire memory was
# pasted into the system prompt on every connect — so growing the memory grew
# every single request. When it filled, _trim_to_limit() deleted the oldest
# entries and printed one line to a console nobody reads. A memory described as
# "deeply remembers projects, preferences and personal context" was in practice
# two pages long, and quietly forgot your sister's name after a few weeks.
#
# Storage and prompt budget are now separate concerns:
#
#   MEMORY_MAX_CHARS  — a runaway guard, not a feature limit. Nothing normal
#                       reaches it; a bug writing in a loop does.
#   PROMPT_CORE_CHARS — what actually rides in the system prompt every session.
#                       Smaller than the old whole-memory dump, so sessions
#                       start *faster* than before, not slower.
#
# Everything above the core stays on disk and is fetched on demand by the
# recall_memory tool — see search_memory() and format_memory_for_prompt().
MEMORY_MAX_CHARS  = 200_000
PROMPT_CORE_CHARS = 900
PROMPT_INDEX_CHARS = 420
# Most entries any one category may contribute to the core block, so a person
# with forty stored preferences still gets their sister into the prompt.
PROMPT_MAX_PER_CATEGORY = 6

_cached_memory: dict | None = None
_cached_mtime: float = 0.0
MEMORY_BACKUP_PATH = BASE_DIR / "memory" / "long_term.json.bak"


def _empty_memory() -> dict:
    return {
        "identity":      {},
        "preferences":   {},
        "projects":      {},
        "relationships": {},
        "wishes":        {},
        "notes":         {},
    }


def load_memory(force_reload: bool = False) -> dict:
    global _cached_memory, _cached_mtime
    with _lock:
        if not MEMORY_PATH.exists():
            if MEMORY_BACKUP_PATH.exists():
                try:
                    data = json.loads(MEMORY_BACKUP_PATH.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        _cached_memory = data
                        _cached_mtime = MEMORY_BACKUP_PATH.stat().st_mtime
                        return copy.deepcopy(data)
                except Exception:
                    pass
            _cached_memory = _empty_memory()
            _cached_mtime = 0.0
            return copy.deepcopy(_cached_memory)

        try:
            mtime = MEMORY_PATH.stat().st_mtime
            if not force_reload and _cached_memory is not None and mtime == _cached_mtime:
                return copy.deepcopy(_cached_memory)

            data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                base = _empty_memory()
                for key in base:
                    if key not in data:
                        data[key] = {}
                _cached_memory = data
                _cached_mtime = mtime
                return copy.deepcopy(data)
            return _empty_memory()
        except Exception as e:
            print(f"[Memory] ⚠️ Load error: {e}")
            # Try recovering from backup if primary failed or corrupted
            if MEMORY_BACKUP_PATH.exists():
                try:
                    data = json.loads(MEMORY_BACKUP_PATH.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        print("[Memory] 🔄 Successfully recovered memory from backup!")
                        _cached_memory = data
                        _cached_mtime = MEMORY_BACKUP_PATH.stat().st_mtime
                        return copy.deepcopy(data)
                except Exception:
                    pass
            if _cached_memory is not None:
                return copy.deepcopy(_cached_memory)
            return _empty_memory()

def _all_entries(memory: dict) -> list[tuple]:
    entries = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            if isinstance(entry, dict) and "value" in entry:
                entries.append((cat, key, entry))
    return entries


# Set by main.py so a trim can reach the activity log. Deleting something a
# person told you and mentioning it only on stdout is how a memory loses trust.
_trim_notifier = None


def set_trim_notifier(fn) -> None:
    """Register a callable(str) that surfaces trims to the user."""
    global _trim_notifier
    _trim_notifier = fn


def _trim_to_limit(memory: dict) -> dict:
    if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
        return memory
    entries = _all_entries(memory)
    entries.sort(key=lambda t: t[2].get("updated", "0000-00-00"))
    dropped = []
    for cat, key, _ in entries:
        if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
            break
        del memory[cat][key]
        dropped.append(f"{cat}/{key}")
        print(f"[Memory] 🗑️  Trimmed {cat}/{key}")
    if dropped and _trim_notifier:
        try:
            _trim_notifier(
                f"SYS: Memory full — forgot {len(dropped)} oldest entries "
                f"({', '.join(dropped[:3])}{'…' if len(dropped) > 3 else ''})"
            )
        except Exception:
            pass
    return memory

def save_memory(memory: dict) -> None:
    global _cached_memory, _cached_mtime
    if not isinstance(memory, dict):
        return
    with _lock:
        memory = _trim_to_limit(memory)
        MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(memory, indent=2, ensure_ascii=False)

        # Atomic write via temporary file
        temp_file = None
        try:
            with tempfile.NamedTemporaryFile("w", dir=MEMORY_PATH.parent, delete=False, encoding="utf-8") as tf:
                tf.write(payload)
                tf.flush()
                os.fsync(tf.fileno())
                temp_file = tf.name

            # Keep a backup before replacing
            if MEMORY_PATH.exists():
                try:
                    MEMORY_PATH.replace(MEMORY_BACKUP_PATH)
                except Exception:
                    pass

            os.replace(temp_file, MEMORY_PATH)
            _cached_memory = copy.deepcopy(memory)
            _cached_mtime = MEMORY_PATH.stat().st_mtime
        except Exception as e:
            print(f"[Memory] ❌ Atomic save error: {e}")
            if temp_file and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except Exception:
                    pass


def _truncate_value(val: str) -> str:
    if isinstance(val, str) and len(val) > MAX_VALUE_LENGTH:
        return val[:MAX_VALUE_LENGTH].rstrip() + "…"
    return val


def _recursive_update(target: dict, updates: dict) -> bool:
    changed = False
    for key, value in updates.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, dict) and "value" not in value:
            if key not in target or not isinstance(target[key], dict):
                target[key] = {}
                changed = True
            if _recursive_update(target[key], value):
                changed = True
        else:
            new_val  = _truncate_value(str(value["value"] if isinstance(value, dict) else value))
            entry    = {"value": new_val, "updated": datetime.now().strftime("%Y-%m-%d")}
            existing = target.get(key, {})
            if not isinstance(existing, dict) or existing.get("value") != new_val:
                target[key] = entry
                changed = True
    return changed


def update_memory(memory_update: dict) -> dict:
    if not isinstance(memory_update, dict) or not memory_update:
        return load_memory()
    memory = load_memory()
    if _recursive_update(memory, memory_update):
        save_memory(memory)
        print(f"[Memory] 💾 Saved: {list(memory_update.keys())}")
    return memory

def _entry_value(entry) -> str:
    """Accept both the {'value': ..., 'updated': ...} shape and a bare string,
    because early versions of the store wrote plain strings."""
    if isinstance(entry, dict):
        return str(entry.get("value", "") or "").strip()
    return str(entry or "").strip()


def _pretty(key: str) -> str:
    return key.replace("_", " ").strip()


# Identity is always in the prompt; these categories compete for the remaining
# budget by recency.
_CATEGORY_LABELS = {
    "preferences":   "Preferences",
    "projects":      "Active projects / goals",
    "relationships": "People in their life",
    "wishes":        "Wishes / plans",
    "notes":         "Notes",
}

_IDENTITY_FIELDS = ["name", "age", "birthday", "city", "job",
                    "language", "school", "nationality"]


def format_memory_for_prompt(memory: dict | None) -> str:
    """Build the memory block that goes into the system prompt.

    This used to dump everything. It now sends three things:

      1. IDENTITY  - always, in full. It is small, and it is wrong for the
         assistant to have to look up your name.
      2. RECENT    - the most recently updated entries from every other
         category, up to PROMPT_CORE_CHARS. Recency is the cheapest useful
         relevance signal available without embeddings.
      3. AN INDEX  - the *keys* of everything else, values omitted.

    Point 3 is what makes recall work at all. A model cannot decide to look
    something up if it does not know the thing exists: with only points 1 and 2,
    "who is Ayse?" would get "I don't know" while ayse_sister sat on disk
    unread. The index costs a few hundred characters and turns recall from a
    gamble into a lookup.

    Net effect on latency: this block is SMALLER than the old full dump, so
    every session connects with fewer tokens. Occasionally the model spends one
    extra round trip on recall_memory - covered by the acknowledgment it
    already speaks before any slow step."""
    if not memory:
        return ""

    core_lines: list[str] = []

    # 1. Identity - always, in full
    identity = memory.get("identity", {}) or {}
    for field in _IDENTITY_FIELDS:
        val = _entry_value(identity.get(field))
        if not val:
            continue
        if field == "language":
            # Labelled as an observation, not a setting. A bare "Language:
            # English" line written months ago reads like a standing order and
            # was one of the reasons a Turkish question came back in English.
            core_lines.append(
                f"Has spoken to you in: {val} (an observation about the past — "
                f"always answer in the language of their CURRENT message)")
        else:
            core_lines.append(f"{field.title()}: {val}")
    for key, entry in identity.items():
        if key in _IDENTITY_FIELDS:
            continue
        val = _entry_value(entry)
        if val:
            core_lines.append(f"{_pretty(key).title()}: {val}")

    # 2. Everything else, most recently updated first
    rest: list[tuple[str, str, str, str]] = []   # (updated, cat, key, value)
    for cat in _CATEGORY_LABELS:
        for key, entry in (memory.get(cat, {}) or {}).items():
            val = _entry_value(entry)
            if not val:
                continue
            updated = (entry.get("updated", "") if isinstance(entry, dict) else "") or "0000-00-00"
            rest.append((updated, cat, key, val))
    rest.sort(key=lambda t: t[0], reverse=True)

    used    = sum(len(l) + 1 for l in core_lines)
    shown: dict[str, list[str]] = {}
    overflow: dict[str, list[str]] = {}

    # Recency decides order, but no single category may take the whole budget.
    # Without the cap, someone with forty stored preferences gets a prompt that
    # is forty preferences and not one person's name — the categories that
    # matter most in conversation are also the ones that change least often, so
    # pure recency systematically buries them.
    per_cat_used: dict[str, int] = {}
    for _updated, cat, key, val in rest:
        line = f"  - {_pretty(key).title()}: {val}"
        if (per_cat_used.get(cat, 0) < PROMPT_MAX_PER_CATEGORY
                and used + len(line) + 1 <= PROMPT_CORE_CHARS):
            shown.setdefault(cat, []).append(line)
            per_cat_used[cat] = per_cat_used.get(cat, 0) + 1
            used += len(line) + 1
        else:
            overflow.setdefault(cat, []).append(_pretty(key))

    # The index is a table of contents, so it is interleaved across categories
    # rather than continuing in recency order. Sorted by recency it would list
    # twenty-four preferences before the first relationship, and the one entry
    # the index exists for — the old fact the model has no other way to know
    # about — would fall off the end.
    indexed: list[str] = []
    if overflow:
        cats  = [c for c in _CATEGORY_LABELS if overflow.get(c)]
        cursor = {c: 0 for c in cats}
        while cats:
            for cat in list(cats):
                i = cursor[cat]
                if i >= len(overflow[cat]):
                    cats.remove(cat)
                    continue
                indexed.append(overflow[cat][i])
                cursor[cat] = i + 1

    for cat, label in _CATEGORY_LABELS.items():
        if shown.get(cat):
            core_lines.append("")
            core_lines.append(f"{label}:")
            core_lines.extend(shown[cat])

    if not core_lines and not indexed:
        return ""

    out = [
        "[WHAT YOU KNOW ABOUT THIS PERSON — use naturally, never recite like a list]",
        *core_lines,
    ]

    # 3. The index of what is on disk but not in this prompt
    if indexed:
        budget, names = PROMPT_INDEX_CHARS, []
        for n in indexed:
            if budget - len(n) - 2 < 0:
                break
            names.append(n)
            budget -= len(n) + 2
        if names:
            out.append("")
            out.append(
                "[ALSO REMEMBERED — values not shown here. Call recall_memory "
                "with a keyword to read any of these before saying you do not know]"
            )
            out.append(", ".join(names)
                       + (f" (+{len(indexed) - len(names)} more)"
                          if len(indexed) > len(names) else ""))

    return "\n".join(out) + "\n"


# ── Recall ────────────────────────────────────────────────────────────────────

def _score(query_words: list[str], cat: str, key: str, value: str) -> int:
    """Cheap lexical relevance. No embeddings, no network, no model call - this
    runs in well under a millisecond, which is the entire point: recall must
    cost one model round trip, never two."""
    hay_key = _pretty(key).lower()
    hay_val = value.lower()
    score   = 0
    for w in query_words:
        if not w:
            continue
        if w == hay_key:
            score += 10
        elif w in hay_key:
            score += 6
        if w in hay_val:
            score += 3
        if w in cat:
            score += 1
    return score


def search_memory(query: str, limit: int = 8) -> str:
    """Find stored facts matching `query`. Backs the recall_memory tool.

    An empty query is treated as "show me everything you know", capped - the
    model asks that when the user says "what do you remember about me?"."""
    memory = load_memory()
    words  = [w for w in re.split(r"[^\w]+", (query or "").lower()) if len(w) > 1]

    rows: list[tuple[int, str, str, str]] = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue                     # skip 'sessions', which is a list
        for key, entry in items.items():
            val = _entry_value(entry)
            if not val:
                continue
            s = _score(words, cat, key, val) if words else 1
            if s > 0:
                rows.append((s, cat, key, val))

    if not rows:
        return (f"Nothing stored about '{query}'." if query
                else "I have not stored anything about this person yet.")

    rows.sort(key=lambda r: (-r[0], r[2]))
    lines = [f"{cat}/{_pretty(key)}: {val}" for _s, cat, key, val in rows[:max(1, limit)]]
    head  = (f"Stored facts matching '{query}':" if query
             else "Everything currently stored:")
    more  = (f"\n(+{len(rows) - len(lines)} more — search with a narrower keyword)"
             if len(rows) > len(lines) else "")
    return head + "\n" + "\n".join(lines) + more


def all_entries_for_ui() -> list[dict]:
    """Flat list for the memory panel: what KIVO knows, and when it learned it.
    Sorted newest first so the panel opens on what changed most recently."""
    memory = load_memory()
    rows = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            val = _entry_value(entry)
            if not val:
                continue
            rows.append({
                "category": cat,
                "key":      key,
                "value":    val,
                "updated":  (entry.get("updated", "") if isinstance(entry, dict) else ""),
            })
    rows.sort(key=lambda r: (r["updated"] or "0000-00-00"), reverse=True)
    return rows

def remember(key: str, value: str, category: str = "notes") -> str:
    valid = {"identity", "preferences", "projects", "relationships", "wishes", "notes"}
    if category not in valid:
        category = "notes"
    update_memory({category: {key: {"value": value}}})
    return f"Remembered: {category}/{key} = {value}"


def forget(key: str, category: str = "notes") -> str:
    memory = load_memory()
    cat    = memory.get(category, {})
    if key in cat:
        del cat[key]
        memory[category] = cat
        save_memory(memory)
        return f"Forgotten: {category}/{key}"
    return f"Not found: {category}/{key}"


forget_memory = forget


# ── Layered memory ────────────────────────────────────────────────────────────
#
# Personal memory (long_term.json) answers "who is this person?". It is a poor
# place for "which branch is this repo on?" or "what did step 3 of the current
# task find?" — those facts are true only inside a workspace or a task, and
# mixing them into the personal store means the assistant eventually recites
# build commands as if they were hobbies.
#
# Three layers, plus a fourth for agent work:
#
#   personal   long_term.json — durable facts about the user. Unchanged.
#   workspace  keyed by repository/workspace id — branch, conventions, commands.
#   task       keyed by task id — findings that matter while a job is running.
#   agent      keyed by agent/workspace id — durable notes for a long-running
#              agent, including the goal it was created for.
#
# Workspace/task/agent entries live in memory/layers.json, separate from the
# personal store. That is deliberate: every existing reader of long_term.json
# (prompt assembly, recall, the memory panel) keeps working untouched, and a
# corrupt layer file can never take the personal memory down with it.
#
# SENSITIVE ENTRIES
#   An entry can be marked sensitive. Sensitive values are still stored (the
#   user asked for them to be remembered) but they are excluded from every
#   prompt block and from ordinary recall. Reading one requires an explicit
#   include_sensitive=True, which only the caller can supply. Nothing captures
#   screen pixels or audio here — those never enter memory at all.

LAYER_NAMES = ("personal", "workspace", "task", "agent")
_SCOPED_LAYERS = ("workspace", "task", "agent")

LAYERS_PATH = BASE_DIR / "memory" / "layers.json"
LAYERS_BACKUP_PATH = BASE_DIR / "memory" / "layers.json.bak"

MAX_LAYER_ENTRIES = 120       # per scope
MAX_LAYER_SCOPES = 40         # per layer; oldest scope is dropped first
LAYER_PROMPT_CHARS = 900      # per layer, in the system prompt
LAYER_VALUE_LENGTH = MAX_VALUE_LENGTH

_layers_lock = Lock()


def workspace_scope(path: str | os.PathLike | None = None) -> str:
    """A stable id for the workspace containing `path`.

    The id is the directory name plus a short hash of the absolute path, so two
    checkouts both called "app" do not share memory. `.git` is looked for by
    walking up rather than by shelling out to git: this runs at session start
    and must not block on a subprocess.
    """
    root = Path(path or os.getcwd()).expanduser().resolve()
    for candidate in (root, *root.parents):
        if (candidate / ".git").exists():
            root = candidate
            break
    digest = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:8]
    return f"{root.name or 'workspace'}@{digest}"


def _normalise_scope(scope: str | None) -> str:
    scope = (scope or "").strip()
    return scope[:120] if scope else "default"


def _layer_entry(value: str, sensitive: bool = False, source: str = "") -> dict:
    text = str(value or "").strip()
    if len(text) > LAYER_VALUE_LENGTH:
        text = text[:LAYER_VALUE_LENGTH].rstrip() + "…"
    return {
        "value": text,
        "updated": datetime.now().isoformat(timespec="seconds"),
        "sensitive": bool(sensitive),
        "source": str(source or "")[:80],
    }


class LayeredMemoryStore:
    """Workspace, task and agent memory, persisted atomically.

    A separate class rather than module globals makes the store testable with a
    temporary path and keeps the personal store's locking independent.
    """

    def __init__(self, path: Path = LAYERS_PATH, backup_path: Path = LAYERS_BACKUP_PATH):
        self.path = Path(path)
        self.backup_path = Path(backup_path)
        self._lock = Lock()

    # ── Disk ───────────────────────────────────────────────────────────────

    def _empty(self) -> dict:
        return {layer: {} for layer in _SCOPED_LAYERS}

    def _load(self) -> dict:
        if not self.path.exists():
            if self.backup_path.exists():
                try:
                    data = json.loads(self.backup_path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        return self._normalise(data)
                except Exception:
                    pass
            return self._empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return self._normalise(data)
        except Exception as e:
            print(f"[Memory] ⚠️ Layer load error: {e}")
            if self.backup_path.exists():
                try:
                    data = json.loads(self.backup_path.read_text(encoding="utf-8"))
                    if isinstance(data, dict):
                        print("[Memory] 🔄 Recovered layer memory from backup.")
                        return self._normalise(data)
                except Exception:
                    pass
        return self._empty()

    @staticmethod
    def _normalise(data: dict) -> dict:
        out = {layer: {} for layer in _SCOPED_LAYERS}
        for layer in _SCOPED_LAYERS:
            scopes = data.get(layer, {})
            if isinstance(scopes, dict):
                out[layer] = {
                    _normalise_scope(scope): entries
                    for scope, entries in scopes.items()
                    if isinstance(entries, dict)
                }
        return out

    def _save(self, data: dict) -> None:
        data = self._prune(data)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2, ensure_ascii=False)
        temp_file = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", dir=self.path.parent, delete=False, encoding="utf-8"
            ) as tf:
                tf.write(payload)
                tf.flush()
                os.fsync(tf.fileno())
                temp_file = tf.name
            if self.path.exists():
                try:
                    self.path.replace(self.backup_path)
                except Exception:
                    pass
            os.replace(temp_file, self.path)
        except Exception as e:
            print(f"[Memory] ❌ Layer save error: {e}")
            if temp_file and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except Exception:
                    pass

    def _prune(self, data: dict) -> dict:
        """Bound each scope, then bound the number of scopes per layer."""
        for layer in _SCOPED_LAYERS:
            scopes = data.setdefault(layer, {})
            for scope, entries in list(scopes.items()):
                if len(entries) > MAX_LAYER_ENTRIES:
                    ordered = sorted(
                        entries.items(),
                        key=lambda kv: (kv[1] or {}).get("updated", ""),
                    )
                    for key, _ in ordered[: len(entries) - MAX_LAYER_ENTRIES]:
                        del entries[key]
            if len(scopes) > MAX_LAYER_SCOPES:
                newest = sorted(
                    scopes.items(),
                    key=lambda kv: max(
                        ((e or {}).get("updated", "") for e in kv[1].values()),
                        default="",
                    ),
                    reverse=True,
                )
                data[layer] = dict(newest[:MAX_LAYER_SCOPES])
        return data

    # ── Public API ─────────────────────────────────────────────────────────

    def remember(
        self,
        layer: str,
        key: str,
        value: str,
        scope: str = "",
        *,
        sensitive: bool = False,
        source: str = "",
    ) -> str:
        if layer not in _SCOPED_LAYERS:
            return f"Unknown memory layer: {layer}"
        key = (key or "").strip()
        if not key:
            return "A memory key is required."
        if value is None or not str(value).strip():
            return "A memory value is required."
        scope = _normalise_scope(scope)
        with self._lock:
            data = self._load()
            entries = data[layer].setdefault(scope, {})
            entries[key] = _layer_entry(str(value), sensitive=sensitive, source=source)
            self._save(data)
        print(f"[Memory] 💾 {layer}/{scope}/{key}")
        return f"Remembered in {layer} memory: {key}"

    def recall(
        self,
        layer: str,
        scope: str = "",
        query: str = "",
        limit: int = 8,
        *,
        include_sensitive: bool = False,
    ) -> list[dict]:
        if layer not in _SCOPED_LAYERS:
            return []
        scope = _normalise_scope(scope)
        with self._lock:
            entries = dict((self._load().get(layer, {}) or {}).get(scope, {}) or {})
        words = [w for w in re.split(r"[^\w]+", (query or "").lower()) if len(w) > 1]
        scored = []
        for key, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("sensitive") and not include_sensitive:
                continue
            val = str(entry.get("value", "") or "")
            if not val:
                continue
            score = _score(words, layer, key, val) if words else 1
            if score > 0:
                scored.append((score, {
                    "layer": layer,
                    "scope": scope,
                    "key": key,
                    "value": val,
                    "updated": entry.get("updated", ""),
                    "sensitive": bool(entry.get("sensitive")),
                }))
        # Relevance first, then newest. With no query every row scores 1 and
        # recency is what makes the prompt block useful.
        scored.sort(key=lambda pair: (pair[0], pair[1]["updated"]), reverse=True)
        return [row for _score_value, row in scored[: max(1, limit)]]

    def search(
        self,
        query: str = "",
        limit: int = 8,
        *,
        include_sensitive: bool = False,
    ) -> list[dict]:
        """Search every scope in every scoped layer."""
        rows: list[dict] = []
        for layer in _SCOPED_LAYERS:
            for scope in self.scopes(layer):
                rows.extend(self.recall(
                    layer, scope, query, limit,
                    include_sensitive=include_sensitive,
                ))
        return rows[: max(1, limit)]

    def scopes(self, layer: str) -> list[str]:
        if layer not in _SCOPED_LAYERS:
            return []
        with self._lock:
            return list((self._load().get(layer, {}) or {}).keys())

    def forget(self, layer: str, key: str, scope: str = "") -> bool:
        if layer not in _SCOPED_LAYERS:
            return False
        scope = _normalise_scope(scope)
        with self._lock:
            data = self._load()
            entries = (data.get(layer, {}) or {}).get(scope, {})
            if key not in entries:
                return False
            del entries[key]
            self._save(data)
        return True

    def clear(self, layer: str, scope: str | None = None) -> None:
        """Drop one scope, or an entire layer when scope is None."""
        if layer not in _SCOPED_LAYERS:
            return
        with self._lock:
            data = self._load()
            if scope is None:
                data[layer] = {}
            else:
                data.get(layer, {}).pop(_normalise_scope(scope), None)
            self._save(data)

    def snapshot(self, layer: str, scope: str = "", *, include_sensitive: bool = False) -> dict:
        return {
            row["key"]: row["value"]
            for row in self.recall(layer, scope, limit=MAX_LAYER_ENTRIES,
                                   include_sensitive=include_sensitive)
        }

    def format_for_prompt(
        self,
        layer: str,
        scope: str = "",
        max_chars: int = LAYER_PROMPT_CHARS,
        *,
        include_sensitive: bool = False,
    ) -> str:
        rows = self.recall(layer, scope, limit=MAX_LAYER_ENTRIES,
                           include_sensitive=include_sensitive)
        if not rows:
            return ""
        label = {
            "workspace": "WORKSPACE MEMORY",
            "task": "TASK MEMORY",
            "agent": "AGENT MEMORY",
        }[layer]
        scope = _normalise_scope(scope)
        lines = [f"[{label} — {scope}]"]
        used = len(lines[0]) + 1
        for row in rows:
            line = f"  - {row['key']}: {row['value']}"
            if used + len(line) + 1 > max_chars:
                lines.append("  … (more stored; call recall_memory)")
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(lines)


_default_layer_store: LayeredMemoryStore | None = None
_layer_store_lock = Lock()


def get_layered_memory() -> LayeredMemoryStore:
    global _default_layer_store
    with _layer_store_lock:
        if _default_layer_store is None:
            _default_layer_store = LayeredMemoryStore()
        return _default_layer_store


def _personal_rows(query: str = "", limit: int = 8) -> list[dict]:
    memory = load_memory()
    words = [w for w in re.split(r"[^\w]+", (query or "").lower()) if len(w) > 1]
    rows = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            val = _entry_value(entry)
            if not val:
                continue
            score = _score(words, cat, key, val) if words else 1
            if score > 0:
                rows.append({
                    "layer": "personal",
                    "scope": cat,
                    "key": key,
                    "value": val,
                    "updated": (entry.get("updated", "") if isinstance(entry, dict) else ""),
                    "sensitive": False,
                })
    rows.sort(key=lambda r: (r["updated"], r["key"]), reverse=True)
    return rows[: max(1, limit)]


def remember_layer(
    layer: str,
    key: str,
    value: str,
    scope: str = "",
    *,
    category: str = "notes",
    sensitive: bool = False,
    source: str = "",
) -> str:
    """Write to one layer. `personal` delegates to the existing store."""
    if layer == "personal":
        valid = {"identity", "preferences", "projects", "relationships", "wishes", "notes"}
        category = category if category in valid else "notes"
        update_memory({category: {key: {"value": value}}})
        return f"Remembered in personal memory: {category}/{key}"
    return get_layered_memory().remember(
        layer, key, value, scope, sensitive=sensitive, source=source)


def recall_layer(
    layer: str,
    scope: str = "",
    query: str = "",
    limit: int = 8,
    *,
    include_sensitive: bool = False,
) -> list[dict]:
    if layer == "personal":
        return _personal_rows(query, limit)
    return get_layered_memory().recall(
        layer, scope, query, limit, include_sensitive=include_sensitive)


def recall_layer_text(
    layer: str,
    scope: str = "",
    query: str = "",
    limit: int = 8,
) -> str:
    """A readable recall result for one layer, used by the recall_memory tool."""
    rows = recall_layer(layer, scope, query, limit)
    if not rows:
        return (f"Nothing stored in {layer} memory about '{query}'." if query
                else f"No {layer} memory stored yet.")
    lines = [
        f"{r['scope']}/{r['key']}: {r['value']}" if layer != "personal"
        else f"{r['key']}: {r['value']}"
        for r in rows
    ]
    head = (f"{layer} memory matching '{query}':" if query
            else f"{layer} memory:")
    return head + "\n" + "\n".join(lines)


def forget_layer(layer: str, key: str, scope: str = "", category: str = "notes") -> str:
    if layer == "personal":
        return forget(key, category)
    ok = get_layered_memory().forget(layer, key, scope)
    return (f"Forgotten: {layer}/{_normalise_scope(scope)}/{key}" if ok
            else f"Not found: {layer}/{_normalise_scope(scope)}/{key}")


def format_layered_memory_for_prompt(
    workspace: str = "",
    task: str = "",
    agent: str = "",
    max_chars: int = LAYER_PROMPT_CHARS,
) -> str:
    """Personal + workspace + task + agent, in that order.

    Personal comes first because identity should not be buried under a branch
    name. Task and agent blocks are empty unless a scope is supplied, so an
    ordinary Talk session sees only personal and workspace memory.
    """
    blocks: list[str] = []
    personal = format_memory_for_prompt(load_memory()).rstrip()
    if personal:
        blocks.append(personal)
    for layer, scope in (("workspace", workspace), ("task", task), ("agent", agent)):
        if not scope:
            continue
        block = get_layered_memory().format_for_prompt(layer, scope, max_chars)
        if block:
            blocks.append(block)
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def search_all_memory(query: str, limit: int = 8) -> str:
    """recall_memory across every layer and scope, personal first."""
    rows = _personal_rows(query, limit)
    try:
        rows.extend(get_layered_memory().search(query, limit))
    except Exception as e:
        print(f"[Memory] ⚠️ Layer search failed: {e}")
    if not rows:
        return (f"Nothing stored about '{query}'." if query
                else "I have not stored anything about this person yet.")
    # Personal rows come first; layer rows are already relevance-sorted.
    rows = rows[: max(1, limit)]
    lines = [
        f"{r['layer']}/{r['scope']}/{r['key']}: {r['value']}"
        if r["layer"] != "personal" else
        f"personal/{r['key']}: {r['value']}"
        for r in rows
    ]
    head = (f"Stored facts matching '{query}':" if query
            else "Everything currently stored:")
    return head + "\n" + "\n".join(lines)


# ── Session memory ─────────────────────────────────────────────────────────────

_SESSION_MAX = 3   # safety cap — in practice 0-1 entries after pop


def save_session_summary(summary: str, language: str = "") -> None:
    """Append a 1-2 sentence session summary to long_term.json['sessions']."""
    summary = (summary or "").strip()
    if not summary:
        return
    memory   = load_memory()
    sessions = memory.get("sessions", [])
    if not isinstance(sessions, list):
        sessions = []
    entry: dict = {
        "date":    datetime.now().strftime("%Y-%m-%d"),
        "summary": summary[:280],
    }
    if language:
        entry["language"] = language
    sessions.append(entry)
    memory["sessions"] = sessions[-_SESSION_MAX:]
    with _lock:
        MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        MEMORY_PATH.write_text(
            json.dumps(memory, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    print(f"[Memory] 📝 Session saved ({entry['date']}): {summary[:60]}…")


def pop_last_session() -> dict | None:
    """
    Return AND remove the most recent session entry.
    Calling this consumes the entry so it is never repeated in future briefings.
    """
    with _lock:
        if not MEMORY_PATH.exists():
            return None
        try:
            memory   = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
            sessions = memory.get("sessions", [])
            if not isinstance(sessions, list) or not sessions:
                return None
            entry = sessions.pop()          # remove the last entry
            memory["sessions"] = sessions
            MEMORY_PATH.write_text(
                json.dumps(memory, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            return entry
        except Exception as e:
            print(f"[Memory] ⚠️ pop_last_session error: {e}")
            return None