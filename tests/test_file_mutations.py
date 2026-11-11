"""Stage 11.3.1b tests for secure, approval-bound text-file creation.

The tool is intentionally unregistered.  These tests exercise the service,
preflight, handler, and atomic creation path directly, plus one executor/gate
integration that uses a local test registry.
"""
from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from core.action_policy import clear_audit_log, get_audit_log
from kivo.agent import create_default_tool_registry
from kivo.agent.approval_gate import ApprovalGate
from kivo.agent.mutation_audit import record_mutation_event
from kivo.agent.mutation_lifecycle import (
    DURABILITY_SYNCED,
    MutationLifecycle,
)
from kivo.agent.tool_executor import ToolExecutor
from kivo.agent.tool_registry import ToolRegistry
from kivo.agent.tool_schema import (
    ExecutionGrant,
    PermissionLevel,
    ToolCall,
)
from kivo.conversation.context_manager import ContextManager
from kivo.conversation.conversation_manager import ConversationManager
from kivo.conversation.models import ProviderStatus
from kivo.conversation.providers.base import LLMProvider
from kivo.files import mutation_service
from kivo.files.mutation_service import (
    FileMutationError,
    FileMutationService,
    MAX_CREATE_CONTENT_BYTES,
)
from kivo.files.mutation_tools import (
    CREATE_TEXT_FILE_TOOL_NAME,
    create_text_file_tool,
)
from kivo.files.service import (
    INTERNAL_TEMP_PREFIX,
    FileIntelligenceError,
    FileIntelligenceService,
)
from kivo.files.tools import BoundedFileWorker

MUTATION_TOOL = CREATE_TEXT_FILE_TOOL_NAME


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class MutationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "workspace"
        self.root.mkdir()
        self.docs = self.root / "docs"
        self.docs.mkdir()
        self.config_path = self.tmp / "workspaces.json"
        self._write_config(
            {"demo": {"root": str(self.root), "mutation_enabled": True}}
        )
        self.service = self._service()
        self.read_service = FileIntelligenceService(config_path=self.config_path)
        clear_audit_log()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_config(self, workspaces: dict[str, object]) -> None:
        self.config_path.write_text(
            json.dumps({"version": 1, "workspaces": workspaces}),
            encoding="utf-8",
        )

    def _write_raw_config(self, raw: str) -> None:
        self.config_path.write_text(raw, encoding="utf-8")

    def _service(self, **kwargs) -> FileMutationService:
        return FileMutationService(config_path=self.config_path, **kwargs)

    def _bind(self, relative_path: str = "docs/note.txt", content: str = "hello"):
        return self.service.preflight("demo", relative_path, content).binding

    def _create(
        self,
        relative_path: str = "docs/note.txt",
        content: str = "hello",
        *,
        binding=None,
        cancel_event=None,
        lifecycle=None,
    ):
        approved = binding if binding is not None else self._bind(
            relative_path, content
        )
        return self.service.create_text_file(
            "demo",
            relative_path,
            content,
            approved,
            cancel_event=cancel_event,
            lifecycle=lifecycle,
        )

    def _target(self, relative_path: str = "docs/note.txt") -> Path:
        return self.root / relative_path

    def _temp_entries(self) -> list[str]:
        found: list[str] = []
        for base, _dirs, files in os.walk(self.root):
            for name in files:
                if name.startswith(INTERNAL_TEMP_PREFIX):
                    found.append(os.path.join(base, name))
        return sorted(found)

    def _assert_not_created(self, relative_path: str = "docs/note.txt") -> None:
        self.assertFalse(self._target(relative_path).exists())
        self.assertEqual(self._temp_entries(), [])

    def _context(self, relative_path: str = "docs/note.txt", content: str = "hello"):
        preflight = self.service.preflight("demo", relative_path, content)
        return preflight.bind(
            tool_name=MUTATION_TOOL,
            turn_id="turn-handler-1",
            tool_call_id="call-handler-1",
        )

    def _lifecycle(
        self,
        relative_path: str = "docs/note.txt",
        content: str = "hello",
    ) -> MutationLifecycle:
        lifecycle = MutationLifecycle(self._context(relative_path, content))
        lifecycle.mark_approved()
        self.assertTrue(lifecycle.start())
        return lifecycle

    def _grant(self, context, **overrides) -> ExecutionGrant:
        values = {
            "turn_id": context.turn_id,
            "tool_call_id": context.tool_call_id,
            "tool_name": context.tool_name,
            "decision": "require_approval",
            "authorized": True,
            "approved": True,
            "approval_fingerprint": context.fingerprint,
        }
        values.update(overrides)
        return ExecutionGrant(**values)

    def _invoke_handler(
        self,
        tool,
        *,
        relative_path: str = "docs/note.txt",
        content: str = "hello",
        context=None,
        grant=None,
        tool_call_id: str = "call-handler-1",
        turn_id: str = "turn-handler-1",
    ):
        ctx = context if context is not None else self._context(
            relative_path, content
        )
        approved = grant if grant is not None else self._grant(ctx)
        arguments = {
            "workspace_id": "demo",
            "relative_path": relative_path,
            "content": content,
        }
        return asyncio.run(
            tool.handler(
                arguments,
                tool_call_id=tool_call_id,
                execution_grant=approved,
                turn_id=turn_id,
                approval_context=ctx,
            )
        )


class CreationSuccessTests(MutationTestCase):
    def test_successful_creation_returns_explicit_verified_result(self):
        result = self._create(content="hello")
        target = self._target()
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "created")
        self.assertTrue(result["verified"])
        self.assertTrue(result["committed"])
        self.assertEqual(result["relative_path"], "docs/note.txt")
        self.assertEqual(result["byte_count"], 5)
        self.assertEqual(result["content_digest"], _sha(b"hello"))
        self.assertEqual(target.read_bytes(), b"hello")
        self.assertEqual(self._temp_entries(), [])

    def test_final_file_identity_mode_owner_link_and_hash(self):
        result = self._create(content="verified content")
        target = self._target()
        st = target.stat()
        self.assertTrue(stat.S_ISREG(st.st_mode))
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        self.assertEqual(st.st_uid, os.getuid())
        self.assertEqual(st.st_nlink, 1)
        self.assertEqual(st.st_size, len(b"verified content"))
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(),
                         result["content_digest"])

    def test_empty_content_is_accepted(self):
        result = self._create(content="")
        self.assertTrue(result["ok"])
        self.assertEqual(result["byte_count"], 0)
        self.assertEqual(self._target().read_bytes(), b"")

    def test_exact_8192_byte_boundary_is_accepted(self):
        content = "a" * MAX_CREATE_CONTENT_BYTES
        result = self._create(content=content)
        self.assertTrue(result["ok"])
        self.assertEqual(result["byte_count"], MAX_CREATE_CONTENT_BYTES)
        self.assertEqual(self._target().read_bytes(), content.encode("utf-8"))

    def test_8193_byte_content_is_rejected(self):
        content = "a" * (MAX_CREATE_CONTENT_BYTES + 1)
        with self.assertRaises(FileMutationError) as raised:
            self.service.preflight("demo", "docs/note.txt", content)
        self.assertEqual(raised.exception.code, "content_too_large")
        self._assert_not_created()

    def test_unicode_and_crlf_are_preserved_exactly(self):
        content = "héllo\r\n世界\n\ttabbed\r\n"
        result = self._create(content=content)
        self.assertTrue(result["ok"])
        self.assertEqual(
            self._target().read_bytes(), content.encode("utf-8")
        )

    def test_nested_existing_parent_is_used_without_creating_directories(self):
        nested = self.root / "a" / "b"
        nested.mkdir(parents=True)
        result = self._create("a/b/deep.txt", "nested")
        self.assertTrue(result["ok"])
        self.assertEqual((nested / "deep.txt").read_text(encoding="utf-8"), "nested")

    def test_missing_parent_is_rejected_and_not_created(self):
        with self.assertRaises(FileMutationError) as raised:
            self.service.preflight("demo", "missing/note.txt", "hello")
        self.assertEqual(raised.exception.code, "path_not_found")
        self.assertFalse((self.root / "missing").exists())
        self._assert_not_created("missing/note.txt")


class ContentValidationTests(MutationTestCase):
    def test_nul_bytes_are_rejected(self):
        with self.assertRaises(FileMutationError) as raised:
            self.service.preflight("demo", "docs/note.txt", "a\x00b")
        self.assertEqual(raised.exception.code, "nul_byte")
        self._assert_not_created()

    def test_lone_surrogate_is_rejected(self):
        with self.assertRaises(FileMutationError) as raised:
            self.service.preflight("demo", "docs/note.txt", "\ud800")
        self.assertEqual(raised.exception.code, "invalid_utf8")
        self._assert_not_created()

    def test_non_string_content_is_rejected(self):
        for content in (None, b"bytes", 123, ["x"]):
            with self.subTest(content=content):
                with self.assertRaises(FileMutationError) as raised:
                    self.service.preflight("demo", "docs/note.txt", content)
                self.assertEqual(raised.exception.code, "invalid_content")
        self._assert_not_created()

    def test_content_is_not_newline_normalized(self):
        content = "one\r\ntwo\nthree\r"
        self._create(content=content)
        self.assertEqual(self._target().read_bytes(), content.encode("utf-8"))


