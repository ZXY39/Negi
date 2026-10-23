from __future__ import annotations


def _field(value, name: str):
    if isinstance(value, dict):
        return value[name]
    return getattr(value, name)


def test_build_heartbeat_lane_keeps_artifact_refs_out_of_retrieval_query_in_heartbeat_lane() -> None:
    from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane

    lane = build_heartbeat_prompt_lane(
        provider_model="openai:gpt-4.1",
        stable_rules_text="Keep the user informed without exposing internal mechanics.",
        events=[
            {
                "reason": "task_terminal",
                "task_id": "task:521ee9055b4a",
                "status": "failed",
                "brief_text": "provider chain exhausted",
                "terminal_failure_reason": "provider chain exhausted",
                "terminal_output_ref": "artifact:artifact:task-terminal-521ee9055b4a",
            }
        ],
    )

    dynamic_appendix_messages = list(_field(lane, "dynamic_appendix_messages"))
    request_messages = list(_field(lane, "request_messages"))
    retrieval_query = str(_field(lane, "retrieval_query") or "")
    matching_dynamic_user_messages = [
        message
        for message in dynamic_appendix_messages
        if str(message.get("role") or "").strip().lower() == "user"
        and "task:521ee9055b4a" in str(message.get("content") or "")
        and "provider chain exhausted" in str(message.get("content") or "")
    ]

    assert _field(lane, "scope") == "ceo_heartbeat"
    assert "artifact:artifact:task-terminal-521ee9055b4a" not in retrieval_query
    assert "task_terminal" in retrieval_query
    assert "failed" in retrieval_query
    assert "task:521ee9055b4a" in retrieval_query
    assert "provider chain exhausted" in retrieval_query
    assert matching_dynamic_user_messages
    assert any(message in request_messages for message in matching_dynamic_user_messages)


def test_build_heartbeat_lane_reuses_stable_prefix_when_only_event_payload_changes() -> None:
    from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane

    base_kwargs = {
        "provider_model": "openai:gpt-4.1",
        "stable_rules_text": "Keep the user informed without exposing internal mechanics.",
    }
    first = build_heartbeat_prompt_lane(
        **base_kwargs,
        events=[
            {
                "reason": "task_stall",
                "task_id": "task:521ee9055b4a",
                "title": "installer rollout",
                "stalled_minutes": 20,
                "bucket_minutes": 20,
                "latest_node_summary": "node:1 waiting on exec",
                "runtime_summary_excerpt": "no samples in window",
            }
        ],
    )
    second = build_heartbeat_prompt_lane(
        **base_kwargs,
        events=[
            {
                "reason": "task_stall",
                "task_id": "task:521ee9055b4a",
                "title": "installer rollout",
                "stalled_minutes": 30,
                "bucket_minutes": 30,
                "latest_node_summary": "node:1 still waiting on exec",
                "runtime_summary_excerpt": "budget_state=degraded",
            }
        ],
    )

    assert _field(first, "scope") == "ceo_heartbeat"
    assert _field(second, "scope") == "ceo_heartbeat"
    assert list(_field(first, "stable_messages")) == list(_field(second, "stable_messages"))
    assert list(_field(first, "dynamic_appendix_messages")) != list(_field(second, "dynamic_appendix_messages"))
    assert list(_field(first, "request_messages")) != list(_field(second, "request_messages"))
    assert str(_field(first, "retrieval_query") or "") != str(_field(second, "retrieval_query") or "")


def test_node_error_bundle_renders_retry_state_when_previously_failed() -> None:
    from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane

    lane = build_heartbeat_prompt_lane(
        provider_model="",
        stable_rules_text="rules",
        events=[{
            "reason": "task_node_error",
            "task_id": "task:t", "node_id": "node:n", "node_title": "节点N",
            "pause_reason": "error", "error_text": "boom",
            "retry_attempt": 2, "retry_cap": 5,
            "retry_last_attempt_at": "2026-09-03T14:53:00+08:00",
            "retry_next_eligible_at": "2026-09-03T14:57:00+08:00",
            "retry_escalated": False,
            "previous_errors": [{"at": "2026-09-03T14:51:00+08:00", "text": "earlier boom"}],
        }],
    )
    text = str(_field(lane, "event_bundle_text"))
    assert "consecutive_failures=2/5" in text
    assert "next_eligible_at=2026-09-03T14:57:00+08:00" in text
    assert "Previous errors" in text and "earlier boom" in text
    assert "ESCALATION" not in text
    assert "Use manage_task_nodes" in text


