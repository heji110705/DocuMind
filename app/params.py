"""请求参数的唯一校验入口。

为什么要单独一个模块（而不是留在 app/main.py 里）：
`app/main.py` 一 import 就会连带加载 lightrag / fastembed / onnxruntime，
光导入就要几秒，CI 里为了测几个纯函数去装 2GB 依赖不划算。
把校验摘出来之后，`tests/test_params.py` 只依赖标准库，任何机器上都能跑，
这也让"参数边界"这一层真正可以被 CI 守住。

设计原则：
1. **坏输入在边界变成 400，不往下传。** 以前 mode 直接透传给 LightRAG，
   传个 "bogus" 就在深栈里抛异常，curl 看到的是 Internal Server Error。
2. **默认值只在这里定义一次。** 界面的"快速/高精度"、评测的五组配置、
   CLI 的参数都从这里取，避免同一个问题在三个入口召回不同的证据。
3. **不做业务判断。** 这里只回答"这个请求合不合法"，不碰索引、不碰模型。
"""

from __future__ import annotations

from typing import Any

from query_consts import DEFAULT_CHUNK_TOP_K, DEFAULT_TOP_K, MODES, MAX_QUESTION

# 多轮对话最多回带几轮。3 轮足够覆盖"它 / 那个字段 / 上面说的第二种情况"这类指代，
# 再多只是把成本和延迟堆在"模型已经知道的前文"上。
MAX_HISTORY_TURNS = 3
# 每轮历史答案只保留前 N 字。完整的上一轮答案动辄上千字，塞进改写提示词里
# 会挤掉真正有用的对话锚点（问的是什么、答的是哪个对象）。
MAX_HISTORY_ANSWER = 400


class BadRequest(ValueError):
    """带错误码的 400，避免把用户输入问题冒成 500。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _int_in(name: str, value: Any, lo: int, hi: int, default: int) -> int:
    if value is None:
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise BadRequest("E2004", f"{name} 必须是整数")
    if not lo <= n <= hi:
        raise BadRequest("E2004", f"{name} 必须在 {lo}~{hi} 之间")
    return n


def _as_bool(name: str, value: Any, default: bool = False) -> bool:
    """严格解析布尔开关。

    之前直接写 `bool(body.get("enable_rerank", False))`，而 `bool("false") is True`——
    前端或 curl 传字符串 "false" 会**打开**重排，一次请求从 1 秒变 2 分钟，
    现象是"我只改了个字符串怎么突然卡住了"。字符串在这里是合法输入，必须认真解析。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off", ""):
            return False
    raise BadRequest("E2006", f"{name} 必须是布尔值（true/false）")


def read_history(value: Any) -> list[dict]:
    """把客户端传来的历史收敛成 `[{"q": str, "a": str}]`，最多 MAX_HISTORY_TURNS 轮。

    只接受最后 3 轮：多轮对话的成本主要花在改写提示词上，历史越长越容易
    把不相关的旧话题带进来，反而降低改写质量。
    形状不对就整段丢弃而不是报错——历史只是"提示"，不该让一次提问失败，
    但要在响应里如实告诉调用方 `history_used`，别让人以为它生效了。
    """
    if not value:
        return []
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for item in value[-MAX_HISTORY_TURNS:]:
        if not isinstance(item, dict):
            continue
        q = item.get("q", item.get("question"))
        a = item.get("a", item.get("answer"))
        if not isinstance(q, str) or not q.strip():
            continue
        out.append({
            "q": q.strip()[:MAX_QUESTION],
            "a": (a if isinstance(a, str) else "").strip()[:MAX_HISTORY_ANSWER],
        })
    return out


def read_params(body: Any) -> dict:
    """把请求体收敛成一个可信的参数集合。"""
    if not isinstance(body, dict):
        raise BadRequest("E2005", "请求体必须是 JSON 对象")
    raw_q = body.get("question")
    # 传数字/数组进来时 .strip() 会抛 AttributeError 冒成 500，
    # 这类"类型不对"的输入和"内容不对"一样，都该是 400。
    if raw_q is not None and not isinstance(raw_q, str):
        raise BadRequest("E2005", "question 必须是字符串")
    q = (raw_q or "").strip()
    if not q:
        raise BadRequest("E2001", "问题为空")
    if len(q) > MAX_QUESTION:
        raise BadRequest("E2002", f"问题过长（上限 {MAX_QUESTION} 字，当前 {len(q)} 字）")
    mode = str(body.get("mode") or "mix").lower()
    if mode not in MODES:
        raise BadRequest("E2003", f"不支持的 mode：{mode}，可选 {'/'.join(MODES)}")
    return {
        "question": q,
        "mode": mode,
        "top_k": _int_in("top_k", body.get("top_k"), 1, 60, DEFAULT_TOP_K),
        "chunk_top_k": _int_in("chunk_top_k", body.get("chunk_top_k"), 1, 20, DEFAULT_CHUNK_TOP_K),
        # 默认不重排：一条 curl 等两分钟会被当成服务挂了。
        # 要 90.4% 那档精度就显式传 enable_rerank=true，界面的"高精度"就是这么调的。
        "rerank": _as_bool("enable_rerank", body.get("enable_rerank"), False),
        "history": read_history(body.get("history")),
    }
