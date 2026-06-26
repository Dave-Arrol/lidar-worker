<#
  Compress-LidarWorker.ps1
  --------------------------------------------------------------------------
  Zips ONLY the worker source/script files:
    index.js, registry.js, run-once.js, package.json, Dockerfile, scripts\*.py, ...
  Excludes node_modules, build output, .git, secrets (.env*), lock files, and
  binary assets (point clouds, rasters, images, fonts). Produces a small zip you
  can upload to a new chat.

  Usage (from any PowerShell prompt):
    .\Compress-LidarWorker.ps1
    .\Compress-LidarWorker.ps1 -Source 'C:\lidar-worker' -OutDir 'C:\'

  Output: <OutDir>\lidar-worker-src-<timestamp>.zip   (OutDir defaults to the
  parent folder of -Source, e.g. C:\).
#>
param(
  [string]$Source = 'C:\lidar-worker',
  [string]$OutDir = ''            # default: parent folder of $Source
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $Source)) { Write-Error "Source folder not found: $Source"; return }
$root = (Resolve-Path -LiteralPath $Source).Path.TrimEnd('\')
if ([string]::IsNullOrWhiteSpace($OutDir)) { $OutDir = Split-Path -Path $root -Parent }
if (-not (Test-Path -LiteralPath $OutDir)) { New-Item -ItemType Directory -Path $OutDir | Out-Null }

# --- exclusions -----------------------------------------------------------
# whole directories to skip anywhere in the tree
$skipDirs = @(
  'node_modules','.git','.next','.vercel','.turbo',
  'dist','build','out','coverage',
  '__pycache__','.pytest_cache','.mypy_cache','.ipynb_checkpoints',
  '.vscode','.idea'
)
# file-name patterns to skip (secrets / lock files / junk / the zip itself)
$skipFiles = @(
  '.env','.env.*','*.env',
  '*.pem','*.key','*.pfx','*.crt','*.p12',
  '*.pyc','*.pyo','*.log','*.tmp','*.tsbuildinfo','*.map',
  'package-lock.json','yarn.lock','pnpm-lock.yaml',
  '.DS_Store','Thumbs.db','*.zip'
)
# binary / data extensions to skip wherever they live (keeps it to source text)
$skipExt = @(
  '.png','.jpg','.jpeg','.gif','.webp','.ico','.bmp','.tif','.tiff','.heic','.svgz',
  '.woff','.woff2','.ttf','.otf','.eot',
  '.mp4','.mov','.webm','.avi','.mp3','.wav','.ogg',
  '.pdf','.zip','.tar','.gz','.tgz','.7z','.rar',
  '.las','.laz','.copc','.ply','.e57',
  '.exe','.dll','.so','.dylib','.bin','.pyd'
)

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$leaf  = Split-Path -Path $root -Leaf
$Zip   = Join-Path $OutDir ("{0}-src-{1}.zip" -f $leaf, $stamp)

# load the zip API (works on Windows PowerShell 5.1 and PowerShell 7)
try { Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue } catch {}
try { Add-Type -AssemblyName System.IO.Compression -ErrorAction SilentlyContinue } catch {}

# gather the files to include
$files = Get-ChildItem -LiteralPath $root -Recurse -File -Force | Where-Object {
  $rel   = $_.FullName.Substring($root.Length + 1)
  $parts = $rel -split '[\\/]'
  foreach ($p in $parts)      { if ($skipDirs -contains $p)                  { return $false } }
  foreach ($pat in $skipFiles){ if ($_.Name -like $pat)                      { return $false } }
  if ($skipExt -contains $_.Extension.ToLower())                             { return $false }
  return $true
}

if (-not $files -or $files.Count -eq 0) { Write-Error "No files matched after exclusions - check the Source path."; return }

if (Test-Path -LiteralPath $Zip) { Remove-Item -LiteralPath $Zip -Force }
$archive = [System.IO.Compression.ZipFile]::Open($Zip, [System.IO.Compression.ZipArchiveMode]::Create)
$added = 0
try {
  foreach ($f in $files) {
    $entry = ($f.FullName.Substring($root.Length + 1)) -replace '\\','/'
    try {
      [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
        $archive, $f.FullName, $entry, [System.IO.Compression.CompressionLevel]::Optimal) | Out-Null
      $added++
    } catch { Write-Warning ("skipped (locked/unreadable): {0}" -f $entry) }
  }
} finally { $archive.Dispose() }

$sizeMB = [Math]::Round((Get-Item -LiteralPath $Zip).Length / 1MB, 2)
Write-Host ""
Write-Host ("Created: {0}" -f $Zip) -ForegroundColor Green
Write-Host ("  {0} files, {1} MB" -f $added, $sizeMB)
Write-Host ("  Excluded dirs: {0}" -f ($skipDirs -join ', '))
Write-Host ""
Write-Host "Top-level entries included:"
$files | ForEach-Object { (($_.FullName.Substring($root.Length + 1)) -split '[\\/]')[0] } |
  Sort-Object -Unique | ForEach-Object { Write-Host ("  - {0}" -f $_) }
