<#
  One stage of the GitHub-hosted Windows x64 build.

  The source is prepared through the same pinned ungoogled-chromium pipeline as
  build.ps1. Each stage restores C:\c\chromix, resumes ninja, and snapshots the
  tree before the GitHub job deadline.
#>
[CmdletBinding()]
param(
  [int]$StageIndex = 1,
  [int]$MaxStages = 12,
  [switch]$FromArtifact,
  [switch]$UseUpstreamCache,
  [ValidatePattern('\A[0-9]*\z')] [string]$UpstreamRunId = "",
  [switch]$ValidateOnly
)
$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path "$PSScriptRoot\..\..").Path
$Revisions = Import-PowerShellDataFile (Join-Path $Repo "build\ungoogled-revisions.psd1")

$Root = "C:\c"
$WorkDir = "$Root\chromix"
$Src = "$WorkDir\src"
$OutDir = "$Src\out\Chromix"
$RestoredUpstream = $false
# CI opt-in requires a full restore, including validation and artifact resumes.
$RequireUpstreamCache = $UseUpstreamCache -or $UpstreamRunId -or ($env:CHROMIX_USE_UPSTREAM_CACHE -eq "1")
$PartsDir = "C:\parts"
$UpstreamCacheDir = "C:\u"
# Validation runs in a 150-minute job and does not upload a build-tree snapshot.
$StageMinutes = if ($ValidateOnly) { 140 } else { 300 }
$Deadline = (Get-Date).AddMinutes($StageMinutes)
$PackReserveMin = if ($ValidateOnly) { 15 } else { 40 }

function Write-OutVar($key, $value) {
  if ($env:GITHUB_OUTPUT) { Add-Content -Path $env:GITHUB_OUTPUT -Value "$key=$value" }
  Write-Host "==> outvar $key=$value"
}

function Get-RemainingMin {
  return [int][Math]::Floor((New-TimeSpan -Start (Get-Date) -End $Deadline).TotalMinutes)
}

function Test-LastStage { return $StageIndex -ge $MaxStages }