class PathValidationTests(MutationTestCase):
    def test_absolute_paths_are_rejected(self):
        for path in ("/etc/passwd", "/", "//tmp/x", "C:\\Windows\\win.ini"):
            with self.subTest(path=path):
                with self.assertRaises(FileMutationError) as raised:
                    self.service.preflight("demo", path, "x")
                self.assertEqual(raised.exception.code, "invalid_path")

    def test_traversal_and_empty_components_are_rejected(self):
        for path in (
            "",
            ".",
            "..",
            "../outside",
            "docs/../outside",
            "docs//note.txt",
            "./docs/note.txt",
            "docs/note.txt/",
        ):
            with self.subTest(path=path):
                with self.assertRaises(FileMutationError) as raised:
                    self.service.preflight("demo", path, "x")
                self.assertEqual(raised.exception.code, "invalid_path")

    def test_backslashes_and_controls_are_rejected(self):
        for path in (
            "docs\\note.txt",
            "docs/note\x00.txt",
            "docs/note\n.txt",
            "docs/note\t.txt",
            "docs/note\x7f.txt",
            "docs/\u202enote.txt",
        ):
            with self.subTest(path=path):
                with self.assertRaises(FileMutationError) as raised:
                    self.service.preflight("demo", path, "x")
                self.assertEqual(raised.exception.code, "invalid_path")

    def test_component_over_255_bytes_is_rejected(self):
        for component in ("a" * 256, "é" * 128):
            with self.subTest(bytes=len(component.encode("utf-8"))):
                with self.assertRaises(FileMutationError) as raised:
                    self.service.preflight(
                        "demo", f"docs/{component}", "x"
                    )
                self.assertEqual(raised.exception.code, "invalid_path")

    def test_protected_and_credential_paths_are_rejected(self):
        for path in (
            "docs/secret.txt",
            "docs/credentials.json",
            "docs/password.txt",
            "docs/api_key.txt",
            "docs/id_rsa",
            "docs/key.pem",
            "docs/.env",
            ".ssh/config",
            ".git/config",
            ".config/kivo/workspaces.json",
            ".local/share/kivo/state.json",
        ):
            with self.subTest(path=path):
                with self.assertRaises(FileMutationError) as raised:
                    self.service.preflight("demo", path, "x")
                self.assertEqual(raised.exception.code, "sensitive_path")

    def test_internal_temporary_prefix_is_rejected_as_a_target(self):
        with self.assertRaises(FileMutationError) as raised:
            self.service.preflight(
                "demo", f"docs/{INTERNAL_TEMP_PREFIX}mine", "x"
            )
        self.assertEqual(raised.exception.code, "sensitive_path")

    def test_malformed_workspace_identifiers_are_rejected(self):
        for workspace_id in ("", "..", "../demo", "demo/other", "a" * 65):
            with self.subTest(workspace_id=workspace_id):
                with self.assertRaises(FileMutationError) as raised:
                    self.service.preflight(
                        workspace_id, "docs/note.txt", "x"
                    )
                self.assertEqual(raised.exception.code, "invalid_workspace_id")

    def test_legacy_string_workspace_is_read_only(self):
        self._write_config({"demo": str(self.root)})
        service = self._service()
        with self.assertRaises(FileMutationError) as raised:
            service.preflight("demo", "docs/note.txt", "x")
        self.assertEqual(
            raised.exception.code, "workspace_not_mutation_eligible"
        )
        self.assertFalse(service.is_mutation_eligible("demo"))
        self._assert_not_created()

    def test_object_workspace_without_explicit_opt_in_is_read_only(self):
        for entry in (
            {"root": str(self.root)},
            {"root": str(self.root), "mutation_enabled": False},
        ):
            with self.subTest(entry=entry):
                self._write_config({"demo": entry})
                service = self._service()
                with self.assertRaises(FileMutationError) as raised:
                    service.preflight("demo", "docs/note.txt", "x")
                self.assertEqual(
                    raised.exception.code, "workspace_not_mutation_eligible"
                )
                self.assertFalse(service.is_mutation_eligible("demo"))
                self._assert_not_created()

    def test_explicit_object_opt_in_enables_mutation(self):
        self.assertTrue(self.service.is_mutation_eligible("demo"))
        preflight = self.service.preflight("demo", "docs/note.txt", "x")
        self.assertEqual(preflight.binding.workspace_id, "demo")

    def test_code_allowlist_is_an_additional_restriction(self):
        disabled = self._service(mutation_workspace_ids=frozenset())
        with self.assertRaises(FileMutationError) as raised:
            disabled.preflight("demo", "docs/note.txt", "x")
        self.assertEqual(
            raised.exception.code, "workspace_not_mutation_eligible"
        )
        self.assertFalse(disabled.is_mutation_eligible("demo"))
        self._assert_not_created()

        narrowed = self._service(mutation_workspace_ids={"other"})
        self.assertFalse(narrowed.is_mutation_eligible("demo"))

    def test_unknown_workspace_config_field_fails_closed(self):
        self._write_config(
            {
                "demo": {
                    "root": str(self.root),
                    "mutation_enabled": True,
                    "allow_write": True,
                }
            }
        )
        with self.assertRaises(FileMutationError) as raised:
            self._service().preflight("demo", "docs/note.txt", "x")
        self.assertEqual(raised.exception.code, "config_error")
        self._assert_not_created()

    def test_non_boolean_mutation_flag_fails_closed(self):
        for value in ("true", 1, 0, None, [], {}):
            with self.subTest(value=value):
                self._write_config(
                    {
                        "demo": {
                            "root": str(self.root),
                            "mutation_enabled": value,
                        }
                    }
                )
                with self.assertRaises(FileMutationError) as raised:
                    self._service().preflight("demo", "docs/note.txt", "x")
                self.assertEqual(raised.exception.code, "config_error")
                self._assert_not_created()

    def test_missing_root_fails_closed(self):
        self._write_config({"demo": {"mutation_enabled": True}})
        with self.assertRaises(FileMutationError) as raised:
            self._service().preflight("demo", "docs/note.txt", "x")
        self.assertEqual(raised.exception.code, "config_error")

    def test_duplicate_json_keys_fail_closed(self):
        self._write_raw_config(
            json.dumps(
                {
                    "version": 1,
                    "workspaces": {
                        "demo": {
                            "root": str(self.root),
                            "mutation_enabled": True,
                        }
                    },
                }
            ).replace(
                '"mutation_enabled": true',
                '"mutation_enabled": true, "mutation_enabled": false',
            )
        )
        with self.assertRaises(FileMutationError) as raised:
            self._service().preflight("demo", "docs/note.txt", "x")
        self.assertEqual(raised.exception.code, "config_error")

    def test_ambiguous_case_folded_workspace_ids_fail_closed(self):
        other = self.tmp / "other"
        other.mkdir()
        self._write_config(
            {
                "demo": {"root": str(self.root), "mutation_enabled": True},
                "Demo": {"root": str(other), "mutation_enabled": True},
            }
        )
        with self.assertRaises(FileMutationError) as raised:
            self._service().preflight("demo", "docs/note.txt", "x")
        self.assertEqual(raised.exception.code, "config_error")

    def test_workspace_permission_is_not_a_tool_argument(self):
        self.assertNotIn(
            "mutation_enabled",
            create_text_file_tool(self.service).parameters["properties"],
        )
        self.assertFalse(
            create_text_file_tool(self.service).parameters[
                "additionalProperties"
            ]
        )

    def test_unknown_configured_workspace_is_rejected(self):
        with self.assertRaises(FileMutationError) as raised:
            self.service.preflight("other", "docs/note.txt", "x")
        self.assertEqual(raised.exception.code, "unknown_workspace")


class CollisionSafetyTests(MutationTestCase):
    def test_existing_regular_file_is_never_overwritten(self):
        target = self._target()
        target.write_bytes(b"original")
        with self.assertRaises(FileMutationError) as raised:
            self.service.preflight("demo", "docs/note.txt", "replacement")
        self.assertEqual(raised.exception.code, "target_exists")
        self.assertEqual(target.read_bytes(), b"original")

    def test_existing_directory_is_never_overwritten(self):
        self._target().mkdir()
        with self.assertRaises(FileMutationError) as raised:
            self.service.preflight("demo", "docs/note.txt", "x")
        self.assertEqual(raised.exception.code, "target_exists")
        self.assertTrue(self._target().is_dir())

    def test_existing_symlink_is_never_overwritten(self):
        outside = self.tmp / "outside.txt"
        outside.write_bytes(b"outside")
        self._target().symlink_to(outside)
        with self.assertRaises(FileMutationError) as raised:
            self.service.preflight("demo", "docs/note.txt", "x")
        self.assertEqual(raised.exception.code, "target_exists")
        self.assertTrue(self._target().is_symlink())
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_existing_fifo_is_never_overwritten(self):
        os.mkfifo(self._target())
        with self.assertRaises(FileMutationError) as raised:
            self.service.preflight("demo", "docs/note.txt", "x")
        self.assertEqual(raised.exception.code, "target_exists")
        self.assertTrue(stat.S_ISFIFO(self._target().lstat().st_mode))

    def test_existing_socket_is_never_overwritten(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(self._target()))
            with self.assertRaises(FileMutationError) as raised:
                self.service.preflight("demo", "docs/note.txt", "x")
            self.assertEqual(raised.exception.code, "target_exists")
            self.assertTrue(stat.S_ISSOCK(self._target().lstat().st_mode))
        finally:
            sock.close()

    def test_hard_link_target_is_never_overwritten(self):
        original = self.root / "docs" / "original.txt"
        original.write_bytes(b"linked")
        os.link(original, self._target())
        with self.assertRaises(FileMutationError) as raised:
            self.service.preflight("demo", "docs/note.txt", "x")
        self.assertEqual(raised.exception.code, "target_exists")
        self.assertEqual(self._target().read_bytes(), b"linked")
        self.assertEqual(self._target().stat().st_nlink, 2)

    def test_target_created_after_preflight_is_rejected(self):
        binding = self._bind()
        target = self._target()
        target.write_bytes(b"raced")
        with self.assertRaises(FileMutationError) as raised:
            self._create(binding=binding)
        self.assertEqual(raised.exception.code, "target_exists")
        self.assertEqual(target.read_bytes(), b"raced")
        self.assertEqual(self._temp_entries(), [])


