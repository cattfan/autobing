param(
    [switch]$CreateUpdaterArtifacts
)

[console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = "Stop"

Write-Host "Installing PyInstaller..."
python -m pip install pyinstaller
if ($LASTEXITCODE -ne 0) {
    throw "pip failed while installing PyInstaller (exit code $LASTEXITCODE)"
}

$TARGET_DIR = "$PSScriptRoot\autobing-app\src-tauri\bin"
if (-Not (Test-Path -Path $TARGET_DIR)) {
    New-Item -ItemType Directory -Force -Path $TARGET_DIR
}

Write-Host "Cleaning old build outputs..."
Remove-Item "$PSScriptRoot\dist" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item "$PSScriptRoot\build" -Recurse -Force -ErrorAction SilentlyContinue

Push-Location -Path $PSScriptRoot
try {
    Write-Host "Building worker_api.exe from worker_api.spec..."
    python -m PyInstaller --noconfirm --clean --log-level WARN "$PSScriptRoot\worker_api.spec"
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed while building worker_api.exe (exit code $LASTEXITCODE)"
    }

    Write-Host "Building browser_scanner.exe from browser_scanner.spec..."
    python -m PyInstaller --noconfirm --clean --log-level WARN "$PSScriptRoot\browser_scanner.spec"
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed while building browser_scanner.exe (exit code $LASTEXITCODE)"
    }
}
finally {
    Pop-Location
}

Write-Host "Copying sidecar executables to src-tauri/bin..."
$workerApiExe = "$PSScriptRoot\dist\worker_api.exe"
$browserScannerExe = "$PSScriptRoot\dist\browser_scanner.exe"
if (-Not (Test-Path $workerApiExe)) {
    throw "worker_api.exe not found in dist"
}
if (-Not (Test-Path $browserScannerExe)) {
    throw "browser_scanner.exe not found in dist"
}
Copy-Item $workerApiExe -Destination $TARGET_DIR -Force
Copy-Item $browserScannerExe -Destination $TARGET_DIR -Force

if (-Not (Test-Path "$TARGET_DIR\worker_api.exe")) {
    throw "worker_api.exe was not copied"
}
if (-Not (Test-Path "$TARGET_DIR\browser_scanner.exe")) {
    throw "browser_scanner.exe was not copied"
}

Write-Host "Building Tauri MSI installer..."
Set-Location -Path "$PSScriptRoot\autobing-app"
$tauriBuildArgs = @("run", "tauri", "build", "--", "--ci")
$shouldCreateUpdaterArtifacts = $CreateUpdaterArtifacts -or $env:AUTOBING_CREATE_UPDATER_ARTIFACTS -eq "1"

if ($shouldCreateUpdaterArtifacts) {
    $updaterKeyPath = "$PSScriptRoot\autobing-app\updater.key"
    if (-Not $env:TAURI_SIGNING_PRIVATE_KEY -and (Test-Path $updaterKeyPath)) {
        $env:TAURI_SIGNING_PRIVATE_KEY = (Get-Content -Path $updaterKeyPath -Raw).Trim()
        Write-Host "Loaded Tauri updater signing key from autobing-app\updater.key."
    }
    if (-Not $env:TAURI_SIGNING_PRIVATE_KEY) {
        throw "Updater artifacts requested but TAURI_SIGNING_PRIVATE_KEY is not set and autobing-app\updater.key was not found"
    }
} else {
    Write-Host "Skipping updater artifacts for local MSI build. Pass -CreateUpdaterArtifacts or set AUTOBING_CREATE_UPDATER_ARTIFACTS=1 to enable them."
    $tauriConfigOverridePath = "$PSScriptRoot\build\tauri-no-updater.json"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $tauriConfigOverridePath) | Out-Null
    [System.IO.File]::WriteAllText(
        $tauriConfigOverridePath,
        '{"bundle":{"createUpdaterArtifacts":false}}',
        [System.Text.UTF8Encoding]::new($false)
    )
    $tauriBuildArgs += @("--config", $tauriConfigOverridePath)
}

npm @tauriBuildArgs
if ($LASTEXITCODE -ne 0) {
    throw "Tauri build failed (exit code $LASTEXITCODE)"
}

$msiCandidates = @(
    "$PSScriptRoot\target\release\bundle\msi",
    "$PSScriptRoot\autobing-app\src-tauri\target\release\bundle\msi"
)
$latestMsi = $null
foreach ($candidate in $msiCandidates) {
    if (Test-Path $candidate) {
        $found = Get-ChildItem -Path $candidate -Filter "*.msi" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1
        if ($found -and (-Not $latestMsi -or $found.LastWriteTime -gt $latestMsi.LastWriteTime)) {
            $latestMsi = $found
        }
    }
}

if (-Not $latestMsi) {
    throw "Tauri build completed but no MSI artifact was found"
}

Write-Host "==================="
Write-Host "DONE! MSI installer: $($latestMsi.FullName)"
Write-Host "==================="
