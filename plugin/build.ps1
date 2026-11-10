# Build ArcHxv4.dll (the GARbro plugin).
#
# Requires only .NET Framework 4.x (bundled with Windows) -- no Visual Studio,
# no Roslyn, no extra SDK.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File build.ps1 -GarbroDir "C:\GARbro"
#
# Parameters:
#   -GarbroDir   GARbro folder (must contain GameRes.dll and Newtonsoft.Json.dll)
#   -OutDir      output folder, defaults to ..\out

param(
    [Parameter(Mandatory = $true)]
    [string]$GarbroDir,
    [string]$OutDir = (Join-Path (Split-Path $PSScriptRoot -Parent) "out")
)

$ErrorActionPreference = "Stop"

# ------------------------------------------------------------- locate csc.exe
$cscCandidates = @(
    "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe",
    "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
)
$csc = $cscCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $csc) {
    throw "csc.exe not found (needs .NET Framework 4.x, which ships with Windows)"
}

# --------------------------------------------------- locate reference assemblies
$refCandidates = @(
    "C:\Program Files (x86)\Reference Assemblies\Microsoft\Framework\.NETFramework\v4.7.2",
    "C:\Program Files (x86)\Reference Assemblies\Microsoft\Framework\.NETFramework\v4.7.1",
    "C:\Program Files (x86)\Reference Assemblies\Microsoft\Framework\.NETFramework\v4.7",
    "C:\Program Files (x86)\Reference Assemblies\Microsoft\Framework\.NETFramework\v4.6.2",
    "C:\Program Files (x86)\Reference Assemblies\Microsoft\Framework\.NETFramework\v4.5",
    "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319"
)
$refDir = $refCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $refDir) {
    throw "no .NET Framework reference assemblies found"
}

# --------------------------------------------------------- locate GARbro deps
foreach ($dep in @("GameRes.dll", "Newtonsoft.Json.dll")) {
    if (-not (Test-Path (Join-Path $GarbroDir $dep))) {
        throw "missing $dep -- pass the real GARbro folder via -GarbroDir"
    }
}

New-Item -ItemType Directory -Force $OutDir | Out-Null

Write-Host "csc     : $csc"
Write-Host "refasms : $refDir"
Write-Host "garbro  : $GarbroDir"
Write-Host "output  : $OutDir"

& $csc /nologo /target:library /out:"$OutDir\ArcHxv4.dll" /nostdlib+ /noconfig `
    "/r:$refDir\mscorlib.dll" `
    "/r:$refDir\System.dll" `
    "/r:$refDir\System.Core.dll" `
    "/r:$refDir\System.Xml.dll" `
    "/r:$refDir\System.ComponentModel.Composition.dll" `
    "/r:$GarbroDir\GameRes.dll" `
    "/r:$GarbroDir\Newtonsoft.Json.dll" `
    "$PSScriptRoot\ArcHxv4.cs"

if ($LASTEXITCODE -ne 0) { throw "build failed" }
Write-Host "OK -> $OutDir\ArcHxv4.dll"
