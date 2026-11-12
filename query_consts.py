"""检索相关的常量，单独放一个文件。

为什么要单独拆：`query.py` 顶部 import 了 lightrag / fastembed，
谁 import 它谁就得先把这几个重依赖装好。而"问题最长 500 字""默认 top_k 是 10"
这类边界，本质是纯数据，不该被 2GB 的依赖绑架。
拆出来之后，HTTP 边界校验（app/params.py）与 CI 里的单元测试都只依赖标准库，
不需要为了跑一个断言去拉 lightrag。

`query.py` 从本模块 import 这些名字并继续对外暴露，保证老代码里的
`from query import DEFAULT_TOP_K` 不会失效。
"""

from __future__ import annotations

# 允许的检索模式。LightRAG 内部只有这几种，传错值会抛异常并冒成 500，
# 所以在 HTTP 边界就要挡住（见 app/params.py）。
MODES = ("naive", "local", "global", "hybrid", "mix")

# 检索预算的默认值。这三个数字必须只有一处定义：
# 之前 CLI 用 10、HTTP 用 20、非流式用 6，同一个问题在三个入口召回的证据不一样，
# README 上的准确率也就没法用界面复现。
DEFAULT_TOP_K = 10
DEFAULT_CHUNK_TOP_K = 6

MAX_QUESTION = 500

REFUSAL_TEXT = "证据不足：文档中未提及相关内容。"
REFUSAL_HINTS = ("证据不足", "未提及", "未提到", "没有提到", "无法回答", "无法从材料", "材料中没有")
