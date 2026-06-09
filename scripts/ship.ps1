#!/usr/bin/env pwsh
<#
.SYNOPSIS
  wordcracker post-fix pipeline (dev box / Windows). ONE command from a fix to a
  pushed, validated branch. Guards the classic footguns:
    * running in an ORPHAN repo copy (no Standaoerby/wordcracker origin)
    * the Python-3.13 FixtureFreshnessGate false-positive (fixtures hash
      ast.dump(), which is Python-minor specific; recorded on 3.11)
    * forgetting the mandatory ANALYTICS_VERSION bump
    * pushing straight to main

.EXAMPLE
  ./scripts/ship.ps1 -Message "fix(x): ..." -Branch fix/x
  ./scripts/ship.ps1 -Message "fix(x): ..." -AlreadyCommitted   # commit already made
  ./scripts/ship.ps1 -Message "fix(x): ..." -Branch fix/x -Deploy  # + deploy on SOW after you merge
#>
param(
  [Parameter(Mandatory=$true)][string]$Message,
  [string]$Branch,
  [switch]$AlreadyCommitted,
  [switch]$Deploy,
  [switch]$NoTests,
  [string]$Sow = "sow",
  [string]$PinnedPy = "3.11"
)
$ErrorActionPreference = "Stop"
function Die($m){ Write-Host "`nFAIL: $m" -ForegroundColor Red; exit 1 }
function Ok($m){ Write-Host $m -ForegroundColor Green }
function Step($m){ Write-Host "`n=== $m ===" -ForegroundColor Cyan }

# 0 — REPO GUARD: refuse to run anywhere but the real clone
Step "repo guard"
$origin = (git remote get-url origin 2>$null)
if($origin -notmatch 'Standaoerby/wordcracker'){
  Die "origin='$origin' is NOT Standaoerby/wordcracker. You're in an orphan copy (e.g. wordcracker-code). cd into the real clone and retry."
}
$root = (git rev-parse --show-toplevel)
Ok "repo OK: $root"

# 1 — PYTHON AWARENESS: deselect the version-sensitive gate when not on pinned py
Step "python env"
$py = (python -c "import sys;print(str(sys.version_info[0])+'.'+str(sys.version_info[1]))")
$deselect = @()
if($py -ne $PinnedPy){
  Write-Host "WARN local Python $py != pinned $PinnedPy. FixtureFreshnessGate hashes ast.dump() (version-specific) -> known false-fail locally. Deselecting it; CI on $PinnedPy is the real gate." -ForegroundColor Yellow
  $deselect = @('--deselect','tests/v2/test_v1_contracts.py::FixtureFreshnessGate::test_every_fixture_fingerprint_matches_current')
} else { Ok "on pinned Python $PinnedPy" }

# 2 — TESTS: collection (R10) + run
if(-not $NoTests){
  Step "pytest --collect-only (R10: 0 collection errors)"
  python -m pytest tests/v2 --collect-only -q *> $null
  if($LASTEXITCODE -ne 0){ Die "collection errors (syntax error somewhere) - R10" }
  Ok "collection clean"
  Step "pytest tests/v2"
  python -m pytest tests/v2 -q @deselect
  if($LASTEXITCODE -ne 0){ Die "tests RED (excluding the known version gate). Fix before shipping." }
  Ok "tests green"
}

# 3 — VERSION-BUMP GATE vs merge-base with main
Step "version-bump gate"
git fetch origin --quiet 2>$null
git rev-parse --verify -q origin/main *> $null
$main = if($LASTEXITCODE -eq 0){'origin/main'}else{'main'}
$mb = (git merge-base HEAD $main)
python scripts/check_version_bump.py --against git --git-ref $mb --require-strict-increase
if($LASTEXITCODE -ne 0){ Die "ANALYTICS_VERSION not bumped vs $main. Edit scripts/v2/__version__.py (+1) and retry." }

# 4 — COMMIT (skip if already committed)
if(-not $AlreadyCommitted){
  Step "commit"
  $cur = (git branch --show-current)
  if($cur -in @('main','master','')){
    if(-not $Branch){ Die "On '$cur' with no -Branch. Pass -Branch fix/<name>." }
    git switch -c $Branch $main
  }
  git add -A
  git commit -m $Message
}
$cur = (git branch --show-current)
if($cur -in @('main','master')){ Die "refusing to push straight to '$cur' - use a fix branch + PR" }

# 5 — PUSH
Step "push $cur"
git push -u origin $cur
if($LASTEXITCODE -ne 0){ Die "push failed" }
Ok "pushed '$cur'. Open a PR, merge to main. CI runs the FULL suite on Python $PinnedPy (incl FixtureFreshnessGate)."

# 6 — DEPLOY on SOW (only after the PR is merged into main)
if($Deploy){
  Step "deploy on SOW (pulls origin/main - merge the PR first!)"
  ssh $Sow 'cd ~/wordcracker && git fetch && git pull --ff-only origin main && bash scripts/deploy.sh'
  if($LASTEXITCODE -ne 0){ Die "remote deploy failed (deploy.sh auto-rolls-back on red gate)" }
  Ok "deployed."
}
