<#
.SYNOPSIS
    Version-bounded, self-checked one-folder PyInstaller build for the
    WuwaTerm desktop client.

.DESCRIPTION
    Builds client/dist/WuwaTerm/WuwaTerm.exe from client/WuwaTerm.spec using
    the client's own virtual environment (client/.venv). Fails loudly if the
    venv, PySide6, or pyinstaller are missing, rather than falling back to a
    system Python or a different GUI toolkit. No code signing is performed.

    What "version-bounded, self-checked" claims, precisely: the spec is
    committed, the dependency ranges in client/pyproject.toml bound what may
    be installed, this exact script is what CI runs on windows-latest, and the
    artifact it produces is started with --self-check before the build is
    called a success.

    It then packages that folder as client/dist/WuwaTerm-<version>-windows-x64.zip,
    the release asset name, with <version> read from the built package rather
    than written here. The zip is produced only after the self-check has
    passed, so an artifact that cannot start is never packaged. Both outputs
    stay in client/dist/, which client/.gitignore excludes.

    What it does NOT claim: bit-for-bit reproducibility, nor even identical
    inputs between two runs. The client has no lock file - its dependencies
    are ranges (PySide6>=6.7,<7 and pyinstaller>=6.10,<7 among them) - and the
    interpreter patch release and the CI runner image both float, so a later
    build can legitimately consume different versions. There is no
    build-timestamp normalisation, no hash-seed pinning and no two-build
    comparison here either, so two runs are not expected to produce identical
    bytes and nothing verifies that they do. Any such guarantee would have to
    be built and checked; do not read one into this script.
#>

$ErrorActionPreference = "Stop"

$ClientRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ClientRoot ".venv\Scripts\python.exe"
$SpecFile = Join-Path $ClientRoot "WuwaTerm.spec"

if (-not (Test-Path $VenvPython)) {
    Write-Error @"
Client virtual environment not found at: $VenvPython
Create it first, from the client directory:
  py "-V:Astral\CPython3.12.13" -m venv .venv
  .venv\Scripts\python.exe -m pip install -e ".[dev,build]"
"@
    exit 1
}

if (-not (Test-Path $SpecFile)) {
    Write-Error "Spec file not found at: $SpecFile"
    exit 1
}

& $VenvPython -c "import PySide6" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Error "PySide6 is not importable in $VenvPython. Run: $VenvPython -m pip install -e `"$ClientRoot[dev]`""
    exit 1
}

& $VenvPython -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Error "pyinstaller is not importable in $VenvPython. Run: $VenvPython -m pip install -e `"$ClientRoot[build]`""
    exit 1
}

# The version is read BEFORE anything is built, and the stale archive of that
# same version is removed BEFORE anything is built, because the guarantee being
# made is "a failed build leaves no archive that looks like its output". Doing
# the removal down beside the packaging would only reach it after PyInstaller
# and the self-check had both passed - so a build that failed at either of
# those would leave yesterday's zip of the same version sitting in dist\,
# looking exactly like the thing that just failed to be produced.
#
# It is read from the package this environment installs - the same interpreter
# PyInstaller is about to freeze - rather than from a string in this script or
# a hand-parsed pyproject.toml, so the name on the asset is the version the
# program reports about itself. client/tests/test_api_version_compat.py pins
# that constant against `version` in client/pyproject.toml.
#
# The name is exact and load-bearing: the release workflow uploads this zip.
$Version = (& $VenvPython -c "import wuwaterm_client; print(wuwaterm_client.__version__)")
if ($LASTEXITCODE -ne 0) {
    Write-Error "Could not read wuwaterm_client.__version__ from $VenvPython."
    exit 1
}
$Version = "$Version".Trim()
if ([string]::IsNullOrWhiteSpace($Version)) {
    Write-Error "wuwaterm_client.__version__ is empty; refusing to name an asset after it."
    exit 1
}
$ZipPath = Join-Path $ClientRoot "dist\WuwaTerm-$Version-windows-x64.zip"
if (Test-Path $ZipPath) {
    Write-Host "Removing the previous archive before building: $ZipPath"
    Remove-Item -LiteralPath $ZipPath -Force
}

