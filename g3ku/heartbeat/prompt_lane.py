from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from g3ku.core.timefmt import render_local_time
from main.service.task_terminal_callback import TASK_TERMINAL_OUTPUT_INLINE_CHAR_LIMIT


@dataclass(slots=True)
class HeartbeatPromptLane:
    scope: str
    stable_messages: list[dict[str, Any]]
    dynamic_appendix_messages: list[dict[str, Any]]
    request_messages: list[dict[str, Any]]
    retrieval_query: str
    # 仅事件束本体（[SESSION EVENTS] 段），不含稳定规则文本。调用方若已把规则作为
    # 独立 system 消息注入，应使用它作为 user 事件消息，避免规则在同一回合重复出现。
    event_bundle_text: str = ""


def _non_empty_text(value: Any) -> str:
    return str(value or "").strip()


# 事件正文外置的注入点：调用方（heartbeat session_service）持有 content store，
# 渲染器只做"要不要外置、外置后给什么 ref"的判断，不自己碰存储。返回 canonical ref，
# 拿不到就回空串。
EventTextExternalizer = Callable[[str, dict[str, Any]], str]


def _append_retrieval_parts(parts: list[str], *values: Any) -> None:
    for value in values:
        text = _non_empty_text(value)
        if not text or text.startswith("artifact:"):
            continue
        parts.append(text)


def _overflow_head_and_tail(text: str, *, inline_limit: int) -> tuple[str, str]:
    """超限正文取头尾两段，两段合起来仍不超过原上限（外置只是补回中段，不是放大内联）。"""
    head_budget = max(1, int(inline_limit * 0.7))
    tail_budget = max(1, int(inline_limit * 0.2))
    return text[:head_budget].rstrip(), text[len(text) - tail_budget:].lstrip()


def _long_text_body(
    text: str,
    *,
    inline_limit: int,
    event: dict[str, Any],
    externalizer: EventTextExternalizer | None,
    continuation_indent: str = "  ",
) -> tuple[str, str]:
    """渲染一条正文本体，并返回给检索用的那份文本。

    超限时旧做法是只留前 `inline_limit` 字——故障正文的关键句（该核对哪条记录、观测值是
    多少）都写在后半段，截断等于让它不可读也不可搜。这里改成头尾都留、全文外置、行内给
    可取回的 `ref=`；外置不可用时如实说"取不回"，不给一个假 ref。
    """
    if len(text) <= inline_limit:
        return text, text
    head, tail = _overflow_head_and_tail(text, inline_limit=inline_limit)
    omitted = max(0, len(text) - len(head) - len(tail))
    ref = ""
    if callable(externalizer):
        try:
            ref = _non_empty_text(externalizer(text, event))
        except Exception:
            ref = ""
    marker = f'  [正文 {len(text)} 字：中间 {omitted} 字未内联]'
    marker += f' 完整正文用 content_open(ref="{ref}") 取回。' if ref else ' 外置不可用，未内联的部分此刻取不回。'
    return f'{head}\n{continuation_indent}{marker.strip()}\n{continuation_indent}{tail}', f'{head} … {tail}'


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


RUNTIME_FAULT_MARKER = 'runtime_fault:'
MODEL_CONFIG_FAULT_MARKER = 'model_config_fault:'
WINDOW_INTEGRITY_FAULT_MARKER = 'window_integrity_fault:'
SEND_PREFLIGHT_FAULT_MARKER = 'send_preflight_fault:'


def _runtime_fault_class(error_text: str) -> str:
    """只标故障形状，不复述处置。

    处置与观测数字都在 `Error` 正文里（就是同一条 blocking_reason），这里重复一遍会
    出现两份说法：正文改过之后类名还在断言"resume 无效"（实盘有节点在同一类故障后
    又成功跑了 5 跳），也会把模型引去核对正文没点名的载体。例外是 `runtime_fault`——
    它的正文是异常原话，没有任何处置线索。
    """
    text = str(error_text or '')
    if RUNTIME_FAULT_MARKER in text:
        # 这一类例外：正文是异常原话（`runtime_fault:<函数>:<Exc>`），没有任何处置线索，
        # "要重启 worker" 只能写在类名里，否则模型只会反复 resume。
        return 'runtime_fault（运行时自身缺陷，需操作员重启 worker 后再恢复）'
    if MODEL_CONFIG_FAULT_MARKER in text:
        return 'model_config_fault（模型配置与这一跳不匹配，处置见 Error 正文）'
    if WINDOW_INTEGRITY_FAULT_MARKER in text:
        return 'window_integrity_fault（回包被窗口剩余挤掉，处置见 Error 正文）'
    if SEND_PREFLIGHT_FAULT_MARKER in text:
        return 'send_preflight_fault（发送前预检拒发，这一跳没有发出请求，成因见 Error 正文）'
    return ''