class WorkspaceSafetyTests(MutationTestCase):
    def test_missing_workspace_config_fails_closed(self):
        service = FileMutationService(
            config_path=self.tmp / "missing.json",
        )
        with self.assertRaises(FileMutationError) as raised:
            service.preflight("demo", "docs/note.txt", "x")
        self.assertEqual(raised.exception.code, "workspace_not_configured")

    def test_wrong_mutation_root_owner_is_rejected(self):
        with patch(
            "kivo.files.mutation_service._current_uid",
            return_value=os.getuid() + 1,
        ):
            with self.assertRaises(FileMutationError) as raised:
                self.service.preflight("demo", "docs/note.txt", "x")
        self.assertEqual(raised.exception.code, "unsafe_owner")
        self._assert_not_created()

    def test_group_writable_root_is_rejected(self):
        try:
            self.root.chmod(0o775)
            with self.assertRaises(FileMutationError) as raised:
                self.service.preflight("demo", "docs/note.txt", "x")
            self.assertEqual(raised.exception.code, "unsafe_permissions")
        finally:
            self.root.chmod(0o700)

    def test_world_writable_root_is_rejected(self):
        try:
            self.root.chmod(0o777)
            with self.assertRaises(FileMutationError) as raised:
                self.service.preflight("demo", "docs/note.txt", "x")
            self.assertEqual(raised.exception.code, "unsafe_permissions")
        finally:
            self.root.chmod(0o700)

    def test_unsafe_parent_permissions_are_rejected(self):
        try:
            self.docs.chmod(0o775)
            with self.assertRaises(FileMutationError) as raised:
                self.service.preflight("demo", "docs/note.txt", "x")
            self.assertEqual(raised.exception.code, "unsafe_permissions")
            self._assert_not_created()
        finally:
            self.docs.chmod(0o700)

    def test_parent_owner_must_match_current_user(self):
        original = mutation_service._current_uid

        def fake_uid() -> int:
            return original() + 1

        with patch(
            "kivo.files.mutation_service._current_uid", side_effect=fake_uid
        ):
            with self.assertRaises(FileMutationError) as raised:
                self.service.preflight("demo", "docs/note.txt", "x")
        self.assertEqual(raised.exception.code, "unsafe_owner")

    def test_missing_root_mount_identity_fails_closed(self):
        with patch(
            "kivo.files.mutation_service._get_mount_id", return_value=None
        ):
            with self.assertRaises(FileMutationError) as raised:
                self.service.preflight("demo", "docs/note.txt", "x")
        self.assertEqual(raised.exception.code, "mount_identity_unavailable")
        self._assert_not_created()

    def test_missing_parent_mount_identity_fails_closed(self):
        with patch(
            "kivo.files.mutation_service._get_mount_id",
            side_effect=[123, None],
        ):
            with self.assertRaises(FileMutationError) as raised:
                self.service.preflight("demo", "docs/note.txt", "x")
        self.assertEqual(raised.exception.code, "mount_identity_unavailable")

    def test_mismatched_parent_mount_identity_fails_closed(self):
        with patch(
            "kivo.files.mutation_service._get_mount_id",
            side_effect=[123, 456],
        ):
            with self.assertRaises(FileMutationError) as raised:
                self.service.preflight("demo", "docs/note.txt", "x")
        self.assertEqual(raised.exception.code, "cross_device_boundary")

    def test_parent_replacement_is_detected_after_approval(self):
        binding = self._bind()
        self.docs.rename(self.root / "docs-old")
        replacement = self.root / "docs"
        replacement.mkdir()
        with self.assertRaises(FileMutationError) as raised:
            self._create(binding=binding)
        self.assertEqual(raised.exception.code, "state_changed")
        self.assertFalse((replacement / "note.txt").exists())

    def test_parent_replaced_by_symlink_is_detected_after_approval(self):
        binding = self._bind()
        self.docs.rename(self.root / "docs-old")
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.root / "docs").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(FileMutationError) as raised:
            self._create(binding=binding)
        self.assertEqual(raised.exception.code, "symlink_not_allowed")
        self.assertFalse((outside / "note.txt").exists())

    def test_root_replacement_changes_root_identity(self):
        binding = self._bind()
        self.root.rename(self.tmp / "workspace-old")
        self.root.mkdir()
        (self.root / "docs").mkdir()
        with self.assertRaises(FileMutationError) as raised:
            self._create(binding=binding)
        self.assertEqual(raised.exception.code, "state_changed")
        self.assertFalse((self.root / "docs" / "note.txt").exists())


class AtomicFaultTests(MutationTestCase):
    def test_temporary_file_is_reverified_immediately_before_install(self):
        binding = self._bind()
        real_verify = mutation_service._verify_temporary_file
        calls = {"count": 0}

        def counting_verify(fd, root_identity):
            calls["count"] += 1
            return real_verify(fd, root_identity)

        with patch(
            "kivo.files.mutation_service._verify_temporary_file",
            side_effect=counting_verify,
        ):
            result = self._create(binding=binding)

        self.assertTrue(result["ok"])
        self.assertGreaterEqual(calls["count"], 2)
        self.assertEqual(self._target().read_bytes(), b"hello")

    def test_preinstall_reverification_failure_cleans_temporary_file(self):
        binding = self._bind()
        real_verify = mutation_service._verify_temporary_file
        calls = {"count": 0}

        def fail_second_verify(fd, root_identity):
            calls["count"] += 1
            if calls["count"] == 2:
                raise FileMutationError(
                    "unsafe_link_count",
                    "The temporary file changed before installation.",
                )
            return real_verify(fd, root_identity)

        with patch(
            "kivo.files.mutation_service._verify_temporary_file",
            side_effect=fail_second_verify,
        ):
            with self.assertRaises(FileMutationError) as raised:
                self._create(binding=binding)

        self.assertEqual(raised.exception.code, "unsafe_link_count")
        self.assertFalse(raised.exception.committed)
        self._assert_not_created()

    def test_renameat2_collision_fails_without_overwrite(self):
        binding = self._bind()
        with patch(
            "kivo.files.mutation_service._renameat2_noreplace",
            side_effect=OSError(errno.EEXIST, "exists"),
        ):
            with self.assertRaises(FileMutationError) as raised:
                self._create(binding=binding)
        self.assertEqual(raised.exception.code, "target_exists")
        self._assert_not_created()

    def test_renameat2_unavailable_fails_closed(self):
        binding = self._bind()
        with patch(
            "kivo.files.mutation_service._renameat2_noreplace",
            side_effect=OSError(errno.ENOSYS, "unsupported"),
        ):
            with self.assertRaises(FileMutationError) as raised:
                self._create(binding=binding)
        self.assertEqual(raised.exception.code, "renameat2_unavailable")
        self._assert_not_created()

    def test_disk_full_write_failure_cleans_temporary_file(self):
        binding = self._bind()
        with patch(
            "kivo.files.mutation_service._write_fd",
            side_effect=OSError(errno.ENOSPC, "full"),
        ):
            with self.assertRaises(FileMutationError) as raised:
                self._create(binding=binding)
        self.assertEqual(raised.exception.code, "disk_full")
        self._assert_not_created()

    def test_partial_write_then_failure_cleans_temporary_file(self):
        binding = self._bind(content="abcdef")
        real_write = os.write
        calls = {"count": 0}

        def partial_then_fail(fd: int, data: bytes) -> int:
            calls["count"] += 1
            if calls["count"] == 1:
                return real_write(fd, data[:1])
            raise OSError(errno.EIO, "write failed")

        with patch(
            "kivo.files.mutation_service._write_fd",
            side_effect=partial_then_fail,
        ):
            with self.assertRaises(FileMutationError) as raised:
                self._create(content="abcdef", binding=binding)
        self.assertEqual(raised.exception.code, "filesystem_error")
        self._assert_not_created()

    def test_temporary_fsync_failure_cleans_temporary_file(self):
        binding = self._bind()
        with patch(
            "kivo.files.mutation_service.os.fsync",
            side_effect=OSError(errno.EIO, "fsync failed"),
        ):
            with self.assertRaises(FileMutationError) as raised:
                self._create(binding=binding)
        self.assertEqual(raised.exception.code, "fsync_failed")
        self.assertFalse(raised.exception.committed)
        self._assert_not_created()

    def test_parent_fsync_failure_reports_committed_file(self):
        binding = self._bind()
        real_fsync = os.fsync
        calls = {"count": 0}

        def fail_second(fd: int) -> None:
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError(errno.EIO, "parent fsync failed")
            return real_fsync(fd)

        with patch(
            "kivo.files.mutation_service.os.fsync", side_effect=fail_second
        ):
            with self.assertRaises(FileMutationError) as raised:
                self._create(binding=binding)
        self.assertEqual(raised.exception.code, "fsync_failed")
        self.assertTrue(raised.exception.committed)
        self.assertTrue(self._target().exists())
        self.assertEqual(self._target().read_bytes(), b"hello")
        self.assertIn("created", raised.exception.message.lower())

    def test_post_install_verification_failure_reports_committed_file(self):
        binding = self._bind()
        with patch(
            "kivo.files.mutation_service._verify_installed_file",
            side_effect=FileMutationError(
                "verification_failed",
                "The file was created, but verification failed.",
                committed=True,
            ),
        ):
            with self.assertRaises(FileMutationError) as raised:
                self._create(binding=binding)
        self.assertEqual(raised.exception.code, "verification_failed")
        self.assertTrue(raised.exception.committed)
        self.assertTrue(self._target().exists())

    def test_cancellation_before_creation_does_not_create(self):
        binding = self._bind()
        event = threading.Event()
        event.set()
        with self.assertRaises(FileMutationError) as raised:
            self._create(binding=binding, cancel_event=event)
        self.assertEqual(raised.exception.code, "cancelled")
        self.assertFalse(raised.exception.committed)
        self._assert_not_created()

    def test_cancellation_during_write_cleans_temporary_file(self):
        content = "a" * MAX_CREATE_CONTENT_BYTES
        binding = self._bind(content=content)
        event = threading.Event()
        real_write = os.write

        def write_one_byte_then_cancel(fd: int, data: bytes) -> int:
            written = real_write(fd, data[:1])
            event.set()
            return written

        with patch(
            "kivo.files.mutation_service._write_fd",
            side_effect=write_one_byte_then_cancel,
        ):
            with self.assertRaises(FileMutationError) as raised:
                self._create(
                    content=content, binding=binding, cancel_event=event
                )
        self.assertEqual(raised.exception.code, "cancelled")
        self.assertFalse(raised.exception.committed)
        self._assert_not_created()

    def test_cancellation_after_installation_reports_committed_state(self):
        binding = self._bind()
        event = threading.Event()
        real_rename = mutation_service._renameat2_noreplace

        def rename_then_cancel(*args, **kwargs):
            result = real_rename(*args, **kwargs)
            event.set()
            return result

        with patch(
            "kivo.files.mutation_service._renameat2_noreplace",
            side_effect=rename_then_cancel,
        ):
            result = self._create(binding=binding, cancel_event=event)

        self.assertTrue(result["ok"])
        self.assertTrue(result["committed"])
        self.assertTrue(result["cancelled_after_commit"])
        self.assertEqual(self._target().read_bytes(), b"hello")

    def test_concurrent_creates_for_same_target_one_wins(self):
        binding = self._bind()
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait(timeout=2.0)
            try:
                self.service.create_text_file(
                    "demo", "docs/note.txt", "hello", binding
                )
            except FileMutationError as exc:
                with lock:
                    outcomes.append(exc.code)
            else:
                with lock:
                    outcomes.append("created")

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5.0)

        self.assertEqual(sorted(outcomes), ["created", "target_exists"])
        self.assertEqual(self._target().read_bytes(), b"hello")
        self.assertEqual(self._target().stat().st_nlink, 1)
        self.assertEqual(self._temp_entries(), [])

    def test_repeated_failures_do_not_leak_descriptors(self):
        fd_dir = Path("/proc/self/fd")
        if not fd_dir.exists():
            self.skipTest("/proc/self/fd is unavailable")
        self._create("docs/existing.txt", "x")
        before = len(os.listdir(fd_dir))
        for index in range(20):
            with self.assertRaises(FileMutationError):
                self.service.preflight(
                    "demo", "docs/existing.txt", f"attempt-{index}"
                )
        for index in range(10):
            self._create(f"docs/new-{index}.txt", f"value-{index}")
        after = len(os.listdir(fd_dir))
        self.assertLessEqual(after, before + 2)

    def test_successful_creation_leaves_no_temporary_file(self):
        self._create()
        self.assertEqual(self._temp_entries(), [])


