"""录一段 60-90 秒的演示视频：CDP 抓帧 + ffmpeg 合成。

为什么不用现成的录屏软件：
1. 无人值守。面试前想改一句话重录一遍，不该需要点开图形界面。
2. 帧率跟着**真实事件**走：等到答案出现才继续下一步，而不是靠 sleep 猜。
   猜短了截到空卡片，猜长了片子中间一段静止。

做法：
- `Page.startScreencast` 让浏览器把每一帧（JPEG）连同时间戳推过来；
- 脚本按步骤操作页面（输入、点击、等待 DOM 条件）；
- 结束后按每帧的真实时间戳拼成 H.264，节奏就是当时真实发生的节奏。

依赖：imageio-ffmpeg（自带 ffmpeg 二进制，不需要系统安装）
    pip install imageio-ffmpeg

用法：
    python tools/record.py --url http://127.0.0.1:8010/ --out docs/demo.mp4
    python tools/record.py --url ... --out ... --script upload   # 换一段脚本
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9334
WIDTH, HEIGHT = 1280, 860


def ensure_browser():
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1)
        return None
    except Exception:
        pass
    proc = subprocess.Popen(
        [EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         f"--remote-debugging-port={PORT}",
         "--user-data-dir=" + str(Path.home() / ".edge-cdp-record"),
         f"--window-size={WIDTH},{HEIGHT}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/version", timeout=1)
            return proc
        except Exception:
            time.sleep(0.4)
    raise RuntimeError("Edge 没起来")


class Page:
    def __init__(self, ws):
        self.ws = ws
        self.mid = 0
        self.frames: list[tuple[float, bytes]] = []

    async def call(self, method, params=None):
        self.mid += 1
        mid = self.mid
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("method") == "Page.screencastFrame":
                await self._on_frame(msg["params"])
                continue
            if msg.get("id") == mid:
                return msg.get("result", {})

    async def _on_frame(self, p):
        # 每收到一帧必须 ack，否则浏览器只发第一帧就停了（表现为"视频只有一张图"）
        self.frames.append((p["metadata"]["timestamp"], base64.b64decode(p["data"])))
        await self.ws.send(json.dumps({
            "id": 900000 + len(self.frames), "method": "Page.screencastFrameAck",
            "params": {"sessionId": p["sessionId"]}}))

    async def pump(self, seconds: float):
        """空转一会儿，把这段时间里的帧收下来。"""
        end = time.time() + seconds
        while time.time() < end:
            try:
                msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=0.05))
            except asyncio.TimeoutError:
                continue
            if msg.get("method") == "Page.screencastFrame":
                await self._on_frame(msg["params"])

    async def js(self, expr, wait=0.0):
        r = await self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                                "awaitPromise": True})
        if wait:
            await self.pump(wait)
        return (r.get("result") or {}).get("value")

    async def wait_for(self, expr, timeout=40.0, poll=0.4):
        end = time.time() + timeout
        while time.time() < end:
            if await self.js(expr):
                return True
            await self.pump(poll)
        return False

    async def type_text(self, selector: str, text: str, delay=0.055):
        """逐字输入，看起来像真的在打字（直接设 value 会显得很假）。"""
        await self.js(f"document.querySelector('{selector}').focus()")
        await self.pump(0.3)
        for ch in text:
            await self.js(
                f"(()=>{{const el=document.querySelector('{selector}');"
                f"el.value += {json.dumps(ch, ensure_ascii=False)};"
                f"el.dispatchEvent(new Event('input',{{bubbles:true}}));}})()")
            await self.pump(delay)

    async def click(self, selector: str, wait=0.6):
        await self.js(f"document.querySelector('{selector}')?.click()")
        await self.pump(wait)


# ------------------------------------------------------------------ 脚本
async def script_main(page: Page, url: str):
    """主流程：问一题 → 看引用 → 问一个文档里没有的（拒答）→ 展开评测表。"""
    await page.call("Page.navigate", {"url": url})
    await page.wait_for("document.querySelector('#meta')?.textContent.includes('块')", 30)
    await page.pump(2.0)

    await page.type_text("#q", "订单数为什么必须去重？")
    await page.pump(0.4)
    await page.click("#send", 0.2)
    # 等引用角标出现 = 答案真的写完了，不用 sleep 猜
    await page.wait_for("document.querySelectorAll('.cite').length > 0", 45)
    await page.pump(2.6)

    await page.click(".cite", 1.2)          # 打开原文抽屉
    await page.pump(2.4)
    await page.click("#close", 0.8)

    await page.type_text("#q", "2027 年打算上线哪些功能？")   # 语料里没有
    await page.pump(0.4)
    await page.click("#send", 0.2)
    await page.wait_for("document.querySelector('.refused')", 45)
    await page.pump(2.2)

    # 评测表在页面最下面。不滚动的话它即使展开了也在可视区外——
    # 上一版就是这样：点了、也确实展开了，但录出来的后 20 秒画面和前一帧一模一样。
    # 用 smooth 而不是瞬间跳，观感上更像"人在操作"，也让剪辑点更自然。
    await page.js("window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'})")
    await page.pump(1.6)
    await page.click("#evalbox > summary", 1.2)
    await page.pump(4.0)
    await page.js("document.querySelector('#evalbox').scrollIntoView({block: 'center', behavior: 'smooth'})")
    await page.pump(3.0)


async def script_upload(page: Page, url: str):
    """上传流程：展开语料面板 → 看清单 → 问刚上传的文档。"""
    await page.call("Page.navigate", {"url": url + "?panel=0"})
    await page.wait_for("document.querySelector('#meta')?.textContent.includes('块')", 30)
    await page.pump(1.5)
    await page.click("#corpus > summary", 2.0)
    await page.pump(3.0)


async def record(url: str, out: Path, which: str) -> None:
    tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list").read())
    page_info = next(t for t in tabs if t["type"] == "page")
    async with websockets.connect(page_info["webSocketDebuggerUrl"],
                                  max_size=64 * 1024 * 1024) as ws:
        page = Page(ws)
        await page.call("Page.enable")
        await page.call("Runtime.enable")
        await page.call("Network.enable")
        await page.call("Network.setCacheDisabled", {"cacheDisabled": True})
        await page.call("Emulation.setDeviceMetricsOverride",
                        {"width": WIDTH, "height": HEIGHT, "deviceScaleFactor": 1,
                         "mobile": False})
        await page.call("Page.startScreencast",
                        {"format": "jpeg", "quality": 82,
                         "maxWidth": WIDTH, "maxHeight": HEIGHT, "everyNthFrame": 1})

        if which == "upload":
            await script_upload(page, url)
        else:
            await script_main(page, url)

        await page.pump(0.5)
        await page.call("Page.stopScreencast")
        frames = page.frames

    print(f"收到 {len(frames)} 帧，时长约 {frames[-1][0] - frames[0][0]:.1f}s")
    if len(frames) < 10:
        raise RuntimeError("帧太少，检查 Page.startScreencast 是否被 ack")

    # 按真实时间戳建索引文件，ffmpeg 的 concat 会按 duration 排时间轴
    t0 = frames[0][0]
    with tempfile.TemporaryDirectory() as td:
        lines = []
        for i, (ts, data) in enumerate(frames):
            fp = Path(td) / f"f{i:05d}.jpg"
            fp.write_bytes(data)
            nxt = frames[i + 1][0] if i + 1 < len(frames) else ts + 0.2
            dur = max(0.04, round(nxt - ts, 3))
            lines.append(f"file '{fp.as_posix()}'\nduration {dur}\n")
        lines.append(f"file '{(Path(td) / f'f{len(frames) - 1:05d}.jpg').as_posix()}'\n")
        listfile = Path(td) / "list.txt"
        listfile.write_text("".join(lines), encoding="utf-8")

        import imageio_ffmpeg

        out.parent.mkdir(parents=True, exist_ok=True)
        # `-vsync vfr` 在 ffmpeg 9 里已被移除（新名字是 `-fps_mode vfr`）。
        # 这里直接用新名字：imageio-ffmpeg 自带的是新版二进制，
        # 而网上能搜到的绝大多数 concat 示例还是老写法。
        cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error",
               "-f", "concat", "-safe", "0", "-i", str(listfile),
               "-fps_mode", "vfr", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
               str(out)]
        # 不要 check=True 直接把 stderr 吞掉：ffmpeg 失败时那句错误信息是唯一线索，
        # 而 exit code 在 Windows 上经常是个看不出含义的大数（比如 2880417800）。
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print("ffmpeg 失败，命令：\n  " + " ".join(cmd))
            print("stderr:\n" + (r.stderr or "")[-1500:])
            raise SystemExit(1)
    print(f"已生成 {out}（{out.stat().st_size / 1024 / 1024:.1f} MB，"
          f"{frames[-1][0] - t0:.0f}s）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8010/")
    ap.add_argument("--out", default="docs/demo.mp4")
    ap.add_argument("--script", default="main", choices=["main", "upload"])
    a = ap.parse_args()
    if not Path(EDGE).exists():
        sys.exit(f"找不到 Edge：{EDGE}")
    proc = ensure_browser()
    try:
        asyncio.run(record(a.url, Path(a.out), a.script))
    finally:
        if proc:
            proc.terminate()


if __name__ == "__main__":
    main()