def test_node_error_bundle_first_attempt_omits_retry_state() -> None:
    from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane

    lane = build_heartbeat_prompt_lane(
        provider_model="",
        stable_rules_text="rules",
        events=[{
            "reason": "task_node_error", "task_id": "task:t", "node_id": "node:n",
            "error_text": "boom", "retry_attempt": 0, "retry_cap": 5, "retry_escalated": False,
        }],
    )
    text = str(_field(lane, "event_bundle_text"))
    assert "Retry state" not in text  # 首次投递不显示重试状态，避免噪音
    assert "Use manage_task_nodes" in text


def test_node_error_bundle_escalation_forces_user_report() -> None:
    from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane

    lane = build_heartbeat_prompt_lane(
        provider_model="",
        stable_rules_text="rules",
        events=[{
            "reason": "task_node_error", "task_id": "task:t", "node_id": "node:n",
            "error_text": "boom", "retry_attempt": 5, "retry_cap": 5, "retry_escalated": True,
        }],
    )
    text = str(_field(lane, "event_bundle_text"))
    assert "ESCALATION" in text
    assert "连续 5 次无法启动" in text
    assert "是否判为失败让任务继续进行" in text
    # 升级时不再给"自行 resume"的常规指引，改为强制上报 + keep_paused
    assert "Use manage_task_nodes to resume" not in text
    assert "keep_paused" in text


def test_node_error_bundle_marks_runtime_fault_class() -> None:
    from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane

    lane = build_heartbeat_prompt_lane(
        provider_model="",
        stable_rules_text="rules",
        events=[{
            "reason": "task_node_error", "task_id": "task:t", "node_id": "node:n",
            "pause_reason": "error",
            "error_text": "runtime_fault:filesystem_stat:NameError: name '_X' is not defined",
            "retry_attempt": 1, "retry_cap": 5, "retry_escalated": False,
        }],
    )
    text = str(_field(lane, "event_bundle_text"))
    assert "Failure class: runtime_fault" in text
    assert "重启 worker" in text


def test_node_error_bundle_omits_failure_class_for_ordinary_errors() -> None:
    from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane

    lane = build_heartbeat_prompt_lane(
        provider_model="",
        stable_rules_text="rules",
        events=[{
            "reason": "task_node_error", "task_id": "task:t", "node_id": "node:n",
            "error_text": "boom", "retry_attempt": 0, "retry_cap": 5, "retry_escalated": False,
        }],
    )
    assert "Failure class" not in str(_field(lane, "event_bundle_text"))


def test_node_error_bundle_keeps_error_excerpt_after_failure_class_slot() -> None:
    """`Failure class` 插在 Error 之前，长错误正文的外置块不能错位。"""
    from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane

    lane = build_heartbeat_prompt_lane(
        provider_model="",
        stable_rules_text="rules",
        events=[{
            "reason": "task_node_error", "task_id": "task:t", "node_id": "node:n",
            "pause_reason": "error",
            "error_text": "runtime_fault:exec:AttributeError: " + ("very long " * 900) + "TAIL-SENTENCE",
            "retry_attempt": 1, "retry_cap": 5, "retry_escalated": False,
        }],
    )
    text = str(_field(lane, "event_bundle_text"))
    assert "Failure class: runtime_fault" in text
    assert "runtime_fault:exec:AttributeError:" in text
    # 没有外置道时如实标注，不给一个假 ref，也不许把后半句当成已交付。
    assert "外置不可用，未内联的部分此刻取不回" in text
    assert "TAIL-SENTENCE" in text
    assert text.index("Failure class:") < text.index("runtime_fault:exec:AttributeError:")