class TemporaryFileProtectionTests(MutationTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.temp_name = f"{INTERNAL_TEMP_PREFIX}orphan"
        self.temp_path = self.docs / self.temp_name
        self.temp_path.write_bytes(b"TOP-SECRET-ORPHAN")
        (self.docs / "visible.txt").write_bytes(b"visible")

    def test_directory_listing_hides_internal_temporary_files(self):
        result = self.read_service.list_directory("demo", "docs")
        names = [entry["name"] for entry in result["entries"]]
        self.assertNotIn(self.temp_name, names)
        self.assertIn("visible.txt", names)

    def test_metadata_and_read_operations_block_internal_temporary_files(self):
        for operation in (
            self.read_service.get_file_metadata,
            self.read_service.read_text_file,
        ):
            with self.subTest(operation=operation.__name__):
                with self.assertRaises(FileIntelligenceError) as raised:
                    operation("demo", f"docs/{self.temp_name}")
                self.assertEqual(raised.exception.code, "sensitive_path")
                self.assertNotIn("TOP-SECRET", raised.exception.message)

    def test_filename_search_never_returns_internal_temporary_files(self):
        result = self.read_service.search_filenames(
            "demo", "docs", "kivo"
        )
        paths = [item["relative_path"] for item in result["results"]]
        self.assertFalse(
            any(self.temp_name in path for path in paths), paths
        )

    def test_disk_usage_does_not_disclose_filenames(self):
        result = self.read_service.get_disk_usage("demo")
        serialized = json.dumps(result)
        self.assertNotIn(self.temp_name, serialized)
        self.assertNotIn("TOP-SECRET", serialized)


class HandlerApprovalTests(MutationTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.tool = create_text_file_tool(self.service)

    def test_handler_creates_only_with_valid_grant_and_context(self):
        result = self._invoke_handler(self.tool)
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        self.assertEqual(self._target().read_bytes(), b"hello")

    def test_handler_records_one_coherent_lifecycle(self):
        result = self._invoke_handler(self.tool)
        self.assertTrue(result["ok"])
        entries = [
            entry
            for entry in get_audit_log(limit=50)
            if entry.tool == MUTATION_TOOL
        ]
        statuses = [entry.outcome for entry in entries]
        for expected in (
            "approved",
            "execution_started",
            "committed",
            "verification_succeeded",
            "durability_synced",
        ):
            self.assertIn(expected, statuses)
        for status in (
            "approved",
            "execution_started",
            "committed",
            "verification_succeeded",
            "durability_synced",
        ):
            self.assertEqual(statuses.count(status), 1, status)
        self.assertNotIn("intent", statuses)
        self.assertNotIn("executed", statuses)

        operation_ids = {
            entry.parameters.get("operation_id") for entry in entries
        }
        self.assertEqual(len(operation_ids), 1)
        self.assertNotIn(None, operation_ids)
        for entry in entries:
            self.assertTrue(
                {
                    "operation_id",
                    "workspace_id",
                    "relative_path",
                    "content_digest",
                    "byte_count",
                }.issubset(entry.parameters)
            )
            serialized = json.dumps(entry.parameters)
            self.assertNotIn("hello", serialized)
            self.assertNotIn(str(self.root), serialized)

    def test_handler_rejects_missing_approval_context(self):
        context = self._context()
        grant = self._grant(context)
        result = asyncio.run(
            self.tool.handler(
                {
                    "workspace_id": "demo",
                    "relative_path": "docs/note.txt",
                    "content": "hello",
                },
                tool_call_id=context.tool_call_id,
                execution_grant=grant,
                turn_id=context.turn_id,
                approval_context=None,
            )
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "approval_context_missing")
        self.assertFalse(self._target().exists())

    def test_handler_rejects_forged_grants(self):
        variants = {
            "not_approved": {"approved": False},
            "wrong_decision": {"decision": "allow"},
            "wrong_tool": {"tool_name": "other_tool"},
            "wrong_turn": {"turn_id": "turn-other"},
            "wrong_call": {"tool_call_id": "call-other"},
            "unauthorized": {"authorized": False},
        }
        for index, (name, overrides) in enumerate(variants.items()):
            with self.subTest(name=name):
                context = self._context(
                    "docs/forged.txt", f"content-{index}"
                )
                grant = self._grant(context, **overrides)
                result = self._invoke_handler(
                    self.tool,
                    relative_path="docs/forged.txt",
                    content=f"content-{index}",
                    context=context,
                    grant=grant,
                )
                self.assertFalse(result["ok"], name)
                self.assertFalse(
                    (self.root / "docs" / "forged.txt").exists(), name
                )

    def test_handler_rejects_missing_grant(self):
        context = self._context("docs/no-grant.txt", "x")
        result = asyncio.run(
            self.tool.handler(
                {
                    "workspace_id": "demo",
                    "relative_path": "docs/no-grant.txt",
                    "content": "x",
                },
                tool_call_id=context.tool_call_id,
                execution_grant=None,
                turn_id=context.turn_id,
                approval_context=context,
            )
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "denied")
        self.assertFalse((self.root / "docs" / "no-grant.txt").exists())

    def test_handler_rejects_grant_fingerprint_mismatch(self):
        context = self._context()
        grant = self._grant(context, approval_fingerprint="0" * 64)
        result = self._invoke_handler(self.tool, context=context, grant=grant)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "denied")
        self.assertFalse(self._target().exists())

    def test_handler_rejects_arguments_that_no_longer_match_context(self):
        context = self._context("docs/note.txt", "hello")
        for changed in (
            {"content": "different"},
            {"relative_path": "docs/other.txt"},
        ):
            with self.subTest(changed=changed):
                result = self._invoke_handler(
                    self.tool,
                    relative_path=changed.get(
                        "relative_path", "docs/note.txt"
                    ),
                    content=changed.get("content", "hello"),
                    context=context,
                )
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "approval_mismatch")
                self._assert_not_created()
                self.assertFalse((self.root / "docs" / "other.txt").exists())

    def test_handler_rejects_target_created_after_preflight(self):
        context = self._context()
        self._target().write_bytes(b"raced")
        result = self._invoke_handler(self.tool, context=context)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "target_exists")
        self.assertFalse(result["committed"])
        self.assertEqual(self._target().read_bytes(), b"raced")

    def test_intent_audit_failure_prevents_creation(self):
        def audit_sink(**kwargs):
            return kwargs.get("status") != "execution_started"

        with patch(
            "kivo.agent.mutation_lifecycle.mutation_audit.record_mutation_event",
            side_effect=audit_sink,
        ):
            result = self._invoke_handler(self.tool)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "audit_unavailable")
        self.assertFalse(self._target().exists())

    def test_post_commit_audit_failure_reports_committed_state_and_warning(self):
        def audit_sink(**kwargs):
            return kwargs.get("status") != "verification_succeeded"

        with patch(
            "kivo.agent.mutation_lifecycle.mutation_audit.record_mutation_event",
            side_effect=audit_sink,
        ):
            result = self._invoke_handler(self.tool)
        self.assertTrue(result["ok"])
        self.assertTrue(self._target().exists())
        self.assertIn("audit_warning", result)
        self.assertIn("created", result["audit_warning"].lower())

    def test_handler_post_install_verification_failure_is_honest(self):
        with patch(
            "kivo.files.mutation_service._verify_installed_file",
            side_effect=FileMutationError(
                "verification_failed",
                "The file was created, but post-install verification failed.",
                committed=True,
            ),
        ):
            result = self._invoke_handler(self.tool)
        self.assertFalse(result["ok"])
        self.assertTrue(result["committed"])
        self.assertEqual(result["status"], "verification_failed")
        self.assertTrue(self._target().exists())
        self.assertIn("exists", result["committed_notice"].lower())

    def test_handler_rejects_non_dict_arguments(self):
        result = asyncio.run(
            self.tool.handler(
                "not-a-dict",
                tool_call_id="call-handler-1",
                execution_grant=None,
                turn_id="turn-handler-1",
                approval_context=None,
            )
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "invalid_arguments")

    def test_handler_works_with_bounded_worker(self):
        worker = BoundedFileWorker(name="test-mutation-worker")
        try:
            tool = create_text_file_tool(self.service, worker=worker)
            result = self._invoke_handler(tool)
            self.assertTrue(result["ok"])
            self.assertEqual(self._target().read_bytes(), b"hello")
        finally:
            worker.close(timeout_seconds=2.0)


class ExecutorIntegrationTests(MutationTestCase):
    def _executor(self, gate: ApprovalGate) -> tuple[ToolExecutor, Registry]:
        registry = ToolRegistry()
        registry.register(create_text_file_tool(self.service))
        return ToolExecutor(registry, approval_gate=gate), registry

    def test_executor_approval_flow_creates_file_and_bounded_details(self):
        gate = ApprovalGate(timeout_seconds=5.0)
        executor, _ = self._executor(gate)
        call = ToolCall(
            tool_call_id="call-exec-1",
            tool_name=MUTATION_TOOL,
            arguments={
                "workspace_id": "demo",
                "relative_path": "docs/note.txt",
                "content": "hello",
            },
        )

        async def run():
            task = asyncio.create_task(
                executor.execute(call, set(), turn_id="turn-exec-1")
            )
            pending = []
            for _ in range(300):
                pending = gate.get_pending_requests()
                if pending:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(len(pending), 1)
            request = pending[0]
            self.assertTrue(request["requires_tool_call_id"])
            self.assertEqual(request["approval_kind"], "filesystem_mutation")
            self.assertEqual(
                request["details"]["workspace_id"], "demo"
            )
            self.assertEqual(
                request["details"]["relative_path"], "docs/note.txt"
            )
            self.assertNotIn("root_identity", request["details"])
            self.assertNotIn("parent_identity", request["details"])
            self.assertEqual(request["details"]["preview"], "hello")
            self.assertEqual(request["details"]["title"], "Create text file")
            self.assertEqual(request["details"]["byte_count"], 5)
            self.assertTrue(request["details"]["no_overwrite"])
            self.assertIn(
                "never overwritten", request["details"]["notice"].lower()
            )
            self.assertRegex(
                request["details"]["operation_id"], r"^[0-9a-f]{32}$"
            )
            self.assertNotIn(str(self.root), json.dumps(request))
            self.assertNotIn("root-identity", json.dumps(request))
            self.assertNotIn("parent-identity", json.dumps(request))
            approved, message = gate.resolve(
                request["token"],
                True,
                turn_id="turn-exec-1",
                tool_call_id=call.tool_call_id,
            )
            self.assertTrue(approved)
            self.assertIn("success", message.lower())
            return await task

        result = asyncio.run(run())
        self.assertTrue(result.success, result.error_message)
        self.assertTrue(result.output["ok"])
        self.assertEqual(self._target().read_bytes(), b"hello")

    def test_preflight_failure_does_not_request_approval(self):
        gate = ApprovalGate(timeout_seconds=5.0)
        executor, _ = self._executor(gate)
        call = ToolCall(
            tool_call_id="call-exec-missing",
            tool_name=MUTATION_TOOL,
            arguments={
                "workspace_id": "demo",
                "relative_path": "missing/note.txt",
                "content": "hello",
            },
        )

        async def run():
            return await executor.execute(
                call, set(), turn_id="turn-exec-missing"
            )

        result = asyncio.run(run())
        self.assertFalse(result.success)
        self.assertEqual(gate.get_pending_requests(), [])
        self.assertFalse((self.root / "missing").exists())