def format_local_timestamp(value: Any) -> str:
    """把 ISO 时间戳渲染为本地带偏移格式，供提示词展示。

    stall 等内部时间戳在上游被规范化为 UTC（`task_stall_notifier._parse_iso`
    强制 ``astimezone(timezone.utc)``），而会话其余时间戳是本地偏移；两者混用会让
    模型对"已经等了多久 / 下次何时可重试"算错（实测 bundle 里出现 ``+00:00`` 而
    会话其他时间是 ``+08:00``）。这里统一转成本地带偏移格式再注入提示词。

    解析失败、空值或 naive（无 tzinfo）时间戳一律原样返回，绝不臆造偏移。
    """
    text = _non_empty_text(value)
    if not text or text.lower() == "unknown":
        return text or "unknown"
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        return text
    return parsed.astimezone().isoformat(timespec="seconds")


def _task_stall_lines(event: dict[str, Any], retrieval_parts: list[str]) -> list[str]:
    task_id = _non_empty_text(event.get("task_id"))
    title = _non_empty_text(event.get("title")) or task_id or "task"
    stall_reason = (
        _non_empty_text(event.get("stall_reason"))
        or _non_empty_text(event.get("task_stall_reason"))
        or _non_empty_text(event.get("reason"))
        or "suspected_stall"
    )
    stalled_minutes = _int_value(event.get("stalled_minutes"), 0)
    bucket_minutes = _int_value(event.get("bucket_minutes"), 0)
    brief_text = _non_empty_text(event.get("brief_text")) or "No task summary."
    latest_node_summary = _non_empty_text(event.get("latest_node_summary")) or "No latest node summary."
    runtime_excerpt = _non_empty_text(event.get("runtime_summary_excerpt")) or "No runtime summary."
    last_visible_output_at = format_local_timestamp(event.get("last_visible_output_at"))
    _append_retrieval_parts(
        retrieval_parts,
        "task_stall",
        task_id,
        title,
        stall_reason,
        brief_text,
        latest_node_summary,
        runtime_excerpt,
    )
    if stall_reason == "user_paused":
        return [
            f"- Task {title} ({task_id}) was paused by the user",
            "  Reason: user_paused",
            "  Do not investigate the task yet. Wait for the user to resume or redirect it.",
        ]
    if stall_reason == "worker_unavailable":
        return [
            f"- Task {title} ({task_id}) is waiting for the worker to come back",
            "  Reason: worker_unavailable",
            "  Do not treat this as a task logic stall yet. Wait for worker recovery or restart.",
        ]
    lines = [
        f"- Task {title} ({task_id}) may be stalled",
        f"  Reason: {stall_reason}",
        f"  Silent for: {stalled_minutes} min",
        f"  Trigger bucket: {bucket_minutes} min",
        f"  Last visible output at: {last_visible_output_at}",
        f"  Brief: {brief_text}",
        f"  Latest node: {latest_node_summary}",
        f"  Runtime: {runtime_excerpt}",
    ]
    perf_summary = _non_empty_text(event.get("perf_window_summary"))
    if perf_summary:
        lines.append(f"  Perf in stall window: {perf_summary}")
    paused_nodes = [item for item in list(event.get("paused_nodes") or []) if isinstance(item, dict)]
    if paused_nodes:
        rendered = [
            f"{_non_empty_text(item.get('node_id'))} ({_non_empty_text(item.get('pause_reason')) or 'manual'})"
            for item in paused_nodes
            if _non_empty_text(item.get('node_id'))
        ]
        if rendered:
            lines.append(f"  Paused nodes: {', '.join(rendered)}")
    lines.extend(_task_stall_detail_lines(event.get("stall_detail")))
    lines.extend([
        "  Suggested first step: task_progress(task_id)",
        "  In task_progress output, 'in_progress' means non-terminal, not active; only fresh 运行中/检验中 markers indicate real execution.",
    ])
    return lines


