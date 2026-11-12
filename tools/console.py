"""抓浏览器控制台：为什么"页面正常、但什么都不发生"。

这个脚本存在的理由
------------------
前端的失败模式几乎从不表现为"报错页面"，而是**静默**：
    - 某个 ES 模块加载失败 → 整个 <script type="module"> 不执行 → 页面是静态壳
    - 某个 await 抛了异常但没人 catch → 后续初始化全跳过 → 徽标停在初始文字
    - 接口 200 但字段名对不上 → 界面上那个位置永远显示 "…"

肉眼只能看到"没反应"，而"没反应"的四种原因修法完全不同：MIME、模块路径、
异常、字段名。所以必须把 console 与 pageerror 抓出来，而不是去猜。

用法：
    python tools/console.py http://127.0.0.1:8000/ 8
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.request

import websockets

from tools.shot import PORT, ensure_browser


async def run(url: str, wait_s: int) -> int:
    tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list").read())
    page = next(t for t in tabs if t["type"] == "page")
    logs: list[str] = []

    async with websockets.connect(page["webSocketDebuggerUrl"], max_size=40 * 1024 * 1024) as ws:
        mid = 0

        async def call(method, params=None):
            nonlocal mid
            mid += 1
            await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == mid:
                    return msg.get("result", {})

        async def drain():
            """把等待期间收到的非响应消息（事件）记下来。"""
            try:
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.2)
                    msg = json.loads(raw)
                    m = msg.get("method")
                    if m == "Runtime.consoleAPICalled":
                        p = msg["params"]
                        text = " ".join(
                            str(a.get("value", a.get("description", "")))
                            for a in p.get("args", [])
                        )
                        logs.append(f"[console.{p['type']}] {text}")
                    elif m == "Runtime.exceptionThrown":
                        d = msg["params"]["exceptionDetails"]
                        desc = (d.get("exception") or {}).get("description") or d.get("text", "")
                        logs.append(f"[exception] {desc}")
                    elif m == "Log.entryAdded":
                        e = msg["params"]["entry"]
                        logs.append(f"[log.{e['level']}] {e['text']}  ({e.get('url','')})")
                    elif m == "Network.loadingFailed":
                        p = msg["params"]
                        logs.append(f"[net-fail] {p.get('errorText')} {p.get('type')}")
            except asyncio.TimeoutError:
                pass

        await call("Runtime.enable")
        await call("Log.enable")
        await call("Network.enable")
        await call("Page.enable")
        await call("Network.setCacheDisabled", {"cacheDisabled": True})
        await call("Page.navigate", {"url": url})

        deadline = time.time() + wait_s
        while time.time() < deadline:
            await drain()

        # 抓页面当前的可观测状态，和日志对照着看
        state = await call("Runtime.evaluate", {
            "expression": """JSON.stringify({
                badge: (document.getElementById('badge')||{}).textContent,
                meta: (document.getElementById('meta')||{}).textContent,
                modules: [...document.querySelectorAll('script[type=module]')].map(s=>s.src),
                corpusHidden: (document.getElementById('corpus')||{}).hidden,
            })""",
            "returnByValue": True,
        })

    print(f"—— 抓了 {len(logs)} 条 ——")
    seen = set()
    for line in logs:
        if line in seen:      # 同一条错误常被重复报几次
            continue
        seen.add(line)
        print(line)
    print("\n—— DOM 状态 ——")
    print(state.get("result", {}).get("value"))
    return 0 if not any("exception" in x or "net-fail" in x for x in logs) else 2


if __name__ == "__main__":
    proc = ensure_browser()
    try:
        raise SystemExit(asyncio.run(run(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 8)))
    finally:
        if proc:
            proc.terminate()
