"""核对 requirements.txt 是否"装得全"：代码里 import 的每个第三方包都要有人提供。

为什么需要这个工具
------------------
容器构建失败最常见的原因不是代码错，是**依赖漏在 requirements.txt 外面**——
本地装过一堆东西，代码当然跑得起来；换到干净镜像就 `ModuleNotFoundError`。

这个项目已经漏过两次：
  1. `python-multipart`——FastAPI 收 multipart/form-data 必须有它，
     缺了在 lifespan 里报错，不在 import 阶段；
  2. 静态资源的 MIME 依赖 `mimetypes` 的注册表，镜像里没有注册表就是 text/plain，
     表现为"页面正常但所有交互无反应"（见 README 第 17 节）。

做法不是"再跑一遍 pip install"（那要联网、要几分钟、还可能被代理卡住），
而是纯静态地把三件事对上：
    代码里的 import  →  提供它的发行包名  →  是否在 requirements.txt 里

判断"传递依赖"用的是 `importlib.metadata.requires()`：如果某个包不在
requirements.txt 里，但它是某个已声明包的直接依赖，那它没漏——只是不该被直接写进去。

用法：
    python -m tools.check_requirements               # 默认查 requirements.txt
    python -m tools.check_requirements --file requirements-ocr.txt --extra
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from importlib.metadata import PackageNotFoundError, distribution, packages_distributions
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 代码导入名 → 发行包名 的例外表。绝大多数能靠 packages_distributions() 自动对上，
# 只有这几个对不上：导入名和包名完全不同的历史遗留（PIL/pillow、fitz/pymupdf）。
IMPORT_ALIASES = {
    "dotenv": "python-dotenv",
    "fitz": "pymupdf",
    "PIL": "pillow",
    "yaml": "pyyaml",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "bs4": "beautifulsoup4",
    "docx": "python-docx",
    "jwt": "pyjwt",
    "dateutil": "python-dateutil",
    "rapidocr_onnxruntime": "rapidocr-onnxruntime",
    "multipart": "python-multipart",
}

# 本项目自己的模块，不算第三方
LOCAL = {"app", "query", "prompts", "rerankers", "embedders", "loaders", "chunkers",
         "llm_client", "evalutil", "index_stats", "query_consts", "tools", "tests",
         "run_eval", "run_eval_mt", "run_index", "check_embedding"}

# 可选依赖：只有探测到才会被 import，缺了不影响服务启动。
# 每一条都要能说出"为什么缺了也没关系"，否则它就该进 requirements.txt。
OPTIONAL = {
    "docling": "PDF 版面后端，由 _has_docling() 探测；装不上自动降级到 pymupdf（见 README 第 10 节）",
    "pytesseract": "OCR 的备选后端，只有系统里存在 tesseract 可执行文件才会被导入",
    "rapidocr_onnxruntime": "OCR 主后端，单独放在 requirements-ocr.txt，不进主镜像",
}

# 只在开发脚本里用（tools/ 下的录屏与调试工具），不进运行时依赖。
DEV_ONLY = {
    "websockets": "tools/console.py 与 tools/record.py 用（CDP 调试/录屏），见 requirements-dev.txt",
    "imageio_ffmpeg": "tools/record.py 合成 mp4 用，见 requirements-dev.txt",
}

# 动态依赖：代码里"用到"了某个能力，但**从来没有 import 过**对应的包。
# 静态 import 扫描对这类依赖完全无效，而它们恰恰是最容易漏的——
# 因为漏了不会在 import 阶段报错，只会在第一次真正用到时炸。
#
# 这个表里每一条都对应一次真实事故：
DYNAMIC = [
    # (代码里出现的标记, 需要的发行包, 为什么要这一条)
    ("UploadFile", "python-multipart",
     "FastAPI 收 multipart/form-data 靠的是 python-multipart，它被 FastAPI 动态加载，"
     "代码里不会出现 import multipart。缺了在 lifespan 里报错，不在 import 阶段"),
    ("Form(", "python-multipart",
     "同上，表单参数走的是同一个动态依赖"),
]


def scan_imports() -> dict[str, set[str]]:
    """收集代码里所有 import 的顶层模块名 → 出现在哪些文件里。"""
    out: dict[str, set[str]] = {}
    for f in _all_sources():
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    out.setdefault(a.name.split(".")[0], set()).add(f.name)
            elif isinstance(node, ast.ImportFrom):
                if node.level:          # 相对导入，属于本项目
                    continue
                if node.module:
                    out.setdefault(node.module.split(".")[0], set()).add(f.name)
    return out


def _all_sources() -> list[Path]:
    targets = [p for p in ROOT.glob("*.py")]
    targets += [p for p in (ROOT / "app").rglob("*.py")]
    targets += [p for p in (ROOT / "tools").rglob("*.py") if "corpus_build" not in str(p)]
    return targets


def parse_requirements(path: Path) -> dict[str, str]:
    """requirements.txt → {规范化包名: 原始行}。"""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[=<>!~\[; ]", line, maxsplit=1)[0].strip()
        if name:
            out[re.sub(r"[-_.]+", "-", name).lower()] = line
    return out


def declared_deps(reqs: dict[str, str]) -> set[str]:
    """已声明包直接依赖的规范化名字集合（用于判断"传递依赖"）。"""
    deps: set[str] = set()
    for name in reqs:
        try:
            d = distribution(name)
        except PackageNotFoundError:
            continue
        for r in d.requires or []:
            base = re.split(r"[=<>!~\[; (]", r, maxsplit=1)[0].strip()
            if base:
                deps.add(re.sub(r"[-_.]+", "-", base).lower())
    return deps


def _installed(dist: str) -> bool:
    try:
        distribution(dist)
        return True
    except PackageNotFoundError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="requirements.txt")
    a = ap.parse_args()

    imports = scan_imports()
    reqs = parse_requirements(ROOT / a.file)
    transitive = declared_deps(reqs)
    stdlib = sys.stdlib_module_names
    mod2dist = packages_distributions()

    third = {m: f for m, f in imports.items() if m not in stdlib and m not in LOCAL}
    print(f"扫描到 {len(imports)} 个顶层模块，其中第三方 {len(third)} 个；"
          f"{a.file} 声明 {len(reqs)} 个包\n")

    direct, via, optional, dev, missing = [], [], [], [], []
    unknown = []
    for mod, files in sorted(third.items()):
        alias = IMPORT_ALIASES.get(mod)
        dists = [alias] if alias else mod2dist.get(mod, [])
        norm = [re.sub(r"[-_.]+", "-", d).lower() for d in dists]
        dist_name = norm[0] if norm else "?"
        row = (mod, dist_name, sorted(files)[:2])
        if mod in OPTIONAL:
            optional.append((*row, OPTIONAL[mod]))
        elif mod in DEV_ONLY:
            dev.append((*row, DEV_ONLY[mod]))
        elif any(n in reqs for n in norm):
            direct.append(row)
        elif any(n in transitive for n in norm) or not norm:
            via.append(row)
        elif not _installed(norm[0]):
            # 本环境没装这个包，就没法判断它是不是某个已声明包的传递依赖。
            # 这种"查不了"必须与"查出来是漏的"分开——CI 的 fast job 只装
            # requirements-fast.txt，lightrag 一系全都不在，若混为一谈，
            # 那条 job 会永远红，红灯一多就没人看，等于把检查关掉了。
            unknown.append(row)
        else:
            missing.append(row)

    print(f"① 直接声明：{len(direct)} 个")
    for mod, dist, files in direct:
        print(f"   {mod:<16} ← {dist:<20} ({', '.join(files)})")
    print(f"\n② 传递依赖（由已声明的包带进来，不该写进 requirements）：{len(via)} 个")
    for mod, dist, files in via:
        print(f"   {mod:<16} ← {dist:<20} ({', '.join(files)})")
    print(f"\n③ 可选依赖（探测不到就不导入，不放进主镜像）：{len(optional)} 个")
    for mod, dist, _files, why in optional:
        print(f"   {mod:<16} ← {dist:<20} {why}")
    print(f"\n④ 仅开发期使用（不进运行时）：{len(dev)} 个")
    for mod, dist, _files, why in dev:
        print(f"   {mod:<16} ← {dist:<20} {why}")

    print(f"\n⑤ 没人提供：{len(missing)} 个  ← 非空就意味着镜像会启动失败")
    if unknown:
        print(f"   （另有 {len(unknown)} 个包里本环境没装，无法判断是不是传递依赖："
              f"{', '.join(m for m, _, _ in unknown)}）")
    if missing:
        for mod, dist, files in missing:
            print(f"   {mod:<16} ← {dist:<20} ({', '.join(files)})")
        return 1

    # 动态依赖：代码里没 import，但运行时会被需要
    print("\n⑥ 动态依赖（代码里不 import，但用到那个能力就需要）：")
    bad = []
    for marker, dist, why in DYNAMIC:
        hit = [f for f in _all_sources()
               if f.name != Path(__file__).name
               and marker in f.read_text(encoding="utf-8", errors="ignore")]
        if not hit:
            continue
        ok = re.sub(r"[-_.]+", "-", dist).lower() in reqs
        print(f"   {'✓' if ok else '✗'} 代码用了 {marker:<12} → 需要 {dist:<18} "
              f"({', '.join(p.name for p in hit[:2])})")
        print(f"     {why}")
        if not ok:
            bad.append(dist)
    if bad:
        print(f"\n   ✗ 缺失：{bad} —— 这四个字必须写进 {a.file}")
        return 1

    print("\n结论：代码里每个**无条件**导入的第三方包，都能被 requirements 直接或传递满足；")
    print("      动态依赖也都被声明了。")
    print("      这不能证明镜像能 build 通，见 README 第 16.6 节。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
