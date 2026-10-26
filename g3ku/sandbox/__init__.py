"""执行面档位策略（信任分级 × 安装态）。

模块入口只暴露 ``policy``；沙箱后端（P1）会作为同级子模块加进来，届时
``g3ku/sandbox/manager.py`` 等仍由本包统一导出策略判据，避免前门与节点各自判档。
"""

from __future__ import annotations

from g3ku.sandbox.policy import SessionPolicy, session_policy

__all__ = ["SessionPolicy", "session_policy"]
