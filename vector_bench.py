"""pgvector 索引选型 bench：HNSW vs IVFFlat，独立脚本，不接主链路。

为什么独立：LightRAG 1.5.7 没有 pgvector 后端（kg/ 下只有 nano/faiss/milvus/
neo4j/mongo/memgraph），所以只能把向量捞出来单独灌进 pgvector 做对比。
这不影响结论的可比性——比的是「同一批向量、同一批查询，两种索引谁更接近精确答案」。

两个子命令
    dump-queries   把问句编码成向量，写出 JSON（需要 fastembed，在 documind 容器里跑）
    bench          读 chunks + queries，驱动 psql 建表建索引、跑查询、出报表（纯标准库）

recall 的口径（重要）
    金标 eval_set_v2.json 里**没有 chunk id**，只有 must_include 关键词组，
    所以「金标 chunk 是否进 top10」判不了。这里用 ANN 领域的标准定义：
    **真值 = 关掉索引后精确全表扫描的 top-10**，recall@10 = |交集| / 10。
    这个真值可复现、不依赖人工标注，也是所有向量库文档里 recall 的默认含义。
    代价是它衡量的是"近似索引对精确结果的还原度"，不衡量"检索对不对"。

用法
    # 1) 生成查询向量（在容器里，有 fastembed）
    python vector_bench.py dump-queries --eval eval_set_v2.json --out /tmp/queries.json

    # 2) 跑 bench（在宿主机，psql 通过 docker exec 进容器）
    python vector_bench.py bench --chunks rag_storage/prod/vdb_chunks.json \\
        --queries /tmp/queries.json --runs 20 \\
        --psql-cmd "docker exec -i pgbench psql -q -U postgres -d bench"
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import statistics
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

CHUNK_ID_RE = re.compile(r"^\S+-chunk-\d+$")


# --------------------------------------------------------------------- 数据加载


def load_chunks(path: Path) -> list[dict]:
    """读 LightRAG 的 nano-vectordb 文件，解出 (id, content, vector)。

    存储格式是 base64 → zlib → **float16**。别猜成 float32：
    解错不会报错，只会让所有向量变成 NaN 式的垃圾，recall 变成随机数。
    （512 维 × float16 = 1024 字节，实测每块正好这个长度。）
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    rows = raw.get("data") if isinstance(raw, dict) else raw
    out = []
    for r in rows:
        blob = zlib.decompress(base64.b64decode(r["vector"]))
        vec = list(struct.unpack(f"<{len(blob) // 2}e", blob))
        out.append(
            {
                "id": r["__id__"],
                "content": r.get("content") or "",
                "file_path": r.get("file_path") or "",
                "vector": vec,
            }
        )
    if not out:
        raise SystemExit(f"{path} 里没有向量")
    dims = {len(c["vector"]) for c in out}
    if len(dims) != 1:
        raise SystemExit(f"向量维度不一致：{sorted(dims)}")
    return out