function Invoke-Tracked {
  param(
    [string]$File,
    [string]$ArgList,
    [string]$Cwd,
    [int]$TimeoutSec,
    [switch]$Quiet,
    [switch]$FullFailureOutput
  )
  $log = "$env:TEMP\ci-tracked.log"
  $err = "$env:TEMP\ci-tracked.err"
  $wrapperName = "ci-tracked-$PID-$([Guid]::NewGuid().ToString('N'))"
  $wrapper = Join-Path $env:TEMP "$wrapperName.cmd"
  $status = Join-Path $env:TEMP "$wrapperName.exit"
  Remove-Item $log, $err, $wrapper, $status -ErrorAction SilentlyContinue

  $cmdFile = $File.Replace("%", "%%")
  $cmdArgs = $ArgList.Replace("%", "%%")
  $cmdStatus = $status.Replace("%", "%%")
  $wrapperLines = @(
    "@echo off",
    "`"$cmdFile`" $cmdArgs",
    'set "ci_tracked_exit=%ERRORLEVEL%"',
    ">`"$cmdStatus`" echo %ci_tracked_exit%",
    "exit /b %ci_tracked_exit%"
  )

  try {
    [IO.File]::WriteAllLines($wrapper, $wrapperLines, [Text.Encoding]::ASCII)
    $process = Start-Process -FilePath $env:COMSPEC `
      -ArgumentList "/d /s /c `"`"$wrapper`"`"" -WorkingDirectory $Cwd `
      -PassThru -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError $err
    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    $tick = 0
    while (-not $process.HasExited) {
      if ($stopwatch.Elapsed.TotalSeconds -gt $TimeoutSec) {
        Write-Host "==> timeout after $([int]$stopwatch.Elapsed.TotalMinutes) min; killing process tree"
        try { & taskkill.exe /PID $process.Id /T /F 2>&1 | Out-Null } catch {}
        $process.WaitForExit()
        Start-Sleep -Seconds 2
        return 124
      }
      Start-Sleep -Seconds 10
      $tick++
      if (-not $Quiet -and ($tick % 6) -eq 0 -and (Test-Path $log)) {
        Get-Content $log -Tail 3 | ForEach-Object { Write-Host "    | $_" }
      }
    }
    $process.WaitForExit()

    $code = 1
    if (-not (Test-Path -LiteralPath $status -PathType Leaf)) {
      Write-Host "==> tracked process exit status file is missing: $status; treating as failure"
    } else {
      $statusText = Get-Content -LiteralPath $status -Raw -ErrorAction SilentlyContinue
      $statusValue = if ($null -eq $statusText) { "" } else { $statusText.Trim() }
      $parsedCode = 0
      if ($statusValue -notmatch '^-?\d+$' -or
          -not [int]::TryParse($statusValue, [ref]$parsedCode)) {
        $displayStatus = if ($statusValue) { $statusValue } else { "<empty>" }
        Write-Host "==> tracked process exit status is invalid: '$displayStatus'; treating as failure"
      } else {
        $code = $parsedCode
        Write-Host "==> tracked process exit code: $code"
      }
    }
    if ($code -ne 0) {
      if (Test-Path $log) {
        if ($FullFailureOutput) {
          Write-Host "==> tracked process stdout (complete)"
          Get-Content $log | ForEach-Object { Write-Host "  ! | $_" }
        } else {
          Get-Content $log -Tail 200 | ForEach-Object { Write-Host "  ! | $_" }
        }
      }
      if (Test-Path $err) { Get-Content $err | ForEach-Object { Write-Host "  ! | $_" } }
    }
    return $code
  } finally {
    Remove-Item $wrapper, $status -ErrorAction SilentlyContinue
  }
}

function Get-FreeGB { return [math]::Round((Get-PSDrive C).Free / 1GB, 1) }

function Resolve-7Zip {
  $command = Get-Command 7z.exe -ErrorAction SilentlyContinue
  if ($command) { return $command.Source }
  $installed = "$env:ProgramFiles\7-Zip\7z.exe"
  if (Test-Path $installed) { return $installed }
  throw "7z.exe is not available"
}

function Save-Handoff {
  param([ValidateSet("Synced", "Unsynced")] [string]$Mode)
  if (Test-LastStage) { throw "build did not finish within $MaxStages stages" }
  Write-OutVar upload_parts true
  . "$PSScriptRoot\ci-parts.ps1" -Root $Root -PartsDir $PartsDir -Mode $Mode
}

function Assert-CiScripts {
  foreach ($path in @(
    "$PSScriptRoot\ci-stage.ps1",
    "$PSScriptRoot\ci-parts.ps1",
    "$PSScriptRoot\prepare-ungoogled.ps1",
    "$PSScriptRoot\update-restored-source.ps1",
    "$PSScriptRoot\package-win.ps1"
  )) {
    $tokens = $null
    $errors = $null
    [Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors) | Out-Null
    if ($errors.Count -gt 0) { throw "$path failed PowerShell parsing: $($errors[0].Message)" }
  }
  Write-Host "==> CI PowerShell preflight passed"
}

function Free-Disk {
  Write-Host "==> disk before cleanup: $(Get-FreeGB) GB free"
  foreach ($target in @(
    "C:\Android",
    "C:\Program Files\Android",
    "C:\Program Files (x86)\Android",
    "C:\ghcup",
    "C:\Program Files\Haskell",
    "C:\Program Files\MySQL",
    "C:\Program Files\PostgreSQL",
    "C:\Program Files\MongoDB",
    "C:\Miniconda3",
    "C:\Program Files\LLVM",
    "C:\ProgramData\chocolatey\cache",
    "C:\Windows\SoftwareDistribution\Download"
  )) {
    if (Test-Path $target) { Remove-Item -Recurse -Force $target -ErrorAction SilentlyContinue }
  }
  Write-Host "==> disk after cleanup: $(Get-FreeGB) GB free"
}

function Initialize-VisualStudio {
  $vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
  if (-not (Test-Path $vswhere)) { throw "vswhere.exe is not available: $vswhere" }
  $installation = (& $vswhere -latest -products * `
    -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
    -property installationPath).Trim()
  if (-not $installation) { throw "Visual Studio 2022 C++ tools are not installed" }
  $devCmd = Join-Path $installation "Common7\Tools\VsDevCmd.bat"
  if (-not (Test-Path $devCmd)) { throw "VsDevCmd.bat is missing: $devCmd" }

  $command = "`"$devCmd`" -no_logo -arch=x64 -host_arch=x64 >nul && set"
  $environment = & $env:COMSPEC /d /s /c $command
  if ($LASTEXITCODE -ne 0) { throw "VsDevCmd.bat failed with exit $LASTEXITCODE" }
  foreach ($line in $environment) {
    if ($line -match '^([^=]+)=(.*)$') {
      [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], "Process")
    }
  }
  $compiler = (Get-Command cl.exe -ErrorAction Stop).Source
  Write-Host "==> Visual Studio compiler: $compiler"
}

function Install-Debuggers {
  $dbghelp = "${env:ProgramFiles(x86)}\Windows Kits\10\Debuggers\x64\dbghelp.dll"
  if (Test-Path $dbghelp) { return }
  Write-Host "==> installing Windows SDK Debugging Tools"
  New-Item -ItemType Directory -Force -Path $Root | Out-Null
  $iso = "$Root\winsdk.iso"
  for ($attempt = 1; $attempt -le 5; $attempt++) {
    & curl.exe -sSL -o $iso "https://go.microsoft.com/fwlink/?linkid=2348707"
    if ((Test-Path $iso) -and ((Get-Item $iso).Length -gt 10MB)) { break }
    Start-Sleep -Seconds 10
  }
  if (-not (Test-Path $iso) -or ((Get-Item $iso).Length -lt 10MB)) { throw "Windows SDK ISO download failed" }
  $image = Mount-DiskImage -ImagePath $iso -StorageType ISO -PassThru
  $letter = ($image | Get-Volume).DriveLetter
  try {
    $setup = Start-Process -FilePath "$letter`:\WinSDKSetup.exe" `
      -ArgumentList "/features", "OptionId.WindowsDesktop.Debuggers", "/q", "/norestart" -PassThru -Wait
    if ($setup.ExitCode -ne 0) { throw "WinSDKSetup failed with exit $($setup.ExitCode)" }
  } finally {
    Dismount-DiskImage -ImagePath $iso | Out-Null
    Remove-Item $iso -Force -ErrorAction SilentlyContinue
  }
  if (-not (Test-Path $dbghelp)) { throw "dbghelp.dll is missing after Debugging Tools install" }
}

