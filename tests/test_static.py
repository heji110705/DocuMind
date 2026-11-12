"""静态资源与前端模块的静态检查。

两组用例：
1. MIME 注册：`.js` 必须是 text/javascript。依赖宿主注册表的话，换个 Windows 机器
   页面就整体失效，而且报错信息藏在控制台里，极难联想到是 MIME 的问题。
2. 前端模块：每个文件都要存在、被 index.html 引用、且能按 **ESM** 解析通过。
   免构建不代表不用工程化——只是把"打包"换成了"语法检查 + 引用一致性"。
   注意必须是 ESM 而不是 `node --check file.js`（后者按 CommonJS 解析，会漏掉
   重复声明这类会让整个页面失效的错误，见 test_modules_parse 的注释）。
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app import mime

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"


def test_js_mime_is_javascript():
    assert mime.guess("main.js") == "text/javascript"


def test_css_mime():
    assert mime.guess("app.css") == "text/css"


def test_static_files_exist():
    for name in ("index.html", "app.css", "js/main.js"):
        assert (STATIC / name).exists(), name


def test_index_references_module_entry():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'type="module"' in html
    assert "/static/js/main.js" in html
    assert "/static/app.css" in html


def test_no_inline_script_left():
    """界面逻辑都在 js/ 里。index.html 里残留大段 <script> 就说明拆分没做完。"""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    inline = html.count("<script>")
    assert inline == 0, f"index.html 里还有 {inline} 段内联脚本"


def test_imports_resolve():
    """每个 import 的相对路径都要真实存在——拆模块时最容易漏的就是这里。"""
    missing = []
    for f in sorted((STATIC / "js").glob("*.js")):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line.startswith("import "):
                continue
            spec = line.split("from")[-1].strip().strip(";").strip("'\"")
            if not spec.startswith("."):
                continue
            if not (f.parent / spec).resolve().exists():
                missing.append(f"{f.name} -> {spec}")
    assert not missing, missing


@pytest.mark.skipif(not shutil.which("node"), reason="没装 node，跳过语法检查")
def test_modules_parse():
    """语法检查必须按 **ESM** 解析，不能用 `node --check file.js`。

    这里踩过一个代价很大的坑。最初写的是 `node --check <file>`，113 个测试全绿，
    但打开页面是一片死的：徽标停在"索引 …"，点按钮没有任何反应。
    浏览器控制台只有一句 `SyntaxError: Identifier 'drop' has already been declared`，
    因为 docs.js 里既有一个 `const drop = document.getElementById('drop')`，
    又有一个 `async function drop(docId)`，同作用域重名。

    也就是说：**真正会杀死整个前端的错误，测试一个都没拦住。**
    原因是 `node --check foo.js` 在没有 package.json 的目录里按 CommonJS 解析，
    而浏览器按 ESM 解析；同一份文件在两种模式下合法性不同。
    实测同一条重复声明：CJS 模式 exit 0，ESM 模式 exit 1。

    改法是用 `--input-type=module` 从 stdin 喂进去，强制按模块解析——
    不在 static 目录里塞 package.json，免得为了测试去改交付物的形态。
    """
    for f in sorted((STATIC / "js").glob("*.js")):
        r = subprocess.run(
            ["node", "--input-type=module", "--check"],
            input=f.read_text(encoding="utf-8"),
            capture_output=True, text=True,
        )
        assert r.returncode == 0, f"{f.name}: {r.stderr[:300]}"


def test_module_check_would_catch_duplicate_binding():
    """守住上一条测试本身：如果哪天退回 `node --check <file>`，这条会红。

    一条"检查器"式的测试最容易悄悄失效——它永远是绿的，看起来一直在保护你。
    所以给它自己再钉一个用例：故意造一份有重复声明的模块，断言检查方式确实能抓到。
    """
    if not shutil.which("node"):
        pytest.skip("没装 node")
    bad = "const x = 1;\nasync function x() {}\n"
    strict = subprocess.run(
        ["node", "--input-type=module", "--check"],
        input=bad, capture_output=True, text=True,
    )
    assert strict.returncode != 0, "ESM 模式应当报重复声明"


def test_getelementbyid_targets_exist():
    """JS 里 getElementById 的每个 id，index.html 里都得有。

    这是拆模块带来的新风险：以前 394 行单文件，元素和取值写在一起，
    改 id 时肉眼能看见；拆成 8 个模块之后，"改 html 忘了改 js"不会有任何报错，
    只会表现为某个面板永远不出现。这条测试就是拿文本比对把这类漂移钉住。
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([^"]+)"', html))
    missing = []
    for f in sorted((STATIC / "js").glob("*.js")):
        for m in re.finditer(r"getElementById\('([^']+)'\)", f.read_text(encoding="utf-8")):
            if m.group(1) not in ids:
                missing.append(f"{f.name}: #{m.group(1)}")
    assert not missing, missing


def test_eval_json_is_valid():
    """界面上的准确率来自 data/eval_prod.json，格式坏了整块面板会消失。"""
    fp = ROOT / "data" / "eval_prod.json"
    if not fp.exists():
        pytest.skip("还没跑评测")
    rows = json.loads(fp.read_text(encoding="utf-8"))
    assert isinstance(rows, list) and rows
    for r in rows:
        for k in ("config", "n", "acc_fact", "refusal_acc", "overall", "p50_ms"):
            assert k in r, (r.get("config"), k)