def test_overflowed_error_text_is_externalized_with_a_retrievable_ref() -> None:
    """超限正文要全文外置并给可取回的 ref：故障正文的关键句都写在后半段。"""
    from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane

    calls: list[str] = []

    def _externalize(text: str, event: dict) -> str:
        calls.append(text)
        assert str(event.get("node_id") or "") == "node:n"
        return "artifact:hb-overflow-1"

    long_text = "window_integrity_fault: 开头句 " + ("pad " * 3000) + " 结尾该核对 sensenova 的 context_window_tokens"
    lane = build_heartbeat_prompt_lane(
        provider_model="",
        stable_rules_text="rules",
        events=[{
            "reason": "task_node_error", "task_id": "task:t", "node_id": "node:n",
            "pause_reason": "error", "error_text": long_text,
        }],
        output_externalizer=_externalize,
    )
    text = str(_field(lane, "event_bundle_text"))
    assert calls == [long_text], "外置要收到完整正文，不是内联那一段"
    assert 'content_open(ref="artifact:hb-overflow-1")' in text
    assert "结尾该核对 sensenova 的 context_window_tokens" in text
    assert "window_integrity_fault: 开头句" in text
    # 检索面用头+尾，不能再只带被截断的前半句。
    assert "结尾该核对 sensenova 的 context_window_tokens" in str(_field(lane, "retrieval_query"))


def test_overflow_does_not_grow_the_inline_footprint() -> None:
    """外置是补回中段，不是放大内联：内联块仍不超过原上限加一行说明。"""
    from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane

    long_text = "head " + ("x" * 40_000) + " tail"
    lane = build_heartbeat_prompt_lane(
        provider_model="",
        stable_rules_text="rules",
        events=[{
            "reason": "task_node_error", "task_id": "task:t", "node_id": "node:n",
            "pause_reason": "error", "error_text": long_text,
        }],
        output_externalizer=lambda text, event: "artifact:hb-2",
    )
    bundle = str(_field(lane, "event_bundle_text"))
    error_block = bundle[bundle.index("  Error:"):]
    assert len(error_block) <= 4000 + 200


def test_within_limit_error_text_stays_fully_inline_and_unexternalized() -> None:
    from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane

    calls: list[str] = []
    short = "model_config_fault: 这一跳思考顶满输出"
    lane = build_heartbeat_prompt_lane(
        provider_model="",
        stable_rules_text="rules",
        events=[{
            "reason": "task_node_error", "task_id": "task:t", "node_id": "node:n",
            "pause_reason": "error", "error_text": short,
        }],
        output_externalizer=lambda text, event: calls.append(text) or "artifact:never",
    )
    text = str(_field(lane, "event_bundle_text"))
    assert calls == []
    assert f"Error: {short}" in text
    assert "content_open" not in text


def test_distribution_error_overflow_also_gets_a_ref() -> None:
    from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane

    lane = build_heartbeat_prompt_lane(
        provider_model="",
        stable_rules_text="rules",
        events=[{
            "reason": "task_distribution_error", "task_id": "task:t",
            "error_text": "epoch failed: " + ("pad " * 2000) + "TAIL-CAUSE",
        }],
        output_externalizer=lambda text, event: "artifact:hb-dist-1",
    )
    text = str(_field(lane, "event_bundle_text"))
    assert 'content_open(ref="artifact:hb-dist-1")' in text
    assert "TAIL-CAUSE" in text


def test_user_node_supplement_overflow_is_retrievable() -> None:
    """用户定向补充是验收判据的输入：截掉后半句等于换了一条要求去比对。"""
    from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane

    lane = build_heartbeat_prompt_lane(
        provider_model="",
        stable_rules_text="rules",
        events=[{
            "reason": "task_terminal", "task_id": "task:t", "status": "success",
            "brief_text": "done",
            "user_node_supplements": [
                {"node_id": "node:n1", "node_title": "分工变更", "message": "开头 " + ("y" * 20_000) + " 结尾追加：只做只读检查"},
            ],
        }],
        output_externalizer=lambda text, event: "artifact:hb-supp-1",
    )
    text = str(_field(lane, "event_bundle_text"))
    assert 'content_open(ref="artifact:hb-supp-1")' in text
    assert "结尾追加：只做只读检查" in text
    assert "结尾追加：只做只读检查" in str(_field(lane, "retrieval_query"))


