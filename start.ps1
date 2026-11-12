# 一条命令起服务（Windows）。
#   .\start.ps1            默认 8000 端口
#   .\start.ps1 -Port 8080
param([int]$Port = 8000)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "[!] 已生成 .env，请先填入 LLM_API_KEY 再重跑" -ForegroundColor Yellow
    exit 1
}

# 索引目录名要和 app 读到的 TAG 一致，否则脚本查一个目录、服务开另一个，起来就报索引不存在。
$tag = $env:TAG
if (-not $tag) {
    $line = Select-String -Path ".env" -Pattern "^TAG=" -ErrorAction SilentlyContinue | Select-Object -Last 1
    if ($line) { $tag = ($line.Line -split "=", 2)[1].Trim() }
}
if (-not $tag) { $tag = "prod" }
$env:TAG = $tag

# ---- 解释器与依赖 ------------------------------------------------------------
#
# 这个脚本以前直接 `python -m uvicorn`，默认"依赖已经装好了"。问题是
# **"python" 指向哪个解释器取决于 shell**：
#   PowerShell / cmd  -> D:\Python312（系统 Python，没装 lightrag）
#   Git Bash          -> 另一个解释器（装了，因为测试与 CI 都在那边跑）
# 于是出现一个很坑的现象：在一边跑得好好的服务，换到 PowerShell 跑就是一句
# 光秃秃的 ModuleNotFoundError: No module named 'lightrag'，
# 报错里没有任何"请先安装依赖"的提示。
#
# 这里做两件事、不做第三件：
#   1. 有 .venv 就用 .venv 的解释器（可复现）；
#   2. 缺依赖就把**可直接复制**的命令打出来并退出，而不是自动 pip install——
#      往系统 Python 里装包会污染全局，而且这台机器上 pip 出现过静默挂起
#      （零输出、不退出），自动装卡住比报错更难排查。
# 不做的是：静默回退到"默认 python"——那正是这个坑的来源。
$py = "python"
if (Test-Path ".venv/Scripts/python.exe") {
    $py = ".venv/Scripts/python.exe"
    Write-Host "[*] 使用项目内虚拟环境：$py" -ForegroundColor Cyan
}

# 探测依赖时必须临时关掉 Stop：脚本开头设了 $ErrorActionPreference = "Stop"，
# 而 `python -c "import x"` 失败时会往 stderr 写 traceback，在 Stop 模式下
# 整个脚本会当场终止——那就变成"探测本身把脚本打断"，比不探测还糟。
$need = @("lightrag", "fastapi", "uvicorn", "fastembed")
$missing = @()
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
foreach ($m in $need) {
    & $py -c "import $m" *> $null
    if ($LASTEXITCODE -ne 0) { $missing += $m }
}
$ErrorActionPreference = $prevEAP

if ($missing.Count -gt 0) {
    $where = (& $py -c "import sys; print(sys.executable)" 2>$null)
    Write-Host "[!] 当前解释器缺少依赖：$($missing -join ', ')" -ForegroundColor Red
    Write-Host "    解释器：$where" -ForegroundColor Red
    Write-Host "    先执行下面两条（在项目根目录）：" -ForegroundColor Yellow
    Write-Host "      python -m venv .venv" -ForegroundColor Yellow
    Write-Host "      .venv\Scripts\python.exe -m pip install -r requirements.txt" -ForegroundColor Yellow
    Write-Host "    装完重跑本脚本即可——它会自动优先使用 .venv。" -ForegroundColor Yellow
    Write-Host "    注意：PowerShell 默认执行策略会拦这个脚本，需要先执行" -ForegroundColor DarkGray
    Write-Host "      Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass" -ForegroundColor DarkGray
    exit 1
}

if (-not (Test-Path "rag_storage/$tag")) {
    Write-Host "[*] 没有索引，先建（约 3 分钟，¥1 左右）..." -ForegroundColor Cyan
    python run_index.py --input seed --pages 0 --tag $tag --gleaning 0
}

Write-Host "[*] http://127.0.0.1:$Port （索引 tag=$tag）" -ForegroundColor Green
python -m uvicorn app.main:app --host 127.0.0.1 --port $Port