def _task_stall_detail_lines(detail: Any) -> list[str]:
    """停滞的**事实**行：闸位、上游限流、谁占着容量、卡在哪个相位。

    这里刻意一条建议都不写。把"先暂停任务"之类写进事件正文，会话会照着做，而排队的一方
    并不占容量（暂停它释放不出任何东西，恢复还要排到队尾）——动作留给拿到这些数的人判断。
    读不到的块就不写，不填 0：填 0 会被读成"闸口空闲"，而真实形态常常是"这拍没读数"。
    """
    if not isinstance(detail, dict):
        return []
    lines: list[str] = []
    gate = detail.get("entry_gate") if isinstance(detail.get("entry_gate"), dict) else {}
    if gate:
        pool_running = gate.get("pool_running")
        pool_limit = gate.get("pool_limit")
        if pool_running is not None and pool_limit:
            lines.append(
                f"  Node-turn gate (shared pool): running {pool_running}/{pool_limit}, "
                f"waiting {gate.get('pool_queued', 0)}, held {gate.get('binding_seconds', 0)}s"
            )
        reserved = gate.get("reserved")
        if isinstance(reserved, dict) and reserved:
            lines.append("  Gate reserved per role: " + ", ".join(f"{role}={value}" for role, value in sorted(reserved.items())))
        task_running = gate.get("task_running")
        task_queued = gate.get("task_queued")
        if isinstance(task_running, dict) or isinstance(task_queued, dict):
            running_text = ", ".join(f"{role}={value}" for role, value in sorted(dict(task_running or {}).items()))
            queued_text = ", ".join(f"{role}={value}" for role, value in sorted(dict(task_queued or {}).items()))
            lines.append(f"  This task at the gate: running [{running_text}], waiting [{queued_text}]")
    turn = detail.get("turn_queue") if isinstance(detail.get("turn_queue"), dict) else {}
    if turn:
        lines.append(
            "  Node-turn queue (already past the gate, waiting for a model turn): "
            "granted {running}, waiting {waiting}, oldest wait {oldest}s".format(
                running=turn.get("running", "unknown"),
                waiting=turn.get("waiting", "unknown"),
                oldest=turn.get("oldest_wait_seconds", "unknown"),
            )
        )
        head_bits: list[str] = []
        if turn.get("head_pinned_seconds") is not None:
            head_bits.append(f"one request holding the grant window for {turn.get('head_pinned_seconds')}s")
        if turn.get("head_busy_reason"):
            head_bits.append(f"head reason={turn.get('head_busy_reason')}")
        if turn.get("head_node_id"):
            head_bits.append(f"head node={turn.get('head_node_id')}")
        if turn.get("head_required_window"):
            head_bits.append(f"it needs a {turn.get('head_required_window')}-token window")
        if head_bits:
            lines.append("  Turn queue head: " + ", ".join(head_bits))
    rate = detail.get("upstream_rate") if isinstance(detail.get("upstream_rate"), dict) else {}
    if rate:
        lines.append(
            "  Upstream: requests/min {requests_per_minute}, completions/min {completions_per_minute}, "
            "429 penalty {penalty_429_max}, pressure {local_pressure_state}/{machine_pressure_state}".format(
                requests_per_minute=rate.get("requests_per_minute", "unknown"),
                completions_per_minute=rate.get("completions_per_minute", "unknown"),
                penalty_429_max=rate.get("penalty_429_max", "unknown"),
                local_pressure_state=rate.get("local_pressure_state", "unknown"),
                machine_pressure_state=rate.get("machine_pressure_state", "unknown"),
            )
        )
    occupiers = detail.get("occupiers")
    if isinstance(occupiers, list) and occupiers:
        lines.append(
            "  Other in-progress tasks holding capacity: "
            + ", ".join(
                f"{item.get('task_id')} (running {item.get('running', 0)}, waiting {item.get('queued', 0)})"
                for item in occupiers[:8]
                if isinstance(item, dict)
            )
        )
    lane = detail.get("lane") if isinstance(detail.get("lane"), dict) else {}
    if lane:
        state = str(lane.get("distribution_state") or "").strip()
        if state:
            lines.append(f"  Distribution barrier active: state={state} epoch={lane.get('distribution_epoch_id') or ''}")
        deadline = str(lane.get("running_tool_deadline") or "").strip()
        if deadline:
            lines.append(f"  A node tool call is still inside its own deadline until: {deadline}")
        frames = lane.get("frames")
        if isinstance(frames, list) and frames:
            lines.append(
                "  Frame phases at emit time: "
                + ", ".join(
                    f"{item.get('node_id')}={item.get('phase') or '?'}@{item.get('updated_at') or '?'}"
                    for item in frames[:3]
                    if isinstance(item, dict)
                )
            )
    return lines


