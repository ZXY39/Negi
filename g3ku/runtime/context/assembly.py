from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from g3ku.runtime.context.semantic_scope import plan_retrieval_scope, semantic_catalog_rankings
from g3ku.runtime.context.summarizer import estimate_tokens, score_query, truncate_by_tokens
from g3ku.runtime.context.types import (
    ArchiveContextRecord,
    ContextAssemblyResult,
    ContextBlock,
    ContextSnapshot,
    RetrievedContextBundle,
    StageContextRecord,
    TaskContinuityRecord,
)
from g3ku.runtime.core_tools import resolve_core_tool_targets
from g3ku.runtime.web_ceo_sessions import (
    DEFAULT_LIVE_RAW_TAIL_TURNS,
    build_completed_stage_abstracts,
    build_task_continuity_payload,
    extract_active_stage_raw_tail,
    extract_live_raw_tail,
    latest_interaction_trace,
    render_task_continuity_markdown,
)


class ContextAssemblyService:
    """Budget-aware CEO context assembly with visibility-preserving selection."""

    MEMORY_WRITE_STRONG_TERMS: tuple[str, ...] = (
        '记住',
        '请记住',
        '不要再',
        '别再',
        '默认',
        '长期按这个来',
        'please remember',
        'from now on',
        'going forward',
        'default to',
        "don't use",
        'never use',
    )
    MEMORY_WRITE_FUTURE_RE = re.compile(r'(以后|今后).{0,24}(都|默认|不要|别再|按这个|用)')
    MEMORY_WRITE_REMEMBER_RE = re.compile(r'\bremember\b')
    MEMORY_WRITE_REMEMBER_EXCLUSION_RE = re.compile(r'\b(?:what|do)\s+you\s+remember\b')
    EXTENSION_TOOL_HINTS: dict[str, tuple[str, ...]] = {
        'cron': ('remind', 'schedule', 'cron', 'timer', 'recurring'),
        'load_skill_context': ('skill', 'workflow', 'procedure', 'steps', 'context', 'details'),
        'load_tool_context': ('tool', 'api', 'parameters', 'usage', 'context', 'details'),
        'filesystem': ('file', 'path', 'read', 'write', 'edit', 'list', 'open'),
        'exec': ('shell', 'command', 'bash', 'powershell', 'terminal', 'run'),
        'model_config': ('model', 'provider', 'config', 'token', 'temperature'),
    }
    SKILL_RETRIEVAL_HINTS: tuple[str, ...] = ('skill', 'workflow', 'steps', 'skill.md', 'load_skill_context')
    RESOURCE_RETRIEVAL_HINTS: tuple[str, ...] = ('tool', 'api', 'usage', 'install', 'update', 'load_tool_context')
    TARGETED_RETRIEVAL_SCORE_THRESHOLD: float = 2.0
    RESERVED_INTERNAL_TOOLS: tuple[str, ...] = ("wait_tool_execution", "stop_tool_execution")
    def __init__(self, *, loop, prompt_builder) -> None:
        self._loop = loop
        self._prompt_builder = prompt_builder

    @classmethod
    def _detect_memory_write_intent(cls, query_text: str) -> list[str]:
        text = str(query_text or '').strip()
        if not text:
            return []
        lower = text.lower()
        matched: list[str] = []
        for term in cls.MEMORY_WRITE_STRONG_TERMS:
            if term in {'please remember', 'remember'}:
                continue
            haystack = lower if term.isascii() else text
            if term in haystack:
                matched.append(term)
        if 'please remember' in lower:
            matched.append('please remember')
        if (
            cls.MEMORY_WRITE_REMEMBER_RE.search(lower)
            and not cls.MEMORY_WRITE_REMEMBER_EXCLUSION_RE.search(lower)
            and 'please remember' not in lower
        ):
            matched.append('remember')
        if cls.MEMORY_WRITE_FUTURE_RE.search(text):
            if '以后' in text:
                matched.append('以后')
            if '今后' in text:
                matched.append('今后')
        deduped: list[str] = []
        for item in matched:
            if item not in deduped:
                deduped.append(item)
        return deduped

    @staticmethod
    def _memory_write_hint_block(matched_terms: list[str]) -> str:
        terms = ', '.join(matched_terms)
        return '\n'.join(
            [
                '## Long-Term Memory Write Hint',
                f'- The current user turn likely requests durable memory updates ({terms}).',
                '- If this is a stable identity, preference, constraint, default, avoidance rule, workflow rule, or durable project fact, call `memory_write` before replying.',
                '- Do not save temporary task state, guesses, or unconfirmed inferences as permanent memory.',
            ]
        )

    @staticmethod
    def _retrieved_memory_resolution_hint_block() -> str:
        return '\n'.join(
            [
                '## Retrieved Memory Resolution Hint',
                '- The retrieved memory below already contains previously confirmed user defaults or preferences relevant to this turn.',
                '- If the user is asking what the default is, restate the retrieved default directly.',
                '- Do not invent a new default, propose alternatives, or replace the retrieved default with general best practices unless the user explicitly asks to change the rule.',
            ]
        )

    async def build_for_ceo(
        self,
        *,
        session: Any,
        query_text: str,
        exposure: dict[str, Any],
        persisted_session: Any | None,
        user_content: Any | None = None,
    ) -> ContextAssemblyResult:
        main_service = getattr(self._loop, 'main_task_service', None)
        memory_manager = getattr(self._loop, 'memory_manager', None)
        assembly_cfg = getattr(getattr(self._loop, '_memory_runtime_settings', None), 'assembly', None)
        max_prompt_tokens = max(512, int(getattr(assembly_cfg, 'max_prompt_tokens', 3200) or 3200))
        live_raw_tail_turns = max(1, int(getattr(assembly_cfg, 'live_raw_tail_turns', DEFAULT_LIVE_RAW_TAIL_TURNS) or DEFAULT_LIVE_RAW_TAIL_TURNS))
        task_budget = max(96, int(getattr(assembly_cfg, 'task_continuity_max_tokens', 320) or 320))
        stage_budget = max(96, int(getattr(assembly_cfg, 'stage_context_max_tokens', 640) or 640))
        latest_archive_budget = max(96, int(getattr(assembly_cfg, 'latest_archive_overview_max_tokens', 420) or 420))
        older_archive_top_k = max(0, int(getattr(assembly_cfg, 'older_archive_abstracts_top_k', 4) or 4))
        older_archive_budget = max(64, int(getattr(assembly_cfg, 'older_archive_abstracts_max_tokens', 320) or 320))
        retrieval_budget = max(120, int(getattr(assembly_cfg, 'retrieved_context_max_tokens', 1200) or 1200))
        inventory_top_k = max(1, int(getattr(assembly_cfg, 'skill_inventory_top_k', 8) or 8))
        inventory_budget = max(64, int(getattr(assembly_cfg, 'skill_inventory_max_tokens', 480) or 480))
        extension_top_k = max(0, int(getattr(assembly_cfg, 'extension_tool_top_k', 6) or 6))
        core_tools = {
            str(name).strip()
            for name in list(getattr(assembly_cfg, 'core_tools', []) or [])
            if str(name).strip()
        }

        if main_service is not None and memory_manager is not None:
            try:
                await memory_manager.sync_catalog(main_service)
            except Exception:
                pass

        memory_write_terms = self._detect_memory_write_intent(query_text)

        visible_skills = list(exposure.get('skills') or [])
        visible_families = list(exposure.get('tool_families') or [])
        semantic_frontdoor = await semantic_catalog_rankings(
            memory_manager=memory_manager,
            query_text=query_text,
            visible_skills=visible_skills,
            visible_families=visible_families,
            skill_limit=max(inventory_top_k * 4, len(visible_skills), inventory_top_k, 8),
            tool_limit=max(extension_top_k * 4, len(visible_families), extension_top_k, 8),
        )
        selected_skills, skill_trace = self._select_skills(
            visible_skills=visible_skills,
            top_k=inventory_top_k,
            token_budget=inventory_budget,
            ranked_skill_ids=semantic_frontdoor['skill_ids'],
        )
        retrieval_scope = plan_retrieval_scope(
            visible_skills=visible_skills,
            visible_families=visible_families,
            semantic_frontdoor=semantic_frontdoor,
        )
        if memory_write_terms:
            retrieval_scope = {
                **retrieval_scope,
                'search_context_types': ['memory'],
                'allowed_context_types': ['memory'],
                'allowed_resource_record_ids': [],
                'allowed_skill_record_ids': [],
            }
        prompt_skills = list(selected_skills)
        system_prompt = self._prompt_builder.build(skills=prompt_skills)

        external_tools_block, external_trace = self._build_external_tool_block(
            visible_families=visible_families,
            visible_tool_names=list(exposure.get('tool_names') or []),
            top_k=8,
        )
        if external_tools_block:
            system_prompt = f"{system_prompt}\n\n{external_tools_block}"

        memory_write_visible = 'memory_write' in {str(name or '').strip() for name in list(exposure.get('tool_names') or [])}
        if memory_write_terms and memory_write_visible:
            system_prompt = f"{system_prompt}\n\n{self._memory_write_hint_block(memory_write_terms)}"

        selected_tool_names, tool_trace = self._select_tools(
            query_text=query_text,
            visible_names=list(exposure.get('tool_names') or []),
            visible_families=visible_families,
            core_tools=core_tools,
            extension_top_k=extension_top_k,
            ranked_tool_ids=semantic_frontdoor['tool_ids'],
        )
        active_task_count = 0
        active_tasks: list[dict[str, Any]] = []
        list_active_task_snapshots = getattr(main_service, 'list_active_task_snapshots_for_session', None)
        if callable(list_active_task_snapshots):
            active_tasks = list_active_task_snapshots(getattr(session.state, 'session_key', ''), limit=3)
            active_task_count = len(list(active_tasks or []))
        task_payload = build_task_continuity_payload(
            session=persisted_session,
            active_tasks=active_tasks,
            limit=3,
        )
        task_markdown = truncate_by_tokens(
            render_task_continuity_markdown(task_payload),
            task_budget,
        )

        stage_trace, stage_source = latest_interaction_trace(session, persisted_session)
        stage_record = StageContextRecord(
            active_stage=self._active_stage_payload(stage_trace),
            completed_abstracts=build_completed_stage_abstracts(stage_trace),
            source=stage_source,
        )
        stage_markdown = self._render_stage_context(stage_record, token_budget=stage_budget)

        latest_archive_record, older_archive_records = self._collect_archive_records(
            query_text=query_text,
            session=persisted_session,
            older_top_k=older_archive_top_k,
        )
        latest_archive_markdown = self._render_latest_archive_block(
            latest_archive_record,
            token_budget=latest_archive_budget,
        )
        older_archive_markdown = self._render_older_archives_block(
            older_archive_records,
            token_budget=older_archive_budget,
        )

        retrieved_bundle = RetrievedContextBundle(query=query_text)
        retrieved_markdown = ''
        if memory_manager is not None and query_text:
            try:
                if hasattr(memory_manager, 'retrieve_context_bundle'):
                    retrieved_bundle = await memory_manager.retrieve_context_bundle(
                        query=query_text,
                        session_key=session.state.session_key,
                        channel=getattr(session, '_memory_channel', getattr(session, '_channel', 'cli')),
                        chat_id=getattr(session, '_memory_chat_id', getattr(session, '_chat_id', session.state.session_key)),
                        search_context_types=retrieval_scope['search_context_types'],
                        allowed_context_types=retrieval_scope['allowed_context_types'],
                        allowed_resource_record_ids=retrieval_scope['allowed_resource_record_ids'],
                        allowed_skill_record_ids=retrieval_scope['allowed_skill_record_ids'],
                    )
                elif hasattr(memory_manager, 'retrieve_block'):
                    fallback_block = await memory_manager.retrieve_block(
                        query=query_text,
                        session_key=session.state.session_key,
                        channel=getattr(session, '_memory_channel', getattr(session, '_channel', 'cli')),
                        chat_id=getattr(session, '_memory_chat_id', getattr(session, '_chat_id', session.state.session_key)),
                        search_context_types=retrieval_scope['search_context_types'],
                        allowed_context_types=retrieval_scope['allowed_context_types'],
                        allowed_resource_record_ids=retrieval_scope['allowed_resource_record_ids'],
                        allowed_skill_record_ids=retrieval_scope['allowed_skill_record_ids'],
                    )
                    retrieved_markdown = str(fallback_block or '').strip()
            except Exception:
                retrieved_bundle = RetrievedContextBundle(query=query_text)
        if memory_write_terms and (retrieved_bundle.records or retrieved_markdown):
            retrieved_markdown = (
                f"{self._retrieved_memory_resolution_hint_block()}\n\n{retrieved_markdown}".strip()
                if retrieved_markdown
                else self._retrieved_memory_resolution_hint_block()
            )
        rendered_retrieved = self._render_retrieved_context_bundle(
            retrieved_bundle,
            token_budget=retrieval_budget,
            include_l2=True,
        )
        if rendered_retrieved:
            retrieved_markdown = (
                f"{retrieved_markdown}\n\n{rendered_retrieved}".strip()
                if retrieved_markdown
                else rendered_retrieved
            )

        has_active_stage = bool(stage_record.active_stage)
        if has_active_stage:
            live_raw_messages = extract_active_stage_raw_tail(
                session,
                persisted_session,
                turn_limit=live_raw_tail_turns,
            )
        else:
            live_raw_messages = extract_live_raw_tail(
                persisted_session,
                turn_limit=live_raw_tail_turns,
            ) if persisted_session is not None else []

        user_message = {"role": "user", "content": user_content if user_content is not None else query_text}
        system_tokens = estimate_tokens(system_prompt)
        user_tokens = estimate_tokens(query_text or str(user_content or ""))
        task_tokens = estimate_tokens(task_markdown)
        min_live_raw_messages = self._minimum_live_raw_messages(live_raw_messages)
        min_live_raw_tokens = self._messages_tokens(min_live_raw_messages)
        remaining_budget = max(0, max_prompt_tokens - system_tokens - user_tokens - task_tokens - min_live_raw_tokens)

        blocks: list[ContextBlock] = []
        block_messages: list[dict[str, Any]] = []

        if task_markdown:
            block_messages.append({"role": "assistant", "content": task_markdown})
            blocks.append(ContextBlock(kind="task_continuity", content=task_markdown, source="session_metadata", tokens=task_tokens))

        stage_markdown, stage_trim_reason = self._fit_optional_block(stage_markdown, remaining_budget)
        if stage_markdown:
            stage_tokens = estimate_tokens(stage_markdown)
            remaining_budget = max(0, remaining_budget - stage_tokens)
            block_messages.append({"role": "assistant", "content": stage_markdown})
            blocks.append(
                ContextBlock(
                    kind="stage_context",
                    content=stage_markdown,
                    source=stage_source,
                    tokens=stage_tokens,
                    trimmed=bool(stage_trim_reason),
                    trim_reason=stage_trim_reason,
                )
            )

        latest_archive_markdown, latest_trim_reason = self._fit_optional_block(latest_archive_markdown, remaining_budget)
        if latest_archive_markdown:
            latest_tokens = estimate_tokens(latest_archive_markdown)
            remaining_budget = max(0, remaining_budget - latest_tokens)
            block_messages.append({"role": "assistant", "content": latest_archive_markdown})
            blocks.append(
                ContextBlock(
                    kind="latest_archive_overview",
                    content=latest_archive_markdown,
                    source=str(getattr(latest_archive_record, 'overview_uri', '') or ''),
                    level="l1",
                    tokens=latest_tokens,
                    trimmed=bool(latest_trim_reason),
                    trim_reason=latest_trim_reason,
                    degraded_from="l1" if latest_trim_reason == "degraded_to_abstract" else "",
                )
            )

        older_archive_markdown, older_trim_reason = self._fit_optional_block(older_archive_markdown, remaining_budget)
        if older_archive_markdown:
            older_tokens = estimate_tokens(older_archive_markdown)
            remaining_budget = max(0, remaining_budget - older_tokens)
            block_messages.append({"role": "assistant", "content": older_archive_markdown})
            blocks.append(
                ContextBlock(
                    kind="older_archive_abstracts",
                    content=older_archive_markdown,
                    source="archives",
                    level="l0",
                    tokens=older_tokens,
                    trimmed=bool(older_trim_reason),
                    trim_reason=older_trim_reason,
                )
            )

        retrieved_markdown, retrieved_trim_reason = self._fit_optional_block(retrieved_markdown, remaining_budget)
        if retrieved_markdown:
            retrieved_tokens = estimate_tokens(retrieved_markdown)
            remaining_budget = max(0, remaining_budget - retrieved_tokens)
            block_messages.append({"role": "assistant", "content": retrieved_markdown})
            blocks.append(
                ContextBlock(
                    kind="retrieved_context",
                    content=retrieved_markdown,
                    source="memory_manager",
                    level="l1",
                    tokens=retrieved_tokens,
                    trimmed=bool(retrieved_trim_reason),
                    trim_reason=retrieved_trim_reason,
                    degraded_from="l2" if 'L2' not in retrieved_markdown else "",
                )
            )

        live_raw_budget = max(0, remaining_budget + min_live_raw_tokens)
        fitted_live_raw = self._fit_live_raw_messages(
            live_raw_messages,
            budget_tokens=live_raw_budget,
        )
        live_raw_tokens = self._messages_tokens(fitted_live_raw)
        if fitted_live_raw:
            block_messages.extend(fitted_live_raw)
            blocks.append(
                ContextBlock(
                    kind="live_raw_tail",
                    content=self._messages_to_text(fitted_live_raw),
                    source="transcript",
                    tokens=live_raw_tokens,
                    trimmed=len(fitted_live_raw) < len(live_raw_messages),
                    trim_reason="live_raw_tail_budget" if len(fitted_live_raw) < len(live_raw_messages) else "",
                )
            )

        trace = {
            'query': query_text,
            'selected_skills': skill_trace,
            'selected_tools': tool_trace,
            'semantic_frontdoor': semantic_frontdoor['trace'],
            'external_tools': external_trace,
            'archive_context': {
                'latest_archive_id': latest_archive_record.archive_id if latest_archive_record is not None else '',
                'older_archive_ids': [item.archive_id for item in older_archive_records],
            },
            'retrieval_scope': {
                'mode': str(retrieval_scope.get('mode') or ''),
                'search_context_types': list(retrieval_scope['search_context_types']),
                'allowed_context_types': list(retrieval_scope['allowed_context_types']),
                'allowed_resource_record_ids': list(retrieval_scope['allowed_resource_record_ids']),
                'allowed_skill_record_ids': list(retrieval_scope['allowed_skill_record_ids']),
            },
            'active_tasks': {
                'count': active_task_count,
                'included': bool(task_payload and task_payload.get('active_tasks')),
            },
            'stage_context': {
                'source': stage_source,
                'has_active_stage': has_active_stage,
                'completed_count': len(stage_record.completed_abstracts),
            },
            'memory_write_hint': {
                'triggered': bool(memory_write_terms and memory_write_visible),
                'matched_terms': list(memory_write_terms),
                'visible': memory_write_visible,
            },
            'model_messages_count': 1 + len(block_messages) + 1,
            'context_blocks': [
                {
                    'kind': block.kind,
                    'source': block.source,
                    'level': block.level,
                    'tokens': block.tokens,
                    'trimmed': block.trimmed,
                    'trim_reason': block.trim_reason,
                    'degraded_from': block.degraded_from,
                }
                for block in blocks
            ],
            'tokens': {
                'system_prompt': estimate_tokens(system_prompt),
                'skill_inventory': sum(int(item.get('tokens', 0)) for item in skill_trace),
                'external_tools': sum(int(item.get('tokens', 0)) for item in external_trace),
                'task_continuity': task_tokens,
                'stage_context': estimate_tokens(stage_markdown),
                'latest_archive_overview': estimate_tokens(latest_archive_markdown),
                'older_archive_abstracts': estimate_tokens(older_archive_markdown),
                'retrieved_context': estimate_tokens(retrieved_markdown),
                'live_raw_tail': live_raw_tokens,
                'user_message': user_tokens,
                'total': system_tokens + user_tokens + sum(block.tokens for block in blocks),
            },
        }
        if memory_manager is not None and hasattr(memory_manager, 'write_context_assembly_trace'):
            try:
                await memory_manager.write_context_assembly_trace(
                    session_key=session.state.session_key,
                    channel=getattr(session, '_memory_channel', getattr(session, '_channel', 'cli')),
                    chat_id=getattr(session, '_memory_chat_id', getattr(session, '_chat_id', session.state.session_key)),
                    payload=trace,
                )
            except Exception:
                pass
        model_messages = [{"role": "system", "content": system_prompt}, *block_messages, user_message]
        return ContextAssemblyResult(
            model_messages=model_messages,
            tool_names=selected_tool_names,
            trace=trace,
            context_snapshot=ContextSnapshot(
                blocks=blocks,
                total_tokens=trace['tokens']['total'],
                system_tokens=system_tokens,
                user_tokens=user_tokens,
            ),
        )

    @staticmethod
    def _active_stage_payload(trace: dict[str, Any]) -> dict[str, Any] | None:
        for stage in reversed(list(trace.get('stages') or [])):
            if str(stage.get('status') or '').strip() == 'active':
                return dict(stage)
        return None

    def _render_stage_context(self, record: StageContextRecord, *, token_budget: int) -> str:
        if not record.active_stage and not record.completed_abstracts:
            return ''
        lines = ['## Stage Context']
        active = dict(record.active_stage or {})
        if active:
            lines.extend(
                [
                    '### Current Active Stage',
                    f"- Goal: {truncate_by_tokens(str(active.get('stage_goal') or '').strip(), 40)}",
                    f"- Tool round budget: {int(active.get('tool_round_budget') or 0)}",
                    f"- Tool rounds used: {int(active.get('tool_rounds_used') or 0)}",
                ]
            )
            latest_round = None
            rounds = [item for item in list(active.get('rounds') or []) if isinstance(item, dict)]
            if rounds:
                latest_round = rounds[-1]
            if isinstance(latest_round, dict):
                tools = [item for item in list(latest_round.get('tools') or []) if isinstance(item, dict)]
                if tools:
                    lines.append('- Recent round highlights:')
                for tool in tools[-2:]:
                    output_text = truncate_by_tokens(
                        str(tool.get('output_text') or tool.get('output_ref') or '').strip(),
                        36,
                    )
                    status = str(tool.get('status') or 'info').strip() or 'info'
                    tool_name = str(tool.get('tool_name') or 'tool').strip() or 'tool'
                    if output_text:
                        lines.append(f"  - {tool_name} ({status}): {output_text}")
            transition_required = bool(
                int(active.get('tool_round_budget') or 0) > 0
                and int(active.get('tool_rounds_used') or 0) >= int(active.get('tool_round_budget') or 0)
            )
            if transition_required:
                lines.append('- Current blocker: stage budget exhausted; create the next stage before using more budgeted tools.')
            else:
                lines.append('- Next step: continue within the current stage goal and only use tools that directly serve this stage.')
        if record.completed_abstracts:
            lines.append('### Completed Earlier Stages')
            lines.extend(record.completed_abstracts)
        return truncate_by_tokens('\n'.join(lines).strip(), token_budget)

    def _collect_archive_records(
        self,
        *,
        query_text: str,
        session: Any | None,
        older_top_k: int,
    ) -> tuple[ArchiveContextRecord | None, list[ArchiveContextRecord]]:
        if session is None:
            return None, []
        segments = [item for item in list(getattr(session, 'archive_segments', []) or []) if isinstance(item, dict)]
        if not segments:
            return None, []
        records: list[ArchiveContextRecord] = []
        for segment in segments:
            archive_id = str(segment.get('archive_id') or '').strip()
            overview_uri = str(segment.get('overview_uri') or '').strip()
            abstract_uri = str(segment.get('abstract_uri') or '').strip()
            archive_uri = str(segment.get('archive_uri') or '').strip()
            if not archive_id or not overview_uri or not abstract_uri:
                continue
            overview = self._read_text_file(overview_uri)
            abstract = self._read_text_file(abstract_uri)
            if not overview and not abstract:
                continue
            records.append(
                ArchiveContextRecord(
                    archive_id=archive_id,
                    archive_uri=archive_uri,
                    overview_uri=overview_uri,
                    abstract_uri=abstract_uri,
                    overview=overview,
                    abstract=abstract or self._extract_archive_abstract(overview),
                    created_at=str(segment.get('created_at') or '').strip(),
                    summary_version=int(segment.get('summary_version') or 2),
                )
            )
        if not records:
            return None, []
        latest = records[-1]
        older = list(records[:-1])
        for record in older:
            record.score = score_query(query_text, record.abstract, record.overview, record.archive_id)
        older.sort(key=lambda item: (item.score, item.created_at), reverse=True)
        return latest, older[: max(0, int(older_top_k or 0))]

    @staticmethod
    def _read_text_file(path: str) -> str:
        candidate = Path(str(path or '').strip())
        if not candidate.exists():
            return ''
        try:
            return candidate.read_text(encoding='utf-8').strip()
        except Exception:
            return ''

    @staticmethod
    def _extract_archive_abstract(overview: str) -> str:
        text = str(overview or '').strip()
        if not text:
            return ''
        for line in text.splitlines():
            normalized = str(line or '').strip()
            if normalized.lower().startswith('**one-line overview**:'):
                return normalized.split(':', 1)[1].strip()
        for line in text.splitlines():
            normalized = str(line or '').strip()
            if normalized and not normalized.startswith('#'):
                return normalized
        return text[:160].strip()

    def _render_latest_archive_block(
        self,
        record: ArchiveContextRecord | None,
        *,
        token_budget: int,
    ) -> str:
        if record is None:
            return ''
        overview = str(record.overview or '').strip()
        abstract = str(record.abstract or '').strip()
        if not overview and not abstract:
            return ''
        body = overview or abstract
        if estimate_tokens(body) > token_budget and abstract:
            body = abstract
        lines = [
            '## Latest Session Archive Overview',
            f"- Archive: {record.archive_id}",
            truncate_by_tokens(body, max(1, token_budget - 24)),
        ]
        return truncate_by_tokens('\n'.join(lines).strip(), token_budget)

    def _render_older_archives_block(
        self,
        records: list[ArchiveContextRecord],
        *,
        token_budget: int,
    ) -> str:
        if not records or token_budget <= 0:
            return ''
        lines = ['## Older Session Archive Abstracts']
        used = estimate_tokens(lines[0])
        for record in records:
            line = f"- [{record.archive_id}] {record.abstract}"
            line_tokens = estimate_tokens(line)
            if used + line_tokens > token_budget:
                break
            lines.append(line)
            used += line_tokens
        return '\n'.join(lines).strip() if len(lines) > 1 else ''

    def _render_retrieved_context_bundle(
        self,
        bundle: RetrievedContextBundle,
        *,
        token_budget: int,
        include_l2: bool,
    ) -> str:
        records = list(bundle.records or [])
        if not records or token_budget <= 0:
            return ''
        lines = ['## Retrieved Context']
        used = estimate_tokens(lines[0])
        for record in records:
            label = str(record.get('record_id') or '').strip()
            l0 = str(record.get('l0') or '').strip()
            l1 = str(record.get('l1') or '').strip()
            l2 = str(record.get('l2_preview') or '').strip() if include_l2 else ''
            header = f"- [{label}] {l0 or l1}"
            candidate_lines = [header]
            if l1:
                candidate_lines.append(f"  L1: {truncate_by_tokens(l1, 64)}")
            if l2:
                candidate_lines.append(f"  L2: {truncate_by_tokens(l2, 48)}")
            block = '\n'.join(candidate_lines)
            block_tokens = estimate_tokens(block)
            if used + block_tokens > token_budget:
                header_tokens = estimate_tokens(header)
                if used + header_tokens > token_budget:
                    break
                lines.append(header)
                used += header_tokens
                continue
            lines.extend(candidate_lines)
            used += block_tokens
        return '\n'.join(lines).strip() if len(lines) > 1 else ''

    @staticmethod
    def _fit_optional_block(content: str, remaining_budget: int) -> tuple[str, str]:
        text = str(content or '').strip()
        if not text:
            return '', ''
        if remaining_budget <= 0:
            return '', 'token_budget_exceeded'
        tokens = estimate_tokens(text)
        if tokens <= remaining_budget:
            return text, ''
        return truncate_by_tokens(text, remaining_budget), 'token_budget_trimmed'

    @staticmethod
    def _messages_to_text(messages: list[dict[str, Any]]) -> str:
        parts = []
        for message in list(messages or []):
            role = str(message.get('role') or '').strip()
            content = str(message.get('content') or '').strip()
            if role and content:
                parts.append(f"[{role}] {content}")
        return '\n\n'.join(parts)

    @staticmethod
    def _messages_tokens(messages: list[dict[str, Any]]) -> int:
        return sum(estimate_tokens(str(message.get('content') or '')) for message in list(messages or []))

    @staticmethod
    def _minimum_live_raw_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not messages:
            return []
        if len(messages) <= 2:
            return list(messages)
        tail = list(messages[-2:])
        if str(tail[0].get('role') or '').strip().lower() == 'user':
            return tail
        return [messages[-1]]

    def _fit_live_raw_messages(self, messages: list[dict[str, Any]], *, budget_tokens: int) -> list[dict[str, Any]]:
        if not messages:
            return []
        minimum = self._minimum_live_raw_messages(messages)
        if budget_tokens <= 0:
            return minimum
        fitted = list(messages)
        while len(fitted) > len(minimum) and self._messages_tokens(fitted) > budget_tokens:
            fitted = fitted[1:]
        if self._messages_tokens(fitted) <= budget_tokens:
            return fitted
        return minimum

    def _select_skills(
        self,
        *,
        visible_skills: list[Any],
        top_k: int,
        token_budget: int,
        ranked_skill_ids: list[str] | None = None,
    ) -> tuple[list[Any], list[dict[str, Any]]]:
        ordered_skills = list(visible_skills or [])
        if ranked_skill_ids is not None:
            skill_map = {
                str(getattr(item, 'skill_id', '') or '').strip(): item
                for item in list(visible_skills or [])
                if str(getattr(item, 'skill_id', '') or '').strip()
            }
            ranked: list[Any] = []
            seen: set[str] = set()
            for skill_id in list(ranked_skill_ids or []):
                item = skill_map.get(str(skill_id or '').strip())
                if item is None:
                    continue
                normalized = str(getattr(item, 'skill_id', '') or '').strip()
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                ranked.append(item)
            ordered_skills = ranked + [
                item
                for item in list(visible_skills or [])
                if str(getattr(item, 'skill_id', '') or '').strip() not in seen
            ]

        selected: list[Any] = []
        trace: list[dict[str, Any]] = []
        used_tokens = 0
        semantic_rank_map = {
            str(skill_id or '').strip(): index + 1
            for index, skill_id in enumerate(list(ranked_skill_ids or []))
            if str(skill_id or '').strip()
        }
        for item in ordered_skills:
            line = f"- {getattr(item, 'skill_id', '')}: {getattr(item, 'description', '') or getattr(item, 'display_name', '')}"
            line_tokens = estimate_tokens(line)
            if len(selected) >= top_k:
                break
            if used_tokens + line_tokens > token_budget:
                continue
            selected.append(item)
            used_tokens += line_tokens
            trace.append(
                {
                    'skill_id': getattr(item, 'skill_id', ''),
                    'tokens': line_tokens,
                    'semantic_rank': semantic_rank_map.get(str(getattr(item, 'skill_id', '') or '').strip()),
                }
            )
        if not selected:
            selected = ordered_skills[:top_k]
            trace = [
                {
                    'skill_id': getattr(item, 'skill_id', ''),
                    'tokens': 0,
                    'semantic_rank': semantic_rank_map.get(str(getattr(item, 'skill_id', '') or '').strip()),
                }
                for item in selected
            ]
        return selected, trace

    def _build_external_tool_block(
        self,
        *,
        visible_families: list[Any],
        visible_tool_names: list[str],
        top_k: int,
    ) -> tuple[str, list[dict[str, Any]]]:
        visible_name_set = {
            str(name or '').strip()
            for name in list(visible_tool_names or [])
            if str(name or '').strip()
        }
        eligible: list[Any] = []
        for family in list(visible_families or []):
            tool_id = str(getattr(family, 'tool_id', '') or '').strip()
            install_dir = str(getattr(family, 'install_dir', '') or '').strip()
            callable_flag = bool(getattr(family, 'callable', True))
            available_flag = bool(getattr(family, 'available', True))
            registered_callable = self._family_has_registered_callable_executor(family, visible_name_set)
            if not tool_id:
                continue
            if callable_flag and available_flag and registered_callable:
                continue
            if not install_dir and not callable_flag:
                continue
            eligible.append(family)
        if not eligible:
            return '', []

        lines = ['## Tool Resources That Require `load_tool_context`']
        trace: list[dict[str, Any]] = []
        for family in eligible[: max(1, top_k)]:
            tool_id = str(getattr(family, 'tool_id', '') or '').strip()
            display_name = str(getattr(family, 'display_name', '') or tool_id).strip() or tool_id
            description = str(getattr(family, 'description', '') or '').strip()
            install_dir = str(getattr(family, 'install_dir', '') or '').strip()
            callable_flag = bool(getattr(family, 'callable', True))
            available_flag = bool(getattr(family, 'available', True))
            registered_callable = self._family_has_registered_callable_executor(family, visible_name_set)
            issue_summary = self._tool_family_issue_summary(family)
            if not callable_flag:
                state_text = f"Install dir: `{install_dir}`"
                if not available_flag:
                    state_text = f"{state_text}. Status: unavailable"
                    if issue_summary:
                        state_text = f"{state_text} ({issue_summary})"
                line = (
                    f"- `{tool_id}` ({display_name}): {description or 'Registered external tool resource.'} {state_text}. "
                    f"For install, update, troubleshooting, or usage guidance, call `load_tool_context(tool_id=\"{tool_id}\")`."
                )
            else:
                if available_flag and not registered_callable:
                    state_text = "Status: enabled but not currently registered in the callable function tool list for this turn"
                    next_step = f"For repair steps or usage guidance, call `load_tool_context(tool_id=\"{tool_id}\")`."
                elif not available_flag and registered_callable:
                    state_text = "Status: unavailable but exposed in the callable function tool list as `【待修复】`"
                    next_step = (
                        f"If you call `{tool_id}`, it will return structured repair guidance instead of executing the real capability. "
                        f"For repair steps or usage guidance, call `load_tool_context(tool_id=\"{tool_id}\")`."
                    )
                else:
                    state_text = "Status: unavailable"
                    next_step = f"For repair steps or usage guidance, call `load_tool_context(tool_id=\"{tool_id}\")`."
                if issue_summary:
                    state_text = f"{state_text} ({issue_summary})"
                line = (
                    f"- `{tool_id}` ({display_name}): {description or 'Tool guidance resource.'} {state_text}. "
                    f"{next_step}"
                )
            line_tokens = estimate_tokens(line)
            lines.append(line)
            trace.append(
                {
                    'tool_id': tool_id,
                    'install_dir': install_dir,
                    'callable': callable_flag,
                    'available': available_flag,
                    'registered_callable': registered_callable,
                    'tokens': line_tokens,
                }
            )
        return '\n'.join(lines), trace

    @staticmethod
    def _family_has_registered_callable_executor(family: Any, visible_name_set: set[str]) -> bool:
        executor_names: set[str] = set()
        for action in list(getattr(family, 'actions', []) or []):
            executor_names.update(
                str(name or '').strip()
                for name in list(getattr(action, 'executor_names', []) or [])
                if str(name or '').strip()
            )
        return bool(executor_names & visible_name_set)

    @staticmethod
    def _tool_family_issue_summary(family: Any) -> str:
        metadata = getattr(family, 'metadata', {}) or {}
        warnings = [str(item or '').strip() for item in list(metadata.get('warnings') or []) if str(item or '').strip()]
        errors = [str(item or '').strip() for item in list(metadata.get('errors') or []) if str(item or '').strip()]
        issues = errors + warnings
        return '; '.join(issues[:2])

    def _select_tools(
        self,
        *,
        query_text: str,
        visible_names: list[str],
        visible_families: list[Any],
        core_tools: set[str],
        extension_top_k: int,
        ranked_tool_ids: list[str] | None = None,
    ) -> tuple[list[str], dict[str, Any]]:
        visible_set = {str(name).strip() for name in visible_names if str(name).strip()}
        if not visible_set:
            return [], {'core': [], 'extension': []}
        raw_core_entries = {str(name).strip() for name in list(core_tools or []) if str(name).strip()}
        core_resolution = resolve_core_tool_targets(core_tools, visible_families)
        reserved = [name for name in visible_names if name in self.RESERVED_INTERNAL_TOOLS and name in visible_set]
        reserved_set = set(reserved)
        selected = {
            name
            for name in visible_set
            if (name in core_resolution.executor_names or name in raw_core_entries) and name not in reserved_set
        }
        if extension_top_k <= 0:
            ordered = reserved + [name for name in visible_names if name in selected]
            return ordered, {'reserved': reserved, 'core': sorted(selected), 'extension': []}

        if ranked_tool_ids is not None:
            family_map = {
                str(getattr(family, 'tool_id', '') or '').strip(): family
                for family in list(visible_families or [])
                if str(getattr(family, 'tool_id', '') or '').strip()
            }
            picked_extension: list[str] = []
            for tool_id in list(ranked_tool_ids or []):
                family = family_map.get(str(tool_id or '').strip())
                if family is None:
                    continue
                family_names = []
                for action in list(getattr(family, 'actions', []) or []):
                    for executor_name in list(getattr(action, 'executor_names', []) or []):
                        name = str(executor_name or '').strip()
                        if (
                            name
                            and name in visible_set
                            and name not in core_resolution.executor_names
                            and name not in raw_core_entries
                            and name not in reserved_set
                        ):
                            family_names.append(name)
                for name in sorted(set(family_names)):
                    if name in selected or name in picked_extension:
                        continue
                    picked_extension.append(name)
                    if len(picked_extension) >= extension_top_k:
                        break
                if len(picked_extension) >= extension_top_k:
                    break

            if len(picked_extension) < extension_top_k:
                for name in visible_names:
                    normalized = str(name or '').strip()
                    if (
                        normalized
                        and normalized in visible_set
                        and normalized not in selected
                        and normalized not in picked_extension
                        and normalized not in reserved_set
                    ):
                        picked_extension.append(normalized)
                        if len(picked_extension) >= extension_top_k:
                            break

            ordered = (
                reserved
                + [name for name in visible_names if name in selected]
                + [name for name in visible_names if name in picked_extension]
            )
            return ordered, {'reserved': reserved, 'core': sorted(selected), 'extension': picked_extension}

        ext_scored: list[tuple[float, list[str], str]] = []
        for family in visible_families:
            family_names = []
            for action in list(getattr(family, 'actions', []) or []):
                for executor_name in list(getattr(action, 'executor_names', []) or []):
                    name = str(executor_name or '').strip()
                    if (
                        name
                        and name in visible_set
                        and name not in core_resolution.executor_names
                        and name not in raw_core_entries
                        and name not in reserved_set
                    ):
                        family_names.append(name)
            family_names = sorted(set(family_names))
            if not family_names:
                continue
            score = score_query(
                query_text,
                getattr(family, 'tool_id', ''),
                getattr(family, 'display_name', ''),
                getattr(family, 'description', ''),
                ' '.join(family_names),
            )
            for name in family_names:
                for hint in self.EXTENSION_TOOL_HINTS.get(name, ()):
                    if hint and hint.lower() in str(query_text or '').lower():
                        score += 5.0
            ext_scored.append((score, family_names, str(getattr(family, 'tool_id', '') or '')))
        ext_scored.sort(key=lambda item: (item[0], item[2]), reverse=True)

        picked_extension: list[str] = []
        for score, names, tool_id in ext_scored:
            if len(picked_extension) >= extension_top_k:
                break
            for name in names:
                if name in selected or name in picked_extension:
                    continue
                picked_extension.append(name)
                if len(picked_extension) >= extension_top_k:
                    break
        if not picked_extension:
            for name in sorted(visible_set - selected):
                if name in reserved_set:
                    continue
                picked_extension.append(name)
                if len(picked_extension) >= extension_top_k:
                    break

        ordered = (
            reserved
            + [name for name in visible_names if name in selected]
            + [name for name in visible_names if name in picked_extension]
        )
        return ordered, {'reserved': reserved, 'core': sorted(selected), 'extension': picked_extension}