def test_distribution_skipped_event_reads_as_degraded_not_paused() -> None:
    """降级跳过要说清「已跳过 N 个节点 + 消息本体 + 不自动重投」，且不得说成任务暂停。"""
    from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane

    lane = build_heartbeat_prompt_lane(
        provider_model="",
        stable_rules_text="rules",
        events=[
            {
                "reason": "task_distribution_skipped",
                "notice_kind": "skipped",
                "task_id": "task:1d9cddf9858e",
                "title": "AI 时代行业选择研究",
                "epoch_id": "epoch:19a6f750cd85",
                "root_message": "外网注意使用clash端口7897",
                "skipped_count": 2,
                "skipped": [
                    {"node_id": "node:623979e93a3d", "reason": "distribution_decision_missing_message:node:741f61e01d86"},
                    {"node_id": "node:0496de93d81a", "reason": "distribution_decision_missing_child_decisions:node:a301e72969dd"},
                ],
            }
        ],
    )
    text = str(_field(lane, "event_bundle_text"))
    assert "已跳过 2 个节点" in text
    assert "node:623979e93a3d" in text
    assert "外网注意使用clash端口7897" in text
    assert "不会自动重投" in text
    assert "epoch state=failed" not in text
    assert "任务已暂停" not in text


def test_node_error_bundle_marks_model_config_fault_class() -> None:
    """思考顶满最大输出的暂停：类名给分类，处置只从 Error 正文来（同一份说法不许有两处）。"""
    from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane

    lane = build_heartbeat_prompt_lane(
        provider_model="",
        stable_rules_text="rules",
        events=[{
            "reason": "task_node_error", "task_id": "task:t", "node_id": "node:n",
            "pause_reason": "error",
            "error_text": (
                "Invalid final result submission detected 2 consecutive times. Latest issue: "
                "model_config_fault: reply carried no tool call and no text (reasoning-only) "
                "[output-capped] | sent_reasoning_effort=xhigh sent_max_tokens=65536 "
                "同类跳次=2；运行时不会改动模型档位：请把该模型的思考强度调低后再恢复本节点"
            ),
            "retry_attempt": 1, "retry_cap": 5, "retry_escalated": False,
        }],
    )
    text = str(_field(lane, "event_bundle_text"))
    assert "Failure class: model_config_fault" in text
    assert "思考强度" in text
    # 这一类的处置不是重启 worker，别让两个类别的措辞互相污染。
    assert "重启 worker" not in text


def test_node_error_bundle_marks_window_integrity_fault_class() -> None:
    """被窗口挤掉的暂停：提醒要把它和"降档位"那一类分开，否则 agent 会改错那一行配置。"""
    from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane

    lane = build_heartbeat_prompt_lane(
        provider_model="",
        stable_rules_text="rules",
        events=[{
            "reason": "task_node_error", "task_id": "task:t", "node_id": "node:n",
            "pause_reason": "error",
            "error_text": (
                "Invalid final result submission detected 1 consecutive times. Latest issue: "
                "window_integrity_fault: reply carried no tool call and no text (reasoning-only) "
                "[window-clamped] | 这一跳模型=sens-x 该模型声明窗口=390000 "
                "sent_max_tokens=65536 output_tokens=16352 本节点实测最大(输入+输出)=262144 "
                "要核对的只有这一跳模型那一颗的 context_window_tokens"
            ),
            "retry_attempt": 1, "retry_cap": 5, "retry_escalated": False,
        }],
    )
    text = str(_field(lane, "event_bundle_text"))
    assert "Failure class: window_integrity_fault" in text
    assert "context_window_tokens" in text
    assert "这一跳模型=sens-x" in text
    assert "思考强度调低" not in text


