"""依赖完整性：代码里用到的每个第三方包，都必须在 requirements 里有人提供。

这是容器构建最容易失败的地方，而本机开发永远发现不了——因为本地早装好了。
这个项目已经因此漏过两次：`python-multipart`（FastAPI 动态加载，import 阶段不报错）
和静态资源的 MIME 注册表（不是 pip 包，是宿主注册表，见第 17 节）。

测试跑的是 `tools/check_requirements.py`，它做三件事：
  1. AST 扫出代码里所有第三方 import；
  2. 用已声明包的直接依赖集合判断哪些是"传递依赖"（不该写进 requirements）；
  3. 单独核对"动态依赖"——代码里不 import、但用到某能力就需要的那种。

第 3 条是这个测试真正的价值：前两条靠 import 扫描就能做，
但 `python-multipart` 这类依赖**代码里根本不会出现 import**，
只有把"用了 UploadFile → 需要 python-multipart"这条规则写下来才查得到。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_requirements_cover_all_imports():
    r = subprocess.run(
        [sys.executable, "-m", "tools.check_requirements"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert r.returncode == 0, (
        "有第三方包没被 requirements 覆盖（镜像会启动失败）：\n"
        + r.stdout[-2000:] + r.stderr[-1000:]
    )
    # 断言比对的是"结论"，不是中间过程：中间过程会随工具演进变，
    # 而"没有漏包"这个结论必须永远成立。
    assert "⑤ 没人提供：0 个" in r.stdout


def test_python_multipart_is_declared():
    """单独钉一条：这是真的漏过一次的包，而且是最难发现的那种。

    `python-multipart` 由 FastAPI 在收到 multipart 请求时动态导入。
    缺了它，`import app.main` 成功、`/healthz` 正常，只有在 lifespan 启动
    或第一次上传时才报 "Form data requires python-multipart"。
    """
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "python-multipart" in req
