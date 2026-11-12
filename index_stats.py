"""读索引目录，数出里面到底有什么。

为什么值得单独一个模块：这个函数被三处用到——启动时的 `/readyz`、
上传之后刷新统计、以及排查"到底插进去了没有"。之前写在 app/main.py 里，
上传功能要用就得再复制一遍，复制之后必然有一处忘改。

这里只读文件，不起 LightRAG 实例：数一份索引的规模不该付出加载模型的代价。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def count_chunks(tag: str, root: Path | None = None) -> int:
    """索引里已落盘的 chunk 数。上传前后各读一次，差值就是本次新增。"""
    base = (root or ROOT) / "rag_storage" / tag
    data = _load_json(base / "kv_store_text_chunks.json")
    return len(data) if isinstance(data, dict) else 0


def chunks_by_file(tag: str, root: Path | None = None) -> dict[str, int]:
    """每个文件各占多少块，用于"这次上传进了多少块"和排查切块异常。"""
    base = (root or ROOT) / "rag_storage" / tag
    data = _load_json(base / "kv_store_text_chunks.json")
    if not isinstance(data, dict):
        return {}
    out: dict[str, int] = {}
    for v in data.values():
        if not isinstance(v, dict):
            continue
        fp = v.get("file_path") or "unknown_source"
        out[fp] = out.get(fp, 0) + 1
    return out


def documents_of(tag: str, root: Path | None = None) -> list[dict]:
    """当前索引里的文档清单（给上传面板显示"语料里现在有什么"）。

    只读 doc_status，按字数倒序——演示时一眼能看出刚上传的那篇排到哪。
    """
    base = (root or ROOT) / "rag_storage" / tag
    data = _load_json(base / "kv_store_doc_status.json")
    if not isinstance(data, dict):
        return []
    out = []
    for doc_id, v in data.items():
        if not isinstance(v, dict):
            continue
        out.append({
            "doc_id": doc_id,
            "file_path": v.get("file_path") or "unknown_source",
            "chars": v.get("content_length") or 0,
            "status": v.get("status") or "unknown",
        })
    out.sort(key=lambda d: d["chars"], reverse=True)
    return out


def index_stats(tag: str, root: Path | None = None) -> dict:
    """返回 {documents, chunks, chars, entities, relations}。

    /readyz 应该回答"索引里到底有什么"，而不是"上一次查询召回了几个块"——
    后者在第一次提问前恒为 0，会让人以为索引没加载。
    """
    base = (root or ROOT) / "rag_storage" / tag

    docs = _load_json(base / "kv_store_doc_status.json")
    chunks = _load_json(base / "kv_store_text_chunks.json")
    chunk_chars = sum(len((v or {}).get("content") or "") for v in chunks.values()) \
        if isinstance(chunks, dict) else 0
    # 字数用 doc_status 的 content_length 累加，**不要**用 chunk 原文累加：
    # 块之间有 80 token 重叠，累加会多出 13%（100,842 → 114,523），
    # 两个数字都对，但含义不同——前者是语料规模，后者是索引体积。
    # 早先 README 写的 100,864 是"每篇标题行也算进去"的版本，已按 doc_status 更正。
    doc_chars = sum((v or {}).get("content_length") or 0 for v in docs.values()) \
        if isinstance(docs, dict) else 0
    stats = {
        "documents": len(docs) if isinstance(docs, dict) else 0,
        "chunks": len(chunks) if isinstance(chunks, dict) else 0,
        "chars": doc_chars or chunk_chars,
        "chunk_chars": chunk_chars,
        "entities": 0,
        "relations": 0,
    }
    g = base / "graph_chunk_entity_relation.graphml"
    if g.exists():
        try:
            txt = g.read_text(encoding="utf-8")
            stats["entities"] = len(re.findall(r"<node ", txt))
            stats["relations"] = len(re.findall(r"<edge ", txt))
        except Exception:
            pass
    return stats


if __name__ == "__main__":
    import sys

    print(json.dumps(index_stats(sys.argv[1] if len(sys.argv) > 1 else "prod"),
                     ensure_ascii=False, indent=2))
