"""提示词与回答后处理：全是纯函数，不碰索引、不碰模型。

为什么从 query.py 里拆出来：
这些函数决定"拒答还是作答""引用编号对不对""多轮改写有没有跑偏"，
是最容易改坏、又最难靠人眼发现的地方，所以最需要单测。
而 query.py 顶部 import 了 lightrag / fastembed / onnxruntime——
为了测一个字符串函数去装 2GB 依赖，CI 会慢到没人愿意等，最后就是不测。
拆开之后，`tests/test_judge.py` / `test_prompt.py` 只依赖标准库。
"""

from __future__ import annotations

import re

from query_consts import REFUSAL_HINTS, REFUSAL_TEXT

SYSTEM_PROMPT = (
    "你是严谨的文档问答助手。以下规则没有例外：\n"
    "1. 只能使用【证据】中的内容作答，不得引入外部知识或自行推断。\n"
    "2. 每个事实句末尾必须标注来源编号，格式为 [1]、[2]，可多个。\n"
    "3. 只有当【证据】完全没有涉及问题所问的对象时，才输出这一句："
    + REFUSAL_TEXT
    + "\n"
    "   证据里有部分相关信息时，就把能确认的部分答出来并标注来源，不要因为不完整而整段拒答。\n"
    "4. 回答用中文，先给结论再给依据，不要复述问题。\n"
    "5. 如果给了你一句锚点原文，它只是用来帮你定位，不要把它整段抄进答案；"
    "答案要说人话，不要让同一句话出现两次。"
)

# 为什么是"先摘录、再判定"而不是直接输出一个标签：
# 实测发现直接判标签时，证据 1 万多字、答案埋在表格里，模型倾向于整体印象判 INSUFFICIENT，
# 同一题重复跑还会给出不同结论。强制先逐字抄一句原文，等于让它先定位再表态，
# 判定既稳定又可人工复核（摘录原文能直接看出它凭什么判）。
JUDGE_PROMPT = (
    "你在做 RAG 的证据审核。必须按下面两步作答，不要做别的解释。\n\n"
    "第一步 摘录：从【证据】里逐字抄一句能直接回答问题所问的原文（最长两句）。"
    "表格整行、列表项、括号里的数字都可以抄，抄的时候连同上文的对象名一起抄，"
    "确保单看这一句就能对上问题。证据里确实没有，就只写 NONE。\n"
    "第二步 判定：摘录到了写 SUFFICIENT，摘录不到写 INSUFFICIENT。\n\n"
    "只输出这两行：\n"
    "摘录：<原文或 NONE>\n"
    "判定：<SUFFICIENT 或 INSUFFICIENT>\n\n"
    "【问题】{question}\n\n【证据】\n{evidence}"
)

# 单轮检索在多轮场景下会直接失效：用户第二句问"那它的默认值是多少"，
# 向量化之后是一个没有主语的句子，召回的永远是"默认值"这三个字最像的块。
# 做法是先把这一轮问题改写成独立问题（指代消解），再走原来那套检索。
#
# 为什么不让作答模型直接看历史：
# 1. 作答提示词里只有【证据】，把历史塞进去等于给它一条绕开证据的口子，
#    模型会用上一轮的答案回答这一轮的问题——正是我们要防的幻觉。
# 2. 改写是一次 max_tokens=200 的小调用（约 ¥0.0002），比把上千字历史
#    塞进作答提示词便宜一个数量级。
CONDENSE_PROMPT = (
    "把【本轮问题】改写成一句不依赖对话上下文的独立问题。\n"
    "规则：\n"
    "1. 只改写，不回答；不得引入对话里没出现过的新信息。\n"
    "2. 把「它」「这个」「那个」「上面说的」这类指代替换成对话里明确出现的对象，"
    "但**不要**把上一轮的问题合并进来——改完问的仍然是本轮问的那件事。\n"
    "3. 本轮问题本身已经独立，就原样返回。\n"
    "4. 只输出改写后的那一句，不要解释，不要加引号。\n\n"
    "【对话】\n{history}\n\n【本轮问题】{question}"
)

MAX_REWRITE = 200


