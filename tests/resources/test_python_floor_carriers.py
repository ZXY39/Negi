"""Python 版本下限是一条跨载体合同：声明、lint 目标、安装钉版、镜像必须同一个数。

`requires-python` 曾写 >=3.11，而 shipped 代码里存在 PEP 701 同类引号 f-string（3.12 才允许），
`.python-version` 与安装器又钉 3.12——三方各说各话时，只有按镜像/系统解释器跑 3.11 的那条道
在 import 阶段就 SyntaxError，而静态审查看不出这是"哪一侧的承诺作废"。下限定在 3.12，
所以这里钉四个载体同值。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FLOOR = "3.12"


def _pyproject() -> dict:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_all_python_floor_carriers_declare_the_same_version() -> None:
    data = _pyproject()

    requires = str(data["project"]["requires-python"])
    floor = re.fullmatch(r">=\s*(\d+\.\d+)", requires).group(1)
    assert floor == FLOOR, requires

    target = str(data["tool"]["ruff"]["target-version"])
    assert target == f"py{floor.replace('.', '')}", target

    pin = (REPO_ROOT / ".python-version").read_text(encoding="utf-8").strip()
    assert pin == floor, pin

    first_from = ""
    for line in (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines():
        if line.strip().upper().startswith("FROM "):
            first_from = line.strip()
            break
    assert first_from == f"FROM python:{floor}-slim", first_from
