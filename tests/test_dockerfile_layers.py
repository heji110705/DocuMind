"""Dockerfile 图层顺序的守门测试。

为什么值得单独测：图层顺序错了不会报错，只会**变慢**——而且慢得很隐蔽。
2026-11-17 把 ``requirements-lcel.txt`` 那一层插在两个模型预热层之前，
每次重建都要重新下载 600MB 的 reranker，实测卡了 12 分钟，还被我误判成"网络问题"。
Docker 的规则是：任何一层变了，**它之后的所有层全部失效**。所以新增小层
必须放在大缓存层（模型预热）之后，而不是紧跟在 requirements 后面。

这条测试不跑 docker，只读文件——在 CI 的 unit job 里几毫秒就过。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILES = [ROOT / "Dockerfile", ROOT / "deploy" / "Dockerfile.vps"]

# 预热层的判定：这些字符串出现在某条 RUN 里，说明它在下载模型
WARMUP_MARKERS = ("TextEmbedding(", "TextCrossEncoder(")


def _instructions(path: Path) -> list[str]:
    """把 Dockerfile 拆成逻辑指令（合并反斜杠续行，去掉注释与空行）。"""
    out: list[str] = []
    buf: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not buf and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if line.lstrip().startswith("#"):
            continue
        buf.append(line)
        if line.endswith("\\"):
            continue
        out.append("\n".join(buf))
        buf = []
    if buf:
        out.append("\n".join(buf))
    return out


def _index_of(insts: list[str], needle: str) -> int:
    for i, ins in enumerate(insts):
        if needle in ins:
            return i
    return -1


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda p: p.name)
def test_lcel_layer_comes_after_model_warmup(path: Path) -> None:
    assert path.exists(), f"{path} 不存在"
    insts = _instructions(path)

    lcel = _index_of(insts, "requirements-lcel.txt")
    assert lcel >= 0, f"{path.name} 里找不到 requirements-lcel.txt"

    warmups = [
        i for i, ins in enumerate(insts)
        if ins.startswith("RUN") and any(m in ins for m in WARMUP_MARKERS)
    ]
    assert warmups, f"{path.name} 里没有模型预热层——那运行时会联网下载，首问卡几十秒"

    assert lcel > max(warmups), (
        f"{path.name}: requirements-lcel.txt 在第 {lcel + 1} 条指令，"
        f"而模型预热层在 {[w + 1 for w in warmups]}。"
        "新增依赖层必须在预热层之后，否则预热缓存全部失效（实测多花 12 分钟）。"
    )


@pytest.mark.parametrize("path", DOCKERFILES, ids=lambda p: p.name)
def test_heavy_requirements_stay_before_warmup(path: Path) -> None:
    """反向约束：requirements.txt 本身就该在预热之前（预热要用它装的包）。"""
    insts = _instructions(path)
    heavy = _index_of(insts, "pip install --prefix=/install -r requirements.txt")
    warmups = [
        i for i, ins in enumerate(insts)
        if ins.startswith("RUN") and any(m in ins for m in WARMUP_MARKERS)
    ]
    assert heavy >= 0
    assert heavy < min(warmups), (
        f"{path.name}: requirements.txt 必须在预热之前，否则预热时没有 fastembed 可用"
    )


def test_langchain_core_is_declared_in_its_own_file() -> None:
    """langchain-core 只能待在 requirements-lcel.txt 里。

    写进 requirements.txt 会让 lightrag 那一层（装十几分钟）整体失效；
    而完全不声明又不行——镜像里确实没有它（本地有是 lightrag 的传递依赖）。
    """
    heavy = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "langchain" not in heavy, (
        "requirements.txt 里不该出现 langchain —— 它会拖垮重依赖层的缓存，"
        "请写进 requirements-lcel.txt"
    )

    lcel = (ROOT / "requirements-lcel.txt").read_text(encoding="utf-8")
    assert "langchain-core" in lcel