def load_queries(path: Path) -> list[dict]:
    """读 dump-queries 的产物：[{id, q, vector: [...]}, ...]。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("items") if isinstance(data, dict) else data
    return [{"id": q["id"], "q": q.get("q", ""), "vector": q["vector"]} for q in rows]


# --------------------------------------------------------------------- psql 驱动


class Psql:
    """把 SQL 灌给 psql，拿回 stdout。

    为什么不直接用 psycopg：宿主机上没有它，pgvector 容器里也只有 psql。
    为了一个 bench 在两个环境各装一套驱动不值当，而且装驱动本身就意味着
    "这条结论依赖一套额外环境"。走 psql 则只要有 docker 就能复现。
    """

    def __init__(self, cmd: str) -> None:
        self.cmd = cmd

    # 每次调用都是新会话，pset 不会继承。所以统一在这里加前缀——
    # 上一版只在 run_queries 里设了 pset，scalar() 拿回来的是
    # "count \n-------\n 122\n(1 row)" 这种表格文本，int() 直接炸。
    _PRELUDE = "\\pset format unaligned\n\\pset tuples_only on\n"

    def run(self, sql: str, *, timeout: int = 600) -> str:
        proc = subprocess.run(
            self.cmd.split(),
            input=self._PRELUDE + sql,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode != 0:
            raise SystemExit(f"psql 失败（{proc.returncode}）：\n{proc.stderr[-2000:]}")
        return proc.stdout

    def scalar(self, sql: str) -> str:
        return self.run(sql).strip()


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"


def setup_schema(psql: Psql, chunks: list[dict], dim: int) -> None:
    """建表并灌入向量。带 __id__ 主键，避免重复导入时叠数据。"""
    stmts = [
        "SET client_min_messages=warning;",
        "DROP TABLE IF EXISTS chunks;",
        f"CREATE TABLE chunks (id text PRIMARY KEY, embedding vector({dim}));",
    ]
    for c in chunks:
        stmts.append(
            f"INSERT INTO chunks (id, embedding) VALUES ('{c['id']}', "
            f"'{_vec_literal(c['vector'])}');"
        )
    psql.run("\n".join(stmts))
    n = psql.scalar("SELECT count(*) FROM chunks;")
    if int(n) != len(chunks):
        raise SystemExit(f"灌入条数不对：表里 {n}，源 {len(chunks)}")


def build_index(psql: Psql, kind: str) -> tuple[str, float]:
    """建索引，返回 (索引名, 建索引耗时秒)。

    注意：DDL 不能包在 EXPLAIN ANALYZE 里（上一版那么写过，psql 直接报语法错）。
    计时只能在外面做——用 Python 侧的 perf_counter，误差在毫秒级，
    对"建索引几秒 vs 几毫秒"这个量级的比较完全够用。
    """
    psql.run("DROP INDEX IF EXISTS chunks_hnsw; DROP INDEX IF EXISTS chunks_ivf;")
    if kind == "hnsw":
        ddl = (
            "CREATE INDEX chunks_hnsw ON chunks USING hnsw (embedding vector_cosine_ops) "
            "WITH (m=16, ef_construction=200);"
        )
        name = "chunks_hnsw"
    elif kind == "ivfflat":
        ddl = (
            "CREATE INDEX chunks_ivf ON chunks USING ivfflat (embedding vector_cosine_ops) "
            "WITH (lists=100);"
        )
        name = "chunks_ivf"
    else:
        raise SystemExit(f"未知索引类型：{kind}")

    started = time.perf_counter()
    psql.run("SET client_min_messages=warning;\n" + ddl)
    return name, time.perf_counter() - started


def index_sizes(psql: Psql) -> dict[str, str]:
    out = psql.scalar(
        "SELECT COALESCE(string_agg(relname || '=' || pg_size_pretty(pg_relation_size(oid)), '; '), '') "
        "FROM pg_class WHERE relname IN ('chunks_hnsw','chunks_ivf','chunks');"
    )
    sizes: dict[str, str] = {}
    for part in out.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            sizes[k] = v
    return sizes


def run_queries(
    psql: Psql,
    queries: list[dict],
    *,
    setting: str,
    force_exact: bool,
) -> str:
    """生成并执行一轮查询，返回原始输出供解析。

    force_exact 时把索引路径全关掉——122 行这种小表，优化器默认就走 Seq Scan，
    哪怕建了 HNSW 也不会用它。所以测索引必须显式 enable_seqscan=off，
    测真值必须显式把 indexscan/bitmapscan 关掉。不显式设置就会得到
    "两次都跑了全表扫描"的假数据。
    """
    lines = [
        "\\pset format unaligned",
        "\\pset tuples_only on",
        "SET client_min_messages=warning;",
    ]
    if force_exact:
        lines += [
            "SET enable_indexscan=off;",
            "SET enable_bitmapscan=off;",
            "SET enable_indexonlyscan=off;",
            "SET enable_seqscan=on;",
        ]
    else:
        lines += ["SET enable_seqscan=off;", "SET enable_bitmapscan=off;"]
        if setting:
            lines.append(setting)

    for q in queries:
        vec = _vec_literal(q["vector"])
        lines.append(f"\\echo ==={q['id']}")
        lines.append(
            f"EXPLAIN (ANALYZE, TIMING OFF) SELECT id FROM chunks "
            f"ORDER BY embedding <=> '{vec}' LIMIT 10;"
        )
        lines.append(
            f"SELECT id FROM chunks ORDER BY embedding <=> '{vec}' LIMIT 10;"
        )
    return psql.run("\n".join(lines) + "\n")


def parse_run(raw: str) -> dict[str, dict]:
    """按 ===QID 切组，取出 [top10 ids] 与 EXPLAIN 末行的 Execution Time。"""
    groups: dict[str, dict] = {}
    cur: str | None = None
    seen_time = False
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("==="):
            cur = s[3:].strip()
            groups[cur] = {"ids": [], "ms": None, "used_index": False, "plan": []}
            seen_time = False
            continue
        if cur is None:
            continue
        if s.startswith("Execution Time:"):
            try:
                groups[cur]["ms"] = float(s.split(":", 1)[1].replace("ms", "").strip())
            except ValueError:
                pass
            seen_time = True
            continue
        if not s:
            continue
        if not seen_time:
            # EXPLAIN 的计划行
            groups[cur]["plan"].append(s)
            if "Index Scan" in s:
                groups[cur]["used_index"] = True
            continue
        if CHUNK_ID_RE.match(s):
            groups[cur]["ids"].append(s)
    return groups


# --------------------------------------------------------------------- 主流程


def cmd_bench(args: argparse.Namespace) -> None:
    chunks = load_chunks(Path(args.chunks))
    queries = load_queries(Path(args.queries))[: args.limit or None]
    dim = len(chunks[0]["vector"])
    print(f"块 {len(chunks)} × {dim} 维 | 查询 {len(queries)} 条 | 每项 {args.runs} 轮")

    psql = Psql(args.psql_cmd)
    setup_schema(psql, chunks, dim)

    # ---- 真值：精确扫描。确定性操作，跑 3 轮只为确认稳定，不需 20 轮
    truth_rounds = []
    for _ in range(3):
        truth_rounds.append(parse_run(run_queries(psql, queries, setting="", force_exact=True)))
    truth = {
        qid: set(r["ids"])
        for qid, r in truth_rounds[0].items()
    }
    truth_stable = all(
        {k: set(v["ids"]) for k, v in rd.items()} == truth for rd in truth_rounds[1:]
    )
    exact_ms = [
        r["ms"] for rd in truth_rounds for r in rd.values() if r["ms"] is not None
    ]
    print(f"  真值：{len(truth)} 条查询，三轮一致={truth_stable}，"
          f"p50={statistics.median(exact_ms):.3f}ms")

    results = [
        {
            "index": "精确扫描（真值基线）",
            "n": len(exact_ms),
            "recall": 1.0,
            "p50": statistics.median(exact_ms),
            "p99": _pct(exact_ms, 99),
            "size": index_sizes(psql).get("chunks", "?"),
            "used_index": False,
        }
    ]

    for kind, setting, label in (
        ("hnsw", "SET hnsw.ef_search=40;", "HNSW (m=16, ef_construction=200, ef_search=40)"),
        ("ivfflat", "SET ivfflat.probes=10;", "IVFFlat (lists=100, probes=10)"),
    ):
        name, build_s = build_index(psql, kind)
        print(f"  {kind}: 建索引 {build_s * 1000:.0f} ms")
        sizes = index_sizes(psql)
        rounds = []
        for i in range(args.runs):
            rounds.append(parse_run(run_queries(psql, queries, setting=setting, force_exact=False)))
            if i == 0:
                used = sum(1 for r in rounds[-1].values() if r["used_index"])
                print(f"  {kind}: 第一轮 {used}/{len(rounds[-1])} 条走了索引")
                if used == 0:
                    print("    ⚠ 一条都没走索引 —— 这组数字是全表扫描，不能当索引结论")

        recalls, p50s, p99s, all_ms = [], [], [], []
        for rd in rounds:
            per_q = []
            for qid, r in rd.items():
                gt = truth.get(qid) or set()
                if not gt:
                    continue
                per_q.append(len(gt & set(r["ids"])) / len(gt))
            if per_q:
                recalls.append(sum(per_q) / len(per_q))
            ms = [r["ms"] for r in rd.values() if r["ms"] is not None]
            if ms:
                all_ms.extend(ms)
                p50s.append(statistics.median(ms))
                p99s.append(_pct(ms, 99))

        results.append(
            {
                "index": label,
                "n": len(all_ms),
                "recall": statistics.median(recalls) if recalls else 0.0,
                # 用户要的是"各跑 20 次取中位"：这里对每轮的 p99 取中位，
                # 而不是把 2480 个样本混在一起算——后者会把轮间抖动抹平。
                "p50": statistics.median(p50s) if p50s else 0.0,
                "p99": statistics.median(p99s) if p99s else 0.0,
                "size": sizes.get(name, "?"),
                "used_index": True,
                "build_ms": round(build_s * 1000),
            }
        )

    print()
    print("| 索引 | 样本 | recall@10 | p50 (ms) | P99 (ms) | 索引体积 |")
    print("|---|---|---|---|---|---|")
    for r in results:
        print(
            f"| {r['index']} | {r['n']} | {r['recall']:.3f} | "
            f"{r['p50']:.3f} | {r['p99']:.3f} | {r['size']} |"
        )

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nJSON → {args.json_out}")


def _pct(samples: list[float], p: float) -> float:
    if not samples:
        return 0.0
    xs = sorted(samples)
    k = max(0, min(len(xs) - 1, int(round((p / 100) * (len(xs) - 1)))))
    return xs[k]


def cmd_dump_queries(args: argparse.Namespace) -> None:
    """把问句编码成向量。需要 fastembed，所以这一步必须在 documind 容器里跑。"""
    # 脚本可能是被 docker cp 到 /tmp 再执行的：这时 __file__ 的父目录不是项目根，
    # 必须把 cwd（-w /app）也加进 sys.path，否则 embedders 找不到。
    for cand in (str(Path(__file__).resolve().parent), os.getcwd()):
        if cand not in sys.path:
            sys.path.insert(0, cand)
    from embedders import build_embedder  # noqa: PLC0415  重依赖，只在需要时导入

    items = json.loads(Path(args.eval).read_text(encoding="utf-8"))["items"]
    if args.only_answerable:
        items = [q for q in items if q.get("type") == "fact"]
    texts = [q["q"] for q in items]

    fn, dim, name = build_embedder("local")
    import asyncio  # noqa: PLC0415

    vectors = asyncio.run(fn(texts))
    out = {
        "model": name,
        "dim": dim,
        "items": [
            {"id": q["id"], "q": q["q"], "vector": [float(x) for x in v]}
            for q, v in zip(items, vectors)
        ],
    }
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"{len(items)} 条查询向量 → {args.out}（模型 {name}，{dim} 维）")


def main() -> None:
    ap = argparse.ArgumentParser(description="pgvector 索引选型 bench")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("dump-queries", help="编码问句（需 fastembed）")
    d.add_argument("--eval", default="eval_set_v2.json")
    d.add_argument("--out", required=True)
    d.add_argument("--only-answerable", action="store_true")
    d.set_defaults(func=cmd_dump_queries)

    b = sub.add_parser("bench", help="建表建索引并测 recall/延迟")
    b.add_argument("--chunks", default="rag_storage/prod/vdb_chunks.json")
    b.add_argument("--queries", required=True)
    b.add_argument("--runs", type=int, default=20)
    b.add_argument("--limit", type=int, default=0)
    b.add_argument(
        "--psql-cmd",
        default="docker exec -i pgbench psql -q -U postgres -d bench",
    )
    b.add_argument("--json-out", default="")
    b.set_defaults(func=cmd_bench)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
