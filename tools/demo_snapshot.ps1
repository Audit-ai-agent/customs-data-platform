# 데모·스크린샷용 스냅샷 CLI 래퍼 — UTF-8 콘솔·경고 숨김 후 demo_snapshot.py 실행.
#
# 사용법 (저장소 루트에서):
#   .\tools\demo_snapshot.ps1 rdb
#   .\tools\demo_snapshot.ps1 vector "세관장확인대상물품" --k 5
#   .\tools\demo_snapshot.ps1 vector "수입 신고" --as-of 2024-06-01
#   .\tools\demo_snapshot.ps1 all
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

chcp 65001 | Out-Null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONWARNINGS = "ignore"
$env:HF_HUB_DISABLE_PROGRESS_BARS = "1"
$env:TRANSFORMERS_VERBOSITY = "error"

# venv 셔틀 python.exe 가 애플리케이션 제어 정책에 막히는 워크스테이션이 있어,
# 막히면 uv 관리 베이스 인터프리터에 venv site-packages 를 물려 실행한다.
$venvPython = Join-Path $repo ".venv\Scripts\python.exe"
$script = Join-Path $repo "tools\demo_snapshot.py"

$spawned = $false
try {
    & $venvPython $script @args
    $spawned = $true
} catch {
    $spawned = $false
}
if (-not $spawned) {
    $cfg = Get-Content (Join-Path $repo ".venv\pyvenv.cfg") | Select-String '^home = (.+)$'
    $basePython = Join-Path $cfg.Matches[0].Groups[1].Value "python.exe"
    $env:PYTHONPATH = Join-Path $repo ".venv\Lib\site-packages"
    & $basePython $script @args
}
exit $LASTEXITCODE