def test_node_error_bundle_marks_provider_payload_fault_class() -> None:
    """上游网关拼坏参数块的暂停：类名要说清"不是节点提交不合格"，否则会话去重派节点。"""
    from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane

    lane = build_heartbeat_prompt_lane(
        provider_model="",
        stable_rules_text="rules",
        events=[{
            "reason": "task_node_error", "task_id": "task:t", "node_id": "node:n",
            "pause_reason": "error",
            "error_text": (
                "provider_payload_fault: submit_final_result 的参数块被上游网关序列化错位，"
                "截尾残片连续第 2 次相同 | 这一跳模型=deepseek-flash 尾部残片=success</｜DSML｜:"
            ),
            "retry_attempt": 1, "retry_cap": 5, "retry_escalated": False,
        }],
    )
    text = str(_field(lane, "event_bundle_text"))
    assert "Failure class: provider_payload_fault" in text
    assert "非节点提交不合格" in text
    # 别把两个类别的处置互相污染：这一类不该被读成"降思考档位"。
    assert "思考强度" not in text


def test_node_error_bundle_marks_send_preflight_fault_class() -> None:
    """发送前预检拒发的暂停：类名说清"这一跳没发出去"，两个观测值由正文给。"""
    from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane

    lane = build_heartbeat_prompt_lane(
        provider_model="",
        stable_rules_text="rules",
        events=[{
            "reason": "task_node_error", "task_id": "task:t", "node_id": "node:n",
            "pause_reason": "error",
            "error_text": (
                "send_preflight_fault: token compression helper returned an empty summary "
                "after retries (limit 3) [预估=412000 本轮发送窗口=390000] "
                "本轮发送窗口是按当跳目标组算出的链级数字"
            ),
            "retry_attempt": 1, "retry_cap": 5, "retry_escalated": False,
        }],
    )
    text = str(_field(lane, "event_bundle_text"))
    assert "Failure class: send_preflight_fault" in text
    assert "没有发出请求" in text
    assert "预估=412000" in text
    # 这一类同样不该被读成"重启 worker 就好"。
    assert "重启 worker" not in text


def test_failure_class_line_carries_no_action_sentences() -> None:
    """`Failure class` 只许描述形状：处置写两遍就会出现"正文改了、类名还在断言"的那份假事实。

    实盘反例：同一节点在一类被裁的跳之后又成功跑了 5 跳，而类名一直断言 `resume 无效`。
    唯一例外是 `runtime_fault`：它的正文是异常原话，不带任何处置线索。
    """
    from g3ku.heartbeat.prompt_lane import _runtime_fault_class

    for marker in (
        "model_config_fault: whatever [output-capped]",
        "window_integrity_fault: whatever [window-clamped]",
        "send_preflight_fault: whatever [预估=1]",
    ):
        line = _runtime_fault_class(marker)
        assert line, marker
        assert "处置见 Error 正文" in line or "成因见 Error 正文" in line, line
        for banned in ("resume", "无效", "重启", "请先", "必须", "建议"):
            assert banned not in line, f"{marker} → {line}"
    assert "重启 worker" in _runtime_fault_class("runtime_fault:filesystem_stat:NameError: x")


def test_shutdown_resume_wake_does_not_render_as_a_completed_task() -> None:
    """停机重启的唤醒落在终态渲染那一步，模型读到的是"Task task () completed"——
    接着做上一条被打断的请求被换成了宣布任务收工。"""
    from pathlib import Path

    from g3ku.heartbeat.prompt_lane import build_heartbeat_prompt_lane

    lane = build_heartbeat_prompt_lane(
        provider_model="openai:gpt-4.1",
        stable_rules_text="Keep the user informed.",
        events=[{"reason": "shutdown_resume", "session_id": "web:shared"}],
    )

    bundle = str(_field(lane, "event_bundle_text") or "")
    assert "resumed automatically after the project process restarted" in bundle
    assert "Complete the user's previously interrupted request now." in bundle
    assert "completed" not in bundle.lower().replace("resume", "")

    # 这句文案只有一个作者：提示词构造那一路必须调用同一份 helper。
    service = (
        Path(__file__).resolve().parents[2] / "g3ku" / "heartbeat" / "session_service.py"
    ).read_text(encoding="utf-8")
    assert "shutdown_resume_lines()" in service
    assert "Session resumed automatically" not in service