class _NullProvider(LLMProvider):
    """Minimal provider so shutdown-drain tests never touch the network."""

    async def generate(self, messages, tools=None, **kwargs):
        return ""

    async def stream(self, messages, tools=None, **kwargs):
        if False:  # pragma: no cover - keeps this an async generator
            yield ""

    async def get_status(self) -> ProviderStatus:
        return ProviderStatus(provider_name="test", available=True)

    async def close(self) -> None:
        return None


class LifecycleRaceTests(MutationTestCase):
    def _create_with_lifecycle(
        self,
        lifecycle: MutationLifecycle,
        *,
        cancel_event=None,
    ):
        return self._create(
            lifecycle.context.binding.relative_path,
            "hello",
            binding=lifecycle.context.binding,
            cancel_event=cancel_event,
            lifecycle=lifecycle,
        )

    def test_cancellation_before_temporary_creation_is_uncommitted(self):
        lifecycle = self._lifecycle()
        event = threading.Event()
        event.set()
        with self.assertRaises(FileMutationError) as raised:
            self._create_with_lifecycle(lifecycle, cancel_event=event)
        self.assertEqual(raised.exception.code, "cancelled")
        snapshot = lifecycle.snapshot()
        self.assertFalse(snapshot.committed)
        self.assertFalse(snapshot.verified)
        self.assertEqual(snapshot.status, "cancelled")
        self._assert_not_created()

    def test_cancellation_during_write_is_uncommitted(self):
        lifecycle = self._lifecycle()
        event = threading.Event()
        real_write = os.write

        def write_one_byte_then_cancel(fd: int, data: bytes) -> int:
            written = real_write(fd, data[:1])
            event.set()
            return written

        with patch(
            "kivo.files.mutation_service._write_fd",
            side_effect=write_one_byte_then_cancel,
        ):
            with self.assertRaises(FileMutationError) as raised:
                self._create_with_lifecycle(lifecycle, cancel_event=event)
        self.assertEqual(raised.exception.code, "cancelled")
        snapshot = lifecycle.snapshot()
        self.assertFalse(snapshot.committed)
        self.assertEqual(snapshot.status, "cancelled")
        self._assert_not_created()

    def test_cancellation_before_rename_is_uncommitted(self):
        lifecycle = self._lifecycle()
        event = threading.Event()

        def set_cancel_before_install(cancel_event):
            event.set()

        with patch(
            "kivo.files.mutation_service._before_install",
            side_effect=set_cancel_before_install,
        ):
            with self.assertRaises(FileMutationError) as raised:
                self._create_with_lifecycle(lifecycle, cancel_event=event)
        self.assertEqual(raised.exception.code, "cancelled")
        snapshot = lifecycle.snapshot()
        self.assertFalse(snapshot.committed)
        self._assert_not_created()

    def test_cancellation_after_rename_preserves_commit(self):
        lifecycle = self._lifecycle()
        event = threading.Event()
        real_rename = mutation_service._renameat2_noreplace

        def rename_then_cancel(*args, **kwargs):
            result = real_rename(*args, **kwargs)
            event.set()
            return result

        with patch(
            "kivo.files.mutation_service._renameat2_noreplace",
            side_effect=rename_then_cancel,
        ):
            result = self._create_with_lifecycle(lifecycle, cancel_event=event)
        self.assertTrue(result["ok"])
        snapshot = lifecycle.snapshot()
        self.assertTrue(snapshot.committed)
        self.assertTrue(snapshot.verified)
        self.assertTrue(snapshot.cancelled_after_commit)
        self.assertEqual(snapshot.status, "created")
        self.assertEqual(self._target().read_bytes(), b"hello")

    def test_cancellation_during_parent_fsync_preserves_commit(self):
        lifecycle = self._lifecycle()
        event = threading.Event()
        real_fsync_parent = mutation_service._fsync_parent

        def fsync_and_cancel(fd: int) -> None:
            event.set()
            real_fsync_parent(fd)

        with patch(
            "kivo.files.mutation_service._fsync_parent",
            side_effect=fsync_and_cancel,
        ):
            result = self._create_with_lifecycle(lifecycle, cancel_event=event)
        self.assertTrue(result["ok"])
        snapshot = lifecycle.snapshot()
        self.assertTrue(snapshot.committed)
        self.assertTrue(snapshot.verified)
        self.assertEqual(snapshot.status, "created")

    def test_cancellation_during_final_verification_preserves_commit(self):
        lifecycle = self._lifecycle()
        event = threading.Event()
        real_verify = mutation_service._verify_installed_file

        def verify_and_cancel(*args, **kwargs):
            event.set()
            return real_verify(*args, **kwargs)

        with patch(
            "kivo.files.mutation_service._verify_installed_file",
            side_effect=verify_and_cancel,
        ):
            result = self._create_with_lifecycle(lifecycle, cancel_event=event)
        self.assertTrue(result["ok"])
        snapshot = lifecycle.snapshot()
        self.assertTrue(snapshot.committed)
        self.assertTrue(snapshot.verified)
        self.assertTrue(snapshot.ok)
        self.assertEqual(self._target().read_bytes(), b"hello")

    def test_failure_after_commit_cannot_erase_committed_state(self):
        lifecycle = self._lifecycle()
        result = self._create_with_lifecycle(lifecycle)
        self.assertTrue(result["ok"])
        lifecycle.record_failure(
            "verification_failed",
            "Later verification bookkeeping failed.",
            committed=False,
        )
        snapshot = lifecycle.snapshot()
        self.assertTrue(snapshot.committed)
        self.assertTrue(snapshot.verified)
        self.assertEqual(snapshot.status, "created")
        self.assertTrue(self._target().exists())

    def test_repeated_lifecycle_transitions_are_audited_once(self):
        lifecycle = self._lifecycle()
        lifecycle.mark_approved()
        self.assertTrue(lifecycle.start())
        lifecycle.mark_committed()
        lifecycle.mark_committed()
        lifecycle.mark_verification(True)
        lifecycle.mark_verification(True)
        lifecycle.mark_durability(DURABILITY_SYNCED)
        lifecycle.mark_durability(DURABILITY_SYNCED)

        entries = [
            entry
            for entry in get_audit_log(limit=50)
            if entry.tool == MUTATION_TOOL
        ]
        statuses = [entry.outcome for entry in entries]
        for expected in (
            "approved",
            "execution_started",
            "committed",
            "verification_succeeded",
            "durability_synced",
        ):
            self.assertEqual(statuses.count(expected), 1, expected)

    def test_lifecycle_registry_drains_only_committed_unreported_results(self):
        from kivo.agent.mutation_lifecycle import MutationLifecycleRegistry

        registry = MutationLifecycleRegistry()
        pending = registry.create(self._context("docs/pending.txt", "p"))
        pending.mark_approved()
        pending.start()

        committed = registry.create(self._context("docs/done.txt", "d"))
        committed.mark_approved()
        committed.start()
        committed.mark_committed()
        committed.mark_verification(True)
        committed.mark_durability(DURABILITY_SYNCED)

        drained = registry.drain_unreported("turn-handler-1")
        self.assertEqual(len(drained), 1)
        self.assertEqual(drained[0].relative_path, "docs/done.txt")
        self.assertTrue(drained[0].committed)
        self.assertTrue(drained[0].reported)
        self.assertEqual(registry.drain_unreported("turn-handler-1"), [])
        self.assertEqual(registry.drain_unreported("another-turn"), [])

    def test_shutdown_drains_committed_mutation_before_ui_result(self):
        manager = ConversationManager(
            context_manager=ContextManager(),
            provider=_NullProvider(),
            tool_registry=ToolRegistry(),
            turn_timeout_seconds=1.0,
        )
        results: list = []
        manager._on_tool_completed = lambda result: results.append(result)
        try:
            lifecycle = manager._tool_executor.mutation_lifecycles.create(
                self._context()
            )
            lifecycle.mark_approved()
            self.assertTrue(lifecycle.start())
            # Install the real file through the shared lifecycle, then close
            # before the handler can return its ToolResult to the UI.
            self._create_with_lifecycle(lifecycle)
            self.assertTrue(self._target().exists())
        finally:
            manager.close()

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].output["committed"])
        self.assertTrue(results[0].output["ok"])
        self.assertEqual(results[0].tool_name, MUTATION_TOOL)
        self.assertTrue(self._target().exists())


