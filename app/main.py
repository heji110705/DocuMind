"""DocuMind HTTP 服务：SSE 流式问答 + 一个单页 UI。

设计取舍：
1. 流式不是"把最终答案切片再吐出去"，而是真的边生成边推。
   首 token 时间因此从 9s 降到 4s 左右——这是流式唯一真正的价值。
2. 事件类型与 14-接口与SSE参考.md 一一对应。文档不是事后补的，
   是照着这份实现写的（或者反过来，总之两边必须一致）。
3. 不做鉴权。本地演示用，加一个假 token 反而让人以为它安全。

起服务：
    python -m uvicorn app.main:app --port 8000
"""

from __future__ import annotations

import asyncio
import json
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

from llm_client import USAGE, llm_func, llm_stream  # noqa: E402
from query import (JUDGE_PROMPT, REFUSAL_TEXT, SYSTEM_PROMPT,  # noqa: E402
                   DocuMind, _evidence_block, _parse_citations, is_refusal,
                   parse_verdict)

TAG = "prod"
STATE: dict = {}


def _index_stats(tag: str) -> dict:
    """启动时数一遍索引规模，供 /readyz 用。

    /readyz 应该回答"索引里到底有什么"，而不是"上一次查询召回了几个块"——
    后者在第一次提问前恒为 0，会让人以为索引没加载。
    """
    import json
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
    STATE["dm"] = dm
    STATE["lock"] = asyncio.Lock()   # ONNX 会话不是线程安全的，串行化更省心
    STATE["index"] = _index_stats(TAG)
    st = STATE["index"]
    print(f"[ready] 索引 tag={TAG}：{st['documents']} 篇 / {st['chunks']} 块 / "
          f"{st['entities']} 实体 / {st['relations']} 关系")
    yield
    await dm.close()


app = FastAPI(title="DocuMind", lifespan=lifespan)

STATIC = Path(__file__).parent / "static"


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "ocr": "pdf-native"}


@app.get("/readyz")
async def readyz():
    ok = "dm" in STATE
    body = {"ready": ok, "index": TAG, **STATE.get("index", {})}
    return JSONResponse(body, status_code=200 if ok else 503)


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.post("/v1/ask/stream")
async def ask_stream(body: dict):
    question = (body.get("question") or "").strip()
    if not question:
        return JSONResponse({"error": {"code": "E1001", "message": "问题为空"}}, status_code=400)
    if len(question) > 500:
        return JSONResponse({"error": {"code": "E1002", "message": "问题过长"}}, status_code=400)

    mode = body.get("mode", "mix")
    top_k = int(body.get("top_k", 6))
    use_rerank = bool(body.get("enable_rerank", True))
    dm: DocuMind = STATE["dm"]
    lock: asyncio.Lock = STATE["lock"]

    async def work(q: asyncio.Queue) -> None:
        t0 = time.perf_counter()
        trace_id = "t_" + uuid.uuid4().hex[:8]
        c0, p0, q0 = USAGE.calls, USAGE.prompt_tokens, USAGE.completion_tokens
        try:
            await q.put(("meta", {"trace_id": trace_id, "mode": mode,
                                  "rerank": use_rerank, "question": question}))

            await q.put(("stage", {"name": "retrieve", "status": "start"}))
            async with lock:
                from lightrag.base import QueryParam

                data = await dm.rag.aquery_data(
                    question,
                    param=QueryParam(mode=mode, top_k=20, chunk_top_k=top_k,
                                     enable_rerank=use_rerank, include_references=True),
                )
            payload = (data or {}).get("data") or {}
            chunks = payload.get("chunks") or []
            STATE["n_chunks"] = len(chunks)
            await q.put(("stage", {"name": "retrieve", "status": "end",
                                   "ms": int((time.perf_counter() - t0) * 1000),
                                   "n_chunks": len(chunks)}))

            if not chunks:
                await q.put(("token", {"text": REFUSAL_TEXT}))
                await q.put(("done", {"refused": True, "reason": "no_evidence", **_bill(c0, p0, q0, t0)}))
                return

            evidence = _evidence_block(chunks)

            await q.put(("stage", {"name": "judge", "status": "start"}))
            t1 = time.perf_counter()
            raw = await llm_func(JUDGE_PROMPT.format(question=question, evidence=evidence),
                                 max_tokens=400, temperature=0)
            enough, quote = parse_verdict(raw)
            await q.put(("stage", {"name": "judge", "status": "end",
                                   "ms": int((time.perf_counter() - t1) * 1000)}))
            if not enough:
                await q.put(("token", {"text": REFUSAL_TEXT}))
                await q.put(("done", {"refused": True, "reason": "insufficient",
                                      "quote": quote, **_bill(c0, p0, q0, t0)}))
                return

            hint = ""
            if quote and not quote.upper().startswith("NONE"):
                hint = "\n\n【与问题直接相关的原文】\n" + quote

            await q.put(("stage", {"name": "answer", "status": "start"}))
            buf = []
            async for piece in llm_stream(
                question,
                system_prompt=SYSTEM_PROMPT + hint + "\n\n【证据】\n" + evidence,
                temperature=0,
            ):
                if isinstance(piece, dict) and "__usage__" in piece:
                    continue
                buf.append(piece)
                await q.put(("token", {"text": piece}))
            text = "".join(buf).strip()

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
                                  "quote": quote, **_bill(c0, p0, q0, t0)}))
        except Exception as e:  # 任何异常都要落到 error 事件，否则前端一直转
            await q.put(("error", {"code": "E1050", "message": f"{type(e).__name__}: {e}"}))
            await q.put(("done", {"refused": True, "reason": "error", **_bill(c0, p0, q0, t0)}))
        finally:
            await q.put(None)

    def _bill(c0: int, p0: int, q0: int, t0: float) -> dict:
        return {
            "cost_cny": round(USAGE.cost, 6),
            "tokens_in": USAGE.prompt_tokens - p0,
            "tokens_out": USAGE.completion_tokens - q0,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            "calls": USAGE.calls - c0,
        }

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
                task.cancel()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/v1/ask")
async def ask(body: dict):
    """非流式版本，评测脚本和 curl 用这个。"""
    question = (body.get("question") or "").strip()
    if not question:
        return JSONResponse({"error": {"code": "E1001", "message": "问题为空"}}, status_code=400)
    dm: DocuMind = STATE["dm"]
    async with STATE["lock"]:
        a = await dm.ask(question, mode=body.get("mode", "mix"),
                         rerank=bool(body.get("enable_rerank", True)), guard=True,
                         top_k=int(body.get("top_k", 6)))
    return {
        "answer": a.answer,
        "refused": a.refused,
        "citations": a.citations,
        "chunks": [{"n": i, "file_path": c.get("file_path"), "snippet": (c.get("content") or "")[:600]}
                   for i, c in enumerate(a.chunks, 1) if i in a.citations],
        "elapsed_ms": a.latency_ms,
        "cost_cny": round(a.cost, 6) if a.cost else None,
        "tokens": a.tokens,
    }
