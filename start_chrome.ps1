# Restarts Chrome with remote debugging so the agent can reuse your logged-in session.
$chromePaths = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
$chrome = $chromePaths | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) {
    Write-Error "Chrome not found. Install Google Chrome first."
    exit 1
}

$userData = "$env:LOCALAPPDATA\Google\Chrome\User Data"
Write-Host "Closing Chrome..."
taskkill /F /IM chrome.exe 2>$null
Start-Sleep -Seconds 3

Write-Host "Starting Chrome with remote debugging on port 9222..."
Start-Process $chrome -ArgumentList @(
    "--remote-debugging-port=9222",
    "--remote-debugging-address=127.0.0.1",
    "--user-data-dir=$userData",
    "--profile-directory=Default",
    "--no-first-run",
    "--no-default-browser-check",
    "https://www.naukri.com/mnjuser/recommendedjobs"
)
Start-Sleep -Seconds 5
Write-Host "Chrome ready. Run: python agent.py --once"