class ExecutorLifecycleTests(MutationTestCase):
    def _executor(self, gate: ApprovalGate):
        registry = ToolRegistry()
        tool = create_text_file_tool(self.service)
        registry.register(tool)
        return ToolExecutor(registry, approval_gate=gate), tool

    async def _wait_for_pending(self, gate: ApprovalGate, timeout: float = 2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pending = gate.get_pending_requests()
            if pending:
                return pending[0]
            await asyncio.sleep(0.01)
        self.fail("approval request never became pending")

    def _call(self, call_id: str = "call-lifecycle-1") -> ToolCall:
        return ToolCall(
            tool_call_id=call_id,
            tool_name=MUTATION_TOOL,
            arguments={
                "workspace_id": "demo",
                "relative_path": "docs/note.txt",
                "content": "hello",
            },
        )

    def test_executor_timeout_after_installation_reports_commit(self):
        gate = ApprovalGate(timeout_seconds=5.0)
        executor, tool = self._executor(gate)
        tool.timeout_seconds = 0.05
        call = self._call()
        real_verify = mutation_service._verify_installed_file

        def slow_verify(*args, **kwargs):
            time.sleep(0.2)
            return real_verify(*args, **kwargs)

        async def run():
            with patch(
                "kivo.files.mutation_service._verify_installed_file",
                side_effect=slow_verify,
            ):
                task = asyncio.create_task(
                    executor.execute(call, set(), turn_id="turn-timeout")
                )
                pending = await self._wait_for_pending(gate)
                approved, _ = gate.resolve(
                    pending["token"],
                    True,
                    turn_id="turn-timeout",
                    tool_call_id=call.tool_call_id,
                )
                self.assertTrue(approved)
                return await task

        result = asyncio.run(run())
        self.assertTrue(result.success, result.error_message)
        self.assertTrue(result.output["committed"])
        self.assertTrue(result.output["verified"])
        self.assertTrue(result.output["timed_out_after_commit"])
        self.assertEqual(self._target().read_bytes(), b"hello")

        entries = [
            entry
            for entry in get_audit_log(limit=50)
            if entry.tool == MUTATION_TOOL
        ]
        self.assertEqual(
            [entry.outcome for entry in entries].count("timed_out_after_commit"),
            1,
        )

    def test_cancellation_during_parent_fsync_drains_commit(self):
        gate = ApprovalGate(timeout_seconds=5.0)
        executor, _ = self._executor(gate)
        call = self._call("call-cancel-fsync")
        fsync_started = threading.Event()
        release = threading.Event()
        real_fsync_parent = mutation_service._fsync_parent

        def blocking_fsync(fd: int) -> None:
            fsync_started.set()
            release.wait(timeout=5.0)
            real_fsync_parent(fd)

        async def run():
            with patch(
                "kivo.files.mutation_service._fsync_parent",
                side_effect=blocking_fsync,
            ):
                task = asyncio.create_task(
                    executor.execute(call, set(), turn_id="turn-cancel-fsync")
                )
                pending = await self._wait_for_pending(gate)
                gate.resolve(
                    pending["token"],
                    True,
                    turn_id="turn-cancel-fsync",
                    tool_call_id=call.tool_call_id,
                )
                for _ in range(300):
                    if fsync_started.is_set():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(fsync_started.is_set())
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        asyncio.run(run())
        drained = executor.drain_unreported_mutation_results(
            "turn-cancel-fsync"
        )
        self.assertEqual(len(drained), 1)
        self.assertTrue(drained[0].output["committed"])
        self.assertTrue(drained[0].output["verified"])
        self.assertEqual(self._target().read_bytes(), b"hello")

    def test_cancellation_during_verification_drains_verified_commit(self):
        gate = ApprovalGate(timeout_seconds=5.0)
        executor, _ = self._executor(gate)
        call = self._call("call-cancel-verify")
        verify_started = threading.Event()
        release = threading.Event()
        real_verify = mutation_service._verify_installed_file

        def blocking_verify(*args, **kwargs):
            verify_started.set()
            release.wait(timeout=5.0)
            return real_verify(*args, **kwargs)

        async def run():
            with patch(
                "kivo.files.mutation_service._verify_installed_file",
                side_effect=blocking_verify,
            ):
                task = asyncio.create_task(
                    executor.execute(call, set(), turn_id="turn-cancel-verify")
                )
                pending = await self._wait_for_pending(gate)
                gate.resolve(
                    pending["token"],
                    True,
                    turn_id="turn-cancel-verify",
                    tool_call_id=call.tool_call_id,
                )
                for _ in range(300):
                    if verify_started.is_set():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(verify_started.is_set())
                task.cancel()
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        asyncio.run(run())
        drained = executor.drain_unreported_mutation_results(
            "turn-cancel-verify"
        )
        self.assertEqual(len(drained), 1)
        self.assertTrue(drained[0].output["committed"])
        self.assertTrue(drained[0].output["verified"])
        self.assertTrue(drained[0].output["ok"])

    def test_approval_is_bound_to_exact_turn_and_tool_call(self):
        gate = ApprovalGate(timeout_seconds=5.0)
        executor, _ = self._executor(gate)
        call = self._call("call-bound")

        async def run():
            task = asyncio.create_task(
                executor.execute(call, set(), turn_id="turn-bound")
            )
            pending = await self._wait_for_pending(gate)
            wrong_turn, message = gate.resolve(
                pending["token"],
                True,
                turn_id="turn-other",
                tool_call_id=call.tool_call_id,
            )
            self.assertFalse(wrong_turn)
            self.assertIn("turn", message.lower())
            wrong_call, message = gate.resolve(
                pending["token"],
                True,
                turn_id="turn-bound",
                tool_call_id="call-other",
            )
            self.assertFalse(wrong_call)
            self.assertIn("tool call", message.lower())
            self.assertEqual(len(gate.get_pending_requests()), 1)
            approved, _ = gate.resolve(
                pending["token"],
                True,
                turn_id="turn-bound",
                tool_call_id=call.tool_call_id,
            )
            self.assertTrue(approved)
            return await task

        result = asyncio.run(run())
        self.assertTrue(result.success, result.error_message)
        self.assertEqual(self._target().read_bytes(), b"hello")

    def test_rejected_approval_does_not_create_and_is_audited(self):
        gate = ApprovalGate(timeout_seconds=5.0)
        executor, _ = self._executor(gate)
        call = self._call("call-rejected")

        async def run():
            task = asyncio.create_task(
                executor.execute(call, set(), turn_id="turn-rejected")
            )
            pending = await self._wait_for_pending(gate)
            gate.resolve(
                pending["token"],
                False,
                turn_id="turn-rejected",
                tool_call_id=call.tool_call_id,
            )
            return await task

        result = asyncio.run(run())
        self.assertFalse(result.success)
        self._assert_not_created()
        statuses = [
            entry.outcome
            for entry in get_audit_log(limit=50)
            if entry.tool == MUTATION_TOOL
        ]
        self.assertEqual(statuses.count("approval_rejected"), 1)

    def test_expired_approval_does_not_create_and_is_audited(self):
        gate = ApprovalGate(timeout_seconds=5.0)
        executor, tool = self._executor(gate)
        tool.approval_timeout_seconds = 0.05
        result = asyncio.run(
            executor.execute(self._call("call-expired"), set(), turn_id="turn-expired")
        )
        self.assertFalse(result.success)
        self.assertIn("expired", result.error_message.lower())
        self._assert_not_created()
        statuses = [
            entry.outcome
            for entry in get_audit_log(limit=50)
            if entry.tool == MUTATION_TOOL
        ]
        self.assertEqual(statuses.count("approval_expired"), 1)

    def test_cancelled_approval_does_not_create_and_is_audited(self):
        gate = ApprovalGate(timeout_seconds=5.0)
        executor, _ = self._executor(gate)
        call = self._call("call-cancelled-approval")

        async def run():
            task = asyncio.create_task(
                executor.execute(call, set(), turn_id="turn-cancelled-approval")
            )
            await self._wait_for_pending(gate)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(run())
        self._assert_not_created()
        statuses = [
            entry.outcome
            for entry in get_audit_log(limit=50)
            if entry.tool == MUTATION_TOOL
        ]
        self.assertEqual(statuses.count("approval_cancelled"), 1)

    def test_model_supplied_mutation_permission_is_rejected(self):
        gate = ApprovalGate(timeout_seconds=5.0)
        executor, _ = self._executor(gate)
        call = ToolCall(
            tool_call_id="call-permission",
            tool_name=MUTATION_TOOL,
            arguments={
                "workspace_id": "demo",
                "relative_path": "docs/note.txt",
                "content": "hello",
                "mutation_enabled": True,
            },
        )
        result = asyncio.run(
            executor.execute(call, set(), turn_id="turn-permission")
        )
        self.assertFalse(result.success)
        self.assertIn("model-supplied", result.error_message.lower())
        self.assertEqual(gate.get_pending_requests(), [])
        self._assert_not_created()


class ApprovalSerializationTests(MutationTestCase):
    def test_approval_details_are_bounded_ui_safe_and_private(self):
        content = (
            "line one\nline two\t"
            + "\u202e"
            + "\x01"
            + ("a" * 400)
        )
        preflight = self.service.preflight(
            "demo", "docs/preview.txt", content
        )
        details = preflight.bind(
            tool_name=MUTATION_TOOL,
            turn_id="turn-preview",
            tool_call_id="call-preview",
        ).to_approval_details()

        self.assertEqual(details["workspace_id"], "demo")
        self.assertEqual(details["relative_path"], "docs/preview.txt")
        self.assertEqual(details["byte_count"], len(content.encode("utf-8")))
        self.assertTrue(details["no_overwrite"])
        self.assertIn("never overwritten", details["notice"].lower())
        self.assertLessEqual(len(details["preview"]), 240)
        self.assertLessEqual(len(details["summary"]), 400)
        self.assertLessEqual(len(details["notice"]), 240)
        self.assertNotIn("\n", details["preview"])
        self.assertNotIn("\t", details["preview"])
        self.assertNotIn("\u202e", details["preview"])
        self.assertNotIn("\x01", details["preview"])
        self.assertRegex(details["operation_id"], r"^[0-9a-f]{32}$")

        serialized = json.dumps(details)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn("root_identity", serialized)
        self.assertNotIn("parent_identity", serialized)
        self.assertNotIn("approval_token", serialized)
        self.assertNotIn("a" * 300, serialized)
    def test_default_registry_remains_read_only_and_unregistered(self):
        from kivo.agent.action_adapter import STAGE_11_LEGACY_SPECS

        registry = create_default_tool_registry()
        self.assertNotIn(MUTATION_TOOL, registry.list_tool_names())
        self.assertFalse(registry.has_mutating_tools())
        self.assertEqual(registry.list_mutating_tool_names(), [])
        registry.assert_read_only()

        # Model schema exposure check
        openai_tools = registry.get_openai_tools()
        openai_names = [t.get("function", {}).get("name") for t in openai_tools]
        self.assertNotIn(MUTATION_TOOL, openai_names)

        # Legacy action adapter check: no mutation specs exist
        self.assertNotIn(
            MUTATION_TOOL, [spec.tool_name for spec in STAGE_11_LEGACY_SPECS]
        )

        # Execution check: calling through executor refuses unknown tool
        executor = ToolExecutor(registry)
        call = ToolCall(
            tool_call_id="call-unregistered",
            tool_name=MUTATION_TOOL,
            arguments={"workspace_id": "demo", "relative_path": "a.txt", "content": "x"},
        )
        res = asyncio.run(executor.execute(call, set(), turn_id="turn-unreg"))
        self.assertFalse(res.success)
        self.assertIn("unknown tool", res.error_message.lower())

    def test_tool_definition_is_mutating_confirmation_required_and_unregistered(self):
        tool = create_text_file_tool(self.service)
        self.assertEqual(tool.name, MUTATION_TOOL)
        self.assertEqual(
            tool.permission, PermissionLevel.CONFIRMATION_REQUIRED
        )
        self.assertTrue(tool.mutating)
        self.assertIsNone(tool.legacy_binding)
        self.assertFalse(tool.scan_for_forbidden_keywords)
        self.assertIsNotNone(tool.preflight)
        registry = ToolRegistry()
        registry.register(tool)
        self.assertTrue(registry.has_mutating_tools())
        with self.assertRaises(RuntimeError):
            registry.assert_read_only()


class AuditPrivacyTests(MutationTestCase):
    def test_successful_handler_audit_has_no_content_or_absolute_roots(self):
        tool = create_text_file_tool(self.service)
        result = self._invoke_handler(tool, content="AUDIT-SECRET-CONTENT")
        self.assertTrue(result["ok"])
        entries = get_audit_log(limit=50)
        serialized = json.dumps(
            [
                {
                    "tool": entry.tool,
                    "outcome": entry.outcome,
                    "parameters": entry.parameters,
                    "result_snippet": entry.result_snippet,
                }
                for entry in entries
            ]
        )
        self.assertNotIn("AUDIT-SECRET-CONTENT", serialized)
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn(INTERNAL_TEMP_PREFIX, serialized)


class MutationAuditContractTests(unittest.TestCase):
    def test_invalid_audit_metadata_is_rejected(self):
        for invalid in (
            {"workspace_id": "../escape"},
            {"relative_path": "../escape"},
            {"relative_path": "/absolute"},
            {"content_digest": "A" * 64},
            {"byte_count": -1},
            {"byte_count": True},
            {"operation_id": "A" * 32},
            {"operation_id": "0" * 31},
            {"committed": "true"},
            {"verified": 1},
            {"durability": "probably"},
        ):
            with self.subTest(invalid=invalid):
                self.assertFalse(
                    record_mutation_event(
                        tool_name=MUTATION_TOOL,
                        operation_kind=MUTATION_TOOL,
                        status="failed",
                        turn_id="turn-1",
                        tool_call_id="call-1",
                        **invalid,
                    )
                )


class AdversarialLifecycleReviewTests(MutationTestCase):
    """Deterministic tests for Part 2 and Part 3 adversarial lifecycle races."""

    def _create_with_lifecycle(
        self,
        lifecycle: MutationLifecycle,
        *,
        cancel_event=None,
        content: str = "",
    ):
        return self._create(
            lifecycle.context.binding.relative_path,
            content or "hello",
            binding=lifecycle.context.binding,
            cancel_event=cancel_event,
            lifecycle=lifecycle,
        )

    def _executor(self, gate: ApprovalGate):
        registry = ToolRegistry()
        tool = create_text_file_tool(self.service)
        registry.register(tool)
        return ToolExecutor(registry, approval_gate=gate), tool

    def test_executor_timeout_before_install_leaves_no_file_and_reports_uncommitted(self):
        gate = ApprovalGate(timeout_seconds=5.0)
        registry = ToolRegistry()
        tool = create_text_file_tool(self.service)
        registry.register(tool)
        tool.timeout_seconds = 0.05
        executor = ToolExecutor(registry, approval_gate=gate)
        call = ToolCall(
            tool_call_id="call-timeout-pre",
            tool_name=MUTATION_TOOL,
            arguments={
                "workspace_id": "demo",
                "relative_path": "docs/timeout_pre.txt",
                "content": "timeout-data",
            },
        )
        real_temp = mutation_service._create_temporary_file

        def slow_temp(*args, **kwargs):
            time.sleep(0.15)
            return real_temp(*args, **kwargs)

        async def run():
            with patch(
                "kivo.files.mutation_service._create_temporary_file",
                side_effect=slow_temp,
            ):
                task = asyncio.create_task(
                    executor.execute(call, set(), turn_id="turn-timeout-pre")
                )
                deadline = time.monotonic() + 2.0
                pending = None
                while time.monotonic() < deadline:
                    reqs = gate.get_pending_requests()
                    if reqs:
                        pending = reqs[0]
                        break
                    await asyncio.sleep(0.01)
                self.assertIsNotNone(pending)
                gate.resolve(
                    pending["token"],
                    True,
                    turn_id="turn-timeout-pre",
                    tool_call_id=call.tool_call_id,
                )
                return await task

        result = asyncio.run(run())
        self.assertFalse(result.success)
        self.assertFalse(result.output.get("committed", False))
        self.assertIn("timed_out", result.output.get("status", ""))
        self._assert_not_created("docs/timeout_pre.txt")

    def test_shutdown_before_installation_leaves_no_file_and_reports_uncommitted(self):
        manager = ConversationManager(
            context_manager=ContextManager(),
            provider=_NullProvider(),
            tool_registry=ToolRegistry(),
            turn_timeout_seconds=1.0,
        )
        results: list = []
        manager._on_tool_completed = lambda res: results.append(res)
        before_create = threading.Event()
        release = threading.Event()
        real_temp = mutation_service._create_temporary_file

        def block_temp(*args, **kwargs):
            before_create.set()
            release.wait(timeout=5.0)
            return real_temp(*args, **kwargs)

        lifecycle = manager._tool_executor.mutation_lifecycles.create(
            self._context("docs/shutdown_pre.txt", "pre-data")
        )
        lifecycle.mark_approved()
        self.assertTrue(lifecycle.start())

        cancel_event = threading.Event()
        def run_create():
            try:
                self._create_with_lifecycle(
                    lifecycle, cancel_event=cancel_event, content="pre-data"
                )
            except FileMutationError:
                pass

        worker_thread = None
        try:
            with patch(
                "kivo.files.mutation_service._create_temporary_file",
                side_effect=block_temp,
            ):
                worker_thread = threading.Thread(target=run_create)
                worker_thread.start()
                self.assertTrue(before_create.wait(timeout=3.0))
                # Trigger shutdown while write has not installed
                manager.close()
                cancel_event.set()
                release.set()
                worker_thread.join(timeout=2.0)
        finally:
            cancel_event.set()
            release.set()
            if worker_thread and worker_thread.is_alive():
                worker_thread.join(timeout=2.0)

        snapshot = lifecycle.snapshot()
        self.assertFalse(snapshot.committed)
        self.assertEqual(results, [])
        self._assert_not_created("docs/shutdown_pre.txt")

    def test_shutdown_after_installation_before_fsync_reports_commit(self):
        manager = ConversationManager(
            context_manager=ContextManager(),
            provider=_NullProvider(),
            tool_registry=ToolRegistry(),
            turn_timeout_seconds=1.0,
        )
        results: list = []
        manager._on_tool_completed = lambda res: results.append(res)

        installed_event = threading.Event()
        release_fsync = threading.Event()
        real_rename = mutation_service._renameat2_noreplace
        real_fsync = mutation_service._fsync_parent

        def rename_and_signal(*args, **kwargs):
            res = real_rename(*args, **kwargs)
            installed_event.set()
            return res

        def block_fsync(fd: int) -> None:
            release_fsync.wait(timeout=5.0)
            real_fsync(fd)

        lifecycle = manager._tool_executor.mutation_lifecycles.create(
            self._context("docs/shutdown_fsync.txt", "fsync-data")
        )
        lifecycle.mark_approved()
        self.assertTrue(lifecycle.start())

        worker_thread = None
        try:
            with patch(
                "kivo.files.mutation_service._renameat2_noreplace",
                side_effect=rename_and_signal,
            ), patch(
                "kivo.files.mutation_service._fsync_parent",
                side_effect=block_fsync,
            ):
                worker_thread = threading.Thread(
                    target=lambda: self._create_with_lifecycle(lifecycle, content="fsync-data")
                )
                worker_thread.start()
                self.assertTrue(installed_event.wait(timeout=3.0))
                # Rename succeeded; now shut down before parent fsync finishes
                manager.close()
                release_fsync.set()
                worker_thread.join(timeout=2.0)
        finally:
            release_fsync.set()
            if worker_thread and worker_thread.is_alive():
                worker_thread.join(timeout=2.0)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].output["committed"])
        # At shutdown drain time, fsync had not completed yet; committed is True
        self.assertIn("committed_notice", results[0].output)
        self.assertEqual(self._target("docs/shutdown_fsync.txt").read_bytes(), b"fsync-data")
        # Post-worker completion verification
        post_snapshot = lifecycle.snapshot()
        self.assertTrue(post_snapshot.committed)
        self.assertTrue(post_snapshot.verified)

    def test_shutdown_during_final_verification_reports_commit(self):
        manager = ConversationManager(
            context_manager=ContextManager(),
            provider=_NullProvider(),
            tool_registry=ToolRegistry(),
            turn_timeout_seconds=1.0,
        )
        results: list = []
        manager._on_tool_completed = lambda res: results.append(res)

        verify_started = threading.Event()
        release_verify = threading.Event()
        real_verify = mutation_service._verify_installed_file

        def block_verify(*args, **kwargs):
            verify_started.set()
            release_verify.wait(timeout=5.0)
            return real_verify(*args, **kwargs)

        lifecycle = manager._tool_executor.mutation_lifecycles.create(
            self._context("docs/shutdown_verify.txt", "verify-data")
        )
        lifecycle.mark_approved()
        self.assertTrue(lifecycle.start())

        worker_thread = None
        try:
            with patch(
                "kivo.files.mutation_service._verify_installed_file",
                side_effect=block_verify,
            ):
                worker_thread = threading.Thread(
                    target=lambda: self._create_with_lifecycle(lifecycle, content="verify-data")
                )
                worker_thread.start()
                self.assertTrue(verify_started.wait(timeout=3.0))
                manager.close()
                release_verify.set()
                worker_thread.join(timeout=2.0)
        finally:
            release_verify.set()
            if worker_thread and worker_thread.is_alive():
                worker_thread.join(timeout=2.0)

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].output["committed"])
        # Drain happened before verification completed
        self.assertIn("committed_notice", results[0].output)
        self.assertEqual(self._target("docs/shutdown_verify.txt").read_bytes(), b"verify-data")
        # Post-worker completion verification
        post_snapshot = lifecycle.snapshot()
        self.assertTrue(post_snapshot.committed)
        self.assertTrue(post_snapshot.verified)

    def test_multiple_simultaneous_attempts_to_resolve_lifecycle(self):
        lifecycle = self._lifecycle("docs/multithread.txt", "multi")
        barrier = threading.Barrier(10)

        def worker_complete(idx: int):
            barrier.wait()
            lifecycle.complete({
                "ok": True,
                "committed": True,
                "verified": True,
                "durability": "synced",
                "status": f"created_{idx}",
            })

        def worker_failure(code: str):
            barrier.wait()
            lifecycle.record_failure(code, "Failure message", committed=True)

        def worker_cancel():
            barrier.wait()
            lifecycle.record_cancelled(committed=True)

        def worker_timeout():
            barrier.wait()
            lifecycle.record_timeout(committed=True)

        def worker_commit():
            barrier.wait()
            lifecycle.mark_committed()

        threads = [
            threading.Thread(target=worker_complete, args=(0,)),
            threading.Thread(target=worker_complete, args=(1,)),
            threading.Thread(target=worker_complete, args=(2,)),
            threading.Thread(target=worker_failure, args=("late_err_1",)),
            threading.Thread(target=worker_failure, args=("late_err_2",)),
            threading.Thread(target=worker_cancel),
            threading.Thread(target=worker_cancel),
            threading.Thread(target=worker_timeout),
            threading.Thread(target=worker_timeout),
            threading.Thread(target=worker_commit),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=2.0)

        self.assertTrue(lifecycle.committed)
        self.assertTrue(lifecycle.terminal)
        snapshot = lifecycle.snapshot()
        self.assertTrue(snapshot.committed)
        self.assertTrue(snapshot.verified)
        self.assertEqual(snapshot.durability, "synced")

        entries = [
            entry
            for entry in get_audit_log(limit=50)
            if entry.tool == MUTATION_TOOL
        ]
        outcomes = [e.outcome for e in entries]
        self.assertEqual(outcomes.count("committed"), 1)
        self.assertEqual(outcomes.count("verification_succeeded"), 1)

    def test_duplicate_drains_deliver_results_exactly_once(self):
        from kivo.agent.mutation_lifecycle import MutationLifecycleRegistry

        registry = MutationLifecycleRegistry()
        ctx = self._context("docs/dedup.txt", "dedup")
        lifecycle = registry.create(ctx)
        lifecycle.mark_approved()
        lifecycle.start()
        lifecycle.mark_committed()
        lifecycle.mark_verification(True)
        lifecycle.mark_durability(DURABILITY_SYNCED)

        first_drain = registry.drain_unreported(ctx.turn_id)
        self.assertEqual(len(first_drain), 1)
        self.assertTrue(first_drain[0].committed)
        self.assertTrue(first_drain[0].reported)

        second_drain = registry.drain_unreported(ctx.turn_id)
        self.assertEqual(second_drain, [])

        third_drain = registry.drain_unreported("")
        self.assertEqual(third_drain, [])

    def test_drain_retry_when_ui_callback_raises(self):
        manager = ConversationManager(
            context_manager=ContextManager(),
            provider=_NullProvider(),
            tool_registry=ToolRegistry(),
            turn_timeout_seconds=1.0,
        )
        delivered: list = []
        should_fail = True

        def failing_completed(res):
            nonlocal should_fail
            if should_fail:
                should_fail = False
                raise RuntimeError("simulated UI callback error")
            delivered.append(res)

        manager._on_tool_completed = failing_completed
        try:
            ctx = self._context("docs/drain_retry.txt", "retry-data")
            lifecycle = manager._tool_executor.mutation_lifecycles.create(ctx)
            lifecycle.mark_approved()
            lifecycle.start()
            lifecycle.mark_committed()
            lifecycle.mark_verification(True)
            lifecycle.mark_durability(DURABILITY_SYNCED)

            # First drain fails in callback -> release_reported is called
            manager._drain_mutation_results(ctx.turn_id)
            self.assertEqual(delivered, [])

            # Second drain succeeds
            manager._drain_mutation_results(ctx.turn_id)
            self.assertEqual(len(delivered), 1)
            self.assertTrue(delivered[0].output["committed"])
            self.assertTrue(delivered[0].output["ok"])
        finally:
            manager.close()

    def test_late_handler_result_after_timeout_is_delivered_to_observer(self):
        delivered: list = []
        gate = ApprovalGate(timeout_seconds=5.0)
        executor, tool = self._executor(gate)
        executor._on_late_mutation_result = lambda res: delivered.append(res)
        ctx = self._context("docs/late_timeout.txt", "late-timeout-data")
        lifecycle = executor.mutation_lifecycles.create(ctx)
        lifecycle.set_late_observer(
            lambda snap: delivered.append(snap.to_tool_result())
        )
        lifecycle.mark_approved()
        lifecycle.start()

        # Simulate executor timing out while operation was still running
        lifecycle.mark_settlement_pending()
        lifecycle.record_timeout(committed=False)
        lifecycle.abandon_settlement()

        # Later, background worker finishes successfully
        lifecycle.complete({
            "ok": True,
            "committed": True,
            "verified": True,
            "durability": DURABILITY_SYNCED,
            "status": "created",
        })

        self.assertEqual(len(delivered), 1)
        self.assertTrue(delivered[0].output["committed"])
        self.assertTrue(delivered[0].output["ok"])
        self.assertTrue(delivered[0].output["timed_out_after_commit"])

    def test_late_handler_result_after_cancellation_is_delivered_to_observer(self):
        delivered: list = []
        gate = ApprovalGate(timeout_seconds=5.0)
        executor, tool = self._executor(gate)
        ctx = self._context("docs/late_cancel.txt", "late-cancel-data")
        lifecycle = executor.mutation_lifecycles.create(ctx)
        lifecycle.set_late_observer(
            lambda snap: delivered.append(snap.to_tool_result())
        )
        lifecycle.mark_approved()
        lifecycle.start()

        # Cancel turn
        lifecycle.record_cancelled(committed=False)

        # Later, background worker completes
        lifecycle.complete({
            "ok": True,
            "committed": True,
            "verified": True,
            "durability": DURABILITY_SYNCED,
            "status": "created",
        })

        self.assertEqual(len(delivered), 1)
        self.assertTrue(delivered[0].output["committed"])
        self.assertTrue(delivered[0].output["ok"])
        self.assertTrue(delivered[0].output["cancelled_after_commit"])

    def test_approval_details_contain_no_fingerprint_roots_or_grants(self):
        preflight = self.service.preflight("demo", "docs/check_details.txt", "sample")
        context = preflight.bind(
            tool_name=MUTATION_TOOL,
            turn_id="turn-check",
            tool_call_id="call-check",
        )
        details = context.to_approval_details()
        serialized = json.dumps(details)

        # Confirm banned security fields never enter user-facing serialized approval data
        self.assertNotIn("fingerprint", details)
        self.assertNotIn("approval_fingerprint", details)
        self.assertNotIn("fingerprint", serialized)
        self.assertNotIn("root_identity", details)
        self.assertNotIn("parent_identity", details)
        self.assertNotIn("root_identity", serialized)
        self.assertNotIn("token", details)
        self.assertNotIn("grant", details)
        self.assertNotIn(str(self.root), serialized)

    def test_workspace_config_strict_validation_matrix(self):
        from kivo.files.workspace import (
            WorkspaceConfigError,
            load_workspace_config,
        )

        cfg_file = self.tmp / "matrix_workspaces.json"

        # 1. Missing file -> empty config
        if cfg_file.exists():
            cfg_file.unlink()
        empty_cfg = load_workspace_config(cfg_file)
        self.assertEqual(empty_cfg.roots, {})

        # 2. Legacy string entry -> read-only
        cfg_file.write_text(
            json.dumps({"version": 1, "workspaces": {"w1": str(self.root)}})
        )
        cfg = load_workspace_config(cfg_file)
        self.assertFalse(cfg.get("w1").mutation_enabled)

        # 3. Object entry without mutation_enabled -> read-only
        cfg_file.write_text(
            json.dumps({"version": 1, "workspaces": {"w1": {"root": str(self.root)}}})
        )
        cfg = load_workspace_config(cfg_file)
        self.assertFalse(cfg.get("w1").mutation_enabled)

        # 4. Explicit mutation_enabled: true -> mutation enabled
        cfg_file.write_text(
            json.dumps({
                "version": 1,
                "workspaces": {"w1": {"root": str(self.root), "mutation_enabled": True}},
            })
        )
        cfg = load_workspace_config(cfg_file)
        self.assertTrue(cfg.get("w1").mutation_enabled)

        # 5. Invalid mutation_enabled types fail closed
        for bad_val in ("true", 1, None, [], {}):
            cfg_file.write_text(
                json.dumps({
                    "version": 1,
                    "workspaces": {"w1": {"root": str(self.root), "mutation_enabled": bad_val}},
                })
            )
            with self.assertRaises(WorkspaceConfigError, msg=f"bad_val={bad_val}"):
                load_workspace_config(cfg_file)

        # 6. Unknown field in object -> fails closed
        cfg_file.write_text(
            json.dumps({
                "version": 1,
                "workspaces": {
                    "w1": {
                        "root": str(self.root),
                        "mutation_enabled": True,
                        "unknown_field": True,
                    }
                },
            })
        )
        with self.assertRaises(WorkspaceConfigError):
            load_workspace_config(cfg_file)

        # 7. Duplicate keys -> fails closed
        raw_dup = f'{{"version": 1, "workspaces": {{"w1": "{self.root}", "w1": "{self.root}"}}}}'
        cfg_file.write_text(raw_dup)
        with self.assertRaises(WorkspaceConfigError):
            load_workspace_config(cfg_file)

        # 8. Case-folded duplicate IDs -> fails closed
        cfg_file.write_text(
            json.dumps({
                "version": 1,
                "workspaces": {
                    "demo": str(self.root),
                    "DEMO": str(self.root),
                },
            })
        )
        with self.assertRaises(WorkspaceConfigError):
            load_workspace_config(cfg_file)

        # 9. Symlink config file -> fails closed
        real_cfg = self.tmp / "real_config.json"
        real_cfg.write_text(json.dumps({"version": 1, "workspaces": {"w1": str(self.root)}}))
        sym_cfg = self.tmp / "sym_config.json"
        if sym_cfg.exists():
            sym_cfg.unlink()
        sym_cfg.symlink_to(real_cfg)
        with self.assertRaises(WorkspaceConfigError):
            load_workspace_config(sym_cfg)

