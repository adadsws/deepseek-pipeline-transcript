param(
    [string]$SevenZip = "7z.exe"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..")).Path
$destination = Join-Path $PSScriptRoot "bin"
$downloadDir = Join-Path $projectRoot "~temp\downloads"
$archive = Join-Path $downloadDir "faster-whisper-gpu.7z"
$url = "https://modelscope.cn/models/bkfengg/whisper-cpp/resolve/master/Faster-Whisper-XXL_r245.2_windows.7z"
$expected = "313A6C1DC97215BD6A076DA8743FF18768BFE619F3915464FC2A077821051A38"

New-Item -ItemType Directory -Path $downloadDir -Force | Out-Null
Invoke-WebRequest -Uri $url -OutFile $archive
$actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash
if ($actual -ne $expected) {
    throw "Faster-Whisper-XXL 归档 SHA-256 不匹配：$actual"
}
New-Item -ItemType Directory -Path $destination -Force | Out-Null
& $SevenZip x $archive "-o$destination" -y
if ($LASTEXITCODE -ne 0) {
    throw "7z 解压失败，退出码：$LASTEXITCODE"
}
$executable = Join-Path $destination "Faster-Whisper-XXL\faster-whisper-xxl.exe"
$exeExpected = "C81157FA0D1A7435AE7EE4165921B078146ABB015A2B833EA1161924CA0DF9EE"
if ((Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash -ne $exeExpected) {
    throw "faster-whisper-xxl.exe SHA-256 不匹配"
}
Copy-Item -LiteralPath $archive -Destination (Join-Path $destination "faster-whisper-gpu.7z") -Force
