"""Immutable, approval-bound context for future filesystem mutations.

This module is foundation-only: no mutation operation is implemented or
registered here.  It defines the shared objects that a future mutation tool
must produce during preflight and that the executor binds to one exact
conversation turn and tool call.

Security boundaries
-------------------
* The public representation (``to_dict`` / ``to_json``) contains only bounded,
  UI-safe display data.  The canonical fingerprint is private proof material
  retained in memory for the handler and is never serialized for IPC, the
  approval UI, or the model.  The private binding (workspace root identity,
  parent-directory identity, and the full binding payload) is likewise never
  serialized.
* The fingerprint is computed by the executor from its own trusted call and
  turn identifiers.  Model-supplied fingerprints are never accepted.
* Every nested value is immutable.  The binding is a frozen dataclass with
  scalar fields; no mutable dict or list is retained.
* Serialized form is size-bounded before an object can be constructed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from kivo.agent.tool_schema import ExecutionGrant

APPROVAL_CONTEXT_VERSION = 1
MAX_APPROVAL_CONTEXT_BYTES = 8_192
MAX_APPROVAL_DISPLAY_TITLE_CHARS = 120
MAX_APPROVAL_DISPLAY_SUMMARY_CHARS = 400
MAX_APPROVAL_DISPLAY_PREVIEW_CHARS = 240
MAX_APPROVAL_DISPLAY_NOTICE_CHARS = 240
MAX_RELATIVE_PATH_CHARS = 512
MAX_COMPONENT_BYTES = 255
MAX_IDENTITY_CHARS = 128
MAX_OPERATION_KIND_CHARS = 64
MAX_TARGET_STATE_CHARS = 32
MAX_TOOL_NAME_CHARS = 128
MAX_CALL_ID_CHARS = 128
MAX_BYTE_COUNT = (1 << 63) - 1

_CONTEXT_VERSION = APPROVAL_CONTEXT_VERSION
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_OPERATION_KIND = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_TARGET_STATE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_TOOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_CALL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_WORKSPACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_IDENTITY = re.compile(r"^[^\s\x00-\x1f\x7f-\x9f]{1,128}$")
_BIDI_CONTROLS = frozenset(range(0x202A, 0x202F)) | frozenset(range(0x2066, 0x206A))


class ApprovalContextError(ValueError):
    """An approval context is malformed, unbounded, or internally inconsistent."""


def _has_control(value: str) -> bool:
    return any(
        ord(ch) < 32
        or ord(ch) == 127
        or 0x7F <= ord(ch) <= 0x9F
        or ord(ch) in _BIDI_CONTROLS
        for ch in value
    )


def _safe_text(
    value: Any,
    *,
    name: str,
    max_chars: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ApprovalContextError(f"{name} must be a string.")
    clean = unicodedata.normalize("NFC", value.strip())
    if not clean and not allow_empty:
        raise ApprovalContextError(f"{name} cannot be empty.")
    if len(clean) > max_chars:
        raise ApprovalContextError(
            f"{name} must be at most {max_chars} characters long."
        )
    if _has_control(clean):
        raise ApprovalContextError(f"{name} contains control characters.")
    return clean


def _safe_preview(value: Any, *, name: str, max_chars: int) -> str:
    """Return a single-line, control-free, size-bounded content preview."""
    if not isinstance(value, str):
        raise ApprovalContextError(f"{name} must be a string.")
    clean = unicodedata.normalize("NFC", value)
    pieces: list[str] = []
    for char in clean:
        code = ord(char)
        if char in "\r\n\t":
            pieces.append(" ")
            continue
        if (
            code < 32
            or code == 127
            or 0x7F <= code <= 0x9F
            or code in _BIDI_CONTROLS
        ):
            continue
        pieces.append(char)
    bounded = " ".join("".join(pieces).split())
    if len(bounded) > max_chars:
        bounded = bounded[: max(0, max_chars - 3)].rstrip() + "..."
    return bounded


def canonicalize_relative_path(value: Any) -> str:
    """Return a canonical workspace-relative path or raise.

    ``""`` and ``"."`` both denote the workspace root.  Redundant ``.`` and
    empty path components are removed, but parent traversal, absolute paths,
    backslashes, control characters, and overlong components are rejected.
    The original spelling is otherwise preserved so the fingerprint always
    refers to the same filesystem name.
    """
    if not isinstance(value, str):
        raise ApprovalContextError("relative_path must be a string.")
    if len(value) > MAX_RELATIVE_PATH_CHARS:
        raise ApprovalContextError(
            f"relative_path must be at most {MAX_RELATIVE_PATH_CHARS} characters."
        )
    if value.startswith("~"):
        raise ApprovalContextError("Home-directory expansion is not allowed.")
    if "\x00" in value or _has_control(value):
        raise ApprovalContextError("relative_path contains control characters.")
    if "\\" in value:
        raise ApprovalContextError("Backslashes are not valid path separators.")
    if value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value):
        raise ApprovalContextError("Absolute paths are not allowed.")

    components: list[str] = []
    for component in value.split("/"):
        if component in ("", "."):
            continue
        if component == "..":
            raise ApprovalContextError("Parent-directory traversal is not allowed.")
        if len(component.encode("utf-8")) > MAX_COMPONENT_BYTES:
            raise ApprovalContextError("A relative path component is too long.")
        components.append(component)
    return "/".join(components)


def _normalize_digest(value: Any) -> str:
    if not isinstance(value, str):
        raise ApprovalContextError("content_digest must be a string.")
    digest = value.strip()
    if not _SHA256_HEX.fullmatch(digest):
        raise ApprovalContextError(
            "content_digest must be a canonical lowercase SHA-256 hex digest."
        )
    return digest


def _normalize_byte_count(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ApprovalContextError("byte_count must be an integer.")
    if value < 0 or value > MAX_BYTE_COUNT:
        raise ApprovalContextError("byte_count is outside the supported range.")
    return value


def _normalize_identity(value: Any, *, name: str) -> str:
    if not isinstance(value, str):
        raise ApprovalContextError(f"{name} must be a string.")
    clean = value.strip()
    if not _IDENTITY.fullmatch(clean) or _has_control(clean):
        raise ApprovalContextError(
            f"{name} must be a bounded, non-whitespace identity string."
        )
    return clean


def _canonical_json(payload: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ApprovalContextError("Approval context is not JSON serializable.") from exc


@dataclass(frozen=True)
class MutationBinding:
    """Handler-only binding data for one proposed filesystem mutation.

    This object is intentionally not part of ``ApprovalContext.to_dict``.
    It is frozen and contains only immutable scalar values.
    """

    workspace_id: str
    relative_path: str
    content_digest: str
    byte_count: int
    target_state: str
    root_identity: str = field(repr=False)
    parent_identity: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.workspace_id, str) or not _WORKSPACE_ID.fullmatch(
            self.workspace_id
        ):
            raise ApprovalContextError(
                "workspace_id must be 1-64 letters, digits, dots, underscores, or hyphens."
            )
        object.__setattr__(
            self, "relative_path", canonicalize_relative_path(self.relative_path)
        )
        object.__setattr__(self, "content_digest", _normalize_digest(self.content_digest))
        object.__setattr__(self, "byte_count", _normalize_byte_count(self.byte_count))
        if not isinstance(self.target_state, str):
            raise ApprovalContextError("target_state must be a string.")
        target_state = self.target_state.strip().lower()
        if not _TARGET_STATE.fullmatch(target_state):
            raise ApprovalContextError(
                "target_state must be a short lowercase operation-state token."
            )
        object.__setattr__(self, "target_state", target_state)
        object.__setattr__(
            self,
            "root_identity",
            _normalize_identity(self.root_identity, name="root_identity"),
        )
        object.__setattr__(
            self,
            "parent_identity",
            _normalize_identity(self.parent_identity, name="parent_identity"),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MutationBinding":
        if not isinstance(value, Mapping):
            raise ApprovalContextError("Mutation binding must be a mapping or instance.")
        required = (
            "workspace_id",
            "relative_path",
            "content_digest",
            "byte_count",
            "target_state",
            "root_identity",
            "parent_identity",
        )
        missing = [key for key in required if key not in value]
        if missing:
            raise ApprovalContextError(
                "Mutation binding is missing required fields: " + ", ".join(missing)
            )
        extra = [key for key in value if key not in required]
        if extra:
            raise ApprovalContextError(
                "Mutation binding contains unexpected fields: " + ", ".join(extra)
            )
        return cls(**{key: value[key] for key in required})

    def _canonical_dict(self) -> dict[str, Any]:
        """Full private payload used only for fingerprinting and handlers."""
        return {
            "workspace_id": self.workspace_id,
            "relative_path": self.relative_path,
            "content_digest": self.content_digest,
            "byte_count": self.byte_count,
            "target_state": self.target_state,
            "root_identity": self.root_identity,
            "parent_identity": self.parent_identity,
        }


@dataclass(frozen=True)
class ApprovalDisplay:
    """Bounded, UI-safe text shown in an approval request.

    Display text is deliberately separate from the private binding.  Tool
    authors must not place file contents or absolute roots in these fields.
    """

    title: str
    summary: str = ""
    preview: str = ""
    notice: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "title",
            _safe_text(
                self.title,
                name="display.title",
                max_chars=MAX_APPROVAL_DISPLAY_TITLE_CHARS,
            ),
        )
        object.__setattr__(
            self,
            "summary",
            _safe_text(
                self.summary,
                name="display.summary",
                max_chars=MAX_APPROVAL_DISPLAY_SUMMARY_CHARS,
                allow_empty=True,
            ),
        )
        object.__setattr__(
            self,
            "preview",
            _safe_preview(
                self.preview,
                name="display.preview",
                max_chars=MAX_APPROVAL_DISPLAY_PREVIEW_CHARS,
            ),
        )
        object.__setattr__(
            self,
            "notice",
            _safe_text(
                self.notice,
                name="display.notice",
                max_chars=MAX_APPROVAL_DISPLAY_NOTICE_CHARS,
                allow_empty=True,
            ),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ApprovalDisplay":
        if not isinstance(value, Mapping):
            raise ApprovalContextError("Approval display must be a mapping or instance.")
        extra = [
            key
            for key in value
            if key not in ("title", "summary", "preview", "notice")
        ]
        if extra:
            raise ApprovalContextError(
                "Approval display contains unexpected fields: " + ", ".join(extra)
            )
        if "title" not in value:
            raise ApprovalContextError("Approval display is missing a title.")
        return cls(
            title=value["title"],
            summary=value.get("summary", ""),
            preview=value.get("preview", ""),
            notice=value.get("notice", ""),
        )


@dataclass(frozen=True)
class ApprovalContext:
    """Immutable proof material for exactly one proposed mutation.

    The executor creates this object from trusted call/turn identifiers and the
    preflight result.  A handler can compare its grant fingerprint against
    ``fingerprint`` and reject any mismatch.
    """

    operation_kind: str
    tool_name: str
    turn_id: str
    tool_call_id: str
    display: ApprovalDisplay
    binding: MutationBinding = field(repr=False)
    context_version: int = APPROVAL_CONTEXT_VERSION
    fingerprint: str = field(init=False, compare=True)

    def __post_init__(self) -> None:
        if not isinstance(self.context_version, int) or isinstance(
            self.context_version, bool
        ):
            raise ApprovalContextError("context_version must be an integer.")
        if self.context_version != _CONTEXT_VERSION:
            raise ApprovalContextError(
                f"Unsupported approval context version: {self.context_version}."
            )

        if not isinstance(self.operation_kind, str):
            raise ApprovalContextError("operation_kind must be a string.")
        operation_kind = self.operation_kind.strip().lower()
        if not _OPERATION_KIND.fullmatch(operation_kind):
            raise ApprovalContextError(
                "operation_kind must be a short lowercase operation token."
            )
        object.__setattr__(self, "operation_kind", operation_kind)

        if not isinstance(self.tool_name, str) or not _TOOL_NAME.fullmatch(
            self.tool_name.strip()
        ):
            raise ApprovalContextError("tool_name is malformed.")
        object.__setattr__(self, "tool_name", self.tool_name.strip())

        for name, value in (
            ("turn_id", self.turn_id),
            ("tool_call_id", self.tool_call_id),
        ):
            if not isinstance(value, str) or not _CALL_ID.fullmatch(value.strip()):
                raise ApprovalContextError(f"{name} is malformed or missing.")
            object.__setattr__(self, name, value.strip())

        display = self.display
        if isinstance(display, Mapping):
            display = ApprovalDisplay.from_mapping(display)
        if not isinstance(display, ApprovalDisplay):
            raise ApprovalContextError("display must be an ApprovalDisplay or mapping.")
        object.__setattr__(self, "display", display)

        binding = self.binding
        if isinstance(binding, Mapping):
            binding = MutationBinding.from_mapping(binding)
        if not isinstance(binding, MutationBinding):
            raise ApprovalContextError(
                "binding must be a MutationBinding or mapping."
            )
        object.__setattr__(self, "binding", binding)

        if binding.workspace_id == "":
            raise ApprovalContextError("workspace_id cannot be empty.")

        fingerprint_payload = {
            "context_version": self.context_version,
            "operation_kind": self.operation_kind,
            "tool_name": self.tool_name,
            "turn_id": self.turn_id,
            "tool_call_id": self.tool_call_id,
            "binding": binding._canonical_dict(),
        }
        serialized = _canonical_json(fingerprint_payload)
        encoded = serialized.encode("utf-8")
        if len(encoded) > MAX_APPROVAL_CONTEXT_BYTES:
            raise ApprovalContextError(
                "Approval context exceeds the maximum serialized size."
            )

        object.__setattr__(
            self,
            "fingerprint",
            hashlib.sha256(encoded).hexdigest(),
        )

        # Public display data is bounded independently of the private payload.
        public_serialized = _canonical_json(self._public_payload())
        if len(public_serialized.encode("utf-8")) > MAX_APPROVAL_CONTEXT_BYTES:
            raise ApprovalContextError(
                "Approval context display exceeds the maximum serialized size."
            )

    @property
    def kind(self) -> str:
        """Alias used by UI/approval consumers."""
        return self.operation_kind

    @property
    def version(self) -> int:
        return self.context_version

    @property
    def operation_id(self) -> str:
        """Stable, non-secret correlation ID for one approved operation."""
        return hashlib.sha256(
            ("kivo-mutation-op:" + self.fingerprint).encode("utf-8")
        ).hexdigest()[:32]

    def _public_payload(self) -> dict[str, Any]:
        return {
            "context_version": self.context_version,
            "operation_id": self.operation_id,
            "operation_kind": self.operation_kind,
            "tool_name": self.tool_name,
            "turn_id": self.turn_id,
            "tool_call_id": self.tool_call_id,
            "display": {
                "title": self.display.title,
                "summary": self.display.summary,
                "preview": self.display.preview,
                "notice": self.display.notice,
            },
        }

    def _fingerprint_payload(self) -> dict[str, Any]:
        return {
            "context_version": self.context_version,
            "operation_kind": self.operation_kind,
            "tool_name": self.tool_name,
            "turn_id": self.turn_id,
            "tool_call_id": self.tool_call_id,
            "binding": self.binding._canonical_dict(),
        }

    def _canonical_fingerprint_json(self) -> str:
        """Private canonical payload used for the fingerprint (handler-only)."""
        return _canonical_json(self._fingerprint_payload())

    def to_dict(self) -> dict[str, Any]:
        """Return the UI/IPC-safe representation without private binding data."""
        return self._public_payload()

    def to_json(self) -> str:
        """Deterministic public JSON serialization."""
        return _canonical_json(self.to_dict())

    def canonical_json(self) -> str:
        """Deterministic public JSON, safe to hand to UI/approval transports."""
        return self.to_json()

    def to_approval_details(self) -> dict[str, Any]:
        """Bounded metadata for an approval card.

        Root identity, parent identity, the approval fingerprint, approval
        tokens, grants, file contents, and absolute roots are deliberately
        excluded.  Only the display-safe operation description remains.
        """
        return {
            "context_version": self.context_version,
            "operation_id": self.operation_id,
            "operation_kind": self.operation_kind,
            "workspace_id": self.binding.workspace_id,
            "relative_path": self.binding.relative_path or ".",
            "content_digest": self.binding.content_digest,
            "byte_count": self.binding.byte_count,
            "target_state": self.binding.target_state,
            "title": self.display.title,
            "summary": self.display.summary,
            "preview": self.display.preview,
            "notice": self.display.notice,
            "no_overwrite": True,
        }

    def verify_fingerprint(self, candidate: Any) -> bool:
        """Constant-time fingerprint comparison; malformed input is a mismatch."""
        if not isinstance(candidate, str):
            return False
        return hmac.compare_digest(self.fingerprint, candidate.strip().lower())

    def require_grant(
        self,
        grant: Optional[ExecutionGrant],
        *,
        tool_name: str,
        turn_id: str,
        tool_call_id: str,
    ) -> Optional[str]:
        """Validate a mutation grant against this exact context."""
        return validate_mutation_grant(
            grant,
            tool_name=tool_name,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            context=self,
        )


@dataclass(frozen=True)
class MutationPreflight:
    """Value returned by a mutating tool's preflight callback.

    Preflight runs before any approval card is created.  If it fails or returns
    no binding, the executor fails closed and no approval is requested.
    """

    operation_kind: str
    display: ApprovalDisplay
    binding: MutationBinding

    def __post_init__(self) -> None:
        if not isinstance(self.operation_kind, str):
            raise ApprovalContextError("operation_kind must be a string.")
        operation_kind = self.operation_kind.strip().lower()
        if not _OPERATION_KIND.fullmatch(operation_kind):
            raise ApprovalContextError(
                "operation_kind must be a short lowercase operation token."
            )
        object.__setattr__(self, "operation_kind", operation_kind)

        display = self.display
        if isinstance(display, Mapping):
            display = ApprovalDisplay.from_mapping(display)
        if not isinstance(display, ApprovalDisplay):
            raise ApprovalContextError("display must be an ApprovalDisplay or mapping.")
        object.__setattr__(self, "display", display)

        binding = self.binding
        if isinstance(binding, Mapping):
            binding = MutationBinding.from_mapping(binding)
        if not isinstance(binding, MutationBinding):
            raise ApprovalContextError(
                "binding must be a MutationBinding or mapping."
            )
        object.__setattr__(self, "binding", binding)

    @classmethod
    def from_value(cls, value: Any) -> "MutationPreflight":
        if isinstance(value, cls):
            return value
        if isinstance(value, ApprovalContext):
            return cls(
                operation_kind=value.operation_kind,
                display=value.display,
                binding=value.binding,
            )
        if isinstance(value, Mapping):
            if "approval_context" in value:
                return cls.from_value(value["approval_context"])
            required = ("operation_kind", "display", "binding")
            missing = [key for key in required if key not in value]
            if missing:
                raise ApprovalContextError(
                    "Mutation preflight is missing required fields: "
                    + ", ".join(missing)
                )
            return cls(
                operation_kind=value["operation_kind"],
                display=value["display"],
                binding=value["binding"],
            )
        raise ApprovalContextError(
            "Mutating tool preflight must return a MutationPreflight."
        )

    def bind(
        self,
        *,
        tool_name: str,
        turn_id: str,
        tool_call_id: str,
    ) -> ApprovalContext:
        return ApprovalContext(
            operation_kind=self.operation_kind,
            tool_name=tool_name,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            display=self.display,
            binding=self.binding,
        )


def validate_mutation_grant(
    grant: Optional[ExecutionGrant],
    *,
    tool_name: str,
    turn_id: str,
    tool_call_id: str,
    context: Optional[ApprovalContext] = None,
) -> Optional[str]:
    """Return an error string if a mutation grant is forged or incomplete.

    Read-only callers must not use this helper; it deliberately requires an
    explicit approval decision and fingerprint.
    """
    if not isinstance(grant, ExecutionGrant):
        return "Mutation authorization grant is missing or has the wrong type."
    if grant.authorized is not True:
        return "Mutation authorization grant is not authorized."
    if grant.approved is not True:
        return "Mutation authorization grant is missing explicit approval."
    if grant.decision != "require_approval":
        return "Mutation authorization grant has the wrong decision."
    if not isinstance(grant.tool_name, str) or not grant.tool_name:
        return "Mutation authorization grant is missing a tool name."
    if not isinstance(grant.tool_call_id, str) or not grant.tool_call_id:
        return "Mutation authorization grant is missing a tool-call identity."
    if not isinstance(grant.turn_id, str) or not grant.turn_id:
        return "Mutation authorization grant is missing a turn identity."
    if grant.tool_name != tool_name:
        return "Mutation authorization grant is bound to a different tool."
    if grant.turn_id != turn_id:
        return "Mutation authorization grant is bound to a different turn."
    if grant.tool_call_id != tool_call_id:
        return "Mutation authorization grant is bound to a different tool call."

    fingerprint = grant.approval_fingerprint
    if not isinstance(fingerprint, str) or not _SHA256_HEX.fullmatch(fingerprint):
        return "Mutation authorization grant has an invalid approval fingerprint."

    if context is not None:
        if context.tool_name != tool_name:
            return "Approval context is bound to a different tool."
        if context.turn_id != turn_id:
            return "Approval context is bound to a different turn."
        if context.tool_call_id != tool_call_id:
            return "Approval context is bound to a different tool call."
        if not hmac.compare_digest(context.fingerprint, fingerprint):
            return "Approval fingerprint does not match the approval context."
    return None


__all__ = [
    "APPROVAL_CONTEXT_VERSION",
    "MAX_APPROVAL_CONTEXT_BYTES",
    "MAX_APPROVAL_DISPLAY_NOTICE_CHARS",
    "MAX_APPROVAL_DISPLAY_PREVIEW_CHARS",
    "MAX_APPROVAL_DISPLAY_SUMMARY_CHARS",
    "MAX_APPROVAL_DISPLAY_TITLE_CHARS",
    "ApprovalContext",
    "ApprovalContextError",
    "ApprovalDisplay",
    "MutationBinding",
    "MutationPreflight",
    "canonicalize_relative_path",
    "validate_mutation_grant",
]