def _task_node_error_lines(
    event: dict[str, Any],
    retrieval_parts: list[str],
    *,
    output_inline_limit: int,
    externalizer: EventTextExternalizer | None,
) -> list[str]:
    task_id = _non_empty_text(event.get('task_id'))
    node_id = _non_empty_text(event.get('node_id'))
    node_title = _non_empty_text(event.get('node_title') or node_id) or 'node'
    pause_reason = _non_empty_text(event.get('pause_reason')) or 'error'
    error_text = _non_empty_text(event.get('error_text') or event.get('remark')) or 'Unknown node error.'
    task_title = _non_empty_text(event.get('task_title') or task_id) or 'task'
    error_line, error_for_retrieval = _long_text_body(
        error_text,
        inline_limit=output_inline_limit,
        event=event,
        externalizer=externalizer,
    )
    _append_retrieval_parts(
        retrieval_parts, 'task_node_error', task_title, task_id, node_title, node_id, pause_reason, error_for_retrieval
    )
    error_line = f'  Error: {error_line}'
    failure_class = _runtime_fault_class(error_text)
    lines = [
        f'- Task {task_title} ({task_id}) has a node paused after an error',
        f'  Node: {node_title} ({node_id})',
        f'  Pause reason: {pause_reason}',
        *([f'  Failure class: {failure_class}'] if failure_class else []),
        error_line,
    ]
    # 重试状态：让模型知道这是第几次失败、上次/下次时间，而不是误以为每次都是第一次。
    # 数据由 session_service 从 heartbeat_node_retry_state 表 + task_error_logs 注入。
    failed = _int_value(event.get('retry_attempt'), 0)
    cap = _int_value(event.get('retry_cap'), 0)
    escalated = bool(event.get('retry_escalated'))
    if failed >= 1:
        state_bits = [f'consecutive_failures={failed}' + (f'/{cap}' if cap else '')]
        last_at = _non_empty_text(event.get('retry_last_attempt_at'))
        next_at = _non_empty_text(event.get('retry_next_eligible_at'))
        if last_at and last_at != 'unknown':
            state_bits.append(f'last_attempt_at={last_at}')
        if next_at and next_at != 'unknown':
            state_bits.append(f'next_eligible_at={next_at}')
        lines.append(f'  Retry state: {" | ".join(state_bits)}')
        previous_errors = [item for item in list(event.get('previous_errors') or []) if isinstance(item, dict)]
        if previous_errors:
            lines.append('  Previous errors (newest first, deduped):')
            for item in previous_errors:
                text = _non_empty_text(item.get('text'))
                if not text:
                    continue
                at = _non_empty_text(item.get('at'))
                prefix = f'[{at}] ' if at and at != 'unknown' else ''
                lines.append(f'    - {prefix}{text}')
    if escalated:
        # 连续失败达上限：强制模型向用户报告并请其裁决，而不是继续静默重试。
        times = cap or failed
        lines.append(
            f'  ESCALATION: 该节点已连续 {times} 次无法启动、自动恢复失败。'
            '你**必须**现在向用户报告并请求裁决，不要继续静默重试。建议措辞：'
            f'"这些节点已经连续 {times} 次无法启动，是否判为失败让任务继续进行？这些节点的任务分别为……"。'
            '在用户决定前用 manage_task_nodes 的 keep_paused 保持暂停，并在 remark 写明等待用户裁决。'
        )
    else:
        lines.append('  Use manage_task_nodes to resume, keep_paused with a reason, fail, or pause related nodes as appropriate.')
    return lines


