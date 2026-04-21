from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from g3ku.agent.tools.base import Tool
from g3ku.content import content_summary_and_ref, parse_content_envelope
from g3ku.providers.base import ToolCallRequest
from g3ku.runtime.tool_error_guidance import (
    append_parameter_error_guidance,
    is_parameter_like_tool_exception,
)
from g3ku.runtime.tool_result_status import is_error_like_tool_result
from g3ku.runtime.stage_prompt_compaction import (
    STAGE_COMPACT_PREFIX as _STAGE_COMPACT_PREFIX,
    STAGE_EXTERNALIZED_PREFIX as _STAGE_EXTERNALIZED_PREFIX,
    completed_stage_blocks as _shared_completed_stage_blocks,
    current_stage_active_window as _shared_current_stage_active_window,
    decompose_stage_prompt_messages as _shared_decompose_stage_prompt_messages,
    is_stage_context_message as _shared_is_stage_context_message,
    prepare_stage_prompt_messages as _shared_prepare_stage_prompt_messages,
    retained_completed_stage_ids as _shared_retained_completed_stage_ids,
    stage_prompt_prefix as _shared_stage_prompt_prefix,
)
from g3ku.runtime.tool_history import analyze_tool_call_history, extract_call_id
from g3ku.runtime.tool_watchdog import (
    actor_role_allows_detached_watchdog,
    actor_role_allows_watchdog,
    run_tool_with_watchdog,
)
from main.governance.exec_tool_policy import EXEC_TOOL_EXECUTOR_NAME, EXEC_TOOL_FAMILY_ID
from main.errors import TaskPausedError, describe_exception
from main.models import NodeEvidenceItem, NodeFinalResult, RESULT_SCHEMA_VERSION, SpawnChildSpec, normalize_execution_stage_metadata
from main.runtime.chat_backend import build_actual_request_diagnostics, build_stable_prompt_cache_key
from main.runtime.append_notice_context import (
    APPEND_NOTICE_CONTEXT_KEY,
    APPEND_NOTICE_TAIL_PREFIX,
    build_append_notice_tail_messages,
)
from main.runtime.node_prompt_contract import (
    NodeRuntimeToolContract,
    extract_node_dynamic_contract_payload,
    inject_node_dynamic_contract_message,
    is_node_dynamic_contract_message,
    strip_node_dynamic_contract_messages,
    upsert_node_dynamic_contract_message,
)
from main.runtime.recovery_check import RecoveryCheckDecision, RecoveryCheckEngine
from g3ku.providers.fallback import PUBLIC_PROVIDER_FAILURE_MESSAGE
from g3ku.config.live_runtime import get_runtime_config
from main.runtime import chat_backend as runtime_chat_backend
from main.runtime import send_token_preflight as runtime_send_token_preflight
from main.runtime.stage_budget import (
    DEFAULT_NON_BUDGET_STAGE_TOOLS,
    FINAL_RESULT_TOOL_NAME,
    STAGE_TOOL_NAME,
    STAGE_TOOL_ROUND_BUDGET_MAX,
    STAGE_TOOL_ROUND_BUDGET_MIN,
    callable_tool_names_for_stage_iteration,
    stage_gate_error_for_tool,
    visible_tools_for_stage_iteration,
)
from main.runtime.stage_messages import build_execution_stage_overlay, build_execution_stage_result_block_message
from main.runtime.tool_call_repair import (
    XML_REPAIR_ATTEMPT_LIMIT,
    build_xml_tool_repair_message,
    detect_xml_pseudo_tool_call,
    extract_tool_calls_from_xml_pseudo_content,
    format_xml_repair_failure_reason,
    recover_tool_calls_from_json_payload,
)
from main.protocol import now_iso

_ARTIFACT_REF_PATTERN = re.compile(r'artifact:artifact:[A-Za-z0-9_-]+')
_STAGE_HISTORY_ARCHIVE_SOURCE_KIND = 'stage_history_archive'
_COMPACT_HISTORY_STEP_MAX_CHARS = 160
_ORPHAN_TOOL_RESULT_THRESHOLD = 3
_STAGE_SPAWN_TOOL_NAME = 'spawn_child_nodes'
_UNCOMPACTED_COMPLETED_STAGE_WINDOWS = 3
_READ_ONLY_REPEAT_SOFT_REJECT_LIMIT = 3
_INVALID_FINAL_SUBMISSION_LIMIT = 5
_INVALID_STAGE_SUBMISSION_LIMIT = 5
_STAGE_ONLY_TRANSITION_LIMIT = 5
_DEFAULT_MODEL_RESPONSE_TIMEOUT_SECONDS = 120.0
_NODE_SEND_CONTEXT_WINDOW_HARD_MIN_TOKENS = 25000
_NODE_TOKEN_COMPACT_MARKER = "[G3KU_TOKEN_COMPACT_V2]"
_NODE_TOKEN_COMPACTION_RECENT_TAIL_COUNT = 12
_RESULT_REQUIRED_KEYS = (
    'status',
    'delivery_status',
    'summary',
    'answer',
    'evidence',
    'remaining_work',
    'blocking_reason',
)
_STAGE_BUDGET_NODE_KINDS = {'execution', 'acceptance'}
_UNSET = object()


class RepeatedActionCircuitBreaker:
    def __init__(self, *, window: int = 3, threshold: int = 3) -> None:
        self._recent: deque[str] = deque(maxlen=max(1, int(window)))
        self._threshold = max(1, int(threshold))

    def register(self, signature: str) -> str | None:
        self._recent.append(signature)
        if len(self._recent) < self._threshold:
            return None
        tail = list(self._recent)[-self._threshold :]
        if len(set(tail)) == 1:
            return tail[-1]
        return None


