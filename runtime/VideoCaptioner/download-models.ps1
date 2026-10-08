param(
    [string]$Revision = "main"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$destination = Join-Path $PSScriptRoot "models\faster-whisper-medium"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "缺少项目 Python：$python"
}
New-Item -ItemType Directory -Path $destination -Force | Out-Null
$script = "from huggingface_hub import snapshot_download; snapshot_download(repo_id='Systran/faster-whisper-medium', revision=r'$Revision', local_dir=r'$destination')"
& $python -c $script
if ($LASTEXITCODE -ne 0) {
    throw "模型下载失败，退出码：$LASTEXITCODE"
}
$model = Join-Path $destination "model.bin"
$expected = "9B45E1009DCC4AB601EFF815B61D80E60CE3FD8C74C1A14F4A282258286B51AE"
if ((Get-FileHash -LiteralPath $model -Algorithm SHA256).Hash -ne $expected) {
    throw "model.bin SHA-256 不匹配；下载内容与当前固定资源不同"
}
