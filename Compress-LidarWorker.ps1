# Compress-LidarWorker.ps1
# Produces a lean zip of the Arrol LiDAR worker for session upload — source only, no
# node_modules, no Python caches / virtualenvs, no large sample point clouds, no VCS.
# Output: %USERPROFILE%\Downloads.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\Compress-LidarWorker.ps1
#   powershell -ExecutionPolicy Bypass -File .\Compress-LidarWorker.ps1 -Source C:\lidar-worker

param(
    [string]$Source = "C:\lidar-worker",
    [string]$OutDir = (Join-Path $env:USERPROFILE "Downloads")
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $Source)) { throw "Source folder not found: $Source" }

# Exclude deps, caches, virtualenvs, VCS, secrets — and heavy point-cloud/raster data
# that should never travel in a code upload.
$excludeDirs = @(
    "node_modules", ".git", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".venv", "venv", "env", ".cache", "dist", "build",
    "data", "samples", "test-data", "tmp", "output", "outputs"
)
$excludeFilePatterns = @(
    "*.log", "*.tmp", "*.zip",
    "*.las", "*.laz", "*.copc.laz", "*.tif", "*.tiff", "*.pyc",
    ".env", ".env.local", "*.pem", "*.key"
)

$stamp   = Get-Date -Format "yyyyMMdd-HHmmss"
$zipName = "lidar-worker-src-$stamp.zip"
$zipPath = Join-Path $OutDir $zipName
$staging = Join-Path $env:TEMP "lidar-worker-stage-$stamp"

Write-Host "Source : $Source"
Write-Host "Output : $zipPath"
Write-Host "Staging: $staging"
Write-Host ""

if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
New-Item -ItemType Directory -Path $staging | Out-Null

$roboArgs = @($Source, $staging, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP", "/R:1", "/W:1")
$roboArgs += "/XD"; $roboArgs += $excludeDirs
$roboArgs += "/XF"; $roboArgs += $excludeFilePatterns

& robocopy @roboArgs | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed with code $LASTEXITCODE" }

if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $staging, $zipPath,
    [System.IO.Compression.CompressionLevel]::Optimal, $false)

Remove-Item $staging -Recurse -Force

$sizeMB = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
Write-Host ""
Write-Host "Done. $zipName  ($sizeMB MB)"
Write-Host $zipPath
Write-Host ""
Write-Host "NOTE: point clouds (*.las/*.laz/*.copc.laz) and rasters (*.tif) are excluded by design."
