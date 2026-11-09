# 功能：命名漂移护栏——扫描处理区旧名（o-t-i-c-a）残留（必须为 0）+ 关键新名基线计数
param([switch]$Baseline)

$ErrorActionPreference = "Continue"

$scriptsDir = $PSScriptRoot
$projDir = Split-Path -Parent $scriptsDir        # bandi/
$repoDir = Split-Path -Parent $projDir           # repo root
$baselinePath = Join-Path $scriptsDir "naming-baseline.json"
$needle = ("ont" + "ica")
$needleCap = ("Ont" + "ica")
$needleUp = ("ONT" + "ICA")

$excludeRe = "\.venv|__pycache__|\.pytest_cache|\.ruff_cache|node_modules|\\dist|\.git|\.bandi|\.bandi-comms|\.workbuddy|\.opencode"
$textExt = @(".py", ".ps1", ".yaml", ".yml", ".toml", ".md", ".txt", ".cfg", ".ini", ".json",
             ".bat", ".sh", ".ts", ".tsx", ".js", ".jsx", ".css", ".html", ".cjs", ".mjs", ".lock")

$targets = @(
    (Join-Path $projDir "bandi"),
    (Join-Path $projDir "tests"),
    (Join-Path $projDir "scripts"),
    (Join-Path $repoDir "frontend\src"),
    (Join-Path $repoDir "frontend\electron")
)

$residue = New-Object System.Collections.ArrayList
foreach ($t in $targets) {
    if (-not (Test-Path $t)) { continue }
    Get-ChildItem $t -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object {
        $f = $_
        if ($f.FullName -match $excludeRe) { return }
        if ($textExt -notcontains $f.Extension.ToLower()) { return }
        $hits = Select-String -LiteralPath $f.FullName -Pattern @($needle, $needleCap, $needleUp) -SimpleMatch -ErrorAction SilentlyContinue
        foreach ($h in $hits) { [void]$residue.Add(("{0}:{1}" -f $h.Path, $h.LineNumber)) }
    }
}
foreach ($rf in @((Join-Path $repoDir "AGENTS.md"), (Join-Path $repoDir ".gitignore"))) {
    if (Test-Path $rf) {
        $hits = Select-String -LiteralPath $rf -Pattern @($needle, $needleCap, $needleUp) -SimpleMatch -ErrorAction SilentlyContinue
        foreach ($h in $hits) { [void]$residue.Add(("{0}:{1}" -f $h.Path, $h.LineNumber)) }
    }
}

$markers = @("bandi_knowledge_search", "bandi_skills_list", "bandi_memory_search",
             "bandi_ontology_query", "com.bandi.desktop", "BandiBot", "BandiError", "BandiSocket")
$counts = @{}
foreach ($m in $markers) { $counts[$m] = 0 }
$scanRoots = @((Join-Path $repoDir "bandi"), (Join-Path $repoDir "frontend\src"), (Join-Path $repoDir "frontend\electron"))
Get-ChildItem $scanRoots -Recurse -File -ErrorAction SilentlyContinue | ForEach-Object {
    if ($_.FullName -match $excludeRe) { return }
    if ($textExt -notcontains $_.Extension.ToLower()) { return }
    $c = Get-Content -LiteralPath $_.FullName -Raw -ErrorAction SilentlyContinue
    if ($c) { foreach ($m in $markers) { $counts[$m] += ([regex]::Matches($c, [regex]::Escape($m))).Count } }
}

Write-Host "== Naming Guard =="
Write-Host ("[{0}] residue hits (must be 0)" -f $residue.Count)
$residue | Select-Object -First 20 | ForEach-Object { Write-Host "  $_" }
foreach ($m in $markers) { Write-Host ("  {0} = {1}" -f $m, $counts[$m]) }

if ($Baseline) {
    $snap = [ordered]@{
        residue   = $residue.Count
        markers   = $counts
        taken_at  = (Get-Date -Format "yyyy-MM-dd HH:mm:ss")
    }
    ($snap | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $baselinePath -Encoding UTF8
    Write-Host ("baseline written -> {0}" -f $baselinePath)
}

if ($residue.Count -gt 0) { Write-Host "FAIL: residue found"; exit 1 }
Write-Host "PASS: no residue"
exit 0
