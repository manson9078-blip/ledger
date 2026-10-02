# Runs the stock picker on this PC and pushes the results to GitHub.
#
# PTT and Dcard block GitHub Actions (data-center IPs), but a home connection in
# Taiwan gets through, so the social board is built here. Scheduled by
# install_task.ps1; can also be run by hand. Log: stock\windows\run.log
#
# API keys come from user environment variables, e.g.
#   setx GEMINI_API_KEY "your-key"
#   setx FINMIND_TOKEN "your-token"

$ErrorActionPreference = "Continue"
$repo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$log = Join-Path $PSScriptRoot "run.log"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
Set-Location $repo

if ((Test-Path $log) -and (Get-Item $log).Length -gt 1MB) { Remove-Item $log }

function Write-Log($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg" | Out-File -FilePath $log -Append -Encoding utf8
}

# Runs a command through cmd so stderr goes to the log instead of becoming PowerShell errors.
function Invoke-Logged($cmd) {
    Write-Log "> $cmd"
    cmd /c "$cmd >> `"$log`" 2>&1"
    return $LASTEXITCODE
}

function Invoke-WithRetry($cmd, $tries = 4) {
    for ($i = 1; $i -le $tries; $i++) {
        if ((Invoke-Logged $cmd) -eq 0) { return $true }
        Start-Sleep -Seconds (15 * $i)
    }
    return $false
}

Write-Log "===== start ====="

$branch = (git rev-parse --abbrev-ref HEAD).Trim()
if ($branch -ne "main") {
    Write-Log "Repo is on branch '$branch', not main. Skipping so nothing lands on the wrong branch."
    exit 1
}

# Right after boot/wake the network may not be up yet, hence the retries.
if (-not (Invoke-WithRetry "git pull --rebase --autostash origin main")) {
    Write-Log "git pull failed; giving up."
    exit 1
}

if ((Invoke-Logged "python stock\picker.py") -ne 0) {
    Write-Log "picker.py failed; see output above."
    exit 1
}

Invoke-Logged "git add stock/data" | Out-Null
git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Log "No data changes; done."
    exit 0
}

$today = Get-Date -Format "yyyy-MM-dd"
Invoke-Logged "git commit -q -m `"Stock picks $today (local)`"" | Out-Null

# If the cloud run pushed in the meantime, keep this PC's data on conflicts (it has the social board).
if (-not (Invoke-WithRetry "git pull --rebase --autostash -X theirs origin main")) {
    Write-Log "Rebase failed; aborting it. The commit stays local, run this script again later."
    Invoke-Logged "git rebase --abort" | Out-Null
    exit 1
}
if (-not (Invoke-WithRetry "git push origin main")) {
    Write-Log "git push failed; the commit stays local and will go out with the next run."
    exit 1
}
Write-Log "Pushed. ===== done ====="