function Invoke-BoundedBrowser {
  param(
    [Parameter(Mandatory)] [string]$Launcher,
    [Parameter(Mandatory)] [string[]]$Arguments,
    [Parameter(Mandatory)] [string]$WorkingDirectory,
    [int]$TimeoutSec = 60
  )
  $id = [Guid]::NewGuid().ToString('N')
  $stdout = Join-Path $env:TEMP "chromix-browser-$id.out"
  $stderr = Join-Path $env:TEMP "chromix-browser-$id.err"
  try {
    $process = Start-Process -FilePath $Launcher -ArgumentList $Arguments `
      -WorkingDirectory $WorkingDirectory -PassThru -WindowStyle Hidden `
      -RedirectStandardOutput $stdout -RedirectStandardError $stderr
    $stopwatch = [Diagnostics.Stopwatch]::StartNew()
    while (-not $process.HasExited) {
      if ($stopwatch.Elapsed.TotalSeconds -gt $TimeoutSec) {
        try { & taskkill.exe /PID $process.Id /T /F 2>&1 | Out-Null } catch {}
        $process.WaitForExit()
        throw "browser smoke command timed out after $TimeoutSec seconds"
      }
      Start-Sleep -Milliseconds 250
    }
    $output = if (Test-Path $stdout) { Get-Content $stdout -Raw } else { "" }
    $errors = if (Test-Path $stderr) { Get-Content $stderr -Raw } else { "" }
    Write-Host $output
    if ($errors) { Write-Host $errors }
    if ($process.ExitCode -ne 0) {
      throw "browser smoke command failed with exit $($process.ExitCode)"
    }
    return $output
  } finally {
    Remove-Item $stdout, $stderr -Force -ErrorAction SilentlyContinue
  }
}

