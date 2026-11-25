"""DocuMind HTTP 服务：SSE 流式问答 + 单页 UI + 增量入库 + 多租户。

设计取舍：
1. 流式不是"把最终答案切片再吐出去"，而是真的边生成边推。
   首 token 时间因此从 9s 降到 4s 左右——这是流式唯一真正的价值。
2. 检索 / 改写 / 判定 / 拼提示词都复用 query.DocuMind 的方法，不在这里重写一遍。
   第一版是复制过来的，结果流式用 top_k=20、非流式用 6、评测用 10，
   同一个问题三个入口召回的证据都不一样，README 的准确率没法用界面复现。
3. 参数校验在 app/params.py，鉴权与配额在 app/auth.py，租户实例在 app/tenancy.py，
   文件解析在 app/ingest.py。这个文件只负责"把 HTTP 接到这些东西上"。
   之前所有逻辑堆在这里，改一处要读 400 行，测试还得先把 lightrag 装好。

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

from fastapi import FastAPI, Request, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from app import mime  # noqa: E402
from app.auth import AuthError, Gate, Tenant, parse_keys, resolve  # noqa: E402
from app.billing import bill as _bill  # noqa: E402
from app.limits import build_from_env as _build_limiter  # noqa: E402
from app.ingest import MAX_BYTES, IngestError, parse_document, save_bytes  # noqa: E402
from app.params import BadRequest, read_params  # noqa: E402
from chunk_dedup import dedupe, threshold_from_env as dedup_threshold  # noqa: E402
from context_budget import apply_budget, budget_from_env  # noqa: E402
from app.upload import UploadTooLarge, read_upload  # noqa: E402
from app.tenancy import TenantPool  # noqa: E402
from app.net import client_ip as _client_ip  # noqa: E402
from index_stats import chunks_by_file, documents_of  # noqa: E402
from llm_client import (LLMAuthError, LLMError, LLMUnavailable, Usage,  # noqa: E402
                       llm_stream, provider_state_line, track_usage)
from query import (REFUSAL_TEXT, DocuMind, _parse_citations,  # noqa: E402
                   _repair_citations, build_answer_prompt, is_refusal)

# 静态资源的 MIME 必须在挂载 StaticFiles 之前注册好（见 app/mime.py）
mime.register()

TAG = os.environ.get("TAG", "prod")
STATE: dict = {}

STATIC = Path(__file__).parent / "static"

# 兼容旧调用点（测试里 import 的是这两个名字）
_read_params = read_params


def _err(code: str, status: int, message: str, headers: dict | None = None, **extra) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message, **extra}},
                        status_code=status, headers=headers)


def _llm_error_response(e: LLMError) -> JSONResponse:
    """把 LLM 的失败翻成对外的状态码。

    401/403 是**我们这边**的配置问题（key 错/被停），对访客说"模型不可用"
    会让人以为是上游抖动，所以这类单独给 500 和一句能转给运维的话；
    429/5xx/超时是上游暂时不可用，给 503 + Retry-After，让客户端知道
    30 秒后再来，而不是立刻重试把上游再打一遍。
    """
    if isinstance(e, LLMAuthError):
        print(f"[llm] 认证失败：{e}", flush=True)
        return _err(e.code, 500, "模型认证失败，请联系管理员")
    if isinstance(e, LLMUnavailable):
        print(f"[llm] 上游不可用：{e}", flush=True)
        return _err(e.code, 503, "模型暂时不可用，请 30 秒后重试",
                    headers={"Retry-After": str(getattr(e, "retry_after", 30))})
    # 400 这类：请求本身有问题，不是上游宕机，别用 503 把人引到"等一会儿再试"。
    print(f"[llm] 请求被拒：{e}", flush=True)
    return _err(e.code, 500, "模型调用失败，请检查服务端配置")


# ------------------------------------------------------------------- 多租户
def _tenant(request: Request) -> Tenant:
    return resolve(
        request.headers.get("x-api-key"), STATE.get("keys") or {}, STATE["base_tag"],
        default_concurrency=int(os.environ.get("OPEN_CONCURRENCY", "4")),
    )


def _gate(t: Tenant) -> Gate:
    gates: dict[str, Gate] = STATE.setdefault("gates", {})
    g = gates.get(t.tid)
    if g is None or g.tenant.index_tag != t.index_tag:
        g = gates[t.tid] = Gate(tenant=t)
    return g


def _pool() -> TenantPool:
    return STATE["pool"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    base_tag = TAG
    spec = os.environ.get("API_KEYS", "").strip()
    try:
        tenants = parse_keys(spec, base_tag)
    except ValueError as e:
        # 配置错误在启动时就炸掉，别等到第一个请求进来才 500。
        raise SystemExit(f"[FAIL] API_KEYS 配置有误：{e}")
    STATE["keys"] = tenants
    STATE["base_tag"] = base_tag
    # 防刷闸门。配了 API_KEYS 时它仍然生效，但意义不大——有鉴权的场景
    # 由租户配额负责；它的主场是"无鉴权开放演示"，那种场景没人能靠 key 挡。
    STATE["limiter"] = _build_limiter()
    _lim = STATE["limiter"].snapshot()
    print(f"[limit] 每 IP {_lim['per_min'] or '不限'} 次/分、"
          f"{_lim['per_day'] or '不限'} 次/日；全站 {_lim['daily_total'] or '不限'} 次/日")

    # 主用模型 + 备胎状态。放在加载模型之前打，日志顺序才和启动顺序一致；
    # 也保证"failover 关着"这件事在任何请求进来之前就已经可见（不是静默的）。
    print(provider_state_line())

    pool = TenantPool(
        embed="local",
        use_rerank=True,
        capacity=int(os.environ.get("TENANT_POOL", "3")),
        create_if_missing=True,
    )
    STATE["pool"] = pool

    # 预热默认租户：模型加载放在启动阶段，第一次提问才不会卡 40 秒。
    default = Tenant(tid="default", index_tag=base_tag)
    dm = await pool.get(default)
    STATE["index"] = dm.stats()
    st = STATE["index"]
    mode = f"多租户（{len(tenants)} 个 key）" if tenants else "本地开放模式（未配置 API_KEYS）"
    print(f"[ready] {mode}，索引 tag={base_tag}：{st['documents']} 篇 / {st['chunks']} 块 / "
          f"{st['chars']:,} 字 / {st['entities']} 实体 / {st['relations']} 关系")
    yield
    await pool.close_all()


# 自动文档默认关闭：/docs 会把全部接口列在公网上（含 /v1/documents 这类写接口），
# 对公开演示等于一张"这里还能这么调"的地图。需要时 DOCS=1 打开。
_DOCS_ON = os.environ.get("DOCS", "0").strip() == "1"

app = FastAPI(
    title="DocuMind",
    lifespan=lifespan,
    docs_url="/docs" if _DOCS_ON else None,
    redoc_url=None,
    openapi_url="/openapi.json" if _DOCS_ON else None,
)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def _open_mode_limit(request: Request, call_next):
    """开放演示模式的防刷闸门。只拦问句接口，不拦静态资源和健康检查。

    为什么放在中间件而不是每个路由里：三个问句入口（/v1/ask、/v1/ask/stream、
    以及带鉴权的同路径）共用同一套限制，写在路由里必然漏一个。
    为什么只匹配 "/v1/ask" 前缀：/healthz、/readyz、静态页都必须无限制，
    否则监控告警和首屏加载会被自己限流。
    """
    limiter = STATE.get("limiter")
    if limiter is not None and request.method == "POST" \
            and request.url.path.startswith("/v1/ask"):
        ip = _client_ip(request)
        ok, wait, why = limiter.check(ip)
        if not ok:
            return JSONResponse(
                {"error": {"code": "E4290", "message": why, "retry_after": wait}},
                status_code=429,
                headers={"Retry-After": str(wait)},
            )
        response = await call_next(request)
        # 只有真正被处理了才记账：参数校验失败（400）不该吃掉访客的额度。
        # 这样也与 InsightPilot 的限流口径一致，两边不再各说各话。
        if response.status_code < 400:
            limiter.hit(ip)
        return response
    return await call_next(request)


@app.exception_handler(AuthError)
async def _auth_handler(_: Request, e: AuthError) -> JSONResponse:
    headers = {"WWW-Authenticate": "X-API-Key"} if e.status == 401 else None
    if e.retry_after:
        headers = {**(headers or {}), "Retry-After": str(e.retry_after)}
    return JSONResponse({"error": {"code": e.code, "message": e.message}},
                        status_code=e.status, headers=headers)


@app.exception_handler(IngestError)
async def _ingest_handler(_: Request, e: IngestError) -> JSONResponse:
    return _err(e.code, e.status, e.message)



@app.middleware("http")
async def no_store_static(request: Request, call_next):
    """前端是免构建的 ES 模块，模块之间互相 import。

    这里用 no-store 而不是 no-cache：no-cache 只是"允许缓存但每次要回源校验"，
    而 304 响应不带 content-type，浏览器会把**旧的** MIME 类型一直沿用下去。
    实测踩过一次：服务端已经改成 text/javascript，浏览器还在用缓存的 text/plain，
    于是模块死活不被执行，页面上表现为"静态 HTML 正常、所有按钮都没反应"。
    这几个文件加起来几十 KB，每次重下比这种问题便宜得多。
    """
    resp = await call_next(request)
    if request.url.path.startswith("/static") or request.url.path == "/":
        resp.headers["Cache-Control"] = "no-store"
    return resp


# --------------------------------------------------------------------- 探活
@app.get("/healthz")
async def healthz():
    """给容器探活用。只回答"进程还在"，不碰索引——探活不该有副作用。"""
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(request: Request):
    """回答"我这一份索引里有什么"。按租户回答，不泄露别人的语料规模。"""
    try:
        t = _tenant(request)
    except AuthError as e:
        return _err(e.code, e.status, e.message)
    try:
        dm = await _pool().get(t)
    except RuntimeError as e:
        return _err("E2102", 409, str(e))
    body = {"ready": True, "index": t.index_tag, "tenant": t.tid, **dm.stats()}
    g = _gate(t)
    body["quota"] = g.snapshot()
    return body


@app.get("/v1/config")
async def front_config():
    """给界面用的能力清单。

    为什么不写在前端里：UI 要知道"这个实例要不要 API Key、能不能上传"，
    写死的话换一个部署就得改 JS。这里由服务端回答，前端只负责按答案显示。
    不需要鉴权——它只暴露"能力"，不暴露任何语料内容。
    """
    return {
        "auth_required": bool(STATE.get("keys")),
        "allow_upload": os.environ.get("ALLOW_UPLOAD", "1") != "0",
        "max_upload_mb": 20,
        "max_question": 500,
        "history_turns": 3,
        "index": TAG,
    }


@app.get("/v1/documents")
async def list_documents(request: Request):
    """当前租户已经入库的文档清单。上传面板用它显示"语料里现在有什么"。"""
    t = _tenant(request)
    by_file = chunks_by_file(t.index_tag)
    items = documents_of(t.index_tag)
    for it in items:
        it["chunks"] = by_file.get(it["file_path"], 0)
    return {"tenant": t.tid, "items": items}


@app.get("/v1/eval")
async def eval_summary(request: Request):
    """把五组消融的汇总结论吐给界面，省得演示时切到 README 去翻表。

    读不到文件就返回空列表并说明原因，不假装有数据。
    """
    _tenant(request)  # 仍然要求过鉴权：这个接口虽然只读，但不该匿名可用
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
                "citations": r.get("citation_rate"),
                "p50_ms": r["p50_ms"],
                "cost_per_q": r.get("cost_per_q"),
            }
            for r in rows
        ],
    }


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


# ------------------------------------------------------------------- 增量入库
@app.post("/v1/documents")
async def upload_document(request: Request, file: UploadFile):
    """上传一篇文档 → 解析 → 增量入库 → 立刻可被检索。

    这是唯一会改索引的接口，所以它同时受三件事约束：
    鉴权（哪个租户）、配额（还能不能干）、锁（和检索串行）。
    """
    t = _tenant(request)
    if os.environ.get("ALLOW_UPLOAD", "1") == "0":
        # 容器里索引是只读挂载（compose 的 :ro），此时写入会失败但报错很难懂。
        # 显式关掉上传，给出能看懂的原因。
        return _err("E2210", 403, "该实例关闭了上传（ALLOW_UPLOAD=0），索引按只读挂载")
    # 声明长度明显超限时，连请求体都不读，直接 413（多留 8KB 给 multipart 头）
    declared_len = request.headers.get("content-length", "")
    if declared_len.isdigit() and int(declared_len) > MAX_BYTES + 8192:
        raise IngestError("E2203", 413, f"文件超过 {MAX_BYTES // 1024 // 1024}MB 上限")

    gate = _gate(t)
    gate.acquire()
    t0 = time.perf_counter()
    try:
        try:
            data = await read_upload(file, MAX_BYTES)
        except UploadTooLarge:
            raise IngestError("E2203", 413, f"文件超过 {MAX_BYTES // 1024 // 1024}MB 上限") from None
        path = save_bytes(t.index_tag, file.filename or "", data)
        doc = parse_document(path, data, backend=os.environ.get("PDF_BACKEND", "auto"),
                             name=file.filename or "")
        async with _pool().lease(t) as dm:
            res = await dm.ingest(doc.text, doc.display_name)
        STATE["index"] = dm.stats() if t.tid == "default" else STATE.get("index", {})
        body = {
            **res.as_dict(),
            "backend": doc.backend,
            "notes": doc.notes,
            "index": dm.stats(),
            "tenant": t.tid,
            "elapsed_ms": int((time.perf_counter() - t0) * 1000),
        }
        print(f"[ingest] tenant={t.tid} {doc.display_name} {doc.chars}字 "
              f"→ +{res.chunks_added} 块 ({res.elapsed_ms}ms ¥{res.cost:.4f})", flush=True)
        return body
    finally:
        gate.release()


@app.delete("/v1/documents/{doc_id}")
async def delete_document(doc_id: str, request: Request):
    """删掉一篇文档。上传是单向门的话，传错一次就只能重建索引。"""
    t = _tenant(request)
    if os.environ.get("ALLOW_UPLOAD", "1") == "0":
        return _err("E2210", 403, "该实例关闭了写入（ALLOW_UPLOAD=0）")
    gate = _gate(t)
    gate.acquire()
    try:
        async with _pool().lease(t) as dm:
            res = await dm.delete(doc_id)
        print(f"[delete] tenant={t.tid} {doc_id} -{res['chunks_removed']} 块", flush=True)
        return {**res, "index": (await _pool().get(t)).stats(), "tenant": t.tid}
    finally:
        gate.release()


# --------------------------------------------------------------------- 问答
def _sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.post("/v1/ask/stream")
async def ask_stream(body: dict, request: Request):
    try:
        p = _read_params(body)
        t = _tenant(request)
    except BadRequest as e:
        return _err(e.code, 400, e.message)

    gate = _gate(t)
    gate.acquire()
    question = p["question"]

    async def work(q: asyncio.Queue) -> None:
        t0 = time.perf_counter()
        trace_id = "t_" + uuid.uuid4().hex[:8]
        n_chunks = 0
        with track_usage() as used:
            try:
                async with _pool().lease(t) as dm:
                    # 改写要先于检索：多轮里"那它的默认值呢"没有主语，
                    # 直接向量化召回的是"默认值"字面最像的块，不是同一个对象。
                    await q.put(("stage", {"name": "rewrite", "status": "start",
                                           "skip": not p["history"]}))
                    standalone = await dm.condense(question, p["history"])
                    await q.put(("stage", {"name": "rewrite", "status": "end",
                                           "ms": int((time.perf_counter() - t0) * 1000)}))
                    await q.put(("meta", {"trace_id": trace_id, "mode": p["mode"],
                                          "rerank": p["rerank"], "question": question,
                                          "standalone": standalone,
                                          "history_used": len(p["history"]),
                                          "tenant": t.tid}))

                    await q.put(("stage", {"name": "retrieve", "status": "start"}))
                    chunks, used_rerank = await dm.retrieve(
                        standalone, mode=p["mode"], rerank=p["rerank"],
                        top_k=p["top_k"], chunk_top_k=p["chunk_top_k"],
                    )
                    # 与非流式同一条规则：编号之前处理（先去重再裁预算），
                    # 保证 [n] 与返回的 chunks 对齐
                    chunks, dedup_stats = dedupe(chunks, dedup_threshold())
                    chunks, budget_stats = apply_budget(chunks, budget_from_env())
                    n_chunks = len(chunks)
                    await q.put(("stage", {"name": "retrieve", "status": "end",
                                           "ms": int((time.perf_counter() - t0) * 1000),
                                           "n_chunks": n_chunks, "rerank": used_rerank,
                                           "budget": budget_stats, "dedup": dedup_stats}))

                    if not chunks:
                        await q.put(("token", {"text": REFUSAL_TEXT}))
                        await q.put(("done", {"refused": True, "reason": "no_evidence",
                                              **_bill(used, t0)}))
                        return

                    await q.put(("stage", {"name": "judge", "status": "start"}))
                    t1 = time.perf_counter()
                    enough, quote = await dm.judge(standalone, chunks)
                    await q.put(("stage", {"name": "judge", "status": "end",
                                           "ms": int((time.perf_counter() - t1) * 1000)}))
                    if not enough:
                        await q.put(("token", {"text": REFUSAL_TEXT}))
                        await q.put(("done", {"refused": True, "reason": "insufficient",
                                              "quote": quote, **_bill(used, t0)}))
                        return

                    prompt, system = build_answer_prompt(standalone, chunks, quote)
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

                    cites, oob, repaired = _repair_citations(
                        text, _parse_citations(text, len(chunks)), len(chunks)
                    )
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
            except AuthError as e:
                await q.put(("error", {"code": e.code, "message": e.message}))
                await q.put(("done", {"refused": True, "reason": "auth", **_bill(used, t0)}))
            except LLMUnavailable as e:
                # 流已经开始了（HTTP 200 早就发出去了），改不了状态码，所以把 503
                # 放进 error 事件里。**必须先发 error 再 close**：前端靠它判断
                # "不是没答案，是模型暂时不可用"，否则会一直转圈等 token。
                await q.put(("error", {"code": e.code, "status": 503,
                                       "retry_after": e.retry_after,
                                       "message": "模型暂时不可用，请 30 秒后重试"}))
                await q.put(("done", {"refused": True, "reason": "unavailable",
                                      **_bill(used, t0)}))
            except LLMAuthError as e:
                await q.put(("error", {"code": e.code, "status": 500,
                                       "message": "模型认证失败，请联系管理员"}))
                await q.put(("done", {"refused": True, "reason": "auth_error",
                                      **_bill(used, t0)}))
            except LLMError as e:
                # 这一层兜住"上游拒绝请求"（400 之类）：不是宕机，别让前端去等 30 秒。
                await q.put(("error", {"code": e.code, "status": 500,
                                       "message": "模型调用失败，请检查服务端配置"}))
                await q.put(("done", {"refused": True, "reason": "llm_error",
                                      **_bill(used, t0)}))
            except Exception as e:  # 任何异常都要落到 error 事件，否则前端一直转
                await q.put(("error", {"code": "E2050", "message": f"{type(e).__name__}: {e}"}))
                await q.put(("done", {"refused": True, "reason": "error", **_bill(used, t0)}))
                print(f"[error] trace={trace_id} {type(e).__name__}: {e}", flush=True)
            finally:
                # 一条请求一行日志：演示时能直接在终端指着它讲"这次多少钱、卡在哪一段"。
                print(f"[ask] trace={trace_id} tenant={t.tid} mode={p['mode']} "
                      f"rerank={p['rerank']} hist={len(p['history'])} chunks={n_chunks} "
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
            gate.release()

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/v1/ask")
async def ask(body: dict, request: Request):
    """非流式版本，评测脚本和 curl 用这个。"""
    try:
        p = _read_params(body)
        t = _tenant(request)
    except BadRequest as e:
        return _err(e.code, 400, e.message)
    gate = _gate(t)
    gate.acquire()
    try:
        async with _pool().lease(t) as dm:
            a = await dm.ask(p["question"], mode=p["mode"], rerank=p["rerank"], guard=True,
                             top_k=p["top_k"], chunk_top_k=p["chunk_top_k"],
                             history=p["history"])
    except LLMError as e:
        # 模型侧的失败用状态码表达，200 + 空答案会让调用方以为"拒答了"。
        return _llm_error_response(e)
    finally:
        gate.release()
    return {
        "answer": a.answer,
        "refused": a.refused,
        "citations": a.citations,
        "citation_oob": a.extra.get("citation_oob", 0),
        "citation_repair": a.extra.get("citation_repair", False),
        "model_used": a.extra.get("model_used", ""),
        "chunks": [{"n": i, "file_path": c.get("file_path"), "snippet": (c.get("content") or "")[:600]}
                   for i, c in enumerate(a.chunks, 1) if i in a.citations],
        "standalone": a.extra.get("standalone"),
        "history_used": a.extra.get("history_used", 0),
        "elapsed_ms": a.latency_ms,
        "cost_cny": round(a.cost, 6),
        "tokens": a.tokens,
        "rerank": a.rerank,
        "tenant": t.tid,
    }