class ReActToolLoop:
    _CONTROL_TOOL_NAMES = {'stop_tool_execution'}
    _ORDERED_CONTROL_TOOL_NAMES = ('stop_tool_execution',)
    _EXCLUSIVE_TOOL_TURN_NAMES = {FINAL_RESULT_TOOL_NAME}
    _BUDGET_BYPASS_TOOL_NAMES = set(DEFAULT_NON_BUDGET_STAGE_TOOLS) - {'wait_tool_execution'}

    def __init__(
        self,
        *,
        chat_backend,
        log_service,
        max_iterations: int | None | object = _UNSET,
        parallel_tool_calls_enabled: bool = True,
        max_parallel_tool_calls: int | None | object = _UNSET,
    ) -> None:
        self._chat_backend = chat_backend
        self._log_service = log_service
        self._max_iterations = self._normalize_optional_limit(max_iterations, default=16)
        self._parallel_tool_calls_enabled = bool(parallel_tool_calls_enabled)
        self._max_parallel_tool_calls = self._normalize_optional_limit(max_parallel_tool_calls, default=10)
        self._node_turn_controller = None
        self._recovery_check_engine = RecoveryCheckEngine(workspace_root=Path.cwd())
        self._model_response_timeout_seconds: float | None | object = _UNSET
        self._runtime_config_refresh_for_retry_invalidation = None

    async def run(
        self,
        *,
        task,
        node,
        messages: list[dict[str, Any]],
        request_body_seed_messages: list[dict[str, Any]] | None = None,
        tools: dict[str, Tool],
        tools_supplier=None,
        model_refs: list[str],
        model_refs_supplier=None,
        runtime_context: dict[str, Any],
        max_iterations: int | None | object = _UNSET,
        max_parallel_tool_calls: int | None | object = _UNSET,
    ) -> NodeFinalResult:
        breaker = RepeatedActionCircuitBreaker()
        limit = self._normalize_optional_limit(max_iterations, default=self._max_iterations)
        attempts = 0
        last_contract_violations: list[str] = []
        message_history = list(messages or [])
        fresh_turn_request_seed_messages = self._prompt_message_records(request_body_seed_messages)
        previous_actual_request_messages: list[dict[str, Any]] = []
        pending_request_delta_messages: list[dict[str, Any]] = []
        orphan_tool_result_strikes = 0
        repair_overlay_text: str | None = None
        invalid_final_submission_count = 0
        invalid_stage_submission_count = 0
        stage_only_transition_streak = 0
        last_invalid_final_submission_reason = ''
        last_invalid_stage_submission_reason = ''
        xml_repair_attempt_count = 0
        xml_repair_excerpt = ''
        xml_repair_tool_names: list[str] = []
        xml_repair_last_issue = ''
        read_only_repeat_violation_counts: dict[str, int] = {}
        persisted_frame = self._runtime_frame(task.task_id, node.node_id)
        if isinstance(persisted_frame, dict):
            try:
                invalid_final_submission_count = max(
                    0,
                    int(persisted_frame.get('invalid_final_submission_count') or 0),
                )
            except (TypeError, ValueError):
                invalid_final_submission_count = 0
            last_invalid_final_submission_reason = str(
                persisted_frame.get('last_invalid_final_submission_reason') or ''
            ).strip()
            last_contract_violations = [
                str(item or '').strip()
                for item in list(persisted_frame.get('last_contract_violations') or [])
                if str(item or '').strip()
            ]
        while limit is None or attempts < limit:
            attempts += 1
            self._check_pause_or_cancel(task.task_id)
            current_tools = dict(tools or {})
            if callable(tools_supplier):
                supplied_tools = tools_supplier()
                if isinstance(supplied_tools, dict):
                    current_tools = dict(supplied_tools)
            resumed_history = await self._resume_pending_tool_turn_if_needed(
                task=task,
                node=node,
                message_history=message_history,
                tools=current_tools,
                runtime_context=runtime_context,
            )
            if resumed_history is None:
                resumed_history = await self._resume_waiting_children_turn_if_needed(
                    task=task,
                    node=node,
                    message_history=message_history,
                    tools=current_tools,
                    runtime_context=runtime_context,
                )
            if resumed_history is not None:
                message_history = resumed_history
                fresh_turn_request_seed_messages = []
                attempts = max(0, attempts - 1)
                continue
            stage_gate = self._execution_stage_gate(
                task_id=task.task_id,
                node_id=node.node_id,
                node_kind=node.node_kind,
            )
            callable_visible_tools = self._visible_tools_for_iteration(
                tools=current_tools,
                node_kind=node.node_kind,
                stage_gate=stage_gate,
            )
            model_visible_tools, tool_schema_selection = self._model_visible_tools_for_iteration(
                task_id=task.task_id,
                node_id=node.node_id,
                node_kind=node.node_kind,
                visible_tools=current_tools,
                stage_gate=stage_gate,
                runtime_context=runtime_context,
            )
            provider_tool_names = self._normalized_name_list(
                list(tool_schema_selection.get('provider_tool_names') or list(model_visible_tools.keys()))
            )
            tool_schemas = [
                current_tools[name].to_model_schema()
                for name in provider_tool_names
                if name in current_tools
            ]
            dynamic_contract = self._build_node_dynamic_contract(
                node=node,
                message_history=message_history,
                tool_schema_selection=tool_schema_selection,
                stage_gate=stage_gate,
            )
            dynamic_contract_payload = dynamic_contract.to_message_payload()
            selected_skill_ids = self._normalized_name_list(
                [
                    item.get('skill_id')
                    for item in list(dynamic_contract_payload.get('candidate_skills') or [])
                    if isinstance(item, dict) and str(item.get('skill_id') or '').strip()
                ]
            )
            candidate_skill_ids = self._normalized_name_list(
                [
                    item.get('skill_id') if isinstance(item, dict) else item
                    for item in list(dynamic_contract_payload.get('candidate_skills') or [])
                ]
            )
            model_messages = self._prepare_messages(message_history, runtime_context=runtime_context)
            overlay_parts = [
                build_execution_stage_overlay(node_kind=node.node_kind, stage_gate=stage_gate),
                repair_overlay_text,
            ]
            request_messages = self._apply_temporary_system_overlay(
                model_messages,
                overlay_text='\n\n'.join(str(part or '').strip() for part in overlay_parts if str(part or '').strip()),
            )
            request_messages = inject_node_dynamic_contract_message(
                request_messages,
                dynamic_contract,
            )
            if fresh_turn_request_seed_messages:
                request_messages = self._fresh_turn_live_request_messages_from_seed_request(
                    seed_request_messages=fresh_turn_request_seed_messages,
                    stable_messages=model_messages,
                    live_request_messages=request_messages,
                )
                fresh_turn_request_seed_messages = []
            request_tail_messages = request_messages[len(model_messages) :]
            request_messages = self._same_turn_append_only_request_messages(
                previous_request_messages=previous_actual_request_messages,
                current_model_messages=model_messages,
                pending_delta_messages=pending_request_delta_messages,
                request_tail_messages=request_tail_messages,
            )
            previous_actual_request_messages = self._prompt_message_records(request_messages)
            pending_request_delta_messages = []
            repair_overlay_text = None
            current_model_refs = list(
                model_refs_supplier() if callable(model_refs_supplier) else model_refs
            )
            if not current_model_refs:
                raise RuntimeError('no model refs configured for node runtime')
            turn_prompt_cache_key = self._execution_prompt_cache_key(
                model_messages=model_messages,
                tool_schemas=tool_schemas,
                model_refs=current_model_refs,
            )
            current_tool_choice = self._repair_tool_choice(
                visible_tools=callable_visible_tools,
                stage_gate=stage_gate,
                invalid_final_submission_count=invalid_final_submission_count,
                invalid_stage_submission_count=invalid_stage_submission_count,
            )
            (
                request_messages,
                token_preflight_diagnostics,
                history_shrink_reason,
                preflight_failure_reason,
            ) = self._apply_node_send_token_preflight(
                task_id=str(task.task_id),
                node_id=str(node.node_id),
                model_refs=current_model_refs,
                request_messages=request_messages,
                tool_schemas=tool_schemas,
                prompt_cache_key=turn_prompt_cache_key,
                tool_choice=current_tool_choice,
                parallel_tool_calls=(self._parallel_tool_calls_enabled if tool_schemas else None),
            )
            if str(history_shrink_reason or '').strip() == 'token_compression':
                committed_model_visible_tools, committed_tool_schema_selection = self._model_visible_tools_for_iteration(
                    task_id=task.task_id,
                    node_id=node.node_id,
                    node_kind=node.node_kind,
                    visible_tools=current_tools,
                    stage_gate=stage_gate,
                    runtime_context={
                        **dict(runtime_context or {}),
                        'provider_tool_exposure_commit_reason': 'token_compression',
                    },
                )
                committed_provider_tool_names = self._normalized_name_list(
                    list(
                        committed_tool_schema_selection.get('provider_tool_names')
                        or list(committed_model_visible_tools.keys())
                    )
                )
                if committed_provider_tool_names != provider_tool_names:
                    committed_tool_schemas = [
                        current_tools[name].to_model_schema()
                        for name in committed_provider_tool_names
                        if name in current_tools
                    ]
                    committed_prompt_cache_key = self._execution_prompt_cache_key(
                        model_messages=model_messages,
                        tool_schemas=committed_tool_schemas,
                        model_refs=current_model_refs,
                    )
                    committed_estimate_payload = self._estimate_node_send_preflight_tokens(
                        task_id=task.task_id,
                        node_id=node.node_id,
                        config=None,
                        model_refs=current_model_refs,
                        provider_model=str(token_preflight_diagnostics.get('provider_model') or ''),
                        request_messages=request_messages,
                        tool_schemas=committed_tool_schemas,
                        prompt_cache_key=str(committed_prompt_cache_key or '').strip(),
                        tool_choice=current_tool_choice,
                        parallel_tool_calls=(self._parallel_tool_calls_enabled if committed_tool_schemas else None),
                        allow_usage_ground_truth=False,
                    )
                    committed_final_tokens = int(committed_estimate_payload.get('final_estimate_tokens') or 0)
                    context_window_tokens = int(token_preflight_diagnostics.get('context_window_tokens') or 0)
                    if context_window_tokens <= 0 or committed_final_tokens <= context_window_tokens:
                        model_visible_tools = committed_model_visible_tools
                        tool_schema_selection = committed_tool_schema_selection
                        provider_tool_names = committed_provider_tool_names
                        tool_schemas = committed_tool_schemas
                        turn_prompt_cache_key = committed_prompt_cache_key
                        token_preflight_diagnostics = {
                            **dict(token_preflight_diagnostics or {}),
                            'estimated_total_tokens': committed_final_tokens,
                            'preview_estimate_tokens': int(
                                committed_estimate_payload.get('preview_estimate_tokens') or 0
                            ),
                            'usage_based_estimate_tokens': int(
                                committed_estimate_payload.get('usage_based_estimate_tokens') or 0
                            ),
                            'delta_estimate_tokens': int(
                                committed_estimate_payload.get('delta_estimate_tokens') or 0
                            ),
                            'effective_input_tokens': int(
                                committed_estimate_payload.get('effective_input_tokens') or 0
                            ),
                            'estimate_source': str(
                                committed_estimate_payload.get('estimate_source') or 'preview_estimate'
                            ),
                            'comparable_to_previous_request': bool(
                                committed_estimate_payload.get('comparable_to_previous_request')
                            ),
                            'final_estimate_tokens': committed_final_tokens,
                            'final_request_tokens': committed_final_tokens,
                            'provider_tool_exposure_commit_reason': 'token_compression',
                        }
            actual_request_diagnostics = build_actual_request_diagnostics(
                request_messages=request_messages,
                tool_schemas=tool_schemas,
            )
            allowed_content_refs = self._collect_content_refs(request_messages)
            self._log_service.update_node_input(task.task_id, node.node_id, json.dumps(model_messages, ensure_ascii=False, indent=2))
            tool_history = analyze_tool_call_history(request_messages)
            if tool_history.has_orphan_tool_results:
                orphan_tool_result_strikes += 1
                if orphan_tool_result_strikes >= _ORPHAN_TOOL_RESULT_THRESHOLD:
                    return self._orphan_tool_result_failure(
                        call_ids=tool_history.orphan_tool_result_ids,
                        strike_count=orphan_tool_result_strikes,
                    )
            self._log_service.upsert_frame(
                task.task_id,
                {
                    'node_id': node.node_id,
                    'depth': node.depth,
                    'node_kind': node.node_kind,
                    'phase': 'before_model',
                    'messages': model_messages,
                    'pending_tool_calls': [],
                    'pending_child_specs': [],
                    'partial_child_results': [],
                    'tool_calls': [],
                    'child_pipelines': [],
                    'callable_tool_names': list(tool_schema_selection.get('tool_names') or list(model_visible_tools.keys())),
                    'candidate_tool_names': list(tool_schema_selection.get('candidate_tool_names') or []),
                    'candidate_tool_items': [
                        dict(item)
                        for item in list(dynamic_contract_payload.get('candidate_tools') or [])
                        if isinstance(item, dict)
                    ],
                    'selected_skill_ids': list(selected_skill_ids),
                    'candidate_skill_ids': list(candidate_skill_ids),
                    'candidate_skill_items': [
                        dict(item)
                        for item in list(dynamic_contract_payload.get('candidate_skills') or [])
                        if isinstance(item, dict)
                    ],
                    'rbac_visible_tool_names': list(
                        (tool_schema_selection.get('trace') or {}).get('rbac_visible_tool_names') or []
                    ),
                    'rbac_visible_skill_ids': list(selected_skill_ids),
                    'lightweight_tool_ids': list(tool_schema_selection.get('lightweight_tool_ids') or []),
                    'hydrated_executor_state': list(tool_schema_selection.get('hydrated_executor_names') or []),
                    'hydrated_executor_names': list(tool_schema_selection.get('hydrated_executor_names') or []),
                    'model_visible_tool_names': list(tool_schema_selection.get('tool_names') or list(model_visible_tools.keys())),
                    'provider_tool_names': list(provider_tool_names),
                    'pending_provider_tool_names': list(
                        tool_schema_selection.get('pending_provider_tool_names') or []
                    ),
                    'provider_tool_exposure_pending': bool(
                        tool_schema_selection.get('provider_tool_exposure_pending')
                    ),
                    'provider_tool_exposure_revision': str(
                        tool_schema_selection.get('provider_tool_exposure_revision') or ''
                    ),
                    'provider_tool_exposure_commit_reason': str(
                        tool_schema_selection.get('provider_tool_exposure_commit_reason') or ''
                    ),
                    'model_visible_tool_selection_trace': dict(tool_schema_selection.get('trace') or {}),
                    'exec_runtime_policy': (
                        dict(dynamic_contract_payload.get('exec_runtime_policy') or {})
                        if isinstance(dynamic_contract_payload.get('exec_runtime_policy'), dict)
                        else None
                    ),
                    'prompt_cache_key_hash': (
                        hashlib.sha256(str(turn_prompt_cache_key).encode('utf-8')).hexdigest()
                        if str(turn_prompt_cache_key or '').strip()
                        else ''
                    ),
                    'actual_request_hash': str(actual_request_diagnostics.get('actual_request_hash') or ''),
                    'actual_request_message_count': int(actual_request_diagnostics.get('actual_request_message_count') or 0),
                    'actual_tool_schema_hash': str(actual_request_diagnostics.get('actual_tool_schema_hash') or ''),
                    'token_preflight_diagnostics': dict(token_preflight_diagnostics or {}),
                    'history_shrink_reason': str(history_shrink_reason or '').strip(),
                    **self._execution_stage_frame_payload(node_kind=node.node_kind, stage_gate=stage_gate),
                    'last_error': str(preflight_failure_reason or '').strip(),
                },
                publish_snapshot=True,
            )
            if str(preflight_failure_reason or '').strip():
                return self._token_preflight_failure(
                    reason=str(preflight_failure_reason or '').strip(),
                )
            node_turn_lease = None
            node_turn_controller = getattr(self, '_node_turn_controller', None)
            primary_model_ref = str(current_model_refs[0] or '').strip()
            if node_turn_controller is not None and primary_model_ref:
                node_turn_lease = await self._await_with_model_marker(
                    task_id=task.task_id,
                    node_id=node.node_id,
                    marker='node_turn.acquire',
                    awaitable=node_turn_controller.acquire_turn(
                        task_id=task.task_id,
                        node_id=node.node_id,
                        model_ref=primary_model_ref,
                    ),
                )
            provider_retry_count = 0
            empty_response_retry_count = 0
            restart_with_refreshed_runtime = False
            inflight_notice_callback_triggered = False
            try:
                while True:
                    self._check_pause_or_cancel(task.task_id)
                    try:
                        self._set_model_await_marker(
                            task_id=task.task_id,
                            node_id=node.node_id,
                            marker='model.chat.dispatch',
                            started_at=now_iso(),
                        )
                        chat_coro = self._chat_with_optional_extensions(
                            messages=request_messages,
                            tools=tool_schemas or None,
                            model_refs=current_model_refs,
                            tool_choice=current_tool_choice,
                            parallel_tool_calls=(self._parallel_tool_calls_enabled if tool_schemas else None),
                            prompt_cache_key=turn_prompt_cache_key,
                            node_turn_lease=node_turn_lease,
                            model_concurrency_controller=getattr(self, '_model_concurrency_controller', None),
                        )
                        timeout_seconds = self._resolved_model_response_timeout_seconds(
                            model_refs=current_model_refs,
                        )
                        if timeout_seconds is not None:
                            response = await self._await_with_model_marker(
                                task_id=task.task_id,
                                node_id=node.node_id,
                                marker='model.chat.await_response',
                                awaitable=asyncio.wait_for(chat_coro, timeout=timeout_seconds),
                            )
                        else:
                            response = await self._await_with_model_marker(
                                task_id=task.task_id,
                                node_id=node.node_id,
                                marker='model.chat.await_response',
                                awaitable=chat_coro,
                            )
                        consume_inflight_notice_callback = runtime_context.get('consume_inflight_notice_callback')
                        if callable(consume_inflight_notice_callback) and not inflight_notice_callback_triggered:
                            consume_inflight_notice_callback()
                            inflight_notice_callback_triggered = True
                        self._set_model_await_marker(
                            task_id=task.task_id,
                            node_id=node.node_id,
                            marker='model.chat.response_postprocess',
                            started_at=now_iso(),
                        )
                    except asyncio.TimeoutError:
                        timeout_message = self._model_response_timeout_message(timeout_seconds=timeout_seconds)
                        self._log_service.update_frame(
                            task.task_id,
                            node.node_id,
                            lambda frame: {
                                **frame,
                                'last_error': timeout_message,
                            },
                            publish_snapshot=True,
                        )
                        raise RuntimeError(timeout_message)
                    except Exception as exc:
                        if not self._is_provider_chain_exhausted_error(exc):
                            raise
                        if self._refresh_runtime_config_for_retry_invalidation():
                            self._log_service.update_frame(
                                task.task_id,
                                node.node_id,
                                lambda frame: {
                                    **frame,
                                    'last_error': 'Runtime config changed during provider retry; restarting with refreshed model chain.',
                                },
                                publish_snapshot=True,
                            )
                            restart_with_refreshed_runtime = True
                            break
                        provider_retry_count += 1
                        delay_seconds = self._provider_retry_delay_seconds(provider_retry_count)
                        self._log_service.update_frame(
                            task.task_id,
                            node.node_id,
                            lambda frame: {
                                **frame,
                                'last_error': (
                                    f'{PUBLIC_PROVIDER_FAILURE_MESSAGE} '
                                    f'Retrying automatically in {delay_seconds:.1f}s '
                                    f'(attempt {provider_retry_count}).'
                                ),
                            },
                            publish_snapshot=True,
                        )
                        await asyncio.sleep(delay_seconds)
                        continue
                    if self._is_empty_model_response(response):
                        if self._refresh_runtime_config_for_retry_invalidation():
                            self._log_service.update_frame(
                                task.task_id,
                                node.node_id,
                                lambda frame: {
                                    **frame,
                                    'last_error': 'Runtime config changed during empty-response retry; restarting with refreshed model chain.',
                                },
                                publish_snapshot=True,
                            )
                            restart_with_refreshed_runtime = True
                            break
                        empty_response_retry_count += 1
                        delay_seconds = self._empty_response_retry_delay_seconds(empty_response_retry_count)
                        self._log_service.update_frame(
                            task.task_id,
                            node.node_id,
                            lambda frame: {
                                **frame,
                                'last_error': (
                                    'Model returned an empty response with no text and no tool calls. '
                                    f'Retrying automatically in {delay_seconds:.1f}s '
                                    f'(attempt {empty_response_retry_count}).'
                                ),
                            },
                            publish_snapshot=True,
                        )
                        await asyncio.sleep(delay_seconds)
                        continue
                    break
            finally:
                self._set_model_await_marker(task_id=task.task_id, node_id=node.node_id, marker='')
                if node_turn_lease is not None and node_turn_controller is not None:
                    node_turn_controller.release_turn(node_turn_lease)
            if restart_with_refreshed_runtime:
                attempts = max(0, attempts - 1)
                continue
            visible_tool_names = {
                str(name or '').strip()
                for name in callable_visible_tools.keys()
                if str(name or '').strip()
            }
            response_tool_calls = list(response.tool_calls or [])
            synthetic_tool_calls_used = False
            xml_pseudo_call = None
            matched_raw_final_result_payload = False
            if not response_tool_calls and visible_tool_names:
                xml_extraction = self._extract_tool_calls_from_xml_pseudo_content(
                    response.content,
                    visible_tools=callable_visible_tools,
                )
                if xml_extraction.tool_calls:
                    response_tool_calls = xml_extraction.tool_calls
                    synthetic_tool_calls_used = True
                if not response_tool_calls and xml_repair_attempt_count > 0:
                    repaired_tool_calls = self._recover_tool_calls_from_json_payload(
                        response.content,
                        allowed_tool_names=visible_tool_names,
                    )
                    if repaired_tool_calls:
                        response_tool_calls = repaired_tool_calls
                        synthetic_tool_calls_used = True
                if not response_tool_calls and xml_extraction.matched:
                    xml_pseudo_call = {
                        'excerpt': xml_extraction.excerpt,
                        'tool_names': list(xml_extraction.tool_names or []),
                        'issue': str(xml_extraction.issue or '').strip(),
                    }
                if not response_tool_calls and FINAL_RESULT_TOOL_NAME in visible_tool_names:
                    repaired_final_call, matched_raw_final_result_payload = self._recover_final_result_tool_call_from_raw_json(
                        response.content,
                        attempt_auto_repair=invalid_final_submission_count > 0,
                    )
                    if repaired_final_call is not None:
                        response_tool_calls = [repaired_final_call]
                        synthetic_tool_calls_used = True
                if not response_tool_calls and STAGE_TOOL_NAME in visible_tool_names:
                    repaired_stage_call = self._recover_stage_submission_tool_call_from_context(
                        node=node,
                        stage_gate=stage_gate,
                        model_messages=model_messages,
                        invalid_stage_submission_count=invalid_stage_submission_count,
                    )
                    if repaired_stage_call is not None:
                        response_tool_calls = [repaired_stage_call]
                        synthetic_tool_calls_used = True
            tool_calls = [
                {
                    'id': call.id,
                    'name': call.name,
                    'arguments': self._normalize_tool_call_arguments(getattr(call, 'arguments', {})),
                }
                for call in response_tool_calls
            ]
            updated_node = self._log_service.append_node_output(
                task.task_id,
                node.node_id,
                content=str(response.content or ''),
                tool_calls=tool_calls,
                usage_attempts=list(response.attempts or []),
                model_messages=model_messages,
                request_messages=request_messages,
                prompt_cache_key=turn_prompt_cache_key,
                request_message_count=getattr(response, 'request_message_count', None),
                request_message_chars=getattr(response, 'request_message_chars', None),
                actual_tool_schemas=tool_schemas,
                callable_tool_names=list(tool_schema_selection.get('tool_names') or list(model_visible_tools.keys())),
                provider_tool_names=list(provider_tool_names),
                provider_tool_bundle_seeded=bool(
                    dict(tool_schema_selection.get('trace') or {}).get('provider_tool_bundle_seeded')
                ),
                provider_tool_exposure_revision=str(
                    tool_schema_selection.get('provider_tool_exposure_revision') or ''
                ),
                provider_tool_exposure_commit_reason=str(
                    tool_schema_selection.get('provider_tool_exposure_commit_reason') or ''
                ),
                provider_request_meta=getattr(response, 'provider_request_meta', None),
                provider_request_body=getattr(response, 'provider_request_body', None),
            )
            if response_tool_calls:
                if xml_repair_attempt_count > 0:
                    xml_repair_attempt_count = 0
                    xml_repair_excerpt = ''
                    xml_repair_tool_names = []
                    xml_repair_last_issue = ''
                final_result_turn = self._is_final_result_turn(response_tool_calls)
                final_result_mixed_turn = self._contains_tool_name(
                    response_tool_calls,
                    FINAL_RESULT_TOOL_NAME,
                ) and not final_result_turn
                stage_only_transition_turn = self._is_stage_only_transition_turn(response_tool_calls)
                ordinary_tool_turn = self._has_ordinary_tool_call(response_tool_calls)
                if ordinary_tool_turn:
                    invalid_final_submission_count = 0
                    invalid_stage_submission_count = 0
                    stage_only_transition_streak = 0
                    last_invalid_final_submission_reason = ''
                    last_invalid_stage_submission_reason = ''
                    self._clear_invalid_final_submission_state(
                        task_id=task.task_id,
                        node_id=node.node_id,
                    )
                control_only_turn = all(call.name in self._CONTROL_TOOL_NAMES for call in response_tool_calls)
                read_only_repeat_violations = self._read_only_repeat_violations(
                    response_tool_calls=response_tool_calls,
                    task_id=task.task_id,
                    node_kind=node.node_kind,
                    message_history=message_history,
                    prior_violation_counts=read_only_repeat_violation_counts,
                )
                if read_only_repeat_violations:
                    repair_messages: list[str] = []
                    threshold_violation: dict[str, Any] | None = None
                    for violation in read_only_repeat_violations:
                        signature = str(violation.get('signature') or '').strip()
                        if not signature:
                            continue
                        next_count = int(read_only_repeat_violation_counts.get(signature, 0) or 0) + 1
                        read_only_repeat_violation_counts[signature] = next_count
                        repair_text = str(violation.get('repair_text') or '').strip()
                        if repair_text and repair_text not in repair_messages:
                            repair_messages.append(repair_text)
                        if next_count >= _READ_ONLY_REPEAT_SOFT_REJECT_LIMIT and threshold_violation is None:
                            threshold_violation = {
                                **violation,
                                'count': next_count,
                            }
                    combined_repair_text = '\n\n'.join(repair_messages).strip()
                    error_content = (
                        f'Error: {combined_repair_text}'
                        if combined_repair_text
                        else 'Error: repeated read-only retrieval call requires repair'
                    )
                    pending_request_delta_messages = self._rejected_tool_turn_delta_messages(
                        node=node,
                        response=response,
                        response_tool_calls=response_tool_calls,
                        message_history=message_history,
                        runtime_context=runtime_context,
                        error_content=error_content,
                    )
                    message_history = self._record_rejected_tool_turn(
                        task=task,
                        node=node,
                        response=response,
                        response_tool_calls=response_tool_calls,
                        message_history=message_history,
                        runtime_context=runtime_context,
                        error_content=error_content,
                    )
                    if threshold_violation is not None:
                        return self._read_only_repeat_failure(
                            signature=str(threshold_violation.get('signature') or '').strip(),
                            count=int(threshold_violation.get('count') or 0),
                            repair_text=str(threshold_violation.get('repair_text') or '').strip(),
                        )
                    repair_overlay_text = combined_repair_text
                    continue
                if final_result_mixed_turn:
                    pending_request_delta_messages = self._rejected_tool_turn_delta_messages(
                        node=node,
                        response=response,
                        response_tool_calls=response_tool_calls,
                        message_history=message_history,
                        runtime_context=runtime_context,
                        error_content=self._exclusive_tool_turn_error(FINAL_RESULT_TOOL_NAME),
                    )
                    message_history = self._record_rejected_tool_turn(
                        task=task,
                        node=node,
                        response=response,
                        response_tool_calls=response_tool_calls,
                        message_history=message_history,
                        runtime_context=runtime_context,
                        error_content=self._exclusive_tool_turn_error(FINAL_RESULT_TOOL_NAME),
                    )
                    invalid_final_submission_count += 1
                    last_contract_violations = [
                        f'{FINAL_RESULT_TOOL_NAME} must be the only tool call in its turn',
                    ]
                    last_invalid_final_submission_reason = '; '.join(last_contract_violations)
                    if invalid_final_submission_count >= _INVALID_FINAL_SUBMISSION_LIMIT:
                        return self._invalid_final_submission_failure(
                            reason=last_invalid_final_submission_reason,
                            count=invalid_final_submission_count,
                        )
                    repair_overlay_text = self._result_contract_violation_message(
                        last_contract_violations,
                        node_kind=node.node_kind,
                    )
                    continue
                if final_result_turn:
                    terminal_result, next_history, contract_violations, protocol_error = await self._handle_final_result_tool_turn(
                        task=task,
                        node=node,
                        response=response,
                        tool_call=response_tool_calls[0],
                        tools=tools,
                        message_history=message_history,
                        runtime_context=runtime_context,
                        assistant_content=None if synthetic_tool_calls_used else response.content,
                    )
                    message_history = next_history
                    if terminal_result is not None:
                        self._log_service.remove_frame(task.task_id, node.node_id, publish_snapshot=True)
                        return terminal_result
                    reason_parts = list(contract_violations or [])
                    if protocol_error:
                        reason_parts.append(protocol_error)
                    return self._invalid_final_submission_failure(
                        reason='; '.join(reason_parts) or f'{FINAL_RESULT_TOOL_NAME} rejected',
                        count=1,
                    )
                duplicate_call_violations: list[dict[str, Any]] = []
                for call in response_tool_calls:
                    tool_name = str(getattr(call, 'name', '') or '').strip()
                    arguments = self._normalize_tool_call_arguments(getattr(call, 'arguments', {}))
                    signature = f"{tool_name}:{json.dumps(arguments, ensure_ascii=False, sort_keys=True)}"
                    if tool_name in self._CONTROL_TOOL_NAMES or tool_name in {STAGE_TOOL_NAME, FINAL_RESULT_TOOL_NAME}:
                        continue
                    repeated_signature = breaker.register(signature)
                    if repeated_signature:
                        duplicate_call_violations.append(
                            {
                                'signature': repeated_signature,
                                'tool_name': tool_name,
                                'arguments': arguments,
                            }
                        )
                if duplicate_call_violations:
                    repair_messages: list[str] = []
                    for violation in duplicate_call_violations:
                        repair_text = self._duplicate_tool_call_repair_message(
                            tool_name=str(violation.get('tool_name') or '').strip(),
                            arguments=dict(violation.get('arguments') or {}),
                        )
                        if repair_text and repair_text not in repair_messages:
                            repair_messages.append(repair_text)
                    combined_repair_text = '\n\n'.join(repair_messages).strip()
                    error_content = (
                        f'Error: {combined_repair_text}'
                        if combined_repair_text
                        else 'Error: duplicate tool call detected; reuse the prior result or change the arguments before retrying'
                    )
                    pending_request_delta_messages = self._rejected_tool_turn_delta_messages(
                        node=node,
                        response=response,
                        response_tool_calls=response_tool_calls,
                        message_history=message_history,
                        runtime_context=runtime_context,
                        error_content=error_content,
                    )
                    message_history = self._record_rejected_tool_turn(
                        task=task,
                        node=node,
                        response=response,
                        response_tool_calls=response_tool_calls,
                        message_history=message_history,
                        runtime_context=runtime_context,
                        error_content=error_content,
                    )
                    repair_overlay_text = combined_repair_text
                    continue
                active_round_payload = None
                if self._should_record_execution_stage_round(
                    node_kind=node.node_kind,
                    stage_gate=stage_gate,
                    response_tool_calls=tool_calls,
                ):
                    created_at = ''
                    if updated_node is not None and list(getattr(updated_node, 'output', []) or []):
                        created_at = str(updated_node.output[-1].created_at or '')
                    active_round_payload = self._log_service.record_execution_stage_round(
                        task.task_id,
                        node.node_id,
                        tool_calls=tool_calls,
                        created_at=created_at or now_iso(),
                    )
                assistant_tool_calls = [
                    {
                        'id': call.id,
                        'type': 'function',
                        'function': {'name': call.name, 'arguments': json.dumps(call.arguments, ensure_ascii=False)},
                    }
                    for call in response_tool_calls
                ]
                live_tool_calls = [self._live_tool_entry(call) for call in response_tool_calls]
                self._log_service.update_frame(
                    task.task_id,
                    node.node_id,
                    lambda frame: {
                        **frame,
                        'depth': node.depth,
                        'node_kind': node.node_kind,
                        'phase': 'waiting_tool_results',
                        'messages': model_messages,
                        'pending_tool_calls': tool_calls,
                        'active_round_id': str((active_round_payload or {}).get('round_id') or ''),
                        'active_round_tool_call_ids': [
                            str(item.get('id') or '').strip()
                            for item in tool_calls
                            if str(item.get('id') or '').strip()
                        ],
                        'active_round_started_at': str((active_round_payload or {}).get('created_at') or now_iso()),
                        'tool_calls': live_tool_calls,
                        **self._execution_stage_frame_payload(
                            node_kind=node.node_kind,
                            stage_gate=self._execution_stage_gate(
                                task_id=task.task_id,
                                node_id=node.node_id,
                                node_kind=node.node_kind,
                            ),
                        ),
                        'last_error': '',
                    },
                    publish_snapshot=True,
                )
                results = await self._execute_tool_calls(
                    task=task,
                    node=node,
                    response_tool_calls=response_tool_calls,
                    tools=current_tools,
                    allowed_content_refs=allowed_content_refs,
                    runtime_context={
                        **runtime_context,
                        'stage_turn_granted': bool(
                            stage_gate.get('enabled')
                            and stage_gate.get('has_active_stage')
                            and not stage_gate.get('transition_required')
                        ),
                    },
                    prior_overflow_signatures=self._overflowed_search_signatures(message_history),
                    max_parallel_tool_calls=max_parallel_tool_calls,
                )
                self._promote_tool_context_hydration_after_results(
                    task_id=task.task_id,
                    node_id=node.node_id,
                    response_tool_calls=response_tool_calls,
                    results=results,
                    runtime_context=runtime_context,
                )
                assistant_message = {
                    'role': 'assistant',
                    'content': self._externalize_message_content(
                        None if synthetic_tool_calls_used else response.content,
                        runtime_context=runtime_context,
                        display_name=f'assistant:{node.node_id}',
                        source_kind='assistant_message',
                    ),
                    'tool_calls': assistant_tool_calls,
                }
                record_tool_results = getattr(self._log_service, 'record_tool_result_batch', None)
                if callable(record_tool_results):
                    record_tool_results(
                        task_id=task.task_id,
                        node_id=node.node_id,
                        response_tool_calls=response_tool_calls,
                        results=results,
                    )
                message_history.append(assistant_message)
                tool_messages = self._dedupe_tool_messages(
                    [item['tool_message'] for item in results],
                    existing_messages=message_history,
                )
                pending_request_delta_messages = [
                    dict(assistant_message),
                    *[dict(item) for item in list(tool_messages or []) if isinstance(item, dict)],
                ]
                message_history.extend(tool_messages)
                prepared_history = self._prepare_messages(message_history, runtime_context=runtime_context)
                self._log_service.update_node_input(
                    task.task_id,
                    node.node_id,
                    json.dumps(prepared_history, ensure_ascii=False, indent=2),
                )
                self._log_service.update_frame(
                    task.task_id,
                    node.node_id,
                    lambda frame: {
                        **frame,
                        'depth': node.depth,
                        'node_kind': node.node_kind,
                        'phase': 'waiting_tool_results',
                        'messages': prepared_history,
                        'pending_tool_calls': [],
                        'active_round_id': '',
                        'active_round_tool_call_ids': [],
                        'active_round_started_at': '',
                        'tool_calls': [item['live_state'] for item in results],
                        **self._execution_stage_frame_payload(
                            node_kind=node.node_kind,
                            stage_gate=self._execution_stage_gate(
                                task_id=task.task_id,
                                node_id=node.node_id,
                                node_kind=node.node_kind,
                            ),
                        ),
                        'last_error': '',
                    },
                    publish_snapshot=True,
                )
                message_history = list(prepared_history)
                if stage_only_transition_turn:
                    stage_turn_succeeded = self._tool_results_succeeded(results)
                    stage_goal = str((tool_calls[0].get('arguments') or {}).get('stage_goal') or '').strip()
                    if stage_turn_succeeded:
                        invalid_stage_submission_count = 0
                        last_invalid_stage_submission_reason = ''
                        stage_only_transition_streak += 1
                        if stage_only_transition_streak >= _STAGE_ONLY_TRANSITION_LIMIT:
                            return self._stage_only_transition_failure(
                                count=stage_only_transition_streak,
                                stage_goal=stage_goal,
                            )
                    else:
                        stage_only_transition_streak = 0
                        invalid_stage_submission_count += 1
                        last_invalid_stage_submission_reason = (
                            self._first_tool_error_text(results) or f'{STAGE_TOOL_NAME} rejected'
                        )
                        if invalid_stage_submission_count >= _INVALID_STAGE_SUBMISSION_LIMIT:
                            return self._invalid_stage_submission_failure(
                                reason=last_invalid_stage_submission_reason,
                                count=invalid_stage_submission_count,
                                stage_goal=stage_goal,
                            )
                        repair_overlay_text = self._stage_submission_repair_message(
                            reason=last_invalid_stage_submission_reason,
                            node_kind=node.node_kind,
                        )
                if control_only_turn:
                    attempts = max(0, attempts - 1)
                continue

            if str(response.finish_reason or '').strip().lower() == 'error':
                error_text = str(getattr(response, 'error_text', None) or response.content or 'model response failed').strip() or 'model response failed'
                raise RuntimeError(error_text)

            if xml_pseudo_call is not None:
                xml_repair_attempt_count += 1
                xml_repair_excerpt = str(xml_pseudo_call.get('excerpt') or '').strip()
                xml_repair_tool_names = list(xml_pseudo_call.get('tool_names') or [])
                xml_repair_last_issue = (
                    str(xml_pseudo_call.get('issue') or '').strip()
                    or 'reply used XML-like pseudo tool syntax instead of a valid tool call'
                )
                if xml_repair_attempt_count >= XML_REPAIR_ATTEMPT_LIMIT:
                    return self._xml_repair_failure(
                        count=xml_repair_attempt_count,
                        tool_names=xml_repair_tool_names,
                        content_excerpt=xml_repair_excerpt,
                    )
                repair_overlay_text = self._xml_tool_repair_message(
                    xml_excerpt=xml_repair_excerpt,
                    tool_names=xml_repair_tool_names,
                    attempt_count=xml_repair_attempt_count,
                    attempt_limit=XML_REPAIR_ATTEMPT_LIMIT,
                    latest_issue=xml_repair_last_issue,
                )
                continue

            if xml_repair_attempt_count > 0:
                xml_repair_attempt_count += 1
                xml_repair_last_issue = 'reply still did not contain valid structured tool_calls or a valid JSON repair payload'
                if xml_repair_attempt_count >= XML_REPAIR_ATTEMPT_LIMIT:
                    return self._xml_repair_failure(
                        count=xml_repair_attempt_count,
                        tool_names=xml_repair_tool_names,
                        content_excerpt=str(response.content or ''),
                    )
                repair_overlay_text = self._xml_tool_repair_message(
                    xml_excerpt=xml_repair_excerpt,
                    tool_names=xml_repair_tool_names,
                    attempt_count=xml_repair_attempt_count,
                    attempt_limit=XML_REPAIR_ATTEMPT_LIMIT,
                    latest_issue=xml_repair_last_issue,
                )
                continue

            stage_protocol_message = (
                build_execution_stage_result_block_message(
                    node_kind=node.node_kind,
                    stage_gate=stage_gate,
                )
                if bool(stage_gate.get('enabled'))
                else ''
            )
            if bool(stage_gate.get('enabled')) and stage_protocol_message:
                invalid_stage_submission_count += 1
                last_invalid_stage_submission_reason = (
                    str(stage_protocol_message or '').strip()
                    or 'reply did not use tools, submit_next_stage, or submit_final_result'
                )
                if invalid_stage_submission_count >= _INVALID_STAGE_SUBMISSION_LIMIT:
                    active_stage = stage_gate.get('active_stage') if isinstance(stage_gate.get('active_stage'), dict) else {}
                    return self._invalid_stage_submission_failure(
                        reason=last_invalid_stage_submission_reason,
                        count=invalid_stage_submission_count,
                        stage_goal=str((active_stage or {}).get('stage_goal') or ''),
                    )
                repair_overlay_text = stage_protocol_message or build_execution_stage_overlay(
                    node_kind=node.node_kind,
                    stage_gate=stage_gate,
                )
                continue

            auto_wrapped_final_call = (
                None
                if matched_raw_final_result_payload
                else self._wrap_plain_text_final_result_tool_call(
                    response_content=response.content,
                    message_history=message_history,
                )
            )
            if auto_wrapped_final_call is not None:
                terminal_result, next_history, contract_violations, protocol_error = await self._handle_final_result_tool_turn(
                    task=task,
                    node=node,
                    response=response,
                    tool_call=auto_wrapped_final_call,
                    tools=tools,
                    message_history=message_history,
                    runtime_context=runtime_context,
                    assistant_content=response.content,
                )
                message_history = next_history
                if terminal_result is not None:
                    self._log_service.remove_frame(task.task_id, node.node_id, publish_snapshot=True)
                    return terminal_result
                reason_parts = list(contract_violations or [])
                if protocol_error:
                    reason_parts.append(protocol_error)
                return self._invalid_final_submission_failure(
                    reason='; '.join(reason_parts) or f'{FINAL_RESULT_TOOL_NAME} rejected',
                    count=1,
                )

            invalid_final_submission_count += 1
            last_contract_violations = []
            last_invalid_final_submission_reason = (
                f'final result must be submitted via {FINAL_RESULT_TOOL_NAME}'
            )
            self._persist_invalid_final_submission_state(
                task_id=task.task_id,
                node_id=node.node_id,
                count=invalid_final_submission_count,
                reason=last_invalid_final_submission_reason,
                violations=last_contract_violations,
            )
            if invalid_final_submission_count >= _INVALID_FINAL_SUBMISSION_LIMIT:
                return self._invalid_final_submission_failure(
                    reason=last_invalid_final_submission_reason,
                    count=invalid_final_submission_count,
                )
            repair_overlay_text = self._result_protocol_message(node_kind=node.node_kind)

        if last_contract_violations:
            raise RuntimeError('result contract violation: ' + '; '.join(last_contract_violations))
        raise RuntimeError('node exceeded maximum ReAct iterations')

    async def _resume_pending_tool_turn_if_needed(
        self,
        *,
        task,
        node,
        message_history: list[dict[str, Any]],
        tools: dict[str, Tool],
        runtime_context: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        frame = self._runtime_frame(task.task_id, node.node_id)
        if not isinstance(frame, dict):
            return None
        pending_tool_calls = [
            dict(item)
            for item in list(frame.get('pending_tool_calls') or [])
            if isinstance(item, dict) and str(item.get('id') or '').strip() and str(item.get('name') or '').strip()
        ]
        if not pending_tool_calls:
            return None

        live_tool_map = {
            str(item.get('tool_call_id') or '').strip(): dict(item)
            for item in list(frame.get('tool_calls') or [])
            if isinstance(item, dict) and str(item.get('tool_call_id') or '').strip()
        }
        assistant_content = self._pending_tool_turn_content(node=node, pending_tool_calls=pending_tool_calls)
        assistant_tool_calls = [
            {
                'id': str(item.get('id') or ''),
                'type': 'function',
                'function': {
                    'name': str(item.get('name') or ''),
                    'arguments': json.dumps(self._normalize_tool_call_arguments(item.get('arguments')), ensure_ascii=False),
                },
            }
            for item in pending_tool_calls
        ]
        allowed_content_refs = self._collect_content_refs(message_history)
        round_id = str(frame.get('active_round_id') or '').strip()
        replay_calls: list[Any] = []
        ordered_results: list[dict[str, Any] | None] = []
        result_indexes_by_call_id: dict[str, list[int]] = {}
        inspected_items: list[dict[str, Any]] = []

        for index, item in enumerate(pending_tool_calls):
            call_id = str(item.get('id') or '').strip()
            tool_name = str(item.get('name') or '').strip() or 'tool'
            arguments = self._normalize_tool_call_arguments(item.get('arguments'))
            live_state = dict(live_tool_map.get(call_id) or {})
            inspection = self._recovery_check_engine.inspect_tool_call(
                tool_name=tool_name,
                arguments=arguments,
                runtime_context=runtime_context,
            )
            decision = inspection.decision
            if decision == RecoveryCheckDecision.RERUN_SAFE and tool_name not in tools:
                decision = RecoveryCheckDecision.MODEL_DECIDE
            inspected_item = {
                'call': dict(item),
                'tool_name': tool_name,
                'decision': decision,
                'expected_tool_status': (
                    inspection.expected_tool_status
                    if decision != RecoveryCheckDecision.MODEL_DECIDE
                    else 'interrupted'
                ),
                'lost_result_summary': str(inspection.lost_result_summary or '').strip(),
                'evidence': [dict(evidence) for evidence in list(inspection.evidence or []) if isinstance(evidence, dict)],
                'live_state': live_state,
            }
            inspected_items.append(inspected_item)
            if decision == RecoveryCheckDecision.VERIFIED_DONE:
                self._record_recovery_resolution_tool_result(
                    task_id=task.task_id,
                    node_id=node.node_id,
                    round_id=round_id,
                    item=inspected_item,
                )
                synthetic_live_state = self._resume_live_tool_state(
                    live_state,
                    call_id=call_id,
                    tool_name=tool_name,
                    status='success',
                )
                synthetic_live_state['finished_at'] = now_iso()
                ordered_results.append(
                    {
                        'live_state': synthetic_live_state,
                        'tool_message': self._resume_tool_message(
                            synthetic_live_state,
                            call_id=call_id,
                            tool_name=tool_name,
                            content=self._recovery_checked_tool_content(inspected_item),
                            status='success',
                        ),
                    }
                )
                continue
            if decision == RecoveryCheckDecision.MODEL_DECIDE:
                self._record_recovery_resolution_tool_result(
                    task_id=task.task_id,
                    node_id=node.node_id,
                    round_id=round_id,
                    item=inspected_item,
                )
                synthetic_live_state = self._resume_live_tool_state(
                    live_state,
                    call_id=call_id,
                    tool_name=tool_name,
                    status='interrupted',
                )
                synthetic_live_state['finished_at'] = now_iso()
                ordered_results.append(
                    {
                        'live_state': synthetic_live_state,
                        'tool_message': self._resume_tool_message(
                            synthetic_live_state,
                            call_id=call_id,
                            tool_name=tool_name,
                            content=self._recovery_checked_tool_content(inspected_item),
                            status='interrupted',
                        ),
                    }
                )
                continue
            ordered_results.append(None)
            result_indexes_by_call_id.setdefault(call_id, []).append(index)
            replay_calls.append(
                SimpleNamespace(
                    id=call_id,
                    name=tool_name,
                    arguments=arguments,
                )
            )

        overall_decision = self._recovery_check_overall_decision(inspected_items)
        self._record_recovery_check_tool_result(
            task_id=task.task_id,
            node_id=node.node_id,
            round_id=round_id,
            overall_decision=overall_decision,
            inspected_items=inspected_items,
        )

        if replay_calls:
            replay_results = await self._execute_tool_calls(
                task=task,
                node=node,
                response_tool_calls=replay_calls,
                tools=tools,
                allowed_content_refs=allowed_content_refs,
                runtime_context={
                    **runtime_context,
                    'stage_turn_granted': True,
                },
                prior_overflow_signatures=self._overflowed_search_signatures(message_history),
            )
            record_tool_results = getattr(self._log_service, 'record_tool_result_batch', None)
            if callable(record_tool_results):
                record_tool_results(
                    task_id=task.task_id,
                    node_id=node.node_id,
                    response_tool_calls=replay_calls,
                    results=replay_results,
                )
            for result in replay_results:
                tool_message = dict(result.get('tool_message') or {})
                call_id = str(tool_message.get('tool_call_id') or '').strip()
                for index in result_indexes_by_call_id.pop(call_id, []):
                    ordered_results[index] = result

        for index, item in enumerate(ordered_results):
            if item is not None:
                continue
            pending_item = pending_tool_calls[index]
            call_id = str(pending_item.get('id') or '').strip()
            tool_name = str(pending_item.get('name') or '').strip()
            ordered_results[index] = {
                'live_state': self._resume_live_tool_state(
                    live_tool_map.get(call_id),
                    call_id=call_id,
                    tool_name=tool_name,
                    status='error',
                ),
                'tool_message': self._resume_tool_message(
                    live_tool_map.get(call_id),
                    call_id=call_id,
                    tool_name=tool_name,
                    content=f'Error: failed to resume tool call: {tool_name}',
                    status='error',
                ),
            }

        resumed_history = list(message_history)
        resumed_history.append(
            {
                'role': 'assistant',
                'content': assistant_content,
                'tool_calls': assistant_tool_calls,
            }
        )
        tool_messages = self._dedupe_tool_messages(
            [dict(item.get('tool_message') or {}) for item in ordered_results if isinstance(item, dict)],
            existing_messages=resumed_history,
        )
        resumed_history.extend(tool_messages)
        prepared_history = self._prepare_messages(resumed_history, runtime_context=runtime_context)
        self._log_service.update_node_input(
            task.task_id,
            node.node_id,
            json.dumps(prepared_history, ensure_ascii=False, indent=2),
        )
        self._log_service.update_frame(
            task.task_id,
            node.node_id,
            lambda current: {
                **current,
                'depth': node.depth,
                'node_kind': node.node_kind,
                'phase': 'before_model',
                'messages': prepared_history,
                'pending_tool_calls': [],
                'active_round_id': '',
                'active_round_tool_call_ids': [],
                'active_round_started_at': '',
                'tool_calls': [dict(item.get('live_state') or {}) for item in ordered_results if isinstance(item, dict)],
                **self._execution_stage_frame_payload(
                    node_kind=node.node_kind,
                    stage_gate=self._execution_stage_gate(
                        task_id=task.task_id,
                        node_id=node.node_id,
                        node_kind=node.node_kind,
                    ),
                ),
                'last_error': '',
            },
            publish_snapshot=True,
        )
        return prepared_history

    async def _resume_waiting_children_turn_if_needed(
        self,
        *,
        task,
        node,
        message_history: list[dict[str, Any]],
        tools: dict[str, Tool],
        runtime_context: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        frame = self._runtime_frame(task.task_id, node.node_id)
        if not isinstance(frame, dict):
            return None
        if str(frame.get('phase') or '').strip() != 'waiting_children':
            return None
        if list(frame.get('pending_tool_calls') or []):
            return None

        replay_calls = self._recover_waiting_children_tool_calls(node=node)
        if not replay_calls:
            return None

        pending_tool_calls = [
            {
                'id': str(getattr(call, 'id', '') or ''),
                'name': str(getattr(call, 'name', '') or ''),
                'arguments': self._normalize_tool_call_arguments(getattr(call, 'arguments', {})),
            }
            for call in replay_calls
        ]
        assistant_content = self._pending_tool_turn_content(node=node, pending_tool_calls=pending_tool_calls)
        if not str(assistant_content or '').strip():
            assistant_content = 'Resuming interrupted spawn_child_nodes round after waiting_children recovery.'
        assistant_tool_calls = [
            {
                'id': str(call.id or ''),
                'type': 'function',
                'function': {
                    'name': str(call.name or ''),
                    'arguments': json.dumps(self._normalize_tool_call_arguments(getattr(call, 'arguments', {})), ensure_ascii=False),
                },
            }
            for call in replay_calls
        ]
        allowed_content_refs = self._collect_content_refs(message_history)
        replay_results = await self._execute_tool_calls(
            task=task,
            node=node,
            response_tool_calls=replay_calls,
            tools=tools,
            allowed_content_refs=allowed_content_refs,
            runtime_context={
                **runtime_context,
                'stage_turn_granted': True,
            },
            prior_overflow_signatures=self._overflowed_search_signatures(message_history),
        )
        record_tool_results = getattr(self._log_service, 'record_tool_result_batch', None)
        if callable(record_tool_results):
            record_tool_results(
                task_id=task.task_id,
                node_id=node.node_id,
                response_tool_calls=replay_calls,
                results=replay_results,
            )

        resumed_history = list(message_history)
        resumed_history.append(
            {
                'role': 'assistant',
                'content': assistant_content,
                'tool_calls': assistant_tool_calls,
            }
        )
        tool_messages = self._dedupe_tool_messages(
            [dict(item.get('tool_message') or {}) for item in replay_results if isinstance(item, dict)],
            existing_messages=resumed_history,
        )
        resumed_history.extend(tool_messages)
        prepared_history = self._prepare_messages(resumed_history, runtime_context=runtime_context)
        self._log_service.update_node_input(
            task.task_id,
            node.node_id,
            json.dumps(prepared_history, ensure_ascii=False, indent=2),
        )
        self._log_service.update_frame(
            task.task_id,
            node.node_id,
            lambda current: {
                **current,
                'depth': node.depth,
                'node_kind': node.node_kind,
                'phase': 'before_model',
                'messages': prepared_history,
                'pending_tool_calls': [],
                'active_round_id': '',
                'active_round_tool_call_ids': [],
                'active_round_started_at': '',
                'tool_calls': [dict(item.get('live_state') or {}) for item in replay_results if isinstance(item, dict)],
                'child_pipelines': [],
                'pending_child_specs': [],
                'partial_child_results': [],
                **self._execution_stage_frame_payload(
                    node_kind=node.node_kind,
                    stage_gate=self._execution_stage_gate(
                        task_id=task.task_id,
                        node_id=node.node_id,
                        node_kind=node.node_kind,
                    ),
                ),
                'last_error': '',
            },
            publish_snapshot=True,
        )
        return prepared_history

    def _runtime_frame(self, task_id: str, node_id: str) -> dict[str, Any] | None:
        frame = self._log_service.read_runtime_frame(task_id, node_id)
        return dict(frame or {}) if frame is not None else None

    def _persist_invalid_final_submission_state(
        self,
        *,
        task_id: str,
        node_id: str,
        count: int,
        reason: str,
        violations: list[str],
    ) -> None:
        self._log_service.update_frame(
            task_id,
            node_id,
            lambda current: {
                **current,
                'invalid_final_submission_count': max(0, int(count or 0)),
                'last_invalid_final_submission_reason': str(reason or '').strip(),
                'last_contract_violations': [
                    str(item or '').strip()
                    for item in list(violations or [])
                    if str(item or '').strip()
                ],
            },
            publish_snapshot=True,
        )

    def _clear_invalid_final_submission_state(self, *, task_id: str, node_id: str) -> None:
        self._log_service.update_frame(
            task_id,
            node_id,
            lambda current: {
                **current,
                'invalid_final_submission_count': 0,
                'last_invalid_final_submission_reason': '',
                'last_contract_violations': [],
            },
            publish_snapshot=True,
        )

    @staticmethod
    def _recover_waiting_children_tool_calls(*, node) -> list[Any]:
        metadata = dict(getattr(node, 'metadata', {}) or {})
        spawn_operations = dict(metadata.get('spawn_operations') or {})
        replay_calls: list[Any] = []
        for cache_key, payload in spawn_operations.items():
            if not isinstance(payload, dict) or bool(payload.get('completed')):
                continue
            specs: list[dict[str, Any]] = []
            for item in list(payload.get('specs') or []):
                if not isinstance(item, dict):
                    continue
                try:
                    normalized = SpawnChildSpec.model_validate(item).model_dump(mode='json', exclude_none=True)
                except Exception:
                    continue
                specs.append(dict(normalized))
            if not specs:
                continue
            replay_calls.append(
                SimpleNamespace(
                    id=str(cache_key or '').strip(),
                    name=_STAGE_SPAWN_TOOL_NAME,
                    arguments={'children': specs},
                )
            )
        return replay_calls

    def _pending_tool_turn_content(self, *, node, pending_tool_calls: list[dict[str, Any]]) -> str:
        pending_ids = [str(item.get('id') or '').strip() for item in list(pending_tool_calls or []) if str(item.get('id') or '').strip()]
        for entry in reversed(list(getattr(node, 'output', []) or [])):
            entry_tool_calls = [
                str(item.get('id') or '').strip()
                for item in list(getattr(entry, 'tool_calls', []) or [])
                if isinstance(item, dict) and str(item.get('id') or '').strip()
            ]
            if pending_ids and entry_tool_calls != pending_ids:
                continue
            ref = str(getattr(entry, 'content_ref', '') or '').strip()
            if ref:
                resolved = self._resolve_content_ref(ref)
                if str(resolved or '').strip():
                    return str(resolved or '')
            text = str(getattr(entry, 'content', '') or '')
            if text.strip():
                return text
        return ''

    def _resolve_content_ref(self, ref: str) -> str:
        content_store = getattr(self._log_service, '_content_store', None)
        resolver = getattr(content_store, '_resolve', None) if content_store is not None else None
        if not callable(resolver):
            return ''
        try:
            text, _handle = resolver(ref=ref, path=None)
        except Exception:
            return ''
        return str(text or '')

    @staticmethod
    def _resume_live_tool_state(
        live_state: dict[str, Any] | None,
        *,
        call_id: str,
        tool_name: str,
        status: str | None = None,
    ) -> dict[str, Any]:
        payload = dict(live_state or {})
        payload['tool_call_id'] = str(call_id or '')
        payload['tool_name'] = str(payload.get('tool_name') or tool_name or 'tool')
        payload['status'] = str(status or payload.get('status') or 'error')
        payload.setdefault('started_at', '')
        payload.setdefault('finished_at', '')
        payload.setdefault('elapsed_seconds', None)
        payload['ephemeral'] = bool(payload.get('ephemeral'))
        return payload

    @staticmethod
    def _resume_tool_message(
        live_state: dict[str, Any] | None,
        *,
        call_id: str,
        tool_name: str,
        content: str,
        status: str | None = None,
    ) -> dict[str, Any]:
        payload = dict(live_state or {})
        message_status = str(status or payload.get('status') or '').strip().lower()
        return {
            'role': 'tool',
            'tool_call_id': str(call_id or ''),
            'name': str(tool_name or payload.get('tool_name') or 'tool'),
            'content': str(content or ''),
            'started_at': str(payload.get('started_at') or ''),
            'finished_at': str(payload.get('finished_at') or ''),
            'elapsed_seconds': payload.get('elapsed_seconds'),
            'status': message_status,
            'ephemeral': bool(payload.get('ephemeral')),
        }

    @staticmethod
    def _recovery_check_tool_call_id(round_id: str, pending_tool_calls: list[dict[str, Any]]) -> str:
        normalized_round_id = str(round_id or '').strip()
        if normalized_round_id:
            return f'recovery_check:{normalized_round_id}'
        first_call_id = str(((pending_tool_calls or [{}])[0]).get('id') or '').strip()
        return f'recovery_check:{first_call_id or "pending"}'

    @staticmethod
    def _recovery_check_overall_decision(items: list[dict[str, Any]]) -> RecoveryCheckDecision:
        decisions = [item.get('decision') for item in list(items or [])]
        if any(decision == RecoveryCheckDecision.MODEL_DECIDE for decision in decisions):
            return RecoveryCheckDecision.MODEL_DECIDE
        if any(decision == RecoveryCheckDecision.RERUN_SAFE for decision in decisions):
            return RecoveryCheckDecision.RERUN_SAFE
        return RecoveryCheckDecision.VERIFIED_DONE

    @staticmethod
    def _recovery_check_summary_text(items: list[dict[str, Any]]) -> str:
        lines = ['Recovery check executed before resuming interrupted tool round.']
        for item in list(items or []):
            tool_name = str(item.get('tool_name') or 'tool').strip() or 'tool'
            decision = str(item.get('decision') or '').strip() or RecoveryCheckDecision.MODEL_DECIDE
            summary = str(item.get('lost_result_summary') or '').strip()
            lines.append(f'- {tool_name}: {decision}{f" | {summary}" if summary else ""}')
        return '\n'.join(lines)

    @staticmethod
    def _recovery_checked_tool_content(item: dict[str, Any]) -> str:
        tool_name = str(item.get('tool_name') or 'tool').strip() or 'tool'
        summary = str(item.get('lost_result_summary') or '').strip()
        decision = item.get('decision')
        if decision == RecoveryCheckDecision.VERIFIED_DONE:
            return f'Recovery check confirmed that the previous {tool_name} attempt already completed. {summary}'.strip()
        if decision == RecoveryCheckDecision.RERUN_SAFE:
            return f'Recovery check marked the previous {tool_name} attempt as safe to rerun. {summary}'.strip()
        return (
            f'Recovery check: the previous {tool_name} attempt may have already produced side effects, '
            f'but its result was lost during shutdown. {summary}'
        ).strip()

    def _record_recovery_check_tool_result(
        self,
        *,
        task_id: str,
        node_id: str,
        round_id: str,
        overall_decision: RecoveryCheckDecision,
        inspected_items: list[dict[str, Any]],
    ) -> None:
        recorder = getattr(self._log_service, 'upsert_synthetic_tool_result', None)
        if not callable(recorder):
            return
        recorder(
            task_id=task_id,
            node_id=node_id,
            tool_call_id=self._recovery_check_tool_call_id(round_id, [dict(item.get('call') or {}) for item in inspected_items]),
            tool_name='recovery_check',
            status='warning' if overall_decision == RecoveryCheckDecision.MODEL_DECIDE else 'success',
            output_text=self._recovery_check_summary_text(inspected_items),
            payload={
                'kind': 'recovery_check',
                'round_id': str(round_id or '').strip(),
                'recovery_decision': str(overall_decision),
                'related_tool_call_ids': [
                    str((item.get('call') or {}).get('id') or '').strip()
                    for item in list(inspected_items or [])
                    if str((item.get('call') or {}).get('id') or '').strip()
                ],
                'attempted_tools': [
                    str(item.get('tool_name') or '').strip()
                    for item in list(inspected_items or [])
                    if str(item.get('tool_name') or '').strip()
                ],
                'evidence': [
                    evidence
                    for item in list(inspected_items or [])
                    for evidence in list(item.get('evidence') or [])
                    if isinstance(evidence, dict)
                ],
                'lost_result_summary': self._recovery_check_summary_text(inspected_items),
            },
        )

    def _record_recovery_resolution_tool_result(
        self,
        *,
        task_id: str,
        node_id: str,
        round_id: str,
        item: dict[str, Any],
    ) -> None:
        recorder = getattr(self._log_service, 'upsert_synthetic_tool_result', None)
        if not callable(recorder):
            return
        call = dict(item.get('call') or {})
        call_id = str(call.get('id') or '').strip()
        if not call_id:
            return
        arguments = self._normalize_tool_call_arguments(call.get('arguments'))
        recorder(
            task_id=task_id,
            node_id=node_id,
            tool_call_id=call_id,
            tool_name=str(item.get('tool_name') or call.get('name') or 'tool'),
            status=str(item.get('expected_tool_status') or ''),
            arguments_text=json.dumps(arguments, ensure_ascii=False, indent=2) if arguments else '',
            started_at=str(dict(item.get('live_state') or {}).get('started_at') or ''),
            finished_at=now_iso(),
            output_text=self._recovery_checked_tool_content(item),
            payload={
                'kind': 'recovery_resolution',
                'round_id': str(round_id or '').strip(),
                'recovery_decision': str(item.get('decision') or ''),
                'related_tool_call_ids': [call_id],
                'attempted_tools': [str(item.get('tool_name') or call.get('name') or 'tool')],
                'evidence': [evidence for evidence in list(item.get('evidence') or []) if isinstance(evidence, dict)],
                'lost_result_summary': str(item.get('lost_result_summary') or ''),
            },
        )

    def _execution_stage_gate(self, *, task_id: str, node_id: str, node_kind: str) -> dict[str, Any]:
        if str(node_kind or '').strip().lower() not in _STAGE_BUDGET_NODE_KINDS:
            return {'enabled': False, 'has_active_stage': False, 'transition_required': False, 'active_stage': None}
        getter = getattr(self._log_service, 'execution_stage_gate_snapshot', None)
        if not callable(getter):
            return {'enabled': False, 'has_active_stage': False, 'transition_required': False, 'active_stage': None}
        payload = getter(task_id, node_id)
        if not isinstance(payload, dict):
            return {'enabled': False, 'has_active_stage': False, 'transition_required': False, 'active_stage': None}
        return {'enabled': True, **payload}

    @staticmethod
    def _visible_tools_for_iteration(*, tools: dict[str, Tool], node_kind: str, stage_gate: dict[str, Any]) -> dict[str, Tool]:
        if str(node_kind or '').strip().lower() not in _STAGE_BUDGET_NODE_KINDS:
            return dict(tools or {})
        if not bool(stage_gate.get('enabled')):
            return dict(tools or {})
        return visible_tools_for_stage_iteration(
            tools,
            has_active_stage=bool(stage_gate.get('has_active_stage')),
            transition_required=bool(stage_gate.get('transition_required')),
            stage_tool_name=STAGE_TOOL_NAME,
        )

    def _model_visible_tools_for_iteration(
        self,
        *,
        task_id: str,
        node_id: str,
        node_kind: str,
        visible_tools: dict[str, Tool],
        stage_gate: dict[str, Any],
        runtime_context: dict[str, Any],
    ) -> tuple[dict[str, Tool], dict[str, Any]]:
        selected_tools = dict(visible_tools or {})
        selection_payload: dict[str, Any] = {
            'tool_names': list(selected_tools.keys()),
            'provider_tool_names': list(selected_tools.keys()),
            'pending_provider_tool_names': [],
            'provider_tool_exposure_pending': False,
            'provider_tool_exposure_revision': '',
            'provider_tool_exposure_commit_reason': '',
            'lightweight_tool_ids': [],
            'hydrated_executor_names': [],
            'trace': {},
        }
        if str(node_kind or '').strip().lower() not in _STAGE_BUDGET_NODE_KINDS:
            return selected_tools, selection_payload
        selector = getattr(self, '_model_visible_tool_schema_selector', None)
        if callable(selector):
            raw_selection = selector(
                task_id=str(task_id or '').strip(),
                node_id=str(node_id or '').strip(),
                node_kind=str(node_kind or '').strip(),
                visible_tools=dict(visible_tools or {}),
                runtime_context=dict(runtime_context or {}),
            )
            if isinstance(raw_selection, dict):
                requested_names: list[str] = []
                seen_requested_names: set[str] = set()
                for item in list(raw_selection.get('tool_names') or []):
                    normalized = str(item or '').strip()
                    if not normalized or normalized in seen_requested_names or normalized not in visible_tools:
                        continue
                    seen_requested_names.add(normalized)
                    requested_names.append(normalized)
                if requested_names:
                    selected_tools = {name: visible_tools[name] for name in requested_names}
                    selection_payload['tool_names'] = list(requested_names)
                requested_provider_names: list[str] = []
                seen_provider_names: set[str] = set()
                for item in list(
                    raw_selection.get('provider_tool_names')
                    or (dict(raw_selection.get('trace') or {})).get('provider_tool_names')
                    or selection_payload.get('tool_names')
                    or []
                ):
                    normalized = str(item or '').strip()
                    if not normalized or normalized in seen_provider_names or normalized not in visible_tools:
                        continue
                    seen_provider_names.add(normalized)
                    requested_provider_names.append(normalized)
                if requested_provider_names:
                    selection_payload['provider_tool_names'] = list(requested_provider_names)
                selection_payload['lightweight_tool_ids'] = [
                    str(item or '').strip()
                    for item in list(raw_selection.get('lightweight_tool_ids') or [])
                    if str(item or '').strip()
                ]
                selection_payload['hydrated_executor_names'] = [
                    str(item or '').strip()
                    for item in list(raw_selection.get('hydrated_executor_names') or [])
                    if str(item or '').strip()
                ]
                selection_payload['candidate_tool_names'] = [
                    str(item or '').strip()
                    for item in list(raw_selection.get('candidate_tool_names') or [])
                    if str(item or '').strip()
                ]
                selection_payload['pending_provider_tool_names'] = [
                    str(item or '').strip()
                    for item in list(
                        raw_selection.get('pending_provider_tool_names')
                        or (dict(raw_selection.get('trace') or {})).get('pending_provider_tool_names')
                        or []
                    )
                    if str(item or '').strip()
                ]
                selection_payload['provider_tool_exposure_pending'] = bool(
                    raw_selection.get('provider_tool_exposure_pending')
                    or (dict(raw_selection.get('trace') or {})).get('provider_tool_exposure_pending')
                )
                selection_payload['provider_tool_exposure_revision'] = str(
                    raw_selection.get('provider_tool_exposure_revision')
                    or (dict(raw_selection.get('trace') or {})).get('provider_tool_exposure_revision')
                    or ''
                ).strip()
                selection_payload['provider_tool_exposure_commit_reason'] = str(
                    raw_selection.get('provider_tool_exposure_commit_reason')
                    or (dict(raw_selection.get('trace') or {})).get('provider_tool_exposure_commit_reason')
                    or ''
                ).strip()
                selection_payload['trace'] = dict(raw_selection.get('trace') or {})
        selection_trace = dict(selection_payload.get('trace') or {})
        traced_full_callable_tool_names = self._normalized_name_list(
            list(
                selection_trace.get('full_callable_tool_names')
                or selection_trace.get('callable_tool_names')
                or []
            )
        )
        full_callable_tool_names = traced_full_callable_tool_names or self._normalized_name_list(
            list(selection_payload.get('tool_names') or list(selected_tools.keys()))
        )
        model_visible_callable_tool_names = callable_tool_names_for_stage_iteration(
            full_callable_tool_names,
            has_active_stage=bool(stage_gate.get('has_active_stage')),
            transition_required=bool(stage_gate.get('transition_required')),
            stage_tool_name=STAGE_TOOL_NAME,
        )
        selected_tools = {
            name: visible_tools[name]
            for name in model_visible_callable_tool_names
            if name in visible_tools
        }
        provider_tool_names = self._normalized_name_list(
            list(selection_payload.get('provider_tool_names') or selection_payload.get('tool_names') or [])
        )
        provider_tool_names = [
            name
            for name in provider_tool_names
            if name in visible_tools
        ]
        if not provider_tool_names:
            provider_tool_names = list(model_visible_callable_tool_names)
        selection_payload['tool_names'] = list(model_visible_callable_tool_names)
        selection_payload['provider_tool_names'] = list(provider_tool_names)
        selection_payload['trace'] = {
            **selection_trace,
            'full_callable_tool_names': list(full_callable_tool_names),
            'provider_tool_names': list(provider_tool_names),
            'pending_provider_tool_names': list(selection_payload.get('pending_provider_tool_names') or []),
            'provider_tool_exposure_pending': bool(selection_payload.get('provider_tool_exposure_pending')),
            'provider_tool_exposure_revision': str(selection_payload.get('provider_tool_exposure_revision') or ''),
            'provider_tool_exposure_commit_reason': str(
                selection_payload.get('provider_tool_exposure_commit_reason') or ''
            ),
            'stage_locked_to_submit_next_stage': (
                list(model_visible_callable_tool_names) == [STAGE_TOOL_NAME]
                and list(full_callable_tool_names) != [STAGE_TOOL_NAME]
            ),
        }
        return selected_tools, selection_payload

    @staticmethod
    def _normalized_name_list(items: list[Any] | None) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()
        for item in list(items or []):
            normalized = str(item or '').strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)
        return ordered

    @staticmethod
    def _stage_prompt_payload_from_gate(stage_gate: dict[str, Any]) -> dict[str, Any]:
        active_stage = stage_gate.get('active_stage') if isinstance(stage_gate.get('active_stage'), dict) else None
        return {
            'has_active_stage': bool(stage_gate.get('has_active_stage')),
            'transition_required': bool(stage_gate.get('transition_required')),
            'active_stage': dict(active_stage or {}) if isinstance(active_stage, dict) else None,
        }

    def _build_node_dynamic_contract(
        self,
        *,
        node,
        message_history: list[dict[str, Any]],
        tool_schema_selection: dict[str, Any],
        stage_gate: dict[str, Any],
    ) -> NodeRuntimeToolContract:
        existing_payload = extract_node_dynamic_contract_payload(message_history) or {}
        current_frame = self._runtime_frame(
            str(getattr(node, 'task_id', '') or '').strip(),
            str(getattr(node, 'node_id', '') or '').strip(),
        ) or {}
        callable_tool_names = self._normalized_name_list(list(tool_schema_selection.get('tool_names') or []))
        candidate_tool_names = self._normalized_name_list(
            [
                item.get('tool_id') if isinstance(item, dict) else item
                for item in list(
                    tool_schema_selection.get('candidate_tool_names')
                    or existing_payload.get('candidate_tools')
                    or current_frame.get('candidate_tool_items')
                    or current_frame.get('candidate_tool_names')
                    or []
                )
            ]
        )
        candidate_tool_names = [name for name in candidate_tool_names if name not in set(callable_tool_names)]
        candidate_tool_items = [dict(item) for item in list(existing_payload.get('candidate_tools') or []) if isinstance(item, dict)]
        if not candidate_tool_items:
            candidate_tool_items = [dict(item) for item in list(current_frame.get('candidate_tool_items') or []) if isinstance(item, dict)]
        if candidate_tool_items:
            candidate_tool_items = [
                dict(item)
                for item in candidate_tool_items
                if str(item.get('tool_id') or '').strip() and str(item.get('tool_id') or '').strip() not in set(callable_tool_names)
            ]
        candidate_skill_ids = self._normalized_name_list(
            [
                item.get('skill_id') if isinstance(item, dict) else item
                for item in list(
                    existing_payload.get('candidate_skills')
                    or current_frame.get('candidate_skill_items')
                    or current_frame.get('candidate_skill_ids')
                    or current_frame.get('selected_skill_ids')
                    or []
                )
            ]
        )
        candidate_skill_items = [dict(item) for item in list(existing_payload.get('candidate_skills') or []) if isinstance(item, dict)]
        if not candidate_skill_items:
            candidate_skill_items = [dict(item) for item in list(current_frame.get('candidate_skill_items') or []) if isinstance(item, dict)]
        if not candidate_skill_items:
            candidate_skill_items = [dict(item) for item in list(current_frame.get('visible_skills') or []) if isinstance(item, dict)]
        exec_runtime_policy = None
        if EXEC_TOOL_EXECUTOR_NAME in set(callable_tool_names + candidate_tool_names) or EXEC_TOOL_FAMILY_ID in set(callable_tool_names + candidate_tool_names):
            raw_exec_runtime_policy = current_frame.get('exec_runtime_policy') or existing_payload.get('exec_runtime_policy')
            if isinstance(raw_exec_runtime_policy, dict):
                exec_runtime_policy = dict(raw_exec_runtime_policy)
        return NodeRuntimeToolContract(
            node_id=str(getattr(node, 'node_id', '') or '').strip(),
            node_kind=str(getattr(node, 'node_kind', '') or '').strip(),
            callable_tool_names=callable_tool_names,
            candidate_tool_names=candidate_tool_names,
            candidate_tool_items=candidate_tool_items,
            visible_skills=[],
            candidate_skill_ids=candidate_skill_ids,
            candidate_skill_items=candidate_skill_items,
            stage_payload=self._stage_prompt_payload_from_gate(stage_gate),
            hydrated_executor_names=self._normalized_name_list(list(tool_schema_selection.get('hydrated_executor_names') or [])),
            lightweight_tool_ids=self._normalized_name_list(list(tool_schema_selection.get('lightweight_tool_ids') or [])),
            selection_trace=dict(tool_schema_selection.get('trace') or {}),
            exec_runtime_policy=exec_runtime_policy,
        )

    def _refresh_node_dynamic_contract_message(
        self,
        *,
        node,
        message_history: list[dict[str, Any]],
        tool_schema_selection: dict[str, Any],
        stage_gate: dict[str, Any],
    ) -> list[dict[str, Any]]:
        contract = self._build_node_dynamic_contract(
            node=node,
            message_history=message_history,
            tool_schema_selection=tool_schema_selection,
            stage_gate=stage_gate,
        )
        return upsert_node_dynamic_contract_message(message_history, contract)

    def _promote_tool_context_hydration_after_results(
        self,
        *,
        task_id: str,
        node_id: str,
        response_tool_calls: list[Any],
        results: list[dict[str, Any]],
        runtime_context: dict[str, Any],
    ) -> None:
        promoter = getattr(self, '_tool_context_hydration_promoter', None)
        if not callable(promoter):
            return
        for result in list(results or []):
            if not isinstance(result, dict):
                continue
            live_state = dict(result.get('live_state') or {}) if isinstance(result.get('live_state'), dict) else {}
            if str(live_state.get('status') or '').strip().lower() != 'success':
                continue
            raw_result = self._tool_context_hydration_payload(result.get('raw_result'))
            if not isinstance(raw_result, dict) or not bool(raw_result.get('ok')):
                continue
            tool_message = dict(result.get('tool_message') or {}) if isinstance(result.get('tool_message'), dict) else {}
            tool_name = str(tool_message.get('name') or live_state.get('tool_name') or '').strip()
            if tool_name not in {'load_tool_context', 'load_tool_context_v2'}:
                continue
            hydrated_tool_id = str(raw_result.get('tool_id') or '').strip()
            if not hydrated_tool_id:
                continue
            promoter(
                task_id=str(task_id or '').strip(),
                node_id=str(node_id or '').strip(),
                tool_call=SimpleNamespace(
                    name=tool_name,
                    arguments={'tool_id': hydrated_tool_id},
                ),
                raw_result=dict(raw_result),
                runtime_context=dict(runtime_context or {}),
            )

    @staticmethod
    def _tool_context_hydration_payload(raw_result: Any) -> dict[str, Any] | None:
        if isinstance(raw_result, dict):
            return dict(raw_result)
        if isinstance(raw_result, str):
            text = str(raw_result or '').strip()
            if not text or not text.startswith('{'):
                return None
            try:
                parsed = json.loads(text)
            except Exception:
                return None
            return dict(parsed) if isinstance(parsed, dict) else None
        return None

    @classmethod
    def model_visible_always_callable_tool_names(
        cls,
        *,
        visible_tool_names: list[str] | None = None,
    ) -> list[str]:
        visible_name_set = {
            str(item or '').strip()
            for item in list(visible_tool_names or [])
            if str(item or '').strip()
        }
        return [
            name
            for name in cls._ordered_budget_bypass_tool_names()
            if not visible_name_set or name in visible_name_set
        ]

    @classmethod
    def _ordered_budget_bypass_tool_names(cls) -> tuple[str, ...]:
        ordered: list[str] = []
        for name in (*cls._ORDERED_CONTROL_TOOL_NAMES, STAGE_TOOL_NAME, FINAL_RESULT_TOOL_NAME, _STAGE_SPAWN_TOOL_NAME):
            if name not in cls._BUDGET_BYPASS_TOOL_NAMES or name in ordered:
                continue
            ordered.append(name)
        for name in sorted(cls._BUDGET_BYPASS_TOOL_NAMES):
            if name in ordered:
                continue
            ordered.append(name)
        return tuple(ordered)

    @staticmethod
    def _execution_stage_frame_payload(*, node_kind: str, stage_gate: dict[str, Any]) -> dict[str, Any]:
        if str(node_kind or '').strip().lower() not in _STAGE_BUDGET_NODE_KINDS:
            return {}
        if not bool(stage_gate.get('enabled')):
            return {}
        active = stage_gate.get('active_stage') if isinstance(stage_gate, dict) else None
        if not isinstance(active, dict):
            return {
                'stage_mode': '',
                'stage_status': '',
                'stage_goal': '',
                'stage_total_steps': 0,
            }
        return {
            'stage_mode': str(active.get('mode') or ''),
            'stage_status': str(active.get('status') or ''),
            'stage_goal': str(active.get('stage_goal') or ''),
            'stage_total_steps': int(active.get('tool_round_budget') or 0),
        }

    @staticmethod
    def _should_record_execution_stage_round(*, node_kind: str, stage_gate: dict[str, Any], response_tool_calls: list[dict[str, Any]]) -> bool:
        if str(node_kind or '').strip().lower() not in _STAGE_BUDGET_NODE_KINDS:
            return False
        if not bool(stage_gate.get('enabled')):
            return False
        if not bool(stage_gate.get('has_active_stage')) or bool(stage_gate.get('transition_required')):
            return False
        names = [str(item.get('name') or '').strip() for item in list(response_tool_calls or []) if str(item.get('name') or '').strip()]
        if any(name == STAGE_TOOL_NAME for name in names):
            return False
        return any(name != STAGE_TOOL_NAME for name in names)

    @staticmethod
    def _execution_prompt_cache_key(*, model_messages: list[dict[str, Any]], tool_schemas: list[dict[str, Any]], model_refs: list[str]) -> str:
        return build_stable_prompt_cache_key(
            model_messages,
            tool_schemas or None,
            '|'.join(str(item or '').strip() for item in list(model_refs or []) if str(item or '').strip()),
        )

    async def _execute_tool_calls(
        self,
        *,
        task,
        node,
        response_tool_calls: list[Any],
        tools: dict[str, Tool],
        allowed_content_refs: list[str],
        runtime_context: dict[str, Any],
        prior_overflow_signatures: set[str] | None = None,
        max_parallel_tool_calls: int | None | object = _UNSET,
    ) -> list[dict[str, Any]]:
        exclusive_turn_tool = next(
            (
                str(getattr(call, 'name', '') or '').strip()
                for call in list(response_tool_calls or [])
                if str(getattr(call, 'name', '') or '').strip() in self._EXCLUSIVE_TOOL_TURN_NAMES
            ),
            '',
        )
        if exclusive_turn_tool and len(list(response_tool_calls or [])) != 1:
            return [
                {
                    'index': index,
                    'live_state': {
                        'tool_call_id': str(call.id or ''),
                        'tool_name': str(call.name or 'tool'),
                        'status': 'error',
                        'started_at': '',
                        'finished_at': '',
                        'elapsed_seconds': None,
                    },
                    'tool_message': {
                        'role': 'tool',
                        'tool_call_id': call.id,
                        'name': call.name,
                        'content': self._exclusive_tool_turn_error(exclusive_turn_tool),
                        'started_at': '',
                        'finished_at': '',
                        'elapsed_seconds': None,
                    },
                }
                for index, call in enumerate(list(response_tool_calls or []))
            ]
        configured_parallel_limit = self._normalize_optional_limit(
            max_parallel_tool_calls,
            default=self._max_parallel_tool_calls,
        )
        semaphore = asyncio.Semaphore(
            self._parallel_slot_count(
                configured_parallel_limit,
                len(list(response_tool_calls or [])),
                enabled=self._parallel_tool_calls_enabled,
            )
        )
        current_frame = self._runtime_frame(str(task.task_id or ''), str(node.node_id or '')) or {}
        candidate_tool_names = self._normalized_name_list(
            list(runtime_context.get('candidate_tool_names') or current_frame.get('candidate_tool_names') or [])
        )
        candidate_skill_ids = self._normalized_name_list(
            list(runtime_context.get('candidate_skill_ids') or current_frame.get('candidate_skill_ids') or [])
        )

        def _call_runtime_context(call: Any, *, stage_turn_granted: bool | None = None) -> dict[str, Any]:
            granted = bool(runtime_context.get('stage_turn_granted')) if stage_turn_granted is None else bool(stage_turn_granted)
            return {
                **runtime_context,
                'current_tool_call_id': call.id,
                'tool_contract_enforced': True,
                'candidate_tool_names': list(candidate_tool_names),
                'candidate_skill_ids': list(candidate_skill_ids),
                'allowed_content_refs': allowed_content_refs,
                'enforce_content_ref_allowlist': str(runtime_context.get('node_kind') or '').strip().lower() == 'acceptance',
                'prior_overflow_signatures': sorted(prior_overflow_signatures or set()),
                'stage_turn_granted': granted,
            }

        def _blocked_call_result(index: int, call: Any, *, error_content: str) -> dict[str, Any]:
            content = str(error_content or 'Error: tool call blocked').strip() or 'Error: tool call blocked'
            self._update_tool_live_state(
                task_id=task.task_id,
                node_id=node.node_id,
                tool_call_id=call.id,
                status='error',
                started_at='',
                finished_at='',
                elapsed_seconds=None,
                result_content=content,
                ephemeral=False,
            )
            return {
                'index': index,
                'raw_result': None,
                'live_state': {
                    'tool_call_id': str(call.id or ''),
                    'tool_name': str(call.name or 'tool'),
                    'status': 'error',
                    'started_at': '',
                    'finished_at': '',
                    'elapsed_seconds': None,
                    'ephemeral': False,
                },
                'tool_message': {
                    'role': 'tool',
                    'tool_call_id': call.id,
                    'name': call.name,
                    'content': content,
                    'started_at': '',
                    'finished_at': '',
                    'elapsed_seconds': None,
                    'ephemeral': False,
                },
            }

        def _record_mixed_stage_round(ordinary_calls: list[Any]) -> dict[str, Any] | None:
            if not ordinary_calls:
                return None
            store = getattr(self._log_service, '_store', None)
            get_node = getattr(store, 'get_node', None)
            latest_node = get_node(node.node_id) if callable(get_node) else None
            created_at = ''
            if latest_node is not None:
                outputs = list(getattr(latest_node, 'output', []) or [])
                if outputs:
                    created_at = str(outputs[-1].created_at or '').strip()
            round_payload = self._log_service.record_execution_stage_round(
                task.task_id,
                node.node_id,
                tool_calls=[
                    {
                        'id': str(call.id or '').strip(),
                        'name': str(call.name or '').strip(),
                        'arguments': self._normalize_tool_call_arguments(getattr(call, 'arguments', {})),
                    }
                    for call in list(ordinary_calls or [])
                ],
                created_at=created_at or now_iso(),
            )
            if round_payload is None:
                return None
            self._log_service.update_frame(
                task.task_id,
                node.node_id,
                lambda frame: {
                    **frame,
                    'active_round_id': str(round_payload.get('round_id') or ''),
                    'active_round_tool_call_ids': [
                        str(call.id or '').strip()
                        for call in ordinary_calls
                        if str(call.id or '').strip()
                    ],
                    'active_round_started_at': str(round_payload.get('created_at') or now_iso()),
                    **self._execution_stage_frame_payload(
                        node_kind=node.node_kind,
                        stage_gate=self._execution_stage_gate(
                            task_id=task.task_id,
                            node_id=node.node_id,
                            node_kind=node.node_kind,
                        ),
                    ),
                },
                publish_snapshot=True,
            )
            return round_payload

        async def _run_call(index: int, call: Any, *, stage_turn_granted: bool | None = None) -> dict[str, Any]:
            async with semaphore:
                self._check_pause_or_cancel(task.task_id)
                started_at = now_iso()
                started_monotonic = time.monotonic()
                slot_lease = None
                controller = getattr(self, '_adaptive_tool_budget_controller', None)
                if controller is not None and not self._should_bypass_execution_budget(call=call):
                    slot_lease = await controller.acquire_tool_slot(
                        task_id=task.task_id,
                        node_id=node.node_id,
                        tool_name=str(call.name or 'tool'),
                        tool_call_id=str(call.id or ''),
                    )
                self._update_tool_live_state(
                    task_id=task.task_id,
                    node_id=node.node_id,
                    tool_call_id=call.id,
                    status='running',
                    started_at=started_at,
                    finished_at='',
                    elapsed_seconds=None,
                )
                try:
                    raw_result = await self._execute_tool_raw(
                        tools=tools,
                        tool_name=call.name,
                        arguments=self._normalize_tool_call_arguments(getattr(call, 'arguments', {})),
                        runtime_context=_call_runtime_context(
                            call,
                            stage_turn_granted=stage_turn_granted,
                        ),
                    )
                    promoter = getattr(self, '_tool_context_hydration_promoter', None)
                    hydration_payload = self._tool_context_hydration_payload(raw_result)
                    if (
                        callable(promoter)
                        and str(getattr(call, 'name', '') or '').strip() in {'load_tool_context', 'load_tool_context_v2'}
                        and isinstance(hydration_payload, dict)
                        and bool(hydration_payload.get('ok'))
                        and str(hydration_payload.get('tool_id') or '').strip()
                    ):
                        promoter(
                            task_id=str(task.task_id or '').strip(),
                            node_id=str(node.node_id or '').strip(),
                            tool_call=SimpleNamespace(
                                name=str(getattr(call, 'name', '') or '').strip(),
                                arguments={'tool_id': str(hydration_payload.get('tool_id') or '').strip()},
                            ),
                            raw_result=dict(hydration_payload),
                            runtime_context=dict(runtime_context or {}),
                        )
                    tool_content = self._render_tool_message_content(
                        raw_result,
                        runtime_context=runtime_context,
                        tool_name=str(call.name or ''),
                        delivery_metadata=self._tool_result_delivery_metadata(
                            tools=tools,
                            tool_name=str(call.name or ''),
                            arguments=dict(getattr(call, 'arguments', {}) or {}),
                        ),
                    )
                except TaskPausedError:
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # pragma: no cover - defensive fallback
                    raw_result = None
                    tool_content = f'Error executing {call.name}: {describe_exception(exc)}'
                finally:
                    if controller is not None:
                        controller.release_tool_slot(slot_lease)
                finished_at = now_iso()
                elapsed_seconds = round(max(0.0, time.monotonic() - started_monotonic), 1)
                status = self._tool_message_status(raw_result if raw_result is not None else tool_content)
                ephemeral = self._is_ephemeral_tool_result(
                    raw_result,
                    tool_name=str(call.name or ''),
                )
                self._update_tool_live_state(
                    task_id=task.task_id,
                    node_id=node.node_id,
                    tool_call_id=call.id,
                    status=status,
                    started_at=started_at,
                    finished_at=finished_at,
                    elapsed_seconds=elapsed_seconds,
                    result_content=tool_content,
                    ephemeral=ephemeral,
                )
                return {
                    'index': index,
                    'raw_result': raw_result,
                    'live_state': {
                        'tool_call_id': str(call.id or ''),
                        'tool_name': str(call.name or 'tool'),
                        'status': status,
                        'started_at': started_at,
                        'finished_at': finished_at,
                        'elapsed_seconds': elapsed_seconds,
                        'ephemeral': ephemeral,
                    },
                    'tool_message': {
                        'role': 'tool',
                        'tool_call_id': call.id,
                        'name': call.name,
                        'content': tool_content,
                        'started_at': started_at,
                        'finished_at': finished_at,
                        'elapsed_seconds': elapsed_seconds,
                        'ephemeral': ephemeral,
                    },
                }

        indexed_calls = list(enumerate(list(response_tool_calls or [])))
        stage_items = [
            (index, call)
            for index, call in indexed_calls
            if str(getattr(call, 'name', '') or '').strip() == STAGE_TOOL_NAME
        ]
        ordinary_items = [
            (index, call)
            for index, call in indexed_calls
            if str(getattr(call, 'name', '') or '').strip() != STAGE_TOOL_NAME
        ]
        ordered_results: dict[int, dict[str, Any]] = {}
        stage_failed = False
        for index, call in stage_items:
            result = await _run_call(index, call, stage_turn_granted=False)
            ordered_results[index] = result
            if str(dict(result.get('live_state') or {}).get('status') or '').strip().lower() == 'error':
                stage_failed = True
        if ordinary_items:
            if stage_items and stage_failed:
                blocked_error = (
                    f'Error: {STAGE_TOOL_NAME} failed earlier in this turn; '
                    'retry other tools after a successful stage transition'
                )
                for index, call in ordinary_items:
                    ordered_results[index] = _blocked_call_result(index, call, error_content=blocked_error)
            else:
                if stage_items:
                    _record_mixed_stage_round([call for _, call in ordinary_items])
                ordinary_results = await asyncio.gather(
                    *[
                        _run_call(
                            index,
                            call,
                            stage_turn_granted=True if stage_items else None,
                        )
                        for index, call in ordinary_items
                    ]
                )
                for (index, _call), result in zip(ordinary_items, ordinary_results, strict=False):
                    ordered_results[index] = result
        return [ordered_results[index] for index, _call in indexed_calls if index in ordered_results]

    @classmethod
    def _should_bypass_execution_budget(cls, *, call: Any) -> bool:
        tool_name = str(getattr(call, 'name', '') or '').strip()
        return tool_name in cls._BUDGET_BYPASS_TOOL_NAMES

    @staticmethod
    def _normalize_optional_limit(value: int | None | object, *, default: int | None) -> int | None:
        if value is _UNSET:
            value = default
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return max(0, int(value))

    @staticmethod
    def _parallel_slot_count(limit: int | None, item_count: int, *, enabled: bool) -> int:
        if not enabled or item_count <= 1:
            return 1
        if limit is None:
            return max(1, item_count)
        return max(1, int(limit) if int(limit) > 0 else 1)

    @staticmethod
    def _normalized_model_response_timeout_seconds_value(value: Any | object) -> float | None | object:
        if value is _UNSET:
            return _UNSET
        if value in {None, ''}:
            return None
        try:
            normalized = float(value)
        except (TypeError, ValueError):
            return None
        if normalized <= 0:
            return None
        return normalized

    def _normalized_model_response_timeout_seconds(self) -> float | None:
        value = self._normalized_model_response_timeout_seconds_value(
            getattr(self, '_model_response_timeout_seconds', _UNSET)
        )
        if value is _UNSET:
            return _DEFAULT_MODEL_RESPONSE_TIMEOUT_SECONDS
        return value

    def _resolved_model_response_timeout_seconds(self, *, model_refs: list[str] | None = None) -> float | None:
        override = self._normalized_model_response_timeout_seconds_value(
            getattr(self, '_model_response_timeout_seconds', _UNSET)
        )
        if override is not _UNSET:
            return override
        recommended_timeout: float | None | object = _UNSET
        timeout_supplier = getattr(self._chat_backend, 'recommended_model_response_timeout_seconds', None)
        if callable(timeout_supplier):
            try:
                recommended_timeout = timeout_supplier(model_refs=list(model_refs or []))
            except TypeError:
                recommended_timeout = timeout_supplier(list(model_refs or []))
        normalized_recommended = self._normalized_model_response_timeout_seconds_value(recommended_timeout)
        if normalized_recommended is _UNSET:
            return _DEFAULT_MODEL_RESPONSE_TIMEOUT_SECONDS
        if normalized_recommended is None:
            return None
        return normalized_recommended

    def _model_response_timeout_message(self, *, timeout_seconds: float | None | object = _UNSET) -> str:
        if timeout_seconds is _UNSET:
            timeout_seconds = self._resolved_model_response_timeout_seconds()
        if timeout_seconds is None:
            return 'model request timeout'
        return f'model request timeout after {timeout_seconds:.3f}s'

    def _update_tool_live_state(
        self,
        *,
        task_id: str,
        node_id: str,
        tool_call_id: str,
        status: str,
        started_at: str,
        finished_at: str,
        elapsed_seconds: float | None,
        result_content: str | None = None,
        ephemeral: bool | None = None,
    ) -> None:
        def _mutate(frame: dict[str, Any]) -> dict[str, Any]:
            next_calls: list[dict[str, Any]] = []
            matched = False
            for item in list(frame.get('tool_calls') or []):
                payload = dict(item or {})
                if str(payload.get('tool_call_id') or '') == str(tool_call_id or ''):
                    matched = True
                    payload.update(
                        {
                            'status': status,
                            'started_at': started_at or str(payload.get('started_at') or ''),
                            'finished_at': finished_at,
                            'elapsed_seconds': elapsed_seconds,
                        }
                    )
                    if result_content is not None:
                        payload['result_content'] = result_content
                    if ephemeral is not None:
                        payload['ephemeral'] = bool(ephemeral)
                next_calls.append(payload)
            if not matched:
                payload = {
                    'tool_call_id': str(tool_call_id or ''),
                    'tool_name': 'tool',
                    'status': status,
                    'started_at': started_at,
                    'finished_at': finished_at,
                    'elapsed_seconds': elapsed_seconds,
                }
                if result_content is not None:
                    payload['result_content'] = result_content
                if ephemeral is not None:
                    payload['ephemeral'] = bool(ephemeral)
                next_calls.append(payload)
            frame['tool_calls'] = next_calls
            frame['phase'] = 'waiting_tool_results'
            return frame

        self._log_service.update_frame(task_id, node_id, _mutate, publish_snapshot=True)

    @staticmethod
    def _live_tool_entry(call: Any) -> dict[str, Any]:
        return {
            'tool_call_id': str(call.id or ''),
            'tool_name': str(call.name or 'tool'),
            'status': 'queued',
            'started_at': '',
            'finished_at': '',
            'elapsed_seconds': None,
        }

    @staticmethod
    def _tool_message_status(tool_result: Any) -> str:
        return 'error' if is_error_like_tool_result(tool_result) else 'success'

    @staticmethod
    def _is_ephemeral_tool_result(result: Any, *, tool_name: str) -> bool:
        if str(tool_name or '').strip() != 'content':
            return False
        if not isinstance(result, dict):
            return False
        handle = dict(result.get('handle') or {}) if isinstance(result.get('handle'), dict) else {}
        source_kind = str(handle.get('source_kind') or result.get('source_kind') or '').strip().lower()
        return source_kind == _STAGE_HISTORY_ARCHIVE_SOURCE_KIND

    async def _execute_tool_raw(self, *, tools: dict[str, Tool], tool_name: str, arguments: dict[str, Any], runtime_context: dict[str, Any]) -> Any:
        stage_gate_error = self._execution_tool_gate_error(tool_name=tool_name, runtime_context=runtime_context)
        if stage_gate_error:
            return f'Error: {stage_gate_error}'
        tool = tools.get(tool_name)
        if tool is None:
            return f'Error: tool not available: {tool_name}'
        search_signature = self._search_overflow_signature_for_call(tool_name=tool_name, arguments=arguments)
        prior_overflow_signatures = {
            str(item or '').strip()
            for item in list(runtime_context.get('prior_overflow_signatures') or [])
            if str(item or '').strip()
        }
        if search_signature and search_signature in prior_overflow_signatures:
            return 'Error: previous search overflowed; refine query before retrying'
        try:
            errors = tool.validate_params(arguments)
        except Exception as exc:
            return append_parameter_error_guidance(
                f'Error validating {tool_name}: {exc}',
                tool_name=tool_name,
            )
        if errors:
            return append_parameter_error_guidance(
                'Error: ' + '; '.join(errors),
                tool_name=tool_name,
            )
        execute_kwargs = self._normalize_tool_call_arguments(arguments)
        runtime_param_name = self._runtime_context_parameter_name(tool)
        if runtime_param_name is not None:
            execute_kwargs[runtime_param_name] = runtime_context
        try:
            if not actor_role_allows_watchdog(runtime_context):
                return await tool.execute(**execute_kwargs)
            outcome = await run_tool_with_watchdog(
                tool.execute(**execute_kwargs),
                tool_name=tool_name,
                arguments=arguments,
                runtime_context=runtime_context,
                snapshot_supplier=self._snapshot_supplier(runtime_context),
                manager=(
                    getattr(self, '_tool_execution_manager', None)
                    if actor_role_allows_detached_watchdog(runtime_context)
                    else None
                ),
                on_poll=lambda _poll: self._on_tool_watchdog_poll(runtime_context),
            )
            return outcome.value
        except Exception as exc:
            if is_parameter_like_tool_exception(exc):
                return append_parameter_error_guidance(
                    f'Error executing {tool_name}: {exc}',
                    tool_name=tool_name,
                )
            raise

    def _render_tool_message_content(
        self,
        result: Any,
        *,
        runtime_context: dict[str, Any],
        tool_name: str,
        delivery_metadata: dict[str, Any] | None = None,
    ) -> str:
        rendered = result if isinstance(result, str) else self._render_tool_result(result)
        return self._externalize_message_content(
            rendered,
            runtime_context=runtime_context,
            display_name=f'tool:{tool_name}',
            source_kind=f'tool_result:{tool_name}',
            delivery_metadata=delivery_metadata,
        )

    async def _execute_tool(self, *, tools: dict[str, Tool], tool_name: str, arguments: dict[str, Any], runtime_context: dict[str, Any]) -> str:
        result = await self._execute_tool_raw(
            tools=tools,
            tool_name=tool_name,
            arguments=arguments,
            runtime_context=runtime_context,
        )
        return self._render_tool_message_content(
            result,
            runtime_context=runtime_context,
            tool_name=tool_name,
            delivery_metadata=self._tool_result_delivery_metadata(
                tools=tools,
                tool_name=tool_name,
                arguments=arguments,
            ),
        )

    @staticmethod
    def _tool_result_delivery_metadata(*, tools: dict[str, Tool], tool_name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        tool = (tools or {}).get(str(tool_name or '').strip())
        descriptor = getattr(tool, '_descriptor', None)
        metadata = getattr(descriptor, 'metadata', None) or {}
        normalized_name = str(tool_name or '').strip() or 'tool'
        normalized_arguments = dict(arguments or {})
        invocation_text = (
            f"{normalized_name}({json.dumps(normalized_arguments, ensure_ascii=False, sort_keys=True)})"
            if normalized_arguments
            else normalized_name
        )
        return {
            'tool_result_inline_full': bool(
                getattr(descriptor, 'tool_result_inline_full', False)
                or metadata.get('tool_result_inline_full', False)
            ),
            'invocation_text': invocation_text,
        }

    def _execution_tool_gate_error(self, *, tool_name: str, runtime_context: dict[str, Any]) -> str:
        node_kind = str(runtime_context.get('node_kind') or '').strip().lower()
        if node_kind not in _STAGE_BUDGET_NODE_KINDS:
            return ''
        normalized_tool_name = str(tool_name or '').strip()
        if normalized_tool_name in self._CONTROL_TOOL_NAMES or normalized_tool_name == STAGE_TOOL_NAME:
            return ''
        stage_gate = self._execution_stage_gate(
            task_id=str(runtime_context.get('task_id') or ''),
            node_id=str(runtime_context.get('node_id') or ''),
            node_kind=node_kind,
        )
        if not bool(stage_gate.get('enabled')):
            return ''
        active_stage = stage_gate.get('active_stage') if isinstance(stage_gate.get('active_stage'), dict) else {}
        if (
            normalized_tool_name == _STAGE_SPAWN_TOOL_NAME
            and bool((active_stage or {}).get('final_stage'))
        ):
            return 'final stage forbids spawn_child_nodes; finish synthesis with existing evidence or submit the final result'
        if bool(runtime_context.get('stage_turn_granted')):
            return ''
        if normalized_tool_name == _STAGE_SPAWN_TOOL_NAME and bool(stage_gate.get('transition_required')):
            return f'current stage budget is exhausted; call {STAGE_TOOL_NAME} before using other tools'
        return stage_gate_error_for_tool(
            normalized_tool_name,
            has_active_stage=bool(stage_gate.get('has_active_stage')),
            transition_required=bool(stage_gate.get('transition_required')),
            stage_tool_name=STAGE_TOOL_NAME,
        )

    @classmethod
    def _overflowed_search_signatures(cls, messages: list[dict[str, Any]]) -> set[str]:
        signatures: set[str] = set()
        for message in list(messages or []):
            if str(message.get('role') or '').strip().lower() != 'tool':
                continue
            signature = cls._search_overflow_signature_from_tool_message(message)
            if signature:
                signatures.add(signature)
        return signatures

    @classmethod
    def _search_overflow_signature_from_tool_message(cls, message: dict[str, Any]) -> str:
        tool_name = str(message.get('name') or '').strip()
        if tool_name != 'content':
            return ''
        content = message.get('content')
        if isinstance(content, str):
            text = content.strip()
            if not text.startswith('{'):
                return ''
            try:
                payload = json.loads(text)
            except Exception:
                return ''
        elif isinstance(content, dict):
            payload = content
        else:
            return ''
        if not isinstance(payload, dict) or not bool(payload.get('overflow')):
            return ''
        query = str(payload.get('query') or '').strip()
        scope = str(payload.get('path') or '').strip() or str(payload.get('ref') or '').strip()
        if not query or not scope:
            return ''
        return f'{tool_name}|{scope}|{query}'

    @staticmethod
    def _search_overflow_signature_for_call(*, tool_name: str, arguments: dict[str, Any]) -> str:
        normalized_tool = str(tool_name or '').strip()
        if normalized_tool != 'content':
            return ''
        payload = ReActToolLoop._normalize_tool_call_arguments(arguments)
        if str(payload.get('action') or '').strip().lower() != 'search':
            return ''
        query = str(payload.get('query') or '').strip()
        scope = str(payload.get('path') or '').strip() or str(payload.get('ref') or '').strip()
        if not query or not scope:
            return ''
        return f'{normalized_tool}|{scope}|{query}'

    @staticmethod
    def _normalize_tool_call_arguments(arguments: Any) -> dict[str, Any]:
        if isinstance(arguments, dict):
            source = arguments
        elif isinstance(arguments, str):
            text = str(arguments or '').strip()
            if not text:
                return {}
            try:
                parsed = json.loads(text)
            except Exception:
                return {}
            if not isinstance(parsed, dict):
                return {}
            source = parsed
        elif arguments is None:
            return {}
        else:
            try:
                parsed = dict(arguments)
            except Exception:
                return {}
            if not isinstance(parsed, dict):
                return {}
            source = parsed
        normalized: dict[str, Any] = {}
        for key, value in source.items():
            key_text = str(key or '').strip()
            if key_text:
                normalized[key_text] = value
        return normalized

    async def _on_tool_watchdog_poll(self, runtime_context: dict[str, Any]) -> None:
        task_id = str(runtime_context.get('task_id') or '').strip()
        if not task_id:
            return
        self._check_pause_or_cancel(task_id)

    def _snapshot_supplier(self, runtime_context: dict[str, Any]):
        supplier = runtime_context.get('tool_snapshot_supplier')
        return supplier if callable(supplier) else None

    def _check_pause_or_cancel(self, task_id: str) -> None:
        task = self._log_service._store.get_task(task_id)
        if task is None:
            return
        status = str(getattr(task, 'status', '') or '').strip().lower()
        if status == 'failed':
            raise RuntimeError(str(getattr(task, 'failure_reason', '') or 'task failed').strip() or 'task failed')
        if status == 'success':
            raise RuntimeError('task already completed')
        if bool(task.cancel_requested):
            raise RuntimeError('canceled')
        if bool(task.pause_requested):
            self._log_service.set_pause_state(task_id, pause_requested=True, is_paused=True)
            raise TaskPausedError(task_id)

    @staticmethod
    def _coerce_final_result_payload(raw_payload: dict[str, Any]) -> NodeFinalResult | None:
        if not isinstance(raw_payload, dict):
            return None
        status = str(raw_payload.get('status') or '').strip().lower()
        if status not in {'success', 'failed'}:
            return None
        delivery_status = str(raw_payload.get('delivery_status') or '').strip().lower()
        normalized_delivery_status = delivery_status if delivery_status in {'final', 'blocked'} else 'final'
        evidence_items: list[NodeEvidenceItem] = []
        for item in list(raw_payload.get('evidence') or []):
            if not isinstance(item, dict):
                continue
            try:
                evidence_items.append(NodeEvidenceItem.model_validate(item))
            except Exception:
                continue
        remaining_work_raw = raw_payload.get('remaining_work')
        remaining_work = [
            str(item or '').strip()
            for item in (remaining_work_raw if isinstance(remaining_work_raw, list) else [])
            if str(item or '').strip()
        ]
        return NodeFinalResult(
            status=status,
            delivery_status=normalized_delivery_status,
            summary=str(raw_payload.get('summary') or '').strip(),
            answer=str(raw_payload.get('answer') or ''),
            evidence=evidence_items,
            remaining_work=remaining_work,
            blocking_reason=str(raw_payload.get('blocking_reason') or '').strip(),
        )

    @staticmethod
    def _validate_final_result(
        *,
        result: NodeFinalResult,
        raw_payload: dict[str, Any],
        has_tool_results: bool,
        node_kind: str,
    ) -> list[str]:
        _ = result, has_tool_results, node_kind
        violations: list[str] = []
        missing_keys = [key for key in _RESULT_REQUIRED_KEYS if key not in raw_payload]
        violations.extend([f'missing required field: {key}' for key in missing_keys])

        raw_delivery_status = str(raw_payload.get('delivery_status') or '').strip().lower()
        if raw_delivery_status not in {'final', 'blocked'}:
            violations.append('delivery_status must be one of final|blocked')
        if not str(raw_payload.get('summary') or '').strip():
            violations.append('summary must not be empty')

        raw_evidence = raw_payload.get('evidence')
        if not isinstance(raw_evidence, list):
            violations.append('evidence must be an array')
        else:
            for index, item in enumerate(raw_evidence):
                if not isinstance(item, dict):
                    violations.append(f'evidence[{index}] must be an object')
                    continue
                kind = str(item.get('kind') or '').strip().lower()
                if kind not in {'file', 'artifact', 'url'}:
                    violations.append(f'evidence[{index}].kind must be file|artifact|url')
                if not any(str(item.get(key) or '').strip() for key in ('path', 'ref', 'note')):
                    violations.append(f'evidence[{index}] must include at least one of path/ref/note')
                start_line = item.get('start_line')
                end_line = item.get('end_line')
                if start_line not in {None, ''}:
                    try:
                        start_value = int(start_line)
                    except (TypeError, ValueError):
                        violations.append(f'evidence[{index}].start_line must be an integer')
                    else:
                        if start_value <= 0:
                            violations.append(f'evidence[{index}].start_line must be >= 1')
                if end_line not in {None, ''}:
                    try:
                        end_value = int(end_line)
                    except (TypeError, ValueError):
                        violations.append(f'evidence[{index}].end_line must be an integer')
                    else:
                        if end_value <= 0:
                            violations.append(f'evidence[{index}].end_line must be >= 1')
                        if start_line not in {None, ''}:
                            try:
                                if int(start_line) > end_value:
                                    violations.append(f'evidence[{index}].end_line must be >= start_line')
                            except (TypeError, ValueError):
                                pass

        remaining_work_raw = raw_payload.get('remaining_work')
        if not isinstance(remaining_work_raw, list):
            violations.append('remaining_work must be an array')
        elif any(not str(item or '').strip() for item in remaining_work_raw):
            violations.append('remaining_work items must be non-empty strings')
        return violations

    @staticmethod
    def _contains_tool_name(response_tool_calls: list[Any], tool_name: str) -> bool:
        normalized = str(tool_name or '').strip()
        return any(str(getattr(call, 'name', '') or '').strip() == normalized for call in list(response_tool_calls or []))

    @staticmethod
    def _is_final_result_turn(response_tool_calls: list[Any]) -> bool:
        return len(list(response_tool_calls or [])) == 1 and ReActToolLoop._contains_tool_name(response_tool_calls, FINAL_RESULT_TOOL_NAME)

    @staticmethod
    def _is_stage_only_transition_turn(response_tool_calls: list[Any]) -> bool:
        calls = list(response_tool_calls or [])
        return bool(calls) and all(str(getattr(call, 'name', '') or '').strip() == STAGE_TOOL_NAME for call in calls)

    @classmethod
    def _has_ordinary_tool_call(cls, response_tool_calls: list[Any]) -> bool:
        for call in list(response_tool_calls or []):
            name = str(getattr(call, 'name', '') or '').strip()
            if not name:
                continue
            if name in cls._CONTROL_TOOL_NAMES or name in {STAGE_TOOL_NAME, FINAL_RESULT_TOOL_NAME}:
                continue
            return True
        return False

    @staticmethod
    def _exclusive_tool_turn_error(tool_name: str) -> str:
        normalized = str(tool_name or 'tool').strip() or 'tool'
        return f'Error: {normalized} must be the only tool call in its turn'

    @classmethod
    def _invalid_stage_submission_failure(cls, *, reason: str, count: int, stage_goal: str) -> NodeFinalResult:
        text = str(reason or f'invalid stage submission detected {count} times').strip() or f'invalid stage submission detected {count} times'
        goal = str(stage_goal or '').strip()
        suffix = f' Latest stage goal: {goal}.' if goal else ''
        return NodeFinalResult(
            status='failed',
            delivery_status='blocked',
            summary='invalid stage submission guard triggered',
            answer='',
            evidence=[],
            remaining_work=[],
            blocking_reason=(
                f'Invalid stage progression detected {int(count or 0)} consecutive times. '
                f'Latest issue: {text}.{suffix}'
            ),
        )

    @classmethod
    def _invalid_final_submission_failure(cls, *, reason: str, count: int) -> NodeFinalResult:
        text = str(reason or f'final result submission failed {count} times').strip() or f'final result submission failed {count} times'
        return NodeFinalResult(
            status='failed',
            delivery_status='blocked',
            summary='final result submission guard triggered',
            answer='',
            evidence=[],
            remaining_work=[],
            blocking_reason=(
                f'Invalid final result submission detected {int(count or 0)} consecutive times. '
                f'Latest issue: {text}'
            ),
        )

    @classmethod
    def _read_only_repeat_failure(cls, *, signature: str, count: int, repair_text: str) -> NodeFinalResult:
        normalized_signature = str(signature or '').strip() or '<unknown>'
        guidance = str(repair_text or '').strip() or 'reuse the existing read-only result or change the query/window before retrying'
        return NodeFinalResult(
            status='failed',
            delivery_status='blocked',
            summary='read-only repair guidance guard triggered',
            answer='',
            evidence=[],
            remaining_work=[],
            blocking_reason=(
                f'Ignored read-only repair guidance {int(count or 0)} times for the same call signature: '
                f'{normalized_signature}. Latest repair guidance: {guidance}'
            ),
        )

    @classmethod
    def _stage_only_transition_failure(cls, *, count: int, stage_goal: str) -> NodeFinalResult:
        goal = str(stage_goal or '').strip()
        suffix = f' Latest stage goal: {goal}.' if goal else ''
        return NodeFinalResult(
            status='failed',
            delivery_status='blocked',
            summary='stage transition guard triggered',
            answer='',
            evidence=[],
            remaining_work=[],
            blocking_reason=(
                f'Repeated stage switching without progress detected {int(count or 0)} consecutive times.'
                f'{suffix}'
            ),
        )

    @staticmethod
    def _duplicate_tool_call_target(*, tool_name: str, arguments: dict[str, Any]) -> str:
        normalized_tool = str(tool_name or '').strip()
        if normalized_tool == 'exec':
            command = str(arguments.get('command') or '').strip()
            working_dir = str(arguments.get('working_dir') or arguments.get('cwd') or '').strip()
            if command and working_dir:
                return f'command={command}; working_dir={working_dir}'
            return command or working_dir
        for key in ('ref', 'path', 'task_id', 'node_id', 'query'):
            value = str(arguments.get(key) or '').strip()
            if value:
                return value
        if arguments:
            try:
                return json.dumps(arguments, ensure_ascii=False, sort_keys=True)
            except Exception:
                return str(arguments)
        return ''

    @classmethod
    def _duplicate_tool_call_repair_message(cls, *, tool_name: str, arguments: dict[str, Any]) -> str:
        normalized_tool = str(tool_name or '').strip() or 'tool'
        target = cls._duplicate_tool_call_target(tool_name=normalized_tool, arguments=dict(arguments or {}))
        if normalized_tool == 'exec':
            detail = (
                'You already ran the same `exec` call multiple times in a row for this node. '
                'Do not rerun the exact same command immediately. Reuse the existing command result from the transcript/tool output, '
                'or change `command`, `working_dir`, or other arguments before retrying.'
            )
        else:
            detail = (
                f'You already called `{normalized_tool}` with the same arguments multiple times in a row for this node. '
                'Do not repeat the exact same tool call again. Reuse the previous tool result if it is still valid, '
                'or change the arguments before retrying.'
            )
        if target:
            return f'Duplicate tool call detected. {detail} Latest repeated target: `{target}`.'
        return f'Duplicate tool call detected. {detail}'

    @staticmethod
    def _stage_submission_repair_message(*, reason: str, node_kind: str = 'execution') -> str:
        text = str(reason or '').strip() or f'{STAGE_TOOL_NAME} rejected'
        normalized_kind = str(node_kind or '').strip().lower()
        if normalized_kind == 'acceptance':
            return (
                f'Your last `{STAGE_TOOL_NAME}` call was rejected: {text}. '
                'Do not call `submit_next_stage` again until you first perform verification work in the active stage. '
                'Continue this stage with evidence-checking tools before switching stages.'
            )
        return (
            f'Your last `{STAGE_TOOL_NAME}` call was rejected: {text}. '
            'Do not call `submit_next_stage` again until you first perform substantive work in the active stage. '
            'Continue this stage with a non-control tool call or `spawn_child_nodes` before switching stages.'
        )

    @staticmethod
    def _tool_results_succeeded(results: list[dict[str, Any]]) -> bool:
        statuses = [
            str(((item or {}).get('live_state') or {}).get('status') or '').strip().lower()
            for item in list(results or [])
            if isinstance(item, dict)
        ]
        return bool(statuses) and all(status == 'success' for status in statuses)

    @staticmethod
    def _first_tool_error_text(results: list[dict[str, Any]]) -> str:
        for item in list(results or []):
            if not isinstance(item, dict):
                continue
            live_state = dict(item.get('live_state') or {}) if isinstance(item.get('live_state'), dict) else {}
            tool_message = dict(item.get('tool_message') or {}) if isinstance(item.get('tool_message'), dict) else {}
            status = str(live_state.get('status') or '').strip().lower()
            if status != 'error':
                continue
            return str(tool_message.get('content') or '').strip()
        return ''

    @classmethod
    def _xml_repair_failure(cls, *, count: int, tool_names: list[str], content_excerpt: str) -> NodeFinalResult:
        return NodeFinalResult(
            status='failed',
            delivery_status='blocked',
            summary='xml pseudo tool-call repair guard triggered',
            answer='',
            evidence=[],
            remaining_work=[],
            blocking_reason=format_xml_repair_failure_reason(
                count=count,
                tool_names=tool_names,
                content_excerpt=content_excerpt,
            ),
        )

    @staticmethod
    def _token_preflight_failure(*, reason: str) -> NodeFinalResult:
        normalized_reason = str(reason or '').strip() or 'token preflight failed'
        return NodeFinalResult(
            status='failed',
            delivery_status='blocked',
            summary='node send token preflight failed',
            answer='',
            evidence=[],
            remaining_work=[],
            blocking_reason=normalized_reason,
        )

    @staticmethod
    def _is_append_notice_tail_message(message: dict[str, Any]) -> bool:
        if str((message or {}).get("role") or "").strip().lower() != "assistant":
            return False
        return str((message or {}).get("content") or "").strip().startswith(APPEND_NOTICE_TAIL_PREFIX)

    @classmethod
    def _split_request_messages_for_token_compaction(
        cls,
        *,
        request_messages: list[dict[str, Any]] | None,
        recent_tail_count: int = _NODE_TOKEN_COMPACTION_RECENT_TAIL_COUNT,
    ) -> dict[str, Any]:
        normalized = [
            dict(item)
            for item in list(request_messages or [])
            if isinstance(item, dict)
        ]
        contract_tail: list[dict[str, Any]] = []
        while normalized and is_node_dynamic_contract_message(normalized[-1]):
            contract_tail.insert(0, normalized.pop())

        system_prefix: list[dict[str, Any]] = []
        while normalized and str(normalized[0].get("role") or "").strip().lower() == "system":
            system_prefix.append(normalized.pop(0))

        bootstrap_user: list[dict[str, Any]] = []
        if normalized and str(normalized[0].get("role") or "").strip().lower() == "user":
            bootstrap_user.append(normalized.pop(0))

        append_notice_tail: list[dict[str, Any]] = []
        while normalized and cls._is_append_notice_tail_message(normalized[0]):
            append_notice_tail.append(normalized.pop(0))

        body_messages = list(normalized)
        keep_recent = max(1, int(recent_tail_count or 0))
        recent_tail = body_messages[-keep_recent:] if len(body_messages) > keep_recent else list(body_messages)
        compressible_history = body_messages[:-keep_recent] if len(body_messages) > keep_recent else []
        return {
            "system_prefix": system_prefix,
            "bootstrap_user": bootstrap_user,
            "append_notice_tail": append_notice_tail,
            "compressible_history": compressible_history,
            "recent_tail": recent_tail,
            "contract_tail": contract_tail,
        }

    @classmethod
    def _rewrite_request_messages_for_token_compaction(
        cls,
        *,
        node_id: str = "",
        request_messages: list[dict[str, Any]] | None,
        compressed_text: str = "",
        recent_tail_count: int = _NODE_TOKEN_COMPACTION_RECENT_TAIL_COUNT,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        parts = cls._split_request_messages_for_token_compaction(
            request_messages=request_messages,
            recent_tail_count=recent_tail_count,
        )
        if not any(list(parts.get(key) or []) for key in parts):
            return [], {}

        compacted_payload = {
            "kind": "node_token_compaction_llm" if str(compressed_text or "").strip() else "node_send_token_compaction",
            "node_id": str(node_id or "").strip(),
            "history_message_count": len(list(parts.get("compressible_history") or [])),
            "compacted_message_count": len(list(parts.get("compressible_history") or [])),
            "retained_recent_tail_count": len(list(parts.get("recent_tail") or [])),
            "contract_message_count": len(list(parts.get("contract_tail") or [])),
            "append_notice_tail_count": len(list(parts.get("append_notice_tail") or [])),
        }
        compacted_block = {
            "role": "assistant",
            "content": (
                f"{_NODE_TOKEN_COMPACT_MARKER}\n"
                f"{json.dumps(compacted_payload, ensure_ascii=False, sort_keys=True)}"
                f"{f'\\n\\n{str(compressed_text or '').strip()}' if str(compressed_text or '').strip() else ''}"
            ).strip(),
        }
        rewritten = [
            *list(parts.get("system_prefix") or []),
            *list(parts.get("bootstrap_user") or []),
            *list(parts.get("append_notice_tail") or []),
            compacted_block,
            *list(parts.get("recent_tail") or []),
            *list(parts.get("contract_tail") or []),
        ]
        return rewritten, compacted_payload

    def _estimate_node_send_preflight_tokens(
        self,
        *,
        task_id: str,
        node_id: str,
        config: Any,
        model_refs: list[str],
        provider_model: str,
        request_messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]],
        prompt_cache_key: str,
        tool_choice: str | dict[str, Any] | None,
        parallel_tool_calls: bool | None,
        allow_usage_ground_truth: bool = True,
    ) -> dict[str, Any]:
        preview_error = ""
        preview_payload: dict[str, Any] | None = None
        if config is not None and list(model_refs or []):
            try:
                preview_payload = runtime_chat_backend.build_send_provider_request_preview(
                    config=config,
                    messages=request_messages,
                    tools=tool_schemas,
                    model_refs=model_refs,
                    tool_choice=tool_choice,
                    parallel_tool_calls=parallel_tool_calls,
                    prompt_cache_key=prompt_cache_key,
                )
            except Exception as exc:
                preview_error = str(exc or exc.__class__.__name__).strip() or exc.__class__.__name__
        if preview_payload:
            preview_estimate_tokens = int(
                runtime_chat_backend.estimate_send_provider_request_preview_tokens(
                    preview_payload=preview_payload,
                )
                or 0
            )
        else:
            preview_estimate_tokens = int(
                runtime_send_token_preflight.estimate_runtime_provider_request_preview_tokens(
                    provider_request_body=None,
                    request_messages=request_messages,
                    tool_schemas=tool_schemas,
                )
                or 0
            )

        previous_effective_input_tokens = 0
        delta_estimate_tokens = 0
        comparable_to_previous_request = False
        if allow_usage_ground_truth:
            previous_truth = self._resolve_previous_node_observed_input_truth(
                task_id=task_id,
                node_id=node_id,
            )
            previous_effective_input_tokens = int(previous_truth.get('effective_input_tokens') or 0)
            previous_provider_model = str(previous_truth.get('provider_model') or '').strip()
            if (
                previous_effective_input_tokens > 0
                and previous_provider_model
                and self._provider_models_match(previous_provider_model, str(provider_model or '').strip())
            ):
                previous_record = self._resolve_previous_node_actual_request_record(
                    task_id=task_id,
                    node_id=node_id,
                )
                previous_truth_hash = str(previous_truth.get('actual_request_hash') or '').strip()
                previous_record_hash = str(previous_record.get('actual_request_hash') or '').strip()
                if previous_truth_hash and previous_record_hash and previous_truth_hash == previous_record_hash:
                    delta_estimate_tokens, comparable_to_previous_request = self._append_only_delta_estimate_tokens(
                        previous_request_messages=[
                            dict(item)
                            for item in list(previous_record.get('request_messages') or previous_record.get('messages') or [])
                            if isinstance(item, dict)
                        ],
                        current_request_messages=request_messages,
                        previous_tool_schemas=[
                            dict(item)
                            for item in list(previous_record.get('actual_tool_schemas') or previous_record.get('tool_schemas') or [])
                            if isinstance(item, dict)
                        ],
                        current_tool_schemas=tool_schemas,
                    )
        hybrid_estimate = runtime_send_token_preflight.build_runtime_hybrid_send_token_estimate(
            preview_estimate_tokens=preview_estimate_tokens,
            previous_effective_input_tokens=previous_effective_input_tokens,
            delta_estimate_tokens=delta_estimate_tokens,
            comparable_to_previous_request=comparable_to_previous_request,
        )
        return {
            'final_estimate_tokens': int(hybrid_estimate.final_estimate_tokens or 0),
            'preview_estimate_tokens': int(hybrid_estimate.preview_estimate_tokens or 0),
            'usage_based_estimate_tokens': int(hybrid_estimate.usage_based_estimate_tokens or 0),
            'delta_estimate_tokens': int(hybrid_estimate.delta_estimate_tokens or 0),
            'effective_input_tokens': int(previous_effective_input_tokens or 0),
            'estimate_source': str(hybrid_estimate.estimate_source or 'preview_estimate'),
            'comparable_to_previous_request': bool(hybrid_estimate.comparable_to_previous_request),
            'preview_estimation_error': preview_error,
        }

    def _resolve_previous_node_actual_request_record(
        self,
        *,
        task_id: str,
        node_id: str,
    ) -> dict[str, Any]:
        frame = self._runtime_frame(task_id, node_id) or {}
        actual_request_ref = str(frame.get('actual_request_ref') or '').strip()
        store = getattr(self._log_service, '_store', None)
        get_node = getattr(store, 'get_node', None)
        node = get_node(node_id) if callable(get_node) else None
        metadata = dict(getattr(node, 'metadata', {}) or {}) if node is not None else {}
        if not actual_request_ref:
            actual_request_ref = str(metadata.get('latest_runtime_actual_request_ref') or '').strip()
        if not actual_request_ref:
            return {}
        resolved = self._resolve_content_ref(actual_request_ref)
        if not str(resolved or '').strip():
            resolver = getattr(self._log_service, 'resolve_content_ref', None)
            if callable(resolver):
                try:
                    resolved = str(resolver(actual_request_ref) or '')
                except Exception:
                    resolved = ''
        if not str(resolved or '').strip():
            return {}
        try:
            payload = json.loads(resolved)
        except Exception:
            return {}
        return dict(payload) if isinstance(payload, dict) else {}

    def _resolve_previous_node_observed_input_truth(
        self,
        *,
        task_id: str,
        node_id: str,
    ) -> dict[str, Any]:
        frame = self._runtime_frame(task_id, node_id) or {}
        if isinstance(frame.get('observed_input_truth'), dict):
            return dict(frame.get('observed_input_truth') or {})
        store = getattr(self._log_service, '_store', None)
        get_node = getattr(store, 'get_node', None)
        node = get_node(node_id) if callable(get_node) else None
        metadata = dict(getattr(node, 'metadata', {}) or {}) if node is not None else {}
        if isinstance(metadata.get('latest_runtime_observed_input_truth'), dict):
            return dict(metadata.get('latest_runtime_observed_input_truth') or {})
        record = self._resolve_previous_node_actual_request_record(
            task_id=task_id,
            node_id=node_id,
        )
        if isinstance(record.get('observed_input_truth'), dict):
            return dict(record.get('observed_input_truth') or {})
        return {}

    def _append_only_delta_estimate_tokens(
        self,
        *,
        previous_request_messages: list[dict[str, Any]],
        current_request_messages: list[dict[str, Any]],
        previous_tool_schemas: list[dict[str, Any]],
        current_tool_schemas: list[dict[str, Any]],
    ) -> tuple[int, bool]:
        previous_records = self._prompt_message_records(previous_request_messages)
        current_records = self._prompt_message_records(current_request_messages)
        if not previous_records or len(current_records) < len(previous_records):
            return 0, False
        if not self._fresh_turn_seed_records_match(current_records[: len(previous_records)], previous_records):
            return 0, False
        previous_tool_schema_hash = str(
            build_actual_request_diagnostics(
                request_messages=[],
                tool_schemas=previous_tool_schemas,
            ).get('actual_tool_schema_hash')
            or ''
        ).strip()
        current_tool_schema_hash = str(
            build_actual_request_diagnostics(
                request_messages=[],
                tool_schemas=current_tool_schemas,
            ).get('actual_tool_schema_hash')
            or ''
        ).strip()
        if previous_tool_schema_hash != current_tool_schema_hash:
            return 0, False
        previous_estimate_tokens = int(
            runtime_send_token_preflight.estimate_runtime_provider_request_preview_tokens(
                provider_request_body=None,
                request_messages=previous_records,
                tool_schemas=previous_tool_schemas,
            )
            or 0
        )
        current_estimate_tokens = int(
            runtime_send_token_preflight.estimate_runtime_provider_request_preview_tokens(
                provider_request_body=None,
                request_messages=current_records,
                tool_schemas=current_tool_schemas,
            )
            or 0
        )
        return max(0, current_estimate_tokens - previous_estimate_tokens), True

    @staticmethod
    def _provider_models_match(previous_provider_model: str, current_provider_model: str) -> bool:
        previous_raw = str(previous_provider_model or '').strip()
        current_raw = str(current_provider_model or '').strip()
        if not previous_raw or not current_raw:
            return False
        if previous_raw == current_raw:
            return True
        previous_model = previous_raw.split(':', 1)[1].strip() if ':' in previous_raw else previous_raw
        current_model = current_raw.split(':', 1)[1].strip() if ':' in current_raw else current_raw
        return bool(previous_model and current_model and previous_model == current_model)

    def _apply_node_send_token_preflight(
        self,
        *,
        task_id: str,
        node_id: str,
        model_refs: list[str],
        request_messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]],
        prompt_cache_key: str = "",
        tool_choice: str | dict[str, Any] | None = None,
        parallel_tool_calls: bool | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], str, str]:
        _ = task_id, node_id
        normalized_model_refs = [
            str(item or "").strip()
            for item in list(model_refs or [])
            if str(item or "").strip()
        ]
        history_shrink_reason = ""
        failure_reason = ""

        try:
            config, _revision, _changed = get_runtime_config(force=False)
        except Exception as exc:
            config = None
            resolution_error = str(exc or exc.__class__.__name__).strip() or exc.__class__.__name__
        else:
            resolution_error = ""

        try:
            info = runtime_chat_backend.resolve_send_model_context_window_info(
                config=config,
                model_refs=normalized_model_refs,
            )
        except Exception as exc:
            info = runtime_chat_backend.SendModelContextWindowInfo(
                model_key=normalized_model_refs[0] if normalized_model_refs else "",
                provider_id="",
                provider_model="",
                resolved_model="",
                context_window_tokens=0,
                resolution_error=(
                    resolution_error
                    or str(exc or exc.__class__.__name__).strip()
                    or exc.__class__.__name__
                ),
            )

        context_window_tokens = max(0, int(getattr(info, "context_window_tokens", 0) or 0))
        estimate_payload = self._estimate_node_send_preflight_tokens(
            task_id=task_id,
            node_id=node_id,
            config=config,
            model_refs=normalized_model_refs,
            provider_model=str(getattr(info, "provider_model", "") or "").strip(),
            request_messages=request_messages,
            tool_schemas=tool_schemas,
            prompt_cache_key=str(prompt_cache_key or "").strip(),
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
        )
        final_estimate_tokens = int(estimate_payload.get('final_estimate_tokens') or 0)
        snapshot = runtime_send_token_preflight.build_runtime_send_token_preflight_snapshot(
            context_window_tokens=context_window_tokens,
            estimated_total_tokens=final_estimate_tokens,
        )

        token_preflight_diagnostics: dict[str, Any] = {
            "applied": False,
            "model_key": str(getattr(info, "model_key", "") or "").strip(),
            "provider_model": str(getattr(info, "provider_model", "") or "").strip(),
            "resolution_error": str(getattr(info, "resolution_error", "") or "").strip(),
            "context_window_tokens": int(snapshot.context_window_tokens or 0),
            "estimated_total_tokens": int(snapshot.estimated_total_tokens or 0),
            "trigger_tokens": int(snapshot.trigger_tokens or 0),
            "effective_trigger_tokens": int(snapshot.effective_trigger_tokens or 0),
            "ratio": float(snapshot.ratio or 0.0),
            "would_exceed_context_window": bool(snapshot.would_exceed_context_window),
            "would_trigger_token_compression": bool(snapshot.would_trigger_token_compression),
            "preview_estimate_tokens": int(estimate_payload.get('preview_estimate_tokens') or 0),
            "usage_based_estimate_tokens": int(estimate_payload.get('usage_based_estimate_tokens') or 0),
            "delta_estimate_tokens": int(estimate_payload.get('delta_estimate_tokens') or 0),
            "effective_input_tokens": int(estimate_payload.get('effective_input_tokens') or 0),
            "estimate_source": str(estimate_payload.get('estimate_source') or 'preview_estimate'),
            "comparable_to_previous_request": bool(estimate_payload.get('comparable_to_previous_request')),
            "final_estimate_tokens": int(final_estimate_tokens or 0),
            "final_request_tokens": int(final_estimate_tokens or 0),
        }
        if str(estimate_payload.get('preview_estimation_error') or '').strip():
            token_preflight_diagnostics["preview_estimation_error"] = str(
                estimate_payload.get('preview_estimation_error') or ''
            ).strip()

        if context_window_tokens <= _NODE_SEND_CONTEXT_WINDOW_HARD_MIN_TOKENS:
            failure_reason = (
                f"context_window_tokens <= {_NODE_SEND_CONTEXT_WINDOW_HARD_MIN_TOKENS} "
                f"(got {context_window_tokens})"
            )
        else:
            should_attempt_compaction = bool(
                snapshot.would_trigger_token_compression
                or int(final_estimate_tokens or 0) > int(context_window_tokens or 0)
            )
            if should_attempt_compaction:
                rewritten_messages, compact_payload = self._rewrite_request_messages_for_token_compaction(
                    node_id=node_id,
                    request_messages=request_messages,
                )
                request_messages = rewritten_messages
                pre_compaction_snapshot = dict(token_preflight_diagnostics)
                rewritten_estimate_payload = self._estimate_node_send_preflight_tokens(
                    task_id=task_id,
                    node_id=node_id,
                    config=config,
                    model_refs=normalized_model_refs,
                    provider_model=str(getattr(info, "provider_model", "") or "").strip(),
                    request_messages=request_messages,
                    tool_schemas=tool_schemas,
                    prompt_cache_key=str(prompt_cache_key or "").strip(),
                    tool_choice=tool_choice,
                    parallel_tool_calls=parallel_tool_calls,
                    allow_usage_ground_truth=False,
                )
                post_compaction_tokens = int(rewritten_estimate_payload.get('final_estimate_tokens') or 0)
                post_compaction_snapshot = runtime_send_token_preflight.build_runtime_send_token_preflight_snapshot(
                    context_window_tokens=context_window_tokens,
                    estimated_total_tokens=post_compaction_tokens,
                )
                token_preflight_diagnostics.update(
                    {
                        "applied": True,
                        "mode": "marker",
                        "history_shrink_reason": "token_compression",
                        "pre_compaction_estimated_total_tokens": int(
                            pre_compaction_snapshot.get('estimated_total_tokens') or 0
                        ),
                        "pre_compaction_ratio": float(pre_compaction_snapshot.get('ratio') or 0.0),
                        "pre_compaction_would_exceed_context_window": bool(
                            pre_compaction_snapshot.get('would_exceed_context_window')
                        ),
                        "pre_compaction_would_trigger_token_compression": bool(
                            pre_compaction_snapshot.get('would_trigger_token_compression')
                        ),
                        "pre_compaction_preview_estimate_tokens": int(
                            pre_compaction_snapshot.get('preview_estimate_tokens') or 0
                        ),
                        "pre_compaction_usage_based_estimate_tokens": int(
                            pre_compaction_snapshot.get('usage_based_estimate_tokens') or 0
                        ),
                        "pre_compaction_delta_estimate_tokens": int(
                            pre_compaction_snapshot.get('delta_estimate_tokens') or 0
                        ),
                        "pre_compaction_effective_input_tokens": int(
                            pre_compaction_snapshot.get('effective_input_tokens') or 0
                        ),
                        "pre_compaction_estimate_source": str(
                            pre_compaction_snapshot.get('estimate_source') or 'preview_estimate'
                        ),
                        "pre_compaction_comparable_to_previous_request": bool(
                            pre_compaction_snapshot.get('comparable_to_previous_request')
                        ),
                        "pre_compaction_final_estimate_tokens": int(
                            pre_compaction_snapshot.get('final_estimate_tokens') or 0
                        ),
                        "estimated_total_tokens": int(post_compaction_snapshot.estimated_total_tokens or 0),
                        "ratio": float(post_compaction_snapshot.ratio or 0.0),
                        "would_exceed_context_window": bool(post_compaction_snapshot.would_exceed_context_window),
                        "would_trigger_token_compression": bool(
                            post_compaction_snapshot.would_trigger_token_compression
                        ),
                        "preview_estimate_tokens": int(
                            rewritten_estimate_payload.get('preview_estimate_tokens') or 0
                        ),
                        "usage_based_estimate_tokens": int(
                            rewritten_estimate_payload.get('usage_based_estimate_tokens') or 0
                        ),
                        "delta_estimate_tokens": int(
                            rewritten_estimate_payload.get('delta_estimate_tokens') or 0
                        ),
                        "effective_input_tokens": int(
                            rewritten_estimate_payload.get('effective_input_tokens') or 0
                        ),
                        "estimate_source": str(
                            rewritten_estimate_payload.get('estimate_source') or 'preview_estimate'
                        ),
                        "comparable_to_previous_request": bool(
                            rewritten_estimate_payload.get('comparable_to_previous_request')
                        ),
                        "final_estimate_tokens": int(post_compaction_tokens or 0),
                        "post_compaction_estimate_tokens": int(post_compaction_tokens or 0),
                        "rewritten_preview_estimate_tokens": int(
                            rewritten_estimate_payload.get('preview_estimate_tokens') or 0
                        ),
                        "post_compaction_estimate_source": str(
                            rewritten_estimate_payload.get('estimate_source') or 'preview_estimate'
                        ),
                        "final_request_tokens": int(post_compaction_tokens or 0),
                        "compaction_payload": dict(compact_payload or {}),
                    }
                )
                if str(rewritten_estimate_payload.get('preview_estimation_error') or '').strip():
                    token_preflight_diagnostics["rewritten_preview_estimation_error"] = str(
                        rewritten_estimate_payload.get('preview_estimation_error') or ''
                    ).strip()
                history_shrink_reason = "token_compression"
                if int(post_compaction_tokens or 0) > int(context_window_tokens or 0):
                    failure_reason = (
                        f"final_request_tokens ({int(post_compaction_tokens or 0)}) exceeded "
                        f"context_window_tokens ({int(context_window_tokens or 0)}) after compression"
                    )

        if failure_reason:
            token_preflight_diagnostics["error"] = str(failure_reason or "").strip()

        return (
            request_messages,
            token_preflight_diagnostics,
            history_shrink_reason,
            failure_reason,
        )

    def run_node_send_preflight_for_control_turn(
        self,
        *,
        task_id: str,
        node_id: str,
        model_refs: list[str],
        request_messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]],
        prompt_cache_key: str = "",
        tool_choice: str | dict[str, Any] | None = None,
        parallel_tool_calls: bool | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], str, str]:
        return self._apply_node_send_token_preflight(
            task_id=task_id,
            node_id=node_id,
            model_refs=model_refs,
            request_messages=request_messages,
            tool_schemas=tool_schemas,
            prompt_cache_key=prompt_cache_key,
            tool_choice=tool_choice,
            parallel_tool_calls=parallel_tool_calls,
        )

    @staticmethod
    def _detect_xml_pseudo_tool_call(content: Any, *, allowed_tool_names: set[str]) -> dict[str, Any] | None:
        return detect_xml_pseudo_tool_call(content, allowed_tool_names=allowed_tool_names)

    @classmethod
    def _extract_tool_calls_from_xml_pseudo_content(
        cls,
        content: Any,
        *,
        visible_tools: dict[str, Tool],
    ):
        return extract_tool_calls_from_xml_pseudo_content(content, visible_tools=visible_tools)

    @classmethod
    def _recover_tool_calls_from_json_payload(
        cls,
        content: Any,
        *,
        allowed_tool_names: set[str],
    ):
        return recover_tool_calls_from_json_payload(content, allowed_tool_names=allowed_tool_names)

    @classmethod
    def _tool_calls_from_json_payload(
        cls,
        payload: Any,
        *,
        allowed_tool_names: set[str],
    ):
        from main.runtime.tool_call_repair import tool_calls_from_json_payload

        return tool_calls_from_json_payload(payload, allowed_tool_names=allowed_tool_names)

    @staticmethod
    def _extract_json_payload_candidates(content: str) -> list[str]:
        from main.runtime.tool_call_repair import extract_json_payload_candidates

        return extract_json_payload_candidates(content)

    @classmethod
    def _xml_tool_repair_message(
        cls,
        *,
        xml_excerpt: str,
        tool_names: list[str],
        attempt_count: int,
        attempt_limit: int,
        latest_issue: str = '',
    ) -> str:
        return build_xml_tool_repair_message(
            xml_excerpt=xml_excerpt,
            tool_names=tool_names,
            attempt_count=attempt_count,
            attempt_limit=attempt_limit,
            latest_issue=latest_issue,
        )

    async def _handle_final_result_tool_turn(
        self,
        *,
        task,
        node,
        response,
        tool_call: Any,
        tools: dict[str, Tool],
        message_history: list[dict[str, Any]],
        runtime_context: dict[str, Any],
        assistant_content: Any,
    ) -> tuple[NodeFinalResult | None, list[dict[str, Any]], list[str], str]:
        raw_tool_arguments = self._normalize_tool_call_arguments(getattr(tool_call, 'arguments', {}))
        tool_payload = {
            'id': str(getattr(tool_call, 'id', '') or ''),
            'name': str(getattr(tool_call, 'name', '') or ''),
            'arguments': self._normalize_final_result_payload(
                raw_payload=raw_tool_arguments,
                message_history=message_history,
                response_content=assistant_content,
            ),
        }
        assistant_tool_calls = [
            {
                'id': tool_payload['id'],
                'type': 'function',
                'function': {'name': tool_payload['name'], 'arguments': json.dumps(tool_payload['arguments'], ensure_ascii=False)},
            }
        ]
        stage_gate = self._execution_stage_gate(
            task_id=task.task_id,
            node_id=node.node_id,
            node_kind=node.node_kind,
        )
        self._log_service.update_frame(
            task.task_id,
            node.node_id,
            lambda frame: {
                **frame,
                'depth': node.depth,
                'node_kind': node.node_kind,
                'phase': 'waiting_tool_results',
                'messages': self._prepare_messages(message_history, runtime_context=runtime_context),
                'pending_tool_calls': [tool_payload],
                'tool_calls': [self._live_tool_entry(tool_call)],
                **self._execution_stage_frame_payload(node_kind=node.node_kind, stage_gate=stage_gate),
                'last_error': '',
            },
            publish_snapshot=True,
        )

        started_at = now_iso()
        started_monotonic = time.monotonic()
        self._update_tool_live_state(
            task_id=task.task_id,
            node_id=node.node_id,
            tool_call_id=tool_payload['id'],
            status='running',
            started_at=started_at,
            finished_at='',
            elapsed_seconds=None,
        )
        raw_result = await self._execute_tool_raw(
            tools=tools,
            tool_name=tool_payload['name'],
            arguments=tool_payload['arguments'],
            runtime_context={
                **runtime_context,
                'current_tool_call_id': tool_payload['id'],
                'stage_turn_granted': bool(
                    stage_gate.get('enabled')
                    and stage_gate.get('has_active_stage')
                    and not stage_gate.get('transition_required')
                ),
            },
        )
        tool_content = self._render_tool_message_content(
            raw_result,
            runtime_context=runtime_context,
            tool_name=tool_payload['name'],
            delivery_metadata=self._tool_result_delivery_metadata(
                tools=tools,
                tool_name=tool_payload['name'],
                arguments=dict(tool_payload.get('arguments') or {}),
            ),
        )
        finished_at = now_iso()
        elapsed_seconds = round(max(0.0, time.monotonic() - started_monotonic), 1)
        status = self._tool_message_status(raw_result if raw_result is not None else tool_content)
        self._update_tool_live_state(
            task_id=task.task_id,
            node_id=node.node_id,
            tool_call_id=tool_payload['id'],
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            elapsed_seconds=elapsed_seconds,
            result_content=tool_content,
        )
        record_tool_results = getattr(self._log_service, 'record_tool_result_batch', None)
        if callable(record_tool_results):
            record_tool_results(
                task_id=task.task_id,
                node_id=node.node_id,
                response_tool_calls=[tool_call],
                results=[
                    {
                        'live_state': {
                            'tool_call_id': tool_payload['id'],
                            'tool_name': tool_payload['name'],
                            'status': status,
                            'started_at': started_at,
                            'finished_at': finished_at,
                            'elapsed_seconds': elapsed_seconds,
                        },
                        'tool_message': {
                            'role': 'tool',
                            'tool_call_id': tool_payload['id'],
                            'name': tool_payload['name'],
                            'content': tool_content,
                            'started_at': started_at,
                            'finished_at': finished_at,
                            'elapsed_seconds': elapsed_seconds,
                            'status': status,
                        },
                    }
                ],
            )

        assistant_message = {
            'role': 'assistant',
            'content': self._externalize_message_content(
                assistant_content,
                runtime_context=runtime_context,
                display_name=f'assistant:{node.node_id}',
                source_kind='assistant_message',
            ),
            'tool_calls': assistant_tool_calls,
        }
        tool_messages = [
            {
                'role': 'tool',
                'tool_call_id': tool_payload['id'],
                'name': tool_payload['name'],
                'content': tool_content,
                'started_at': started_at,
                'finished_at': finished_at,
                'elapsed_seconds': elapsed_seconds,
                'status': status,
            }
        ]
        next_history = list(message_history)
        next_history.append(assistant_message)
        next_history.extend(self._dedupe_tool_messages(tool_messages, existing_messages=next_history))
        prepared_history = self._prepare_messages(next_history, runtime_context=runtime_context)
        self._log_service.update_node_input(
            task.task_id,
            node.node_id,
            json.dumps(prepared_history, ensure_ascii=False, indent=2),
        )
        self._log_service.update_frame(
            task.task_id,
            node.node_id,
            lambda frame: {
                **frame,
                'depth': node.depth,
                'node_kind': node.node_kind,
                'phase': 'waiting_tool_results',
                'messages': prepared_history,
                'pending_tool_calls': [],
                'tool_calls': [
                    {
                        'tool_call_id': tool_payload['id'],
                        'tool_name': tool_payload['name'],
                        'status': status,
                        'started_at': started_at,
                        'finished_at': finished_at,
                        'elapsed_seconds': elapsed_seconds,
                    }
                ],
                **self._execution_stage_frame_payload(
                    node_kind=node.node_kind,
                    stage_gate=self._execution_stage_gate(
                        task_id=task.task_id,
                        node_id=node.node_id,
                        node_kind=node.node_kind,
                    ),
                ),
                'last_error': tool_content if status == 'error' else '',
            },
            publish_snapshot=True,
        )

        if isinstance(raw_result, str) and raw_result.startswith('Error:'):
            return None, prepared_history, [], str(raw_result or '').strip()

        raw_payload = raw_result if isinstance(raw_result, dict) else None
        if raw_payload is None:
            return None, prepared_history, [], f'{FINAL_RESULT_TOOL_NAME} must return an object payload'
        raw_payload = self._normalize_final_result_payload(
            raw_payload=raw_payload,
            message_history=message_history,
            response_content=assistant_content,
        )
        result = self._coerce_final_result_payload(raw_payload)
        if result is None:
            return None, prepared_history, [], f'{FINAL_RESULT_TOOL_NAME} returned an invalid payload'
        violations = self._validate_final_result(
            result=result,
            raw_payload=raw_payload,
            has_tool_results=self._node_has_meaningful_tool_results(task_id=task.task_id, node_id=node.node_id),
            node_kind=node.node_kind,
        )
        if violations:
            return None, prepared_history, violations, ''
        return result, prepared_history, [], ''

    def _record_rejected_tool_turn(
        self,
        *,
        task,
        node,
        response,
        response_tool_calls: list[Any],
        message_history: list[dict[str, Any]],
        runtime_context: dict[str, Any],
        error_content: str,
    ) -> list[dict[str, Any]]:
        delta_messages = self._rejected_tool_turn_delta_messages(
            node=node,
            response=response,
            response_tool_calls=response_tool_calls,
            message_history=message_history,
            runtime_context=runtime_context,
            error_content=error_content,
        )
        assistant_message = dict(delta_messages[0] or {}) if delta_messages else {'role': 'assistant', 'content': ''}
        tool_messages = [
            dict(item)
            for item in list(delta_messages[1:] or [])
            if isinstance(item, dict)
        ]
        next_history = list(message_history)
        next_history.append(assistant_message)
        next_history.extend(tool_messages)
        prepared_history = self._prepare_messages(next_history, runtime_context=runtime_context)
        self._log_service.update_node_input(
            task.task_id,
            node.node_id,
            json.dumps(prepared_history, ensure_ascii=False, indent=2),
        )
        self._log_service.update_frame(
            task.task_id,
            node.node_id,
            lambda frame: {
                **frame,
                'depth': node.depth,
                'node_kind': node.node_kind,
                'phase': 'waiting_tool_results',
                'messages': prepared_history,
                'pending_tool_calls': [],
                'tool_calls': [
                    {
                        'tool_call_id': str(getattr(call, 'id', '') or ''),
                        'tool_name': str(getattr(call, 'name', '') or ''),
                        'status': 'error',
                        'started_at': '',
                        'finished_at': '',
                        'elapsed_seconds': None,
                    }
                    for call in list(response_tool_calls or [])
                ],
                **self._execution_stage_frame_payload(
                    node_kind=node.node_kind,
                    stage_gate=self._execution_stage_gate(
                        task_id=task.task_id,
                        node_id=node.node_id,
                        node_kind=node.node_kind,
                    ),
                ),
                'last_error': error_content,
            },
            publish_snapshot=True,
        )
        return prepared_history

    def _rejected_tool_turn_delta_messages(
        self,
        *,
        node,
        response,
        response_tool_calls: list[Any],
        message_history: list[dict[str, Any]],
        runtime_context: dict[str, Any],
        error_content: str,
    ) -> list[dict[str, Any]]:
        assistant_tool_calls = [
            {
                'id': str(getattr(call, 'id', '') or ''),
                'type': 'function',
                'function': {
                    'name': str(getattr(call, 'name', '') or ''),
                    'arguments': json.dumps(self._normalize_tool_call_arguments(getattr(call, 'arguments', {})), ensure_ascii=False),
                },
            }
            for call in list(response_tool_calls or [])
        ]
        assistant_message = {
            'role': 'assistant',
            'content': self._externalize_message_content(
                response.content,
                runtime_context=runtime_context,
                display_name=f'assistant:{node.node_id}',
                source_kind='assistant_message',
            ),
            'tool_calls': assistant_tool_calls,
        }
        tool_messages = [
            {
                'role': 'tool',
                'tool_call_id': str(getattr(call, 'id', '') or ''),
                'name': str(getattr(call, 'name', '') or ''),
                'content': error_content,
                'started_at': '',
                'finished_at': '',
                'elapsed_seconds': None,
                'status': 'error',
            }
            for call in list(response_tool_calls or [])
        ]
        existing_messages = [*list(message_history), dict(assistant_message)]
        return [
            dict(assistant_message),
            *self._dedupe_tool_messages(tool_messages, existing_messages=existing_messages),
        ]

    @classmethod
    def _read_only_repeat_violations(
        cls,
        *,
        response_tool_calls: list[Any],
        task_id: str,
        node_kind: str,
        message_history: list[dict[str, Any]],
        prior_violation_counts: dict[str, int],
    ) -> list[dict[str, Any]]:
        normalized_task_id = str(task_id or '').strip()
        if not normalized_task_id:
            return []
        normalized_kind = str(node_kind or '').strip().lower()
        if normalized_kind not in _STAGE_BUDGET_NODE_KINDS:
            return []
        latest_messages_by_signature = cls._latest_tool_messages_by_signature(message_history)
        latest_spawn_ref = cls._latest_spawn_child_nodes_ref(message_history)
        violations: list[dict[str, Any]] = []
        for call in list(response_tool_calls or []):
            tool_name = str(getattr(call, 'name', '') or '').strip()
            arguments = cls._normalize_tool_call_arguments(getattr(call, 'arguments', {}))
            if not cls._is_soft_reject_read_only_call(tool_name=tool_name, arguments=arguments):
                continue
            signature = f"{tool_name}:{json.dumps(arguments, ensure_ascii=False, sort_keys=True)}"
            if cls._is_current_task_progress_call(
                tool_name=tool_name,
                arguments=arguments,
                task_id=normalized_task_id,
            ):
                violations.append(
                    {
                        'signature': signature,
                        'repair_text': cls._current_task_progress_repair_message(
                            task_id=normalized_task_id,
                            node_kind=normalized_kind,
                            latest_spawn_ref=latest_spawn_ref,
                        ),
                    }
                )
                continue
            if signature in prior_violation_counts or signature in latest_messages_by_signature:
                latest_tool_message = latest_messages_by_signature.get(signature)
                violations.append(
                    {
                        'signature': signature,
                        'repair_text': cls._read_only_repeat_repair_message(
                            tool_name=tool_name,
                            arguments=arguments,
                            latest_tool_message=latest_tool_message,
                        ),
                    }
                )
        return violations

    @staticmethod
    def _is_soft_reject_read_only_call(*, tool_name: str, arguments: dict[str, Any]) -> bool:
        normalized_tool = str(tool_name or '').strip()
        if normalized_tool in {'task_progress', 'task_node_detail'}:
            return True
        action = str(arguments.get('action') or '').strip().lower()
        if normalized_tool == 'content':
            return action in {'open', 'search', 'describe'}
        return False

    @staticmethod
    def _is_current_task_progress_call(*, tool_name: str, arguments: dict[str, Any], task_id: str) -> bool:
        if str(tool_name or '').strip() != 'task_progress':
            return False
        requested_task_id = str(arguments.get('任务id') or arguments.get('task_id') or '').strip()
        return bool(requested_task_id) and requested_task_id == str(task_id or '').strip()

    @classmethod
    def _latest_spawn_child_nodes_ref(cls, messages: list[dict[str, Any]]) -> str:
        for message in reversed(list(messages or [])):
            if str((message or {}).get('role') or '').strip().lower() != 'tool':
                continue
            if str((message or {}).get('name') or '').strip() != _STAGE_SPAWN_TOOL_NAME:
                continue
            payload = cls._tool_message_json_payload(message)
            ref = str(payload.get('ref') or payload.get('resolved_ref') or '').strip()
            if ref:
                return ref
        return ''

    @staticmethod
    def _tool_message_json_payload(message: dict[str, Any]) -> dict[str, Any]:
        content = (message or {}).get('content')
        if isinstance(content, dict):
            return dict(content)
        text = str(content or '').strip()
        if not text or not text.startswith('{'):
            return {}
        try:
            parsed = json.loads(text)
        except Exception:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}

    @classmethod
    def _latest_tool_messages_by_signature(cls, messages: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        call_signatures: dict[str, str] = {}
        latest_messages: dict[str, dict[str, Any]] = {}
        for message in list(messages or []):
            role = str((message or {}).get('role') or '').strip().lower()
            if role == 'assistant':
                for tool_call in list((message or {}).get('tool_calls') or []):
                    call_id = extract_call_id((tool_call or {}).get('id'))
                    if not call_id:
                        continue
                    function_payload = (tool_call or {}).get('function') if isinstance((tool_call or {}).get('function'), dict) else {}
                    tool_name = str(function_payload.get('name') or (tool_call or {}).get('name') or '').strip()
                    arguments = cls._normalize_tool_call_arguments(
                        function_payload.get('arguments') if function_payload else (tool_call or {}).get('arguments')
                    )
                    if not cls._is_soft_reject_read_only_call(tool_name=tool_name, arguments=arguments):
                        continue
                    call_signatures[call_id] = f"{tool_name}:{json.dumps(arguments, ensure_ascii=False, sort_keys=True)}"
                continue
            if role != 'tool':
                continue
            call_id = extract_call_id((message or {}).get('tool_call_id'))
            signature = call_signatures.get(call_id)
            if signature:
                candidate = dict(message or {})
                existing = latest_messages.get(signature)
                candidate_payload = cls._tool_message_json_payload(candidate)
                existing_payload = cls._tool_message_json_payload(existing or {})
                candidate_ref = str(candidate_payload.get('ref') or candidate_payload.get('resolved_ref') or '').strip()
                existing_ref = str(existing_payload.get('ref') or existing_payload.get('resolved_ref') or '').strip()
                if existing is not None and existing_ref and not candidate_ref:
                    continue
                latest_messages[signature] = candidate
        return latest_messages

    @staticmethod
    def _current_task_progress_repair_message(*, task_id: str, node_kind: str, latest_spawn_ref: str) -> str:
        node_label = '验收节点' if str(node_kind or '').strip().lower() == 'acceptance' else '执行节点'
        base = (
            f'{node_label}不得对当前正在执行的 `task_id` 调用 `task_progress`。'
            '不要用它等待子节点、轮询当前任务树或汇总当前节点自己的派生结果。'
        )
        if latest_spawn_ref:
            return (
                f'{base} 最近一次 `spawn_child_nodes` 返回的 ref 是 `{latest_spawn_ref}`；'
                '请先用 `content.open` / `content.search` 查看该 ref，'
                '再基于 `node_output_summary`、`check_result`、`failure_info.summary`、'
                '`failure_info.remaining_work` 判断应直接吸收结果，还是只对失败分支再次 `spawn_child_nodes`。'
            )
        return (
            f'{base} 请回看最近的 `spawn_child_nodes` 输出、已有 `artifact:` 引用和失败分支的 `failure_info`，'
            '优先用 `content.open` / `content.search` 做局部核对，再决定吸收结果、重派失败分支，或推进下一阶段。'
        )

    @classmethod
    def _read_only_repeat_repair_message(
        cls,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        latest_tool_message: dict[str, Any] | None,
    ) -> str:
        latest_payload = cls._tool_message_json_payload(latest_tool_message or {})
        latest_ref = str(latest_payload.get('ref') or latest_payload.get('resolved_ref') or '').strip()
        target_ref = str(arguments.get('ref') or arguments.get('path') or '').strip()
        base = '不要重复调用完全相同的只读/检索工具。'
        normalized_tool = str(tool_name or '').strip()
        if normalized_tool == 'content':
            action = str(arguments.get('action') or '').strip().lower()
            detail = (
                f'你刚刚已经对同一内容执行了 `content.{action or "open"}`。'
                '先基于已有结果继续分析；若信息不足，请改用不同的 `start_line` / `end_line`、不同的 `query`，或直接进入汇总/下一阶段。'
            )
        elif normalized_tool == 'task_node_detail':
            detail = (
                '你刚刚已经请求过同一份节点详情。请先消费已有的 `summary`、`final_output_ref`、`check_result_ref`、'
                '`execution_trace_ref` 或相关 `artifact`，若仍不足，再改用不同节点或不同 detail 需求。'
            )
        else:
            detail = (
                '请先消费已有任务树、节点摘要和 `artifact` 结果；如果仍然不足，请改查不同对象，而不是重复原样查询。'
            )
        if latest_ref and target_ref:
            return (
                f'{base} {detail} 最近同签名结果可复用的 ref：`{latest_ref}`。'
                f' 当前重复命中的目标：`{target_ref}`。'
            )
        if latest_ref:
            return f'{base} {detail} 最近同签名结果可复用的 ref：`{latest_ref}`。'
        if target_ref:
            return f'{base} {detail} 当前重复命中的目标：`{target_ref}`。'
        return f'{base} {detail}'

    def _node_has_meaningful_tool_results(self, *, task_id: str, node_id: str) -> bool:
        store = getattr(self._log_service, '_store', None)
        lister = getattr(store, 'list_task_node_tool_results', None) if store is not None else None
        if not callable(lister):
            return False
        ignored_tool_names = {STAGE_TOOL_NAME, FINAL_RESULT_TOOL_NAME, *ReActToolLoop._CONTROL_TOOL_NAMES}
        for item in list(lister(task_id, node_id) or []):
            tool_name = str(getattr(item, 'tool_name', '') or '').strip()
            if tool_name and tool_name not in ignored_tool_names:
                return True
        return False

    @staticmethod
    def _result_protocol_message(*, node_kind: str = 'execution') -> str:
        guidance = ReActToolLoop._result_repair_guidance(node_kind=node_kind)
        normalized_kind = str(node_kind or '').strip().lower()
        if normalized_kind == 'acceptance':
            return (
                f'你上一条回复不符合结果 JSON 协议 v{RESULT_SCHEMA_VERSION}。'
                '请只回复一个 JSON 对象，并且只使用以下键：'
                '{"status":"success|failed","delivery_status":"final|partial|blocked","summary":"...",'
                '"answer":"...","evidence":[{"kind":"file|artifact|url","path":"","ref":"","start_line":1,"end_line":1,"note":"..."}],'
                '"remaining_work":["..."],"blocking_reason":"..."}。'
                '不要使用 Markdown。'
                f'{guidance}'
            )
        return (
            f'你上一条回复不符合结果 JSON 协议 v{RESULT_SCHEMA_VERSION}。'
            '如果你现在要结束当前节点，只回复一个 JSON 对象，并且只使用以下键：'
            '{"status":"success|failed","delivery_status":"final|partial|blocked","summary":"...",'
            '"answer":"...","evidence":[{"kind":"file|artifact|url","path":"","ref":"","start_line":1,"end_line":1,"note":"..."}],'
            '"remaining_work":["..."],"blocking_reason":"..."}。'
            '如果任务实际上还没有完成，不要输出 prose 或提前结束的结果 JSON，而是继续使用工具调用、阶段切换或子节点动作推进。'
            '当你真正返回最终 JSON 时，也不要使用 Markdown。'
            f'{guidance}'
        )

    @staticmethod
    def _result_contract_violation_message(violations: list[str], *, node_kind: str = 'execution') -> str:
        bullet_text = '; '.join(str(item or '').strip() for item in violations if str(item or '').strip()) or '结果协议违规'
        guidance = ReActToolLoop._result_repair_guidance(node_kind=node_kind)
        normalized_kind = str(node_kind or '').strip().lower()
        if normalized_kind == 'acceptance':
            return (
                f'你上一条回复虽然能解析成 JSON，但违反了结果协议 v{RESULT_SCHEMA_VERSION}：{bullet_text}。'
                '请修复所有违规项，并只回复一个 JSON 对象。'
                '除非交付物已经完整满足要求，否则不要声称 success。'
                f'{guidance}'
            )
        return (
            f'你上一条回复虽然能解析成 JSON，但违反了结果协议 v{RESULT_SCHEMA_VERSION}：{bullet_text}。'
            '如果你现在要结束当前节点，请修复所有违规项，并只回复一个 JSON 对象。'
            '如果任务实际上还没有完成，不要强行再输出一个提前结束的结果 JSON，而是继续使用工具调用、阶段切换或子节点动作推进。'
            '除非交付物已经完整满足要求，否则不要声称 success。'
            f'{guidance}'
        )

    @staticmethod
    def _result_repair_guidance(*, node_kind: str) -> str:
        normalized_kind = str(node_kind or '').strip().lower()
        if normalized_kind == 'acceptance':
            return (
                '验收节点不要使用 delivery_status="partial"。'
                '如果你是在拒绝交付，返回 failed+final。'
                '如果因为证据缺失、artifact 不可读或上下文不足而无法完成验收，返回 failed+blocked。'
            )
        return (
            '执行节点不要使用 delivery_status="partial"。'
            '如果任务实际上还没有完成，继续通过工具调用或阶段切换推进，而不是继续输出结果 JSON。'
            '只有在当前权限、环境和工具条件下确实被阻塞时，才返回 failed+blocked。'
        )

    @staticmethod
    def _extract_json_object_candidates(content: str) -> list[str]:
        text = str(content or '')
        candidates: list[str] = []
        start_index: int | None = None
        depth = 0
        in_string = False
        escape = False
        for index, char in enumerate(text):
            if in_string:
                if escape:
                    escape = False
                elif char == '\\':
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
                continue
            if char == '{':
                if depth == 0:
                    start_index = index
                depth += 1
                continue
            if char == '}' and depth > 0:
                depth -= 1
                if depth == 0 and start_index is not None:
                    candidates.append(text[start_index : index + 1])
                    start_index = None
        candidates.reverse()
        return candidates

    @staticmethod
    def _result_protocol_message(*, node_kind: str = 'execution') -> str:
        guidance = ReActToolLoop._result_repair_guidance(node_kind=node_kind)
        normalized_kind = str(node_kind or '').strip().lower()
        if normalized_kind == 'acceptance':
            return (
                f'Your previous reply did not submit a valid final result for result contract v{RESULT_SCHEMA_VERSION}. '
                f'If you are ending the node now, call `{FINAL_RESULT_TOOL_NAME}` with exactly these fields: '
                '{"status":"success|failed","delivery_status":"final|blocked","summary":"...","answer":"...",'
                '"evidence":[{"kind":"file|artifact|url","path":"","ref":"","start_line":1,"end_line":1,"note":"..."}],'
                '"remaining_work":["..."],"blocking_reason":"..."}. '
                'Do not reply with prose, Markdown, or a raw JSON object. '
                f'{guidance}'
            )
        return (
            f'Your previous reply did not submit a valid final result for result contract v{RESULT_SCHEMA_VERSION}. '
            f'If you are ending the node now, call `{FINAL_RESULT_TOOL_NAME}` with exactly these fields: '
            '{"status":"success|failed","delivery_status":"final|blocked","summary":"...","answer":"...",'
            '"evidence":[{"kind":"file|artifact|url","path":"","ref":"","start_line":1,"end_line":1,"note":"..."}],'
            '"remaining_work":["..."],"blocking_reason":"..."}. '
            'If the task is not complete yet, continue with tools or `submit_next_stage` instead of forcing a premature final submission. '
            'Do not reply with prose, Markdown, or a raw JSON object. '
            f'{guidance}'
        )

    @staticmethod
    def _result_contract_violation_message(violations: list[str], *, node_kind: str = 'execution') -> str:
        bullet_text = '; '.join(str(item or '').strip() for item in violations if str(item or '').strip()) or 'result contract violation'
        guidance = ReActToolLoop._result_repair_guidance(node_kind=node_kind)
        normalized_kind = str(node_kind or '').strip().lower()
        if normalized_kind == 'acceptance':
            return (
                f'Your last `{FINAL_RESULT_TOOL_NAME}` payload violated result contract v{RESULT_SCHEMA_VERSION}: {bullet_text}. '
                f'If you are ending the node now, fix the payload and call `{FINAL_RESULT_TOOL_NAME}` again. '
                'Do not reply with prose or a raw JSON object. '
                f'{guidance}'
            )
        return (
            f'Your last `{FINAL_RESULT_TOOL_NAME}` payload violated result contract v{RESULT_SCHEMA_VERSION}: {bullet_text}. '
            f'If you are ending the node now, fix the payload and call `{FINAL_RESULT_TOOL_NAME}` again. '
            'If the task is not complete yet, do not force another premature final submission. Continue with tools or `submit_next_stage`. '
            f'{guidance}'
        )

    @staticmethod
    def _result_repair_guidance(*, node_kind: str) -> str:
        normalized_kind = str(node_kind or '').strip().lower()
        if normalized_kind == 'acceptance':
            return (
                f'Acceptance nodes must end through `{FINAL_RESULT_TOOL_NAME}`. '
                'Use failed+final for a normal rejection, and failed+blocked only when verification is genuinely blocked.'
            )
        return (
            f'Execution nodes must end through `{FINAL_RESULT_TOOL_NAME}`. '
            'Use success+final on completion, and use failed+blocked only when the node is genuinely blocked. '
            'If work remains, continue with tools or `submit_next_stage` instead of finalizing.'
        )

    @staticmethod
    def _is_provider_chain_exhausted_error(error: Exception | str) -> bool:
        return PUBLIC_PROVIDER_FAILURE_MESSAGE in str(error or '')

    @staticmethod
    def _provider_retry_delay_seconds(attempt_count: int) -> float:
        if attempt_count <= 1:
            return 1.0
        return float(min(10, attempt_count))

    def _refresh_runtime_config_for_retry_invalidation(self) -> bool:
        refresher = getattr(self, '_runtime_config_refresh_for_retry_invalidation', None)
        if not callable(refresher):
            return False
        try:
            return bool(refresher())
        except Exception:
            return False

    @staticmethod
    def _empty_response_retry_delay_seconds(attempt_count: int) -> float:
        if attempt_count <= 1:
            return 1.0
        return float(min(10, attempt_count))

    @staticmethod
    def _is_empty_model_response(response: Any) -> bool:
        if list(getattr(response, 'tool_calls', None) or []):
            return False
        if str(getattr(response, 'content', None) or '').strip():
            return False
        if str(getattr(response, 'error_text', None) or '').strip():
            return False
        if str(getattr(response, 'reasoning_content', None) or '').strip():
            return False
        thinking_blocks = getattr(response, 'thinking_blocks', None)
        if isinstance(thinking_blocks, list) and thinking_blocks:
            return False
        return True

    @staticmethod
    def _repair_tool_choice(
        *,
        visible_tools: dict[str, Tool],
        stage_gate: dict[str, Any],
        invalid_final_submission_count: int,
        invalid_stage_submission_count: int,
    ) -> dict[str, Any] | None:
        visible_tool_names = {
            str(name or '').strip()
            for name in dict(visible_tools or {}).keys()
            if str(name or '').strip()
        }
        if invalid_final_submission_count > 0 and FINAL_RESULT_TOOL_NAME in visible_tool_names:
            return {
                'type': 'function',
                'function': {'name': FINAL_RESULT_TOOL_NAME},
            }
        if (
            invalid_stage_submission_count > 0
            and STAGE_TOOL_NAME in visible_tool_names
            and bool(stage_gate.get('enabled'))
            and (
                not bool(stage_gate.get('has_active_stage'))
                or bool(stage_gate.get('transition_required'))
            )
        ):
            return {
                'type': 'function',
                'function': {'name': STAGE_TOOL_NAME},
            }
        return None

    @classmethod
    def _recover_stage_submission_tool_call_from_context(
        cls,
        *,
        node,
        stage_gate: dict[str, Any],
        model_messages: list[dict[str, Any]],
        invalid_stage_submission_count: int,
    ) -> ToolCallRequest | None:
        if invalid_stage_submission_count <= 0:
            return None
        if not bool(stage_gate.get('enabled')):
            return None
        has_active_stage = bool(stage_gate.get('has_active_stage'))
        transition_required = bool(stage_gate.get('transition_required'))
        if has_active_stage and not transition_required:
            return None
        arguments = cls._default_stage_submission_arguments(
            node_kind=str(getattr(node, 'node_kind', '') or ''),
            stage_gate=stage_gate,
            messages=model_messages,
        )
        return ToolCallRequest(
            id=f'call:auto-stage:{int(invalid_stage_submission_count)}',
            name=STAGE_TOOL_NAME,
            arguments=arguments,
        )

    @classmethod
    def _wrap_plain_text_final_result_tool_call(
        cls,
        *,
        response_content: Any,
        message_history: list[dict[str, Any]],
    ) -> ToolCallRequest | None:
        answer = str(response_content or '')
        if not answer.strip():
            return None
        evidence = cls._auto_wrapped_final_result_evidence(message_history=message_history, response_content=answer)
        return ToolCallRequest(
            id='call:auto-final-wrap:1',
            name=FINAL_RESULT_TOOL_NAME,
            arguments={
                'status': 'success',
                'delivery_status': 'final',
                'summary': 'auto-wrapped plain-text final result',
                'answer': answer,
                'evidence': evidence,
                'remaining_work': [],
                'blocking_reason': '',
            },
        )

    @classmethod
    def _auto_wrapped_final_result_evidence(
        cls,
        *,
        message_history: list[dict[str, Any]],
        response_content: str,
    ) -> list[dict[str, Any]]:
        evidence: list[dict[str, Any]] = []
        seen_signatures: set[str] = set()
        refs = cls._collect_content_refs([*list(message_history or []), str(response_content or '')])
        for ref in refs:
            ref_text = str(ref or '').strip()
            if not ref_text:
                continue
            kind = 'artifact'
            if ref_text.startswith('path:'):
                kind = 'file'
            elif ref_text.startswith('http://') or ref_text.startswith('https://'):
                kind = 'url'
            item = {
                'kind': kind,
                'ref': ref_text,
                'note': 'Auto-collected from prior tool/content history.',
            }
            signature = json.dumps(item, ensure_ascii=False, sort_keys=True)
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            evidence.append(item)
        if evidence:
            return evidence

        tool_names: list[str] = []
        for message in list(message_history or []):
            if str(message.get('role') or '').strip().lower() != 'tool':
                continue
            tool_name = str(message.get('name') or '').strip()
            if not tool_name or tool_name in {STAGE_TOOL_NAME, FINAL_RESULT_TOOL_NAME, *cls._CONTROL_TOOL_NAMES}:
                continue
            if tool_name not in tool_names:
                tool_names.append(tool_name)
        for tool_name in tool_names:
            evidence.append(
                {
                    'kind': 'artifact',
                    'note': f'Auto-collected tool result from {tool_name}.',
                }
            )
        return evidence

    @classmethod
    def _default_stage_submission_arguments(
        cls,
        *,
        node_kind: str,
        stage_gate: dict[str, Any],
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        context = cls._extract_node_context_payload(messages)
        prompt_text = str((context or {}).get('prompt') or '').strip()
        goal_text = str((context or {}).get('goal') or '').strip()
        focus_text = cls._truncate_stage_goal_text(prompt_text or goal_text)
        normalized_kind = str(node_kind or '').strip().lower()
        has_active_stage = bool(stage_gate.get('has_active_stage'))
        active_stage = stage_gate.get('active_stage') if isinstance(stage_gate.get('active_stage'), dict) else {}
        if not has_active_stage:
            if normalized_kind == 'acceptance':
                stage_goal = '先创建首个验收阶段并聚焦当前节点目标，完成第一轮关键证据定位与结论核验。'
            else:
                stage_goal = '先创建首个执行阶段并围绕当前节点目标完成首轮定向信息收集与关键入口定位。'
            if focus_text:
                stage_goal = f'{stage_goal} 当前焦点：{focus_text}'
            return {
                'stage_goal': stage_goal,
                'tool_round_budget': STAGE_TOOL_ROUND_BUDGET_MIN,
            }
        previous_goal = str((active_stage or {}).get('stage_goal') or '').strip()
        previous_budget = int((active_stage or {}).get('tool_round_budget') or 0)
        used_budget = int((active_stage or {}).get('tool_rounds_used') or 0)
        if normalized_kind == 'acceptance':
            next_goal = '在上一阶段基础上继续当前验收目标，优先补齐未确认的关键证据与结论。'
        else:
            next_goal = '在上一阶段基础上继续推进当前节点目标，优先补齐剩余关键证据并避免重复搜索。'
        if previous_goal:
            next_goal = f'{next_goal} 上一阶段目标：{cls._truncate_stage_goal_text(previous_goal)}'
        if focus_text:
            next_goal = f'{next_goal} 当前焦点：{focus_text}'
        return {
            'stage_goal': next_goal,
            'tool_round_budget': min(
                STAGE_TOOL_ROUND_BUDGET_MAX,
                max(
                    STAGE_TOOL_ROUND_BUDGET_MIN,
                    previous_budget if previous_budget > 0 else STAGE_TOOL_ROUND_BUDGET_MIN,
                ),
            ),
            'completed_stage_summary': (
                f'自动阶段切换：上一阶段预算 {used_budget}/{previous_budget or used_budget or 0} '
                '已耗尽，但模型未显式创建下一阶段。'
            ),
            'key_refs': [],
        }

    @staticmethod
    def _extract_node_context_payload(messages: list[dict[str, Any]]) -> dict[str, Any]:
        for message in reversed(list(messages or [])):
            if str(message.get('role') or '').strip().lower() != 'user':
                continue
            content = str(message.get('content') or '').strip()
            if not content.startswith('{'):
                continue
            try:
                parsed = json.loads(content)
            except Exception:
                continue
            if isinstance(parsed, dict):
                return parsed
        return {}

    @staticmethod
    def _truncate_stage_goal_text(value: Any, *, limit: int = 180) -> str:
        text = ' '.join(str(value or '').split())
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 3)].rstrip() + '...'

    @classmethod
    def _recover_final_result_tool_call_from_raw_json(
        cls,
        content: Any,
        *,
        attempt_auto_repair: bool,
    ) -> tuple[ToolCallRequest | None, bool]:
        for candidate in cls._extract_json_object_candidates(str(content or '')):
            try:
                payload = json.loads(candidate)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            if not cls._looks_like_final_result_payload(payload):
                continue
            if not attempt_auto_repair:
                return None, True
            return (
                ToolCallRequest(
                    id='call:raw-final-result:1',
                    name=FINAL_RESULT_TOOL_NAME,
                    arguments=dict(payload),
                ),
                True,
            )
        return None, False

    @staticmethod
    def _looks_like_final_result_payload(payload: dict[str, Any]) -> bool:
        if not isinstance(payload, dict):
            return False
        status = str(payload.get('status') or '').strip().lower()
        if status not in {'success', 'failed'}:
            return False
        return any(
            key in payload
            for key in ('delivery_status', 'summary', 'answer', 'evidence', 'remaining_work', 'blocking_reason')
        )

    @classmethod
    def _normalize_final_result_payload(
        cls,
        *,
        raw_payload: dict[str, Any],
        message_history: list[dict[str, Any]],
        response_content: Any,
    ) -> dict[str, Any]:
        payload = dict(raw_payload or {})
        status = str(payload.get('status') or '').strip().lower()

        payload.setdefault('answer', '')
        payload.setdefault('remaining_work', [])
        payload.setdefault('blocking_reason', '')
        raw_evidence = payload.get('evidence')
        if not isinstance(raw_evidence, list):
            raw_evidence = []
        payload['evidence'] = [
            cls._normalize_final_result_evidence_item(item)
            if isinstance(item, dict)
            else item
            for item in list(raw_evidence or [])
        ]

        if status == 'success':
            payload['remaining_work'] = []
            payload['blocking_reason'] = ''
            if not list(payload.get('evidence') or []):
                payload['evidence'] = cls._auto_wrapped_final_result_evidence(
                    message_history=message_history,
                    response_content=str(response_content or ''),
                )
        elif status == 'failed':
            remaining_work = payload.get('remaining_work')
            if not isinstance(remaining_work, list):
                payload['remaining_work'] = []
            delivery_status = str(payload.get('delivery_status') or '').strip().lower()
            if delivery_status == 'blocked' and not str(payload.get('blocking_reason') or '').strip():
                payload['blocking_reason'] = str(payload.get('summary') or payload.get('answer') or '').strip()

        return payload

    @staticmethod
    def _normalize_final_result_evidence_item(item: dict[str, Any]) -> dict[str, Any]:
        payload = dict(item or {})
        kind = str(payload.get('kind') or '').strip().lower()
        if kind in {'file', 'artifact', 'url'}:
            payload['kind'] = kind
            return payload

        ref = str(payload.get('ref') or '').strip()
        path = str(payload.get('path') or '').strip()
        inferred = ''
        if ref.startswith('artifact:'):
            inferred = 'artifact'
        elif ref.startswith('http://') or ref.startswith('https://') or path.startswith('http://') or path.startswith('https://'):
            inferred = 'url'
        elif ref.startswith('path:') or path:
            inferred = 'file'
        if inferred:
            payload['kind'] = inferred
        return payload

    @staticmethod
    def _parse_final_result(content: str) -> tuple[NodeFinalResult, dict[str, Any]] | None:
        _ = content
        return None

    @staticmethod
    def _collect_content_refs(value: Any) -> list[str]:
        found: set[str] = set()

        def _visit(item: Any) -> None:
            envelope = parse_content_envelope(item)
            if envelope is not None and str(envelope.ref or '').strip():
                found.add(str(envelope.ref or '').strip())
            if isinstance(item, dict):
                for nested in item.values():
                    _visit(nested)
                return
            if isinstance(item, list):
                for nested in item:
                    _visit(nested)
                return
            if isinstance(item, str):
                text = str(item or '')
                for prefix in (_STAGE_COMPACT_PREFIX, _STAGE_EXTERNALIZED_PREFIX):
                    if text.startswith(prefix):
                        payload_text = text[len(prefix) :].strip()
                        if payload_text.startswith('{') or payload_text.startswith('['):
                            try:
                                parsed = json.loads(payload_text)
                            except Exception:
                                parsed = None
                            if parsed is not None:
                                _visit(parsed)
                for match in _ARTIFACT_REF_PATTERN.finditer(text):
                    found.add(match.group(0))
                stripped = text.strip()
                if stripped.startswith('{') or stripped.startswith('['):
                    try:
                        parsed = json.loads(stripped)
                    except Exception:
                        parsed = None
                    if parsed is not None:
                        _visit(parsed)

        _visit(value)
        return sorted(found)

    @staticmethod
    def _accepts_runtime_context(tool: Tool) -> bool:
        return ReActToolLoop._runtime_context_parameter_name(tool) is not None

    @staticmethod
    def _runtime_context_parameter_name(tool: Tool) -> str | None:
        sig = inspect.signature(tool.execute)
        if '__g3ku_runtime' in sig.parameters:
            return '__g3ku_runtime'
        for name in sig.parameters:
            if str(name).endswith('__g3ku_runtime'):
                return str(name)
        if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in sig.parameters.values()):
            return '__g3ku_runtime'
        return None

    @staticmethod
    def _render_tool_result(result: Any) -> str:
        try:
            return json.dumps(result, ensure_ascii=False)
        except TypeError:
            return str(result)

    def _set_model_await_marker(self, *, task_id: str, node_id: str, marker: str, started_at: str = '') -> None:
        normalized_marker = str(marker or '').strip()
        normalized_started_at = str(started_at or '').strip()
        self._log_service.update_frame(
            task_id,
            node_id,
            lambda frame: {
                **frame,
                'await_marker': normalized_marker,
                'await_started_at': normalized_started_at if normalized_marker else '',
            },
            publish_snapshot=True,
        )

    async def _await_with_model_marker(self, *, task_id: str, node_id: str, marker: str, awaitable: Any) -> Any:
        started_at = now_iso()
        self._set_model_await_marker(
            task_id=task_id,
            node_id=node_id,
            marker=marker,
            started_at=started_at,
        )
        try:
            return await awaitable
        finally:
            self._set_model_await_marker(task_id=task_id, node_id=node_id, marker='')

    async def _chat_with_optional_extensions(self, **kwargs) -> Any:
        chat = getattr(self._chat_backend, 'chat')
        signature = inspect.signature(chat)
        accepts_kwargs = any(param.kind is inspect.Parameter.VAR_KEYWORD for param in signature.parameters.values())
        if accepts_kwargs:
            return await chat(**kwargs)
        filtered = {key: value for key, value in kwargs.items() if key in signature.parameters}
        return await chat(**filtered)

    @staticmethod
    def _truncate_compact_text(value: Any) -> str:
        text = ' '.join(str(value or '').split())
        if not text:
            return ''
        if len(text) <= _COMPACT_HISTORY_STEP_MAX_CHARS:
            return text
        return text[: _COMPACT_HISTORY_STEP_MAX_CHARS - 3].rstrip() + '...'

    def _open_thread_from_message(self, message: dict[str, Any]) -> str:
        role = str((message or {}).get('role') or '').strip().lower()
        if role == 'tool':
            summary, _ref = content_summary_and_ref((message or {}).get('content'))
            lowered = summary.lower()
            if (
                is_error_like_tool_result(summary)
                or '"status":"error"' in lowered
                or '"status": "error"' in lowered
            ):
                tool_name = str((message or {}).get('name') or 'tool').strip() or 'tool'
                return self._truncate_compact_text(f'Investigate failed tool result: {tool_name}')
        if role == 'user' and self._is_result_contract_prompt(message):
            return f'Submit a valid `{FINAL_RESULT_TOOL_NAME}` payload once implementation is complete'
        return ''

    @classmethod
    def _is_result_contract_prompt(cls, message: dict[str, Any]) -> bool:
        if str((message or {}).get('role') or '').strip().lower() != 'user':
            return False
        content = str((message or {}).get('content') or '').strip()
        lowered = content.lower()
        return (
            'result contract v' in lowered
            or FINAL_RESULT_TOOL_NAME in lowered
            or content.startswith('你上一条回复不符合结果 JSON 协议 v')
            or content.startswith('你上一条回复虽然能解析成 JSON，但违反了结果协议 v')
        )

    def _dedupe_tool_messages(self, tool_messages: list[dict[str, Any]], *, existing_messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen_signatures: dict[str, dict[str, str]] = {}
        for message in list(existing_messages or []):
            if str(message.get('role') or '').strip().lower() != 'tool':
                continue
            signature = self._tool_result_signature(message.get('content'))
            if not signature:
                continue
            seen_signatures.setdefault(
                signature,
                {
                    'tool_call_id': str(message.get('tool_call_id') or '').strip(),
                    'ref': self._best_tool_ref(message.get('content')),
                    'summary': self._tool_summary(message.get('content')),
                },
            )

        deduped: list[dict[str, Any]] = []
        for message in list(tool_messages or []):
            payload = dict(message or {})
            signature = self._tool_result_signature(payload.get('content'))
            if not signature:
                deduped.append(payload)
                continue
            prior = seen_signatures.get(signature)
            if prior is None:
                seen_signatures[signature] = {
                    'tool_call_id': str(payload.get('tool_call_id') or '').strip(),
                    'ref': self._best_tool_ref(payload.get('content')),
                    'summary': self._tool_summary(payload.get('content')),
                }
                deduped.append(payload)
                continue
            payload['content'] = json.dumps(
                {
                    'status': 'reused',
                    'same_as': prior.get('tool_call_id') or '',
                    'ref': prior.get('ref') or '',
                    'summary': prior.get('summary') or '',
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            deduped.append(payload)
        return deduped

    @staticmethod
    def _tool_result_signature(content: Any) -> str:
        if content is None:
            return ''
        if isinstance(content, str):
            text = content.strip()
            if not text:
                return ''
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = text
        else:
            parsed = content
        try:
            normalized = json.dumps(parsed, ensure_ascii=True, sort_keys=True, default=str)
        except Exception:
            normalized = str(parsed)
        return hashlib.sha256(normalized.encode('utf-8')).hexdigest()

    @staticmethod
    def _best_tool_ref(content: Any) -> str:
        refs = ReActToolLoop._collect_content_refs(content)
        return refs[0] if refs else ''

    def _tool_summary(self, content: Any) -> str:
        if isinstance(content, str):
            text = content.strip()
            if text.startswith('{') or text.startswith('['):
                try:
                    parsed = json.loads(text)
                except Exception:
                    parsed = None
                if isinstance(parsed, dict):
                    summary_value = parsed.get('summary')
                    if str(summary_value or '').strip():
                        return self._truncate_compact_text(summary_value)
        summary, _ref = content_summary_and_ref(content)
        return self._truncate_compact_text(summary)

    @staticmethod
    def _orphan_tool_result_failure(*, call_ids: list[str], strike_count: int) -> NodeFinalResult:
        recent_ids = [str(item or '').strip() for item in list(call_ids or []) if str(item or '').strip()]
        recent_text = ', '.join(recent_ids[:8]) if recent_ids else '<missing>'
        summary = 'orphan tool result circuit breaker triggered'
        blocking_reason = (
            f'Detected orphan tool results {int(strike_count or 0)} times during the same node run. '
            f'Recent orphan call IDs: {recent_text}. '
            'Possible history compaction split or tool replay corruption.'
        )
        return NodeFinalResult(
            status='failed',
            delivery_status='blocked',
            summary=summary,
            answer='',
            evidence=[],
            remaining_work=[],
            blocking_reason=blocking_reason,
        )

    def _execution_stage_state_for_runtime(self, *, runtime_context: dict[str, Any]):
        store = getattr(self._log_service, '_store', None)
        getter = getattr(store, 'get_node', None) if store is not None else None
        if not callable(getter):
            return normalize_execution_stage_metadata({})
        node = getter(str(runtime_context.get('node_id') or '').strip())
        if node is None:
            return normalize_execution_stage_metadata({})
        payload = (node.metadata or {}).get('execution_stages') if isinstance(node.metadata, dict) else {}
        return normalize_execution_stage_metadata(payload)

    @staticmethod
    def _is_stage_context_message(message: dict[str, Any]) -> bool:
        return _shared_is_stage_context_message(message)

    def _stage_prompt_prefix(self, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return _shared_stage_prompt_prefix(messages)

    @staticmethod
    def _retained_completed_stage_ids(stage_state: Any, *, keep_latest: int) -> set[str]:
        return _shared_retained_completed_stage_ids(stage_state, keep_latest=keep_latest)

    @staticmethod
    def _completed_stage_blocks(stage_state: Any, *, skip_stage_ids: set[str] | None = None) -> list[dict[str, Any]]:
        return _shared_completed_stage_blocks(stage_state, skip_stage_ids=skip_stage_ids)

    @staticmethod
    def _current_stage_active_window(messages: list[dict[str, Any]], *, keep_completed_stages: int = 0) -> list[dict[str, Any]]:
        return _shared_current_stage_active_window(
            messages,
            keep_completed_stages=keep_completed_stages,
            stage_tool_name=STAGE_TOOL_NAME,
        )

    def _prepare_messages(self, messages: list[dict[str, Any]], *, runtime_context: dict[str, Any]) -> list[dict[str, Any]]:
        normalized_messages = strip_node_dynamic_contract_messages(messages)
        stage_state = self._execution_stage_state_for_runtime(runtime_context=runtime_context)
        parts = _shared_decompose_stage_prompt_messages(
            normalized_messages,
            stage_state=stage_state,
            keep_latest_completed_stages=_UNCOMPACTED_COMPLETED_STAGE_WINDOWS,
            stage_tool_name=STAGE_TOOL_NAME,
        )
        visible_user_messages = [
            str(item.get('content') or '').strip()
            for item in list(parts.get('active_window') or [])
            if isinstance(item, dict) and str(item.get('role') or '').strip().lower() == 'user'
        ]
        notice_tail_messages = self._append_notice_tail_messages(
            runtime_context=runtime_context,
            visible_user_messages=visible_user_messages,
        )
        return [
            *list(parts.get('prefix') or []),
            *notice_tail_messages,
            *list(parts.get('completed_blocks') or []),
            *list(parts.get('active_window') or []),
        ]

    def _append_notice_tail_messages(
        self,
        *,
        runtime_context: dict[str, Any],
        visible_user_messages: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        store = getattr(self._log_service, '_store', None)
        getter = getattr(store, 'get_node', None) if store is not None else None
        if not callable(getter):
            return []
        node = getter(str(runtime_context.get('node_id') or '').strip())
        if node is None or not isinstance(getattr(node, 'metadata', None), dict):
            return []
        return build_append_notice_tail_messages(
            node.metadata.get(APPEND_NOTICE_CONTEXT_KEY),
            visible_user_messages=list(visible_user_messages or []),
        )

    @staticmethod
    def _prompt_message_records(messages: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        return [dict(item) for item in list(messages or []) if isinstance(item, dict)]

    @classmethod
    def _fresh_turn_seed_normalized_value(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): cls._fresh_turn_seed_normalized_value(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._fresh_turn_seed_normalized_value(item) for item in value]
        if isinstance(value, str):
            return value.replace('\r\n', '\n').rstrip()
        return value

    @classmethod
    def _fresh_turn_seed_records_match(
        cls,
        first: list[dict[str, Any]] | None,
        second: list[dict[str, Any]] | None,
    ) -> bool:
        first_records = cls._prompt_message_records(first)
        second_records = cls._prompt_message_records(second)
        if len(first_records) != len(second_records):
            return False
        return all(
            cls._fresh_turn_seed_normalized_value(left)
            == cls._fresh_turn_seed_normalized_value(right)
            for left, right in zip(first_records, second_records)
        )

    @classmethod
    def _fresh_turn_live_request_messages_from_seed_request(
        cls,
        *,
        seed_request_messages: list[dict[str, Any]] | None,
        stable_messages: list[dict[str, Any]] | None,
        live_request_messages: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        seed_records = cls._prompt_message_records(seed_request_messages)
        stable_records = cls._prompt_message_records(stable_messages)
        live_records = cls._prompt_message_records(live_request_messages)
        stable_len = len(stable_records)
        seed_len = len(seed_records)
        if not seed_records or stable_len <= 0 or len(live_records) < stable_len:
            return live_records
        if not cls._fresh_turn_seed_records_match(live_records[:stable_len], stable_records):
            return live_records
        matched_prefix_len = min(stable_len, seed_len)
        if matched_prefix_len <= 0:
            return live_records
        if not cls._fresh_turn_seed_records_match(
            seed_records[:matched_prefix_len],
            stable_records[:matched_prefix_len],
        ):
            return live_records
        if stable_len < seed_len:
            live_tail = list(live_records[stable_len:])
            return [*seed_records, *live_tail]
        stable_tail = list(stable_records[seed_len:])
        live_tail = list(live_records[stable_len:])
        return [*seed_records, *stable_tail, *live_tail]

    @classmethod
    def _same_turn_append_only_request_messages(
        cls,
        *,
        previous_request_messages: list[dict[str, Any]] | None,
        current_model_messages: list[dict[str, Any]] | None,
        pending_delta_messages: list[dict[str, Any]] | None,
        request_tail_messages: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        current_records = cls._prompt_message_records(current_model_messages)
        tail_records = cls._prompt_message_records(request_tail_messages)
        live_records = [*current_records, *tail_records]
        previous_records = cls._prompt_message_records(previous_request_messages)
        delta_records = cls._prompt_message_records(pending_delta_messages)
        if not previous_records or not delta_records:
            return live_records
        prefix_probe = current_records[: min(2, len(current_records))]
        if prefix_probe and previous_records[: len(prefix_probe)] != prefix_probe:
            return live_records
        return [*previous_records, *delta_records, *tail_records]

    @staticmethod
    def _apply_temporary_system_overlay(messages: list[dict[str, Any]], *, overlay_text: str | None) -> list[dict[str, Any]]:
        base_messages = list(messages or [])
        text = str(overlay_text or '').strip()
        if not text:
            return base_messages
        overlay_block = f'System note for this turn only:\n{text}'
        return [*base_messages, {'role': 'user', 'content': overlay_block}]

    def _externalize_message_content(
        self,
        value: Any,
        *,
        runtime_context: dict[str, Any],
        display_name: str,
        source_kind: str,
        delivery_metadata: dict[str, Any] | None = None,
    ) -> Any:
        store = getattr(self._log_service, '_content_store', None)
        if store is None:
            return value
        return store.externalize_for_message(
            value,
            runtime=runtime_context,
            display_name=display_name,
            source_kind=source_kind,
            compact=True,
            delivery_metadata=delivery_metadata,
        )
