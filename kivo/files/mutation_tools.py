"""Approval-bound ``create_text_file`` tool definition and handler.

Stage 11.3.1c keeps this tool unregistered.  It exists so the preflight, grant
validation, fingerprint revalidation, atomic service call, authoritative
lifecycle, and honest audit/result behavior can be exercised directly.  The
production registry remains read-only.
"""
from __future__ import annotations

import asyncio
import hmac
import threading
from typing import Any, Optional

from kivo.agent.approval_context import (
    ApprovalContext,
    MutationPreflight,
    validate_mutation_grant,
)
from kivo.agent.mutation_lifecycle import MutationLifecycle
from kivo.agent.tool_schema import (
    ExecutionGrant,
    PermissionLevel,
    ToolDefinition,
)
from kivo.files.mutation_service import FileMutationError, FileMutationService
from kivo.files.tools import BoundedFileWorker

CREATE_TEXT_FILE_TOOL_NAME = "create_text_file"
CREATE_TEXT_FILE_MAX_MESSAGE_CHARS = 4_000
CREATE_TEXT_FILE_TIMEOUT_SECONDS = 15.0
CREATE_TEXT_FILE_APPROVAL_TIMEOUT_SECONDS = 120.0
MUTATION_SETTLE_SECONDS = 10.0


def _failure_result(
    context: Optional[ApprovalContext],
    *,
    code: str,
    message: str,
    committed: bool = False,
    audit_warning: str = "",
    lifecycle: Optional[MutationLifecycle] = None,
) -> dict[str, Any]:
    snapshot = lifecycle.snapshot() if lifecycle is not None else None
    effective_committed = bool(committed) or bool(
        snapshot is not None and snapshot.committed
    )
    result: dict[str, Any] = {
        "ok": False,
        "status": code,
        "error": message,
        "committed": effective_committed,
    }
    if isinstance(context, ApprovalContext):
        result.update(
            {
                "operation_id": context.operation_id,
                "workspace_id": context.binding.workspace_id,
                "relative_path": context.binding.relative_path,
                "byte_count": context.binding.byte_count,
                "content_digest": context.binding.content_digest,
            }
        )
    if lifecycle is not None:
        result["operation_id"] = lifecycle.operation_id
    if snapshot is not None:
        result["verified"] = snapshot.verified
        result["durability"] = snapshot.durability
        if snapshot.outcome_unknown:
            result["outcome_unknown"] = True
    if effective_committed:
        result["committed_notice"] = (
            "The file exists at the approved path. Do not report that no file "
            "was created."
        )
    if audit_warning:
        result["audit_warning"] = audit_warning
    return result


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except Exception:
        pass


async def _settle_operation(
    task: asyncio.Task[Any],
    lifecycle: MutationLifecycle,
) -> tuple[bool, Optional[dict[str, Any]]]:
    """Wait briefly for a shielded service call to record its final state.

    Returns ``(settled, successful_result)``.  ``settled`` is false when the
    call is still running at the settlement deadline, in which case a done
    callback completes the lifecycle when the real outcome arrives.  That late
    completion is delivered by the lifecycle's late observer rather than being
    silently discarded.
    """

    def _finish_late(finished: asyncio.Task[Any]) -> None:
        if finished.cancelled():
            return
        try:
            result = finished.result()
        except Exception:
            return
        if isinstance(result, dict) and result.get("ok") is True:
            lifecycle.complete(result)

    if task.done():
        if task.cancelled():
            return True, None
        try:
            result = task.result()
        except Exception:
            return True, None
        return True, result if isinstance(result, dict) else None

    try:
        await asyncio.wait({task}, timeout=MUTATION_SETTLE_SECONDS)
    except asyncio.CancelledError:
        # The surrounding task may be cancelled again; the operation task is
        # intentionally left alive so it can finish fsync/verification.
        pass
    if not task.done():
        task.add_done_callback(_finish_late)
        task.add_done_callback(_consume_task_exception)
        return False, None
    if task.cancelled():
        return True, None
    try:
        result = task.result()
    except Exception:
        return True, None
    return True, result if isinstance(result, dict) else None


