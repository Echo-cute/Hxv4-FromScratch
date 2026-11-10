# pbd2json.ps1 -- 用 CLR4 執行 pbd2json.exe
#
# pbd2json.exe 是 .NET Framework 2.0（CLR 2.0）組件。Windows 沒裝 .NET 3.5 時，
# 直接執行會「無聲結束、exit code 0」。但 PowerShell 跑在 CLR 4 上，
# 用反射把組件從記憶體載入再呼叫 EntryPoint 就能繞過這個限制。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File pbd2json.ps1 -Exe <pbd2json.exe> -File <x.pbd>
#
# 輸出與直接執行相同（JSON 寫到 stdout）。

param(
    [Parameter(Mandatory = $true)][string]$Exe,
    [Parameter(Mandatory = $true)][string]$File
)

$ErrorActionPreference = 'Stop'
try {
    $bytes = [System.IO.File]::ReadAllBytes($Exe)
    $asm = [System.Reflection.Assembly]::Load($bytes)
    $ep = $asm.EntryPoint
    if ($null -eq $ep) { throw 'assembly has no entry point' }
    $argv = [string[]]@($File)
    $null = $ep.Invoke($null, @(,$argv))
    exit 0
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
