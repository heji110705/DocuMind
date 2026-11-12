"""用 CDP 控制 Edge 截图（等到页面真的渲染完再截）。

为什么不用 `msedge --headless --screenshot`：
    它截的是"虚拟时间耗尽那一刻"的画面，而 SSE 是真实网络流。
    结果是截到一张"卡片在、答案空"的图——看起来像功能坏了，其实只是没等。

用法：
    python tools/shot.py "http://127.0.0.1:8000/?q=..." out.png 25
"""

import asyncio
import base64
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websockets

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9333


def ensure_browser():
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1)
        return None
    except Exception:
        pass
    proc = subprocess.Popen(
        [EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         f"--remote-debugging-port={PORT}",
         "--user-data-dir=" + str(Path.home() / ".edge-cdp-tmp"),
         "--window-size=1440,1300", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1)
            return proc
        except Exception:
            time.sleep(0.4)
    raise RuntimeError("Edge 没起来")


async def shot(url: str, out: Path, wait_s: int, click_cite: bool = False):
    tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list").read())
    page = next(t for t in tabs if t["type"] == "page")
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

        await call("Page.enable")
        await call("Runtime.enable")
        # 关缓存：改过 .js/.css 之后浏览器可能还在用旧副本，
        # 截出来的是"上一版界面"，排查时会往完全错误的方向找。
        await call("Network.enable")
        await call("Network.setCacheDisabled", {"cacheDisabled": True})
        await call("Page.navigate", {"url": url})
        await asyncio.sleep(2)

        # 等答案真的出现：轮询 DOM 里有没有 .cite（引用角标），比 sleep 死等准
        deadline = time.time() + wait_s
        while time.time() < deadline:
            r = await call("Runtime.evaluate", {
                "expression": "document.querySelectorAll('.cite').length",
                "returnByValue": True})
            n = r.get("result", {}).get("value") or 0
            if n:
                break
            await asyncio.sleep(1)

        await asyncio.sleep(1.2)   # 等 CSS 过渡收尾

        if click_cite:
            await call("Runtime.evaluate", {
                "expression": "document.querySelector('.cite')?.click()",
                "returnByValue": True})
            await asyncio.sleep(0.6)

        r = await call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        out.write_bytes(base64.b64decode(r["data"]))
        print(f"已保存 {out}（{out.stat().st_size / 1024:.0f} KB）")


if __name__ == "__main__":
    url, out = sys.argv[1], Path(sys.argv[2])
    wait = int(sys.argv[3]) if len(sys.argv) > 3 else 25
    click = "--click" in sys.argv
    proc = ensure_browser()
    try:
        asyncio.run(shot(url, out, wait, click))
    finally:
        if proc:
            proc.terminate()