def _task_terminal_lines(
    event: dict[str, Any],
    retrieval_parts: list[str],
    *,
    output_inline_limit: int,
    externalizer: EventTextExternalizer | None,
) -> list[str]:
    title = _non_empty_text(event.get("title") or event.get("task_id")) or "task"
    task_id = _non_empty_text(event.get("task_id"))
    status = _non_empty_text(event.get("status")) or "unknown"
    summary = _non_empty_text(event.get("brief_text") or event.get("failure_reason")) or "No summary."
    terminal_node_id = _non_empty_text(event.get("terminal_node_id"))
    terminal_node_kind = _non_empty_text(event.get("terminal_node_kind")) or "execution"
    terminal_reason = _non_empty_text(event.get("terminal_node_reason"))
    terminal_output = _non_empty_text(event.get("terminal_output"))
    terminal_output_ref = _non_empty_text(event.get("terminal_output_ref"))
    terminal_check_result = _non_empty_text(event.get("terminal_check_result"))
    terminal_failure_reason = _non_empty_text(event.get("terminal_failure_reason"))
    root_output = _non_empty_text(event.get("root_output"))
    root_output_ref = _non_empty_text(event.get("root_output_ref"))
    # 用户定向补充：任务期间用户对各节点追加的通知（epoch 记账），终态事件携带，
    # 让会话模型明确知道用户修改/追加过哪些节点的需求（对齐最终验收判定）。
    user_node_supplements = [
        item for item in list(event.get("user_node_supplements") or [])
        if isinstance(item, dict)
        and _non_empty_text(item.get("node_id"))
        and _non_empty_text(item.get("message"))
    ]
    # Terminal deliverables are allowed to inline far more than ordinary event
    # excerpts so the heartbeat turn can hand the user the full result without
    # a follow-up content_open round-trip.
    terminal_output_limit = max(output_inline_limit, TASK_TERMINAL_OUTPUT_INLINE_CHAR_LIMIT)
    supplement_retrieval_parts: list[str] = ["user_node_supplements"]
    supplement_entries: list[tuple[str, str, str, str]] = []
    for supplement in user_node_supplements:
        node_id = _non_empty_text(supplement.get("node_id"))
        node_title = _non_empty_text(supplement.get("node_title")) or node_id
        message = _non_empty_text(supplement.get("message"))
        # 用户定向补充是验收判据的输入，截掉后半句等于换了一条要求去比对。
        body, retrieval = _long_text_body(
            message or '<空>',
            inline_limit=terminal_output_limit,
            event=supplement,
            externalizer=externalizer,
            continuation_indent="      ",
        )
        epoch_state = _non_empty_text(supplement.get("epoch_state")).lower()
        state_suffix = ""
        if epoch_state and epoch_state not in {"completed", "unknown"}:
            state_suffix = f" [distribution state: {epoch_state}]"
        supplement_entries.append((node_title, node_id, body, state_suffix))
        for part in (node_title, node_id, retrieval):
            if part:
                supplement_retrieval_parts.append(part)
    _append_retrieval_parts(
        retrieval_parts,
        "task_terminal",
        title,
        task_id,
        status,
        summary,
        terminal_node_kind,
        terminal_node_id,
        terminal_reason,
        terminal_output[:terminal_output_limit] if terminal_output else "",
        terminal_check_result,
        terminal_failure_reason,
        root_output[:terminal_output_limit] if root_output and root_output != terminal_output else "",
        *supplement_retrieval_parts,
    )
    lines = [
        f"- Task {title} ({task_id}) completed",
        f"  Status: {status}",
    ]
    # 收尾规则挂在失败分类与最终验收状态上：这两个值一直在 payload 里但从未渲染，
    # 模型只能从摘要文案反推分类，会把终局失败当成还能自动续跑的失败。
    failure_class = _non_empty_text(event.get("failure_class"))
    if failure_class:
        lines.append(f"  Failure class: {failure_class}")
    final_acceptance_status = _non_empty_text(event.get("final_acceptance_status"))
    if final_acceptance_status and final_acceptance_status != "pending":
        lines.append(f"  Final acceptance: {final_acceptance_status}")
    # finished_at 一直在 payload 里但从未渲染：heartbeat 可能在任务完成后很久才唤醒，
    # 缺了它模型会把"几小时前完成"误报成"刚完成"。
    finished_at = format_local_timestamp(event.get("finished_at"))
    if finished_at and finished_at != "unknown":
        lines.append(f"  Finished at: {finished_at}")
    lines.append(f"  Summary: {summary}")
    if terminal_node_id:
        lines.append(f"  Result node: {terminal_node_kind} {terminal_node_id}")
    if terminal_reason:
        lines.append(f"  Result source: {terminal_reason}")
    if terminal_output:
        if len(terminal_output) > terminal_output_limit:
            excerpt = terminal_output[:terminal_output_limit].rstrip()
            lines.append(f"  Result output excerpt: {excerpt}...")
        else:
            lines.append(f"  Result output: {terminal_output}")
    if terminal_output_ref:
        lines.append(f"  Result output ref: {terminal_output_ref}")
    if root_output and root_output != terminal_output:
        if len(root_output) > terminal_output_limit:
            excerpt = root_output[:terminal_output_limit].rstrip()
            lines.append(f"  Execution output excerpt: {excerpt}...")
        else:
            lines.append(f"  Execution output: {root_output}")
    if root_output_ref and root_output_ref != terminal_output_ref:
        lines.append(f"  Execution output ref: {root_output_ref}")
    if terminal_check_result:
        lines.append(f"  Result check: {terminal_check_result}")
    if terminal_failure_reason and terminal_failure_reason != summary:
        lines.append(f"  Result failure reason: {terminal_failure_reason}")
    # 用户定向补充放在事件块末尾（任务结果之后），点名任务内的节点与追加内容，
    # 会话模型汇报最终结果/做验收判定时必须与这些补充对齐。
    if user_node_supplements:
        lines.append(
            "  User node supplements (用户定向补充：任务期间用户对以下节点追加了信息，"
            "向用户汇报最终结果与验收判定时请将这些补充与任务结果对齐):"
        )
        for node_title, node_id, body, state_suffix in supplement_entries:
            lines.append(f"    - Node {node_title} ({node_id}): {body}{state_suffix}")
    return lines


