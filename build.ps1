$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
& '.\.venv\Scripts\python.exe' -m PyInstaller --noconfirm --clean --distpath dist BiliWatch.spec
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
Copy-Item -LiteralPath '.\README.md' -Destination '.\dist\README.md'
Get-FileHash -LiteralPath '.\dist\BiliWatch.exe' -Algorithm SHA256
