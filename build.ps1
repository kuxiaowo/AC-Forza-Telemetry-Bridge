param([Parameter(Mandatory=$true)][string]$PythonExe)
$ErrorActionPreference = 'Stop'
$sourceRoot = $PSScriptRoot
$buildRoot = Join-Path $sourceRoot 'build-work'
$envRoot = Split-Path -Parent (Resolve-Path -LiteralPath $PythonExe).Path
$env:PATH = "$envRoot;$envRoot\Library\bin;$envRoot\Scripts;$env:PATH"
$oldPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = $sourceRoot
& $PythonExe -m unittest discover -s (Join-Path $sourceRoot 'tests') -t $sourceRoot -v
$env:PYTHONPATH = $oldPythonPath
if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
& $PythonExe -m PyInstaller --clean --noconfirm --windowed --onedir --name ACForzaBridge --paths $sourceRoot --add-binary "$envRoot\Library\bin\tcl86t.dll;." --add-binary "$envRoot\Library\bin\tk86t.dll;." --distpath (Join-Path $buildRoot 'dist') --workpath (Join-Path $buildRoot 'build') --specpath $buildRoot (Join-Path $sourceRoot 'main.py')
if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
Write-Host "Build complete: $buildRoot\dist\ACForzaBridge"