# The dependency scan walks PATH to resolve the DLLs an extension module
# needs, so the interpreter's own runtime directory goes first: otherwise any
# other OpenSSL installed on the build machine wins and the artifact ships a
# libssl/libcrypto pair that its own _ssl.pyd cannot load.
$InterpreterDllDir = & $VenvPython -c "import sys, pathlib; print(pathlib.Path(sys.base_prefix) / 'DLLs')"
$OriginalPath = $env:PATH
$env:PATH = "$InterpreterDllDir;$env:PATH"

Push-Location $ClientRoot
try {
    & $VenvPython -m PyInstaller --noconfirm --clean $SpecFile
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
    $env:PATH = $OriginalPath
}

$ExePath = Join-Path $ClientRoot "dist\WuwaTerm\WuwaTerm.exe"
if (-not (Test-Path $ExePath)) {
    Write-Error "Build finished but the expected artifact was not found at: $ExePath"
    exit 1
}

# The stylesheets have to be IN the artifact, and nothing else here would
# notice if they were not. The application degrades to no styling when it
# cannot read them - deliberately, so a missing resource is not a failed
# start-up - which means the self-check below would still exit 0 and the
# defect would first appear as an unstyled window on the owner's desk. The
# spec lists the files and a client test compares that list to the directory;
# this is the other half, catching a target path that does not land where the
# application looks.
$DistRoot = Join-Path $ClientRoot "dist\WuwaTerm"
$StyleSheets = @(Get-ChildItem -Path $DistRoot -Filter "*.qss" -File -Recurse -ErrorAction SilentlyContinue)
if ($StyleSheets.Count -eq 0) {
    Write-Error "Build finished but no .qss stylesheet was packaged under: $DistRoot"
    exit 1
}
Write-Host "Packaged stylesheets: $($StyleSheets.Count)"

# A produced file is not a working program. This runs the artifact's own
# start-up rehearsal: it imports and constructs everything a normal start
# does, off-screen, and exits without showing a window, asking for a
# credential or sending a request. A build whose entry script cannot import
# its own package fails here instead of at the owner's desk.
$env:QT_QPA_PLATFORM = "offscreen"
$SelfCheck = Start-Process -FilePath $ExePath -ArgumentList "--self-check" -Wait -PassThru
Remove-Item Env:\QT_QPA_PLATFORM
if ($SelfCheck.ExitCode -ne 0) {
    Write-Error "The built artifact failed its start-up self-check (exit code $($SelfCheck.ExitCode)): $ExePath"
    exit 1
}

Write-Host "Build succeeded and passed its start-up self-check: $ExePath"

# The release asset, and only now: everything above had to pass first, so a
# build that cannot start is never packaged. `$Version` and `$ZipPath` were
# resolved at the top and any previous archive of this version was deleted
# there, before the build, so a failure at ANY stage above leaves no zip at all
# rather than yesterday's.
# $DistRoot is the FOLDER, with no wildcard, so the archive keeps a single
# `WuwaTerm\` directory at its root: extracting it anywhere yields one folder
# holding the whole portable program, which is what client/README.md tells the
# reader to expect. `dist\WuwaTerm\*` would spray the interpreter, the Qt
# runtime and several hundred files into whatever directory they extracted to.
Compress-Archive -Path $DistRoot -DestinationPath $ZipPath -CompressionLevel Optimal
if (-not (Test-Path $ZipPath)) {
    Write-Error "Packaging finished but no archive was written to: $ZipPath"
    exit 1
}

$ZipBytes = (Get-Item -LiteralPath $ZipPath).Length
Write-Host "Release asset: $ZipPath ($ZipBytes bytes, $([math]::Round($ZipBytes / 1MB, 1)) MB)"
