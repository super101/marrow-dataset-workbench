[CmdletBinding()]
param(
    [string]$Workspace = $env:MARROW_WORKSPACE,
    [string[]]$AllowRoot = @(),
    [ValidateRange(1024, 65535)][int]$Port = 8765,
    [string]$PythonExecutable,
    [switch]$Demo,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
if (-not $Workspace) {
    $Workspace = Join-Path ([Environment]::GetFolderPath('UserProfile')) '.marrow-workbench'
}
if ($AllowRoot.Count -eq 0 -and $env:MARROW_ALLOW_ROOT) {
    $AllowRoot = @($env:MARROW_ALLOW_ROOT.Split(';') | Where-Object { $_.Trim() } | ForEach-Object { $_.Trim() })
}
$pythonCommand = $null
$pythonPrefix = @()
if ($PythonExecutable) {
    $pythonCommand = $PythonExecutable
} elseif (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.venv\Scripts\python.exe')) {
    $pythonCommand = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3.11 -c 'import sys; assert sys.version_info >= (3, 11)' 2>$null
    if ($LASTEXITCODE -eq 0) {
        $pythonCommand = 'py'
        $pythonPrefix = @('-3.11')
    }
}
if (-not $pythonCommand -and (Get-Command python3 -ErrorAction SilentlyContinue)) {
    & python3 -c 'import sys; assert sys.version_info >= (3, 11)' 2>$null
    if ($LASTEXITCODE -eq 0) { $pythonCommand = 'python3' }
}
if (-not $pythonCommand) {
    throw 'Python 3.11+ was not found. Install Python, or pass -PythonExecutable C:\path\to\python.exe. See docs/USER_GUIDE.md.'
}
& $pythonCommand @pythonPrefix -c 'import sys; assert sys.version_info >= (3, 11); import PIL; import importlib.metadata; importlib.metadata.version(''marrow-dataset-workbench'')'
if ($LASTEXITCODE -ne 0) {
    throw 'Python, Pillow or the workbench package is missing. Explicitly install the project with your chosen interpreter: <python> -m pip install -e . See docs/USER_GUIDE.md. This launcher never installs dependencies.'
}
$launchArguments = @('-m', 'workbench', '--workspace', $Workspace, '--port', "$Port")
foreach ($root in $AllowRoot) { $launchArguments += @('--allow-root', $root) }
if ($Demo) { $launchArguments += '--demo' }
if (-not $NoBrowser) { $launchArguments += '--open-browser' }
& $pythonCommand @pythonPrefix @launchArguments
exit $LASTEXITCODE
