"""DocuMind HTTP 服务：SSE 流式问答 + 一个单页 UI。

设计取舍：
1. 流式不是"把最终答案切片再吐出去"，而是真的边生成边推。
   首 token 时间因此从 9s 降到 4s 左右——这是流式唯一真正的价值。
2. 检索 / 判定 / 拼提示词三件事都复用 query.DocuMind 的方法，不在这里重写一遍。
   第一版是复制过来的，结果流式用 top_k=20、非流式用 6、评测用 10，
   同一个问题三个入口召回的证据都不一样，README 的准确率没法用界面复现。
3. 不做鉴权。本地演示用，加一个假 token 反而让人以为它安全。

起服务：
    python -m uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse  # noqa: E402

from llm_client import Usage, llm_stream, track_usage  # noqa: E402
from query import (DEFAULT_CHUNK_TOP_K, DEFAULT_TOP_K, MODES,  # noqa: E402
                   REFUSAL_TEXT, DocuMind, _parse_citations, build_answer_prompt,
                   is_refusal)

TAG = os.environ.get("TAG", "prod")
MAX_QUESTION = 500
STATE: dict = {}

STATIC = Path(__file__).parent / "static"


# ---------------------------------------------------------------- 参数校验
class BadRequest(ValueError):
    """带错误码的 400，避免把用户输入问题冒成 500。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _int_in(name: str, value, lo: int, hi: int, default: int) -> int:
    if value is None:
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise BadRequest("E1004", f"{name} 必须是整数")
    if not lo <= n <= hi:
        raise BadRequest("E1004", f"{name} 必须在 {lo}~{hi} 之间")
    return n


def _read_params(body: dict) -> dict:
    """把请求体收敛成一个可信的参数集合。

    以前 mode 是直接透传给 LightRAG 的，传个 "bogus" 就抛异常、冒成 HTTP 500，
    curl 里看到的是 "Internal Server Error"，日志里才是一坨 traceback。
    输入问题应该在边界上就变成 400。
    """
    if not isinstance(body, dict):
        raise BadRequest("E1005", "请求体必须是 JSON 对象")
    raw_q = body.get("question")
    # 传数字/数组进来时 .strip() 会抛 AttributeError 冒成 500，
    # 这类"类型不对"的输入和"内容不对"一样，都该是 400。
    if raw_q is not None and not isinstance(raw_q, str):
        raise BadRequest("E1005", "question 必须是字符串")
    q = (raw_q or "").strip()
    if not q:
        raise BadRequest("E1001", "问题为空")
    if len(q) > MAX_QUESTION:
        raise BadRequest("E1002", f"问题过长（上限 {MAX_QUESTION} 字，当前 {len(q)} 字）")
    mode = str(body.get("mode") or "mix").lower()
    if mode not in MODES:
        raise BadRequest("E1003", f"不支持的 mode：{mode}，可选 {'/'.join(MODES)}")
    return {
        "question": q,
        "mode": mode,
        "top_k": _int_in("top_k", body.get("top_k"), 1, 60, DEFAULT_TOP_K),
        "chunk_top_k": _int_in("chunk_top_k", body.get("chunk_top_k"), 1, 20, DEFAULT_CHUNK_TOP_K),
        # 默认不重排：一条 curl 等两分钟会被当成服务挂了。
        # 要 89.4% 那档精度就显式传 enable_rerank=true，界面的"高精度"就是这么调的。
        "rerank": bool(body.get("enable_rerank", False)),
    }


def _bill(used: Usage, t0: float) -> dict:
    """结算一次请求。

    这里踩过两个坑，都是"数字看着有、其实不对"的类型：
    1. cost 曾经直接返回 USAGE.cost（进程累计值），于是界面上第二问的成本是
       "第一问 + 第二问"，token 却是本问的，两个数字对不上。
    2. 改成"调用前后相减"之后，并发请求的窗口会重叠，照样串账。
    现在每个请求有独立的 Usage（见 llm_client.track_usage），直接读，不相减。
    """
    return {
        "cost_cny": round(used.cost, 6),
        "tokens_in": used.prompt_tokens,
        "tokens_out": used.completion_tokens,
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        "calls": used.calls,
    }