def parse_verdict(raw: str) -> tuple[bool, str]:
    """从裁判输出里取 (证据是否足够, 摘录原文)。

    判定标签缺失时按"不足"处理（宁可拒答，不要编）；
    判定与摘录自相矛盾时（说 SUFFICIENT 却摘不出原文）也按"不足"处理。
    """
    text = raw or ""
    m = re.search(r"判定[：:\s]*\s*(INSUFFICIENT|SUFFICIENT)", text, re.I)
    if m:
        verdict = m.group(1).upper()
    elif re.search(r"\bINSUFFICIENT\b", text.upper()):
        verdict = "INSUFFICIENT"
    elif re.search(r"\bSUFFICIENT\b", text.upper()):
        verdict = "SUFFICIENT"
    else:
        verdict = "INSUFFICIENT"

    m2 = re.search(r"摘录[：:]\s*([\s\S]*)", text)
    quote = m2.group(1).strip() if m2 else ""
    quote = re.split(r"判定[：:]", quote)[0].strip()
    if verdict == "SUFFICIENT" and (not quote or quote.upper().startswith("NONE")):
        return False, quote
    return verdict == "SUFFICIENT", quote


def evidence_block(chunks: list[dict]) -> str:
    """证据编号从 1 开始，与答案里的 [n] 一一对应。"""
    lines = []
    for i, c in enumerate(chunks, 1):
        src = c.get("file_path") or "文档"
        body = (c.get("content") or "").strip()
        lines.append(f"[{i}] 来源：{src}\n{body}")
    return "\n\n".join(lines)


def build_answer_prompt(question: str, chunks: list[dict], quote: str = "") -> tuple[str, str]:
    """拼作答提示词，返回 (user_prompt, system_prompt)。

    裁判判"足够"时手上已经有一句能对上问题的原文，把它当锚点交给作答模型：
    否则它要在上万字证据里自己再找一遍，实测会偶发判自己"证据不足"而整段拒答。

    流式与非流式共用这一个函数，避免"两个入口拼出两种提示词"这种低级漂移。
    """
    hint = ""
    if quote and not quote.upper().startswith("NONE"):
        hint = "\n\n【与问题直接相关的原文】\n" + quote
    return question, SYSTEM_PROMPT + hint + "\n\n【证据】\n" + evidence_block(chunks)


def parse_citations(text: str, n_chunks: int) -> list[int]:
    """取出答案里出现过的引用编号，越界的丢掉（模型偶尔会写 [9]）。"""
    seen: list[int] = []
    for m in re.finditer(r"\[(\d{1,2})\]", text):
        k = int(m.group(1))
        if 1 <= k <= n_chunks and k not in seen:
            seen.append(k)
    return seen


def is_refusal(text: str) -> bool:
    if not text:
        return True
    if text.strip().startswith(REFUSAL_TEXT[:5]):
        return True
    head = text[:60]
    return any(h in head for h in REFUSAL_HINTS)


def build_history_block(history: list[dict]) -> str:
    """把历史渲染成提示词片段。答案只取前 400 字：多轮的价值在"在聊哪个对象"，
    不在"上一轮答得多完整"。"""
    lines = []
    for h in history:
        lines.append(f"问：{h.get('q', '')}")
        if h.get("a"):
            lines.append(f"答：{h['a']}")
    return "\n".join(lines)


def clean_rewrite(raw: str, fallback: str) -> str:
    """清洗改写结果：去引号、取第一行、超长就退回原问题。

    改写模型偶尔会顺手回答一遍（"它指的是 dim_book，共 42,180 行"），
    也会加书名号或把整段解释都吐出来。这里只接受"一句干净的问题"，
    任何不像问题的输出都退回原句——退回的代价是这一轮退化回单轮，
    而接受一句错误的改写会让检索跑到完全无关的地方。
    """
    text = (raw or "").strip().strip("“”\"'`")
    if not text:
        return fallback
    text = text.split("\n")[0].strip()
    text = re.sub(r"^(改写后的问题|独立问题|改写|问题)[：:]\s*", "", text)
    if not text or len(text) > MAX_REWRITE or len(text) < 2:
        return fallback
    if len(text) > 120 and not text.endswith(("。", "？", "?")):
        return fallback
    return text


# 兼容旧名字：`from query import _parse_citations` 这类调用仍然可用，
# 但真实定义只在这里一处。
_parse_citations = parse_citations
_evidence_block = evidence_block
