[console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "Cài đặt PyInstaller..."
python -m pip install pyinstaller

$TARGET_DIR = "$PSScriptRoot\autobing-app\src-tauri\bin"
if (-Not (Test-Path -Path $TARGET_DIR)) {
    New-Item -ItemType Directory -Force -Path $TARGET_DIR
}

Write-Host "Biên dịch src/worker_api.py thành .exe..."
# Build Worker API
python -m PyInstaller --noconfirm --onefile --log-level ERROR --name worker_api $PSScriptRoot\src\worker_api.py

Write-Host "Biên dịch src/browser_scanner.py thành .exe..."
# Build Browser Scanner
python -m PyInstaller --noconfirm --onefile --log-level ERROR --name browser_scanner $PSScriptRoot\src\browser_scanner.py

Write-Host "Copy file .exe qua src-tauri/bin..."
if (Test-Path "$PSScriptRoot\dist\worker_api.exe") {
    Copy-Item "$PSScriptRoot\dist\worker_api.exe" -Destination $TARGET_DIR -Force
}
if (Test-Path "$PSScriptRoot\dist\browser_scanner.exe") {
    Copy-Item "$PSScriptRoot\dist\browser_scanner.exe" -Destination $TARGET_DIR -Force
}

Write-Host "Tiến hành đóng gói Tauri App Installer (Giao diện + Code + Trình duyệt)..."
Set-Location -Path "$PSScriptRoot\autobing-app"
npm run tauri build

Write-Host "==================="
Write-Host "HOÀN TẤT! File cài đặt của bạn (.msi) nằm trong autobing-app/src-tauri/target/release/bundle/msi/"
Write-Host "==================="