function Verify-FinalBundle {
  $asset = Join-Path $Root "dist\chromix-win-x64.zip"
  $manifest = Join-Path $Root "dist\SHA256SUMS"
  if (-not (Test-Path $asset) -or -not (Test-Path $manifest)) {
    throw "final Windows bundle or SHA256SUMS is missing"
  }
  $entry = Get-Content $manifest | Where-Object { $_ -match '^([0-9a-fA-F]{64})\s+chromix-win-x64\.zip$' }
  if ($entry.Count -ne 1) { throw "SHA256SUMS has no unique Windows ZIP entry" }
  $expected = [regex]::Match($entry[0], '^([0-9a-fA-F]{64})').Groups[1].Value.ToLowerInvariant()
  $actual = (Get-FileHash $asset -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($actual -ne $expected) { throw "Windows ZIP checksum mismatch" }
  Write-Host "==> Windows ZIP checksum verified: $actual"

  $smokeRoot = Join-Path $Root "smoke"
  Remove-Item $smokeRoot -Recurse -Force -ErrorAction SilentlyContinue
  New-Item -ItemType Directory -Force -Path $smokeRoot | Out-Null
  Expand-Archive -LiteralPath $asset -DestinationPath $smokeRoot -Force
  $bundle = Join-Path $smokeRoot "chromix"
  $launcher = Join-Path $bundle "chromix.cmd"
  $chrome = Join-Path $bundle "chrome.exe"
  if (-not (Test-Path $launcher) -or -not (Test-Path $chrome)) {
    throw "extracted Windows bundle is missing chromix.cmd or chrome.exe"
  }
  $version = Invoke-BoundedBrowser -Launcher $launcher -Arguments @("--version") `
    -WorkingDirectory $bundle -TimeoutSec 30
  if ($version -notmatch [regex]::Escape($Revisions.ChromiumVersion)) {
    throw "extracted Windows browser version does not match the pinned Chromium version"
  }
  $profile = Join-Path $smokeRoot "profile"
  $dom = Invoke-BoundedBrowser -Launcher $launcher -Arguments @(
    "--headless", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    "--user-data-dir=$profile", "--dump-dom", "data:text/html,<p>chromix-smoke-ok</p>"
  ) -WorkingDirectory $bundle -TimeoutSec 60
  if ($dom -notmatch '<p>chromix-smoke-ok</p>') {
    throw "extracted Windows browser did not render the smoke page"
  }
  Write-Host "==> Windows ZIP extraction, version, and headless smoke checks passed"
}

Write-Host "==> Chromix CI stage $StageIndex | Chromium $($Revisions.ChromiumVersion) | remaining $(Get-RemainingMin) min"
Write-OutVar finished false
Write-OutVar upload_parts false
Assert-CiScripts
Free-Disk
Initialize-VisualStudio
Install-Debuggers
git config --global core.longpaths true

Remove-Item Env:PYTHONUTF8 -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONIOENCODING -ErrorAction SilentlyContinue
$env:DEPOT_TOOLS_WIN_TOOLCHAIN = "0"
$env:DEPOT_TOOLS_METRICS = "0"
$env:DEPOT_TOOLS_COLLECT_METRICS = "0"

if ($FromArtifact -and -not (Test-Path "C:\restore\tree.7z.001")) {
  throw "resume artifact missing: C:\restore\tree.7z.001"
}
if (-not $FromArtifact -and $StageIndex -gt 1) {
  throw "stage $StageIndex requires -FromArtifact"
}
if ($FromArtifact) {
  $sevenZip = Resolve-7Zip
  & $sevenZip t "C:\restore\tree.7z.001" | Select-Object -Last 3
  if ($LASTEXITCODE -ne 0) { throw "7z archive test failed" }
  & $sevenZip x "C:\restore\tree.7z.001" -o"$Root" -y | Select-Object -Last 3
  if ($LASTEXITCODE -ne 0) { throw "7z restore failed" }
  Remove-Item C:\restore -Recurse -Force -ErrorAction SilentlyContinue
}

$domainProgress = Join-Path $Src ".chromix-domain-substitution-in-progress"
$domainMarker = Join-Path $Src ".chromix-domain-substituted"
$restoreReceipt = Join-Path $Src ".chromix-upstream-restored.json"
if (Get-ChildItem -LiteralPath $WorkDir -Directory -Filter ".chromix-upstream-restore-*" -ErrorAction SilentlyContinue) {
  throw "upstream restore transaction was interrupted; use a clean work directory"
}
if (Test-Path $restoreReceipt) {
  Write-Host "==> verifying restored upstream source receipt and pins"
  & python (Join-Path $Repo "tools\restore_upstream_cache.py") --phase verify `
    --platform windows --arch x64 --workdir $WorkDir
  if ($LASTEXITCODE -ne 0) { throw "restored upstream source verification failed (exit $LASTEXITCODE)" }
  $RestoredUpstream = $true
  $OutDir = "$Src\out\Default"
}
if (Test-Path $domainProgress) {
  throw "domain substitution was interrupted; use a clean work directory"
}
if ($RequireUpstreamCache -and -not $RestoredUpstream -and
    ($FromArtifact -or $StageIndex -ne 1 -or (Test-Path $Src))) {
  throw "required upstream cache: restore receipt missing; refusing cold preparation or compilation"
}

$MigrateRestoredSource = $false
if ($FromArtifact -and -not $RestoredUpstream) {
  $unpackedMarker = Join-Path $Src ".chromix-source-unpacked"
  $readyMarker = Join-Path $Src ".chromix-source-ready"
  $restoredVersion = ""
  if (Test-Path $unpackedMarker) {
    $restoredVersion = (Get-Content $unpackedMarker -Raw).Trim()
  } elseif (Test-Path $readyMarker) {
    $restoredVersion = ((Get-Content $readyMarker -Raw).Trim() -split '\|', 2)[0]
  }
  if ($restoredVersion -and $restoredVersion -ne $Revisions.ChromiumVersion) {
    Write-Host "==> restored tree targets Chromium $restoredVersion; preserving tooling/download_cache and removing incompatible src/out"
    Remove-Item $Src -Recurse -Force
  } elseif (Test-Path $readyMarker) {
    $MigrateRestoredSource = -not $RestoredUpstream
  } else {
    Write-Host "==> restored source is not ready; deferring migrations until patch preparation completes"
  }
}

if ($StageIndex -eq 1 -and -not $FromArtifact -and
    -not (Test-Path $Src) -and $RequireUpstreamCache) {
  # Leave the stage reserve and at least 30 minutes for restore/preparation.
  $fetchTimeoutSec = [Math]::Min(3600, ((Get-RemainingMin) - $PackReserveMin - 30) * 60)
  if ($fetchTimeoutSec -lt 60) {
    throw "required upstream cache: insufficient stage budget for restore"
  }
  $fetchArgs = @(
    (Join-Path $Repo "tools\fetch_upstream_cache.py"),
    "--platform", "windows", "--arch", "x64", "--destination", $UpstreamCacheDir
  )
  if ($UpstreamRunId) { $fetchArgs += @("--run-id", $UpstreamRunId) }
  # Bound download/extraction by both the cap and this job's remaining deadline.
  $fetchCommandLine = ($fetchArgs | ForEach-Object { "`"$_`"" }) -join " "
  $fetchRc = Invoke-Tracked -File (Get-Command python -ErrorAction Stop).Source `
    -ArgList $fetchCommandLine -Cwd $Repo -TimeoutSec $fetchTimeoutSec
  if ($fetchRc -eq 124) {
    throw "required upstream cache fetch timed out"
  } elseif ($fetchRc -ne 0) {
    throw "upstream cache fetch helper failed (exit $fetchRc)"
  }
  python (Join-Path $Repo "tools\restore_upstream_cache.py") --phase restore `
    --platform windows --arch x64 --workdir $WorkDir --cache-dir $UpstreamCacheDir
  if ($LASTEXITCODE -ne 0) { throw "upstream restore helper failed (exit $LASTEXITCODE)" }
  if (-not (Test-Path -LiteralPath $restoreReceipt -PathType Leaf)) {
    throw "required upstream cache: restore receipt missing after restore; refusing cold preparation or compilation"
  }
  & python (Join-Path $Repo "tools\restore_upstream_cache.py") --phase verify `
    --platform windows --arch x64 --workdir $WorkDir
  if ($LASTEXITCODE -ne 0) { throw "restored upstream source verification failed (exit $LASTEXITCODE)" }
  $RestoredUpstream = $true
  $OutDir = "$Src\out\Default"
  Write-Host "==> restored upstream source/out/Default; appending Chromix patches before incremental Ninja"
}

if (-not (Test-Path (Join-Path $Src ".chromix-source-ready")) -and
    (Get-RemainingMin) -lt ($PackReserveMin + 30)) {
  if ($ValidateOnly) { throw "validate-only: insufficient preparation budget" }
  Save-Handoff -Mode Unsynced
  return
}
$prepareDeadline = [DateTimeOffset]::new($Deadline).ToUnixTimeSeconds()
try {
  # Revalidate ready markers on every stage, including artifact resumes.
  & "$PSScriptRoot\prepare-ungoogled.ps1" -Root $WorkDir -Repo $Repo `
    -DeadlineEpoch $prepareDeadline -ReserveMinutes $PackReserveMin
} catch {
  if ($_.Exception.Message -like "PREPARE_BUDGET_EXHAUSTED:*") {
    if ($ValidateOnly) { throw }
    Save-Handoff -Mode Unsynced
    return
  }
  throw
}
if ($MigrateRestoredSource) {
  & "$PSScriptRoot\update-restored-source.ps1" -Src $Src -OutDir $OutDir
}

$UngoogledTooling = Join-Path $WorkDir "tooling\ungoogled-chromium"
$WindowsTooling = Join-Path $WorkDir "tooling\ungoogled-chromium-windows"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$gnArgs = Join-Path $OutDir "args.gn"
$mergeArgs = @((Join-Path $Repo "tools\merge_gn_args.py"), $gnArgs)
if ($RestoredUpstream) { $mergeArgs += $gnArgs }
$mergeArgs += @(
  (Join-Path $UngoogledTooling "flags.gn"),
  (Join-Path $WindowsTooling "flags.windows.gn"),
  (Join-Path $Repo "build\args.windows.gn")
)
python @mergeArgs
if ($LASTEXITCODE -ne 0) { throw "GN argument merge failed" }

$env:PATH = "$(Join-Path $Src 'third_party\ninja');$(Join-Path $Src 'third_party\node\win');$env:PATH"
$Ninja = Join-Path $Src "third_party\ninja\ninja.exe"
if ($RestoredUpstream) {
  $Ninja = & python (Join-Path $Repo "tools\restore_ninja.py") --workdir $WorkDir --platform windows --arch x64
  if ($LASTEXITCODE -ne 0 -or -not $Ninja) { throw "restored Ninja compatibility check failed" }
  $env:NINJA = $Ninja
}
Push-Location $Src
try {
  if (-not (Test-Path "third_party\rust-toolchain\bin\bindgen.exe")) {
    # bindgen's build script hard-requires cargo+rustc that prepare merged
    # into third_party\rust-toolchain; failing fast here with the directory
    # state keeps a silent merge regression from dying 45 minutes of ninja
    # bootstrap output later with only a bare missing-cargo line.
    foreach ($binary in @("cargo.exe", "rustc.exe")) {
      if (-not (Test-Path "third_party\rust-toolchain\bin\$binary")) {
        Get-ChildItem third_party -Directory |
          Where-Object { $_.Name -like "rust-toolchain*" } | ForEach-Object {
            Write-Host "    toolchain dir: $($_.Name)"
          }
        throw ("bindgen precondition failed: third_party\rust-toolchain\bin\$binary is missing")
      }
    }
    if ($RestoredUpstream) {
      # Only restore known tool-download endpoints, never browser source domains.
      $normalizeToolUrls = @'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]) / 'tools'))
from upstream_script_identity import ENDPOINTS, RESTORED
src = Path(sys.argv[2])
for relative, keys in RESTORED.items():
    path = src / relative
    original = path.read_bytes()
    normalized = original
    for key in keys:
        before, after = ENDPOINTS[key]
        normalized = normalized.replace(before.encode('ascii'), after.encode('ascii'))
    if normalized != original:
        path.write_bytes(normalized)
'@
      python -c $normalizeToolUrls $Repo $Src
      if ($LASTEXITCODE -ne 0) { throw "restored tool download endpoint normalization failed" }
    }
    python tools\rust\build_bindgen.py --skip-test
    if ($LASTEXITCODE -ne 0) { throw "bindgen build failed" }
  }
  if ($RestoredUpstream) {
    python (Join-Path $Repo "tools\prepare_restored_build.py") --phase finish `
      --platform windows --arch x64 --workdir $WorkDir
    if ($LASTEXITCODE -ne 0) { throw "restored build preparation failed (exit $LASTEXITCODE)" }
  }
  $gn = Join-Path $OutDir "gn.exe"
  if (-not (Test-Path $gn)) {
    python tools\gn\bootstrap\bootstrap.py -o $gn --skip-generate-buildfiles
    if ($LASTEXITCODE -ne 0) { throw "GN bootstrap failed" }
  }
  if (-not (Test-Path $domainMarker)) {
    # The pinned helper selects compression from the cache filename's suffix.
    $domainCache = Join-Path $WorkDir "domain_substitution_cache.tar.gz"
    if ((Test-Path $domainCache) -or
        (Test-Path (Join-Path $WorkDir "domain_substitution_cache.tar"))) {
      throw "domain substitution cache exists without a completion marker; use a clean work directory"
    }
    Set-Content -Path $domainProgress -Value $Revisions.UngoogledCommit -Encoding ASCII
    Write-Host "==> applying ungoogled domain substitution"
    python (Join-Path $UngoogledTooling "utils\domain_substitution.py") apply `
      -r (Join-Path $UngoogledTooling "domain_regex.list") `
      -f (Join-Path $WindowsTooling "domain_substitution.list") `
      -c $domainCache $Src
    if ($LASTEXITCODE -ne 0) { throw "domain substitution failed" }
    Move-Item -LiteralPath $domainProgress -Destination $domainMarker
  }
  & $gn gen $OutDir --fail-on-unused-args
  if ($LASTEXITCODE -ne 0) { throw "gn gen failed" }
  if ($RestoredUpstream) {
    Write-Host "==> recording incremental Ninja plan for restored upstream source/out/Default"
    & $Ninja -C $OutDir -n chrome `
      *> (Join-Path $WorkDir "upstream-cache-plan.log")
    if ($LASTEXITCODE -ne 0) { throw "restored upstream build-plan check failed" }
  }
} finally {
  Pop-Location
}

if ($ValidateOnly) {
  Write-Host "==> validate-only: building V8 Torque generation target"
  $validationBudget = (Get-RemainingMin) - $PackReserveMin
  if ($validationBudget -lt 1) { throw "validate-only: insufficient V8 Torque budget" }
  $validationRc = Invoke-Tracked -File $Ninja `
    -ArgList "-C `"$OutDir`" -j 1 -v gen/v8/torque-generated/bit-field-asserts.cc" `
    -Cwd $Src -TimeoutSec ($validationBudget * 60) -FullFailureOutput
  if ($validationRc -ne 0) { throw "V8 Torque validation failed (exit $validationRc)" }
  Write-Host "==> validate-only: gn gen and V8 Torque generation passed"
  Write-OutVar finished true
  return
}

$ninjaBudget = (Get-RemainingMin) - $PackReserveMin
if ($ninjaBudget -lt 20) {
  Save-Handoff -Mode Synced
  return
}
$rc = Invoke-Tracked -File $Ninja `
  -ArgList "-C `"$OutDir`" -j 4 chrome" -Cwd $Src -TimeoutSec ($ninjaBudget * 60)

if ($rc -eq 0) {
  New-Item -ItemType Directory -Force -Path "$Root\dist" | Out-Null
  & "$PSScriptRoot\package-win.ps1" -Out $OutDir -Dest "$Root\dist"
  Verify-FinalBundle
  Write-OutVar finished true
  return
}
if ($rc -eq 124) {
  Save-Handoff -Mode Synced
  return
}
throw "ninja failed (exit $rc)"