def _task_distribution_error_lines(
    event: dict[str, Any],
    retrieval_parts: list[str],
    *,
    output_inline_limit: int,
    externalizer: EventTextExternalizer | None,
) -> list[str]:
    task_id = _non_empty_text(event.get('task_id'))
    task_title = _non_empty_text(event.get('title') or task_id) or 'task'
    error_text = _non_empty_text(event.get('error_text')) or 'message distribution failed'
    error_line, error_for_retrieval = _long_text_body(
        error_text,
        inline_limit=output_inline_limit,
        event=event,
        externalizer=externalizer,
    )
    _append_retrieval_parts(retrieval_parts, 'task_distribution_error', task_title, task_id, error_for_retrieval)
    return [
        f'- Task {task_title} ({task_id}) 的消息分发失败（epoch state=failed），任务已暂停',
        f'  Error: {error_line}',
        '  提醒：如果多次出现，则不要再分发，通知用户现状任务已暂停。',
    ]


def _task_distribution_skipped_lines(
    event: dict[str, Any],
    retrieval_parts: list[str],
    *,
    output_inline_limit: int,
    externalizer: EventTextExternalizer | None,
) -> list[str]:
    task_id = _non_empty_text(event.get('task_id'))
    task_title = _non_empty_text(event.get('title') or task_id) or 'task'
    items = [dict(item) for item in list(event.get('skipped') or []) if isinstance(item, dict)]
    count = int(event.get('skipped_count') or len(items) or 0)
    message = _non_empty_text(event.get('root_message'))
    message_line, message_for_retrieval = _long_text_body(
        message or '<空>',
        inline_limit=output_inline_limit,
        event=event,
        externalizer=externalizer,
    )
    _append_retrieval_parts(
        retrieval_parts,
        'task_distribution_skipped',
        task_title,
        task_id,
        f"已跳过 {count} 个节点；{message_for_retrieval}",
    )
    lines = [
        f'- Task {task_title} ({task_id}) 的消息分发已完成，但**已跳过 {count} 个节点**：'
        '这些子树根节点的转发回合多次未提交有效决策，本轮消息没有进入它们的子树',
        f'  消息本体（未下发给下列子树）: {message_line}',
    ]
    for item in items[:10]:
        node_id = _non_empty_text(item.get('node_id'))
        reason = _non_empty_text(item.get('reason')) or 'distribution turn failed'
        lines.append(f'    - Node {node_id}: {reason}')
    lines.append(
        '  处置：这条降级**不会自动重投、也不会自动关闭**，任务与各节点都在继续跑。'
        '若这些子树确实需要收到该消息，用定向追加通知（append notice，target 填上面的节点）逐支重投；'
        '判断不需要就什么都不用做。'
    )
    return lines


