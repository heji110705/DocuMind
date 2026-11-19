"""failover 默认关闭时，"关"必须在启动日志里可见。

背景：`LLM_FALLBACK_ENABLED` 默认 0，而"关着"是沉默状态——只配了
`LLM_FALLBACK_*` 三项却漏掉开关时，配置看起来齐、实际没启用，等主模型挂了
才发现。之前试过把默认改成 1 来消除这种静默，但那是反向的：备胎（qwen）
会引用越界（只有 6 块却写 `[16]`），切过去会把 `chunks` 打空，正好打穿
"引用可回溯"这条产品承诺。所以默认保持关闭，改用启动日志让它可见。

这几条测试钉住的就是这个约定：
- 三项配齐、开关不写 → 日志必须出现 `fallback=disabled`，且不 import qwen provider；
- 一项没配 → 同样是 `fallback=disabled`（不带"已配置"提示）；
- 显式打开 → 日志换成备用模型名；
- 启动路径（lifespan）确实调到它，且在主模型加载之前。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import llm_client

ROOT = Path(__file__).resolve().parent.parent

_THREE = {
    "LLM_FALLBACK_MODEL": "qwen-plus",
    "LLM_FALLBACK_API_KEY": "sk-fake",
    "LLM_FALLBACK_BASE_URL": "https://example.invalid/v1",
}


def _clear(monkeypatch) -> None:
    for key in ("LLM_FALLBACK_ENABLED", *_THREE):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    sys.modules.pop("llm_fallback", None)


def test_three_vars_without_switch_logs_disabled(monkeypatch):
    """只配三项、不写开关：日志说 disabled，并且不碰 qwen provider。"""
    _clear(monkeypatch)
    for k, v in _THREE.items():
        monkeypatch.setenv(k, v)

    line = llm_client.provider_state_line()

    assert "provider=deepseek-chat" in line
    assert "fallback=disabled" in line
    # 配了却没开，是最容易误判成"已启用"的情形，必须单独标出来
    assert "note=fallback_configured_but_disabled" in line
    assert "llm_fallback" not in sys.modules, "关着 failover，启动时不该 import qwen provider"
    assert llm_client.fallback_enabled() is False


def test_nothing_configured_logs_disabled_without_note(monkeypatch):
    _clear(monkeypatch)
    line = llm_client.provider_state_line()
    assert line == "[llm] provider=deepseek-chat fallback=disabled"


def test_enabled_logs_fallback_model(monkeypatch):
    _clear(monkeypatch)
    for k, v in _THREE.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("LLM_FALLBACK_ENABLED", "1")

    line = llm_client.provider_state_line()
    assert "fallback=qwen-plus" in line
    assert "disabled" not in line


def test_fresh_process_does_not_import_qwen(monkeypatch):
    """在干净子进程里跑一遍：这不是"同进程里恰好还没 import"，是真没加载。

    同进程断言有个漏洞——别的用例先 import 过 `llm_fallback`，`sys.modules`
    里就有了，pop 掉再断言只能证明"这次没再 import"。换新进程才排得干净。
    """
    _clear(monkeypatch)
    # 继承当前环境再覆盖，不要给最小 env：Windows 上 Python 子进程缺 SYSTEMROOT
    # 会直接起不来，报错还是 GBK 编码的（乱码难以判读）。
    import os

    env = dict(os.environ)
    env.update(_THREE)
    env["LLM_MODEL"] = "deepseek-chat"
    env.pop("LLM_FALLBACK_ENABLED", None)  # 关键前提：开关不存在
    code = (
        "import sys, llm_client;"
        "print(llm_client.provider_state_line());"
        "print('QWEN_IMPORTED=' + str('llm_fallback' in sys.modules))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert out.returncode == 0, out.stderr[-800:]
    assert "fallback=disabled" in out.stdout
    assert "QWEN_IMPORTED=False" in out.stdout, out.stdout


def test_startup_wires_the_line_before_model_load():
    """启动路径守卫：lifespan 里确实打了这行，且在主模型加载之前。

    只读源码、不启动服务（导入 app.main 会连带 lightrag，CI 的 unit job 里没有）。
    """
    src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    body = src[src.index("async def lifespan"):]
    body = body[: body.index("yield")]

    assert "provider_state_line()" in body, "lifespan 没打 provider 状态行"
    assert body.index("provider_state_line()") < body.index("pool.get("), (
        "状态行必须在加载模型之前打，否则模型加载失败时看不到 failover 是开是关"
    )