def create_text_file_tool(
    service: FileMutationService,
    worker: Optional[BoundedFileWorker] = None,
    *,
    approval_timeout_seconds: float = CREATE_TEXT_FILE_APPROVAL_TIMEOUT_SECONDS,
) -> ToolDefinition:
    """Build the (unregistered) approval-bound create-text-file tool."""

    def _preflight(arguments: dict[str, Any]) -> MutationPreflight:
        if not isinstance(arguments, dict):
            raise FileMutationError(
                "invalid_arguments", "Tool arguments must be an object."
            )
        return service.preflight(
            arguments.get("workspace_id"),
            arguments.get("relative_path"),
            arguments.get("content"),
        )

    async def _handle_create_text_file(
        arguments: dict[str, Any],
        tool_call_id: str = "",
        execution_grant: Optional[ExecutionGrant] = None,
        turn_id: str = "",
        approval_context: Optional[ApprovalContext] = None,
        mutation_lifecycle: Optional[MutationLifecycle] = None,
        defer_lifecycle_settlement: bool = False,
    ) -> dict[str, Any]:
        # 1. A mutation never runs without the executor's approved context and
        # an approval-bound grant for this exact tool call.
        if not isinstance(arguments, dict):
            return _failure_result(
                None,
                code="invalid_arguments",
                message="Tool arguments must be an object.",
            )
        if not isinstance(approval_context, ApprovalContext):
            return _failure_result(
                None,
                code="approval_context_missing",
                message=(
                    "A mutation requires the executor's trusted approval "
                    "context; execution refused."
                ),
            )
        grant_error = validate_mutation_grant(
            execution_grant,
            tool_name=CREATE_TEXT_FILE_TOOL_NAME,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            context=approval_context,
        )
        if grant_error is not None:
            return _failure_result(
                approval_context,
                code="denied",
                message=grant_error,
            )

        if (
            approval_context.operation_kind != "create_text_file"
            or approval_context.tool_name != CREATE_TEXT_FILE_TOOL_NAME
        ):
            return _failure_result(
                approval_context,
                code="denied",
                message="The approval context is bound to a different operation.",
            )

        lifecycle = mutation_lifecycle
        if lifecycle is not None and lifecycle.context != approval_context:
            return _failure_result(
                approval_context,
                code="invalid_lifecycle",
                message=(
                    "The mutation lifecycle does not belong to the approved "
                    "context; execution refused."
                ),
            )
        if lifecycle is None:
            # Direct handler use (tests, embedders) still gets the same
            # single-owner audit lifecycle.  Executor-driven calls pass the
            # lifecycle that already recorded approval and execution start.
            lifecycle = MutationLifecycle(approval_context)

        # 2. Revalidate the current workspace, parent, target absence, mount
        # identity, and content.  This also recomputes the fingerprint from
        # current trusted state; it never accepts a model-supplied fingerprint.
        try:
            current = await asyncio.to_thread(
                service.preflight,
                arguments.get("workspace_id"),
                arguments.get("relative_path"),
                arguments.get("content"),
            )
        except FileMutationError as exc:
            lifecycle.record_failure(
                exc.code, exc.message, committed=exc.committed
            )
            return _failure_result(
                approval_context,
                code=exc.code,
                message=exc.message,
                committed=exc.committed,
                lifecycle=lifecycle,
            )
        except Exception:
            lifecycle.record_failure("failed", "The mutation preflight failed safely.")
            return _failure_result(
                approval_context,
                code="internal_error",
                message="The mutation preflight failed safely.",
                lifecycle=lifecycle,
            )

        current_context = current.bind(
            tool_name=CREATE_TEXT_FILE_TOOL_NAME,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
        )
        if not hmac.compare_digest(
            current_context.fingerprint, approval_context.fingerprint
        ):
            lifecycle.record_failure(
                "approval_mismatch",
                "The requested mutation no longer matches the approved context.",
            )
            return _failure_result(
                approval_context,
                code="approval_mismatch",
                message=(
                    "The requested mutation no longer matches the approved "
                    "context; execution refused."
                ),
                lifecycle=lifecycle,
            )

        # 3. The mandatory pre-change intent is recorded exactly once.  The
        # executor normally performs this before invoking the handler; direct
        # handler use performs it here.  A failed intent forbids execution.
        lifecycle.mark_approved()
        if not lifecycle.start():
            return _failure_result(
                approval_context,
                code="audit_unavailable",
                message=(
                    "The mutation audit intent could not be recorded; creation "
                    "was refused."
                ),
                lifecycle=lifecycle,
            )

        # 4. Execute the atomic service through the bounded worker when one is
        # supplied, otherwise through a cooperative-cancellation thread.  The
        # service call is shielded so cancellation cannot discard a committed
        # result; cancellation is requested cooperatively first.
        cancel_event = threading.Event()

        def _request_cancel() -> None:
            cancel_event.set()
            if worker is not None:
                try:
                    worker.cancel_active()
                except Exception:
                    pass

        async def _run_operation() -> dict[str, Any]:
            if worker is not None:
                return await worker.run(
                    service.create_text_file,
                    arguments.get("workspace_id"),
                    arguments.get("relative_path"),
                    arguments.get("content"),
                    approval_context.binding,
                    lifecycle=lifecycle,
                )
            return await asyncio.to_thread(
                service.create_text_file,
                arguments.get("workspace_id"),
                arguments.get("relative_path"),
                arguments.get("content"),
                approval_context.binding,
                cancel_event=cancel_event,
                lifecycle=lifecycle,
            )

        operation_task = asyncio.create_task(_run_operation())
        try:
            result = await asyncio.shield(operation_task)
        except asyncio.CancelledError:
            _request_cancel()
            settled, settled_result = await _settle_operation(
                operation_task, lifecycle
            )
            if settled_result is not None:
                # The operation finished during settlement.  The commit is
                # authoritative even though this handler was cancelled.
                lifecycle.complete(settled_result)
            elif not settled:
                lifecycle.mark_settlement_pending()
            if not defer_lifecycle_settlement:
                lifecycle.record_cancelled()
            raise
        except FileMutationError as exc:
            lifecycle.record_failure(
                exc.code, exc.message, committed=exc.committed
            )
            warning = ""
            if lifecycle.audit_warning and exc.committed:
                warning = (
                    "The file was created, but the mutation audit outcome "
                    "could not be recorded."
                )
            return _failure_result(
                approval_context,
                code=exc.code,
                message=exc.message,
                committed=exc.committed,
                audit_warning=warning,
                lifecycle=lifecycle,
            )
        except Exception:
            lifecycle.record_failure(
                "internal_error",
                "The file creation failed safely.",
                committed=lifecycle.committed,
            )
            if not operation_task.done():
                # The worker may have surfaced a shutdown error while the
                # underlying operation is still running.  Do not let the
                # eventual commit disappear.
                lifecycle.mark_settlement_pending()
            return _failure_result(
                approval_context,
                code="internal_error",
                message="The file creation failed safely.",
                audit_warning=(
                    "The mutation failure could not be fully audited."
                    if lifecycle.audit_warning
                    else ""
                ),
                lifecycle=lifecycle,
            )

        if not isinstance(result, dict) or result.get("ok") is not True:
            lifecycle.record_failure(
                "invalid_result",
                "The mutation service did not return an explicit ok: true "
                "result after verification.",
            )
            return _failure_result(
                approval_context,
                code="invalid_result",
                message=(
                    "The mutation service did not return an explicit ok: true "
                    "result after verification."
                ),
                audit_warning=(
                    "The invalid mutation result could not be audited."
                    if lifecycle.audit_warning
                    else ""
                ),
                lifecycle=lifecycle,
            )

        # 5. The service has already recorded commit, verification, and
        # durability on the lifecycle.  Complete again idempotently and report
        # the lifecycle snapshot so the response cannot disagree with the
        # authoritative record (for example by dropping durability).
        lifecycle.complete(result)
        response = dict(result)
        response.update(lifecycle.snapshot().to_output())
        response["operation_id"] = lifecycle.operation_id
        if lifecycle.audit_warning:
            response.setdefault(
                "audit_warning",
                "The file was created and verified, but the mutation audit "
                "trail is incomplete.",
            )
        return response

    return ToolDefinition(
        name=CREATE_TEXT_FILE_TOOL_NAME,
        description=(
            "Create one new UTF-8 text file inside an explicitly mutation-"
            "approved workspace. The parent directory must already exist and "
            "the target must not exist. This tool never overwrites, appends, "
            "renames, moves, or deletes anything, and it requires explicit "
            "human approval bound to one turn and tool call."
        ),
        parameters={
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 64,
                    "description": (
                        "Identifier of an explicitly mutation-approved "
                        "workspace. This is never an absolute filesystem path."
                    ),
                },
                "relative_path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 512,
                    "description": (
                        "Workspace-relative path for the new file. Absolute "
                        "paths, '..', empty components, symlinks, and "
                        "sensitive paths are rejected."
                    ),
                },
                "content": {
                    "type": "string",
                    "maxLength": 8_192,
                    "description": (
                        "Exact UTF-8 text to write. Content must be at most "
                        "8192 UTF-8 bytes and must not contain NUL bytes or "
                        "lone surrogates. Line endings are preserved exactly."
                    ),
                },
            },
            "required": ["workspace_id", "relative_path", "content"],
            "additionalProperties": False,
        },
        handler=_handle_create_text_file,
        permission=PermissionLevel.CONFIRMATION_REQUIRED,
        timeout_seconds=CREATE_TEXT_FILE_TIMEOUT_SECONDS,
        preflight=_preflight,
        scan_for_forbidden_keywords=False,
        max_output_chars=CREATE_TEXT_FILE_MAX_MESSAGE_CHARS,
        mutating=True,
        approval_timeout_seconds=approval_timeout_seconds,
    )


__all__ = [
    "CREATE_TEXT_FILE_APPROVAL_TIMEOUT_SECONDS",
    "CREATE_TEXT_FILE_MAX_MESSAGE_CHARS",
    "CREATE_TEXT_FILE_TIMEOUT_SECONDS",
    "CREATE_TEXT_FILE_TOOL_NAME",
    "MUTATION_SETTLE_SECONDS",
    "create_text_file_tool",
]