def _index_stats(tag: str) -> dict:
    """启动时数一遍索引规模，供 /readyz 用。

    /readyz 应该回答"索引里到底有什么"，而不是"上一次查询召回了几个块"——
    后者在第一次提问前恒为 0，会让人以为索引没加载。
    """
    import re

    base = ROOT / "rag_storage" / tag

    def _count(name: str) -> int:
        p = base / name
        if not p.exists():
            return 0
        try:
            return len(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            return 0

    stats = {
        "documents": _count("kv_store_doc_status.json"),
        "chunks": _count("kv_store_text_chunks.json"),
        "entities": 0,
        "relations": 0,
    }
    g = base / "graph_chunk_entity_relation.graphml"
    if g.exists():
        txt = g.read_text(encoding="utf-8")
        stats["entities"] = len(re.findall(r"<node ", txt))
        stats["relations"] = len(re.findall(r"<edge ", txt))
    return stats


@asynccontextmanager
async def lifespan(app: FastAPI):
    dm = await DocuMind(tag=TAG, embed="local", use_rerank=True).open()
    # 检索锁交给 DocuMind 自己持有：只有 retrieve 那一段串行（ONNX 重排是 CPU 独占），
    # 判定和作答不排队。放在这里而不是每个 endpoint 各写一遍，
    # 是因为非流式接口一旦把整个 ask 包进锁，连 LLM 调用也串行化了。
    dm.lock = asyncio.Lock()
    STATE["dm"] = dm
    STATE["index"] = _index_stats(TAG)
    st = STATE["index"]
    print(f"[ready] 索引 tag={TAG}：{st['documents']} 篇 / {st['chunks']} 块 / "
          f"{st['entities']} 实体 / {st['relations']} 关系")
    yield
    await dm.close()


app = FastAPI(title="DocuMind", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    """给容器探活用。只回答"进程还在"，不碰索引——探活不该有副作用。"""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz():
    ok = "dm" in STATE
    body = {"ready": ok, "index": TAG, **STATE.get("index", {})}
    return JSONResponse(body, status_code=200 if ok else 503)


@app.get("/v1/eval")
async def eval_summary():
    """把四组消融的汇总结论吐给界面，省得演示时切到 README 去翻表。

    读不到文件就返回空列表并说明原因，不假装有数据。
    """
    fp = ROOT / "data" / f"eval_{TAG}.json"
    if not fp.exists():
        return {"available": False, "reason": f"未找到 {fp.name}，先跑 run_eval.py", "rows": []}
    try:
        rows = json.loads(fp.read_text(encoding="utf-8"))
    except Exception as e:
        return {"available": False, "reason": f"{type(e).__name__}: {e}", "rows": []}
    order = ["naive", "mix", "mix_guard", "mix_rr", "full"]
    rows = sorted(rows, key=lambda r: order.index(r["config"]) if r["config"] in order else 99)
    return {
        "available": True,
        "index": TAG,
        "n": rows[0]["n"] if rows else 0,
        "rows": [
            {
                "config": r["config"],
                "acc_fact": r["acc_fact"],
                "refusal_acc": r["refusal_acc"],
                "overall": r["overall"],
                "p50_ms": r["p50_ms"],
                "cost_per_q": r.get("cost_per_q"),
            }
            for r in rows
        ],
    }


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _bad(e: BadRequest) -> JSONResponse:
    return JSONResponse({"error": {"code": e.code, "message": e.message}}, status_code=400)


@app.post("/v1/ask/stream")
async def ask_stream(body: dict):
    try:
        p = _read_params(body)
    except BadRequest as e:
        return _bad(e)

    question = p["question"]
    dm: DocuMind = STATE["dm"]

    async def work(q: asyncio.Queue) -> None:
        t0 = time.perf_counter()
        trace_id = "t_" + uuid.uuid4().hex[:8]
        n_chunks = 0
        with track_usage() as used:
            try:
                await q.put(("meta", {"trace_id": trace_id, "mode": p["mode"],
                                      "rerank": p["rerank"], "question": question}))

                await q.put(("stage", {"name": "retrieve", "status": "start"}))
                chunks, used_rerank = await dm.retrieve(
                    question, mode=p["mode"], rerank=p["rerank"],
                    top_k=p["top_k"], chunk_top_k=p["chunk_top_k"],
                )
                n_chunks = len(chunks)
                await q.put(("stage", {"name": "retrieve", "status": "end",
                                       "ms": int((time.perf_counter() - t0) * 1000),
                                       "n_chunks": n_chunks, "rerank": used_rerank}))

                if not chunks:
                    await q.put(("token", {"text": REFUSAL_TEXT}))
                    await q.put(("done", {"refused": True, "reason": "no_evidence",
                                          **_bill(used, t0)}))
                    return

                await q.put(("stage", {"name": "judge", "status": "start"}))
                t1 = time.perf_counter()
                enough, quote = await dm.judge(question, chunks)
                await q.put(("stage", {"name": "judge", "status": "end",
                                       "ms": int((time.perf_counter() - t1) * 1000)}))
                if not enough:
                    await q.put(("token", {"text": REFUSAL_TEXT}))
                    await q.put(("done", {"refused": True, "reason": "insufficient",
                                          "quote": quote, **_bill(used, t0)}))
                    return

                prompt, system = build_answer_prompt(question, chunks, quote)
                await q.put(("stage", {"name": "answer", "status": "start"}))
                t2 = time.perf_counter()
                buf = []
                async for piece in llm_stream(prompt, system_prompt=system, temperature=0):
                    if isinstance(piece, dict) and "__usage__" in piece:
                        continue
                    buf.append(piece)
                    await q.put(("token", {"text": piece}))
                text = "".join(buf).strip()
                # 这个 end 事件以前漏了，界面上第三个阶段永远不打勾，
                # 答案都写完了还显示"生成中"，看起来像没结束。
                await q.put(("stage", {"name": "answer", "status": "end",
                                       "ms": int((time.perf_counter() - t2) * 1000)}))

                cites = _parse_citations(text, len(chunks))
                await q.put(("citations", {"items": [
                    {
                        "n": i,
                        "file_path": chunks[i - 1].get("file_path") or "文档",
                        "snippet": (chunks[i - 1].get("content") or "")[:600],
                    }
                    for i in cites
                ]}))
                await q.put(("done", {"refused": is_refusal(text), "answer": text,
                                      "quote": quote, **_bill(used, t0)}))
            except Exception as e:  # 任何异常都要落到 error 事件，否则前端一直转
                await q.put(("error", {"code": "E1050", "message": f"{type(e).__name__}: {e}"}))
                await q.put(("done", {"refused": True, "reason": "error", **_bill(used, t0)}))
                print(f"[error] trace={trace_id} {type(e).__name__}: {e}", flush=True)
            finally:
                # 一条请求一行日志：演示时能直接在终端指着它讲"这次多少钱、卡在哪一段"。
                print(f"[ask] trace={trace_id} mode={p['mode']} rerank={p['rerank']} "
                      f"chunks={n_chunks} "
                      f"{int((time.perf_counter() - t0) * 1000)}ms calls={used.calls} "
                      f"¥{used.cost:.4f}", flush=True)
                await q.put(None)

    async def stream():
        q: asyncio.Queue = asyncio.Queue()
        task = asyncio.create_task(work(q))
        try:
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"     # 注释行，EventSource 会忽略，但能防代理空闲断连
                    continue
                if item is None:
                    break
                yield _sse(item[0], item[1])
        finally:
            if not task.done():
                # 客户端点"停止"或关掉页面时走到这里：取消任务，别让重排继续空转。
                task.cancel()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/v1/ask")
async def ask(body: dict):
    """非流式版本，评测脚本和 curl 用这个。"""
    try:
        p = _read_params(body)
    except BadRequest as e:
        return _bad(e)
    dm: DocuMind = STATE["dm"]
    a = await dm.ask(p["question"], mode=p["mode"], rerank=p["rerank"], guard=True,
                     top_k=p["top_k"], chunk_top_k=p["chunk_top_k"])
    return {
        "answer": a.answer,
        "refused": a.refused,
        "citations": a.citations,
        "chunks": [{"n": i, "file_path": c.get("file_path"), "snippet": (c.get("content") or "")[:600]}
                   for i, c in enumerate(a.chunks, 1) if i in a.citations],
        "elapsed_ms": a.latency_ms,
        "cost_cny": round(a.cost, 6),
        "tokens": a.tokens,
        "rerank": a.rerank,
    }