def _event_bundle_content(
    events: list[dict[str, Any]],
    *,
    output_inline_limit: int,
    externalizer: EventTextExternalizer | None,
) -> tuple[str, str]:
    # 唤醒时刻锚点：模型上下文里没有其他"现在几点"来源，事件里又可能引用几小时前
    # 的时间（如 finished_at），头部给出本次唤醒的本地时间供模型直接对照。
    lines = ["[SESSION EVENTS]", "## EVENT BUNDLE", f"当前时间（本次唤醒时刻）：{render_local_time()}"]
    retrieval_parts: list[str] = []
    for raw_event in list(events or []):
        event = dict(raw_event or {})
        reason = _non_empty_text(event.get("event_reason") or event.get("kind") or event.get("reason")).lower()
        if reason == "task_stall":
            lines.extend(_task_stall_lines(event, retrieval_parts))
            continue
        if reason == "task_node_error":
            lines.extend(
                _task_node_error_lines(
                    event, retrieval_parts, output_inline_limit=output_inline_limit, externalizer=externalizer
                )
            )
            continue
        if reason == "task_distribution_error":
            lines.extend(
                _task_distribution_error_lines(
                    event, retrieval_parts, output_inline_limit=output_inline_limit, externalizer=externalizer
                )
            )
            continue
        if reason == "task_distribution_skipped":
            lines.extend(
                _task_distribution_skipped_lines(
                    event, retrieval_parts, output_inline_limit=output_inline_limit, externalizer=externalizer
                )
            )
            continue
        lines.extend(
            _task_terminal_lines(
                event,
                retrieval_parts,
                output_inline_limit=output_inline_limit,
                externalizer=externalizer,
            )
        )
    return "\n".join(lines).strip(), " ".join(part for part in retrieval_parts if part).strip()


def build_heartbeat_prompt_lane(
    *,
    provider_model: str,
    stable_rules_text: str,
    events: list[dict[str, Any]],
    output_inline_limit: int = 4000,
    output_externalizer: EventTextExternalizer | None = None,
) -> HeartbeatPromptLane:
    _ = provider_model
    stable_messages: list[dict[str, Any]] = []
    stable_rules = _non_empty_text(stable_rules_text)
    if stable_rules:
        stable_messages.append({"role": "system", "content": stable_rules})
    event_bundle_text, retrieval_query = _event_bundle_content(
        list(events or []),
        output_inline_limit=output_inline_limit,
        externalizer=output_externalizer,
    )
    dynamic_appendix_messages = (
        [{"role": "user", "content": event_bundle_text}]
        if event_bundle_text
        else []
    )
    request_messages = [*stable_messages, *dynamic_appendix_messages]
    combined_user_sections = [
        str(message.get("content") or "").strip()
        for message in [*stable_messages, *dynamic_appendix_messages]
        if str(message.get("content") or "").strip()
    ]
    if combined_user_sections:
        request_messages.append(
            {
                "role": "user",
                "content": "\n\n".join(combined_user_sections).strip(),
            }
        )
    return HeartbeatPromptLane(
        scope="ceo_heartbeat",
        stable_messages=stable_messages,
        dynamic_appendix_messages=dynamic_appendix_messages,
        request_messages=request_messages,
        retrieval_query=retrieval_query,
        event_bundle_text=event_bundle_text,
    )


__all__ = ["HeartbeatPromptLane", "build_heartbeat_prompt_lane"]
