<#
  Build-only workaround for Node/WASM native access violations on Windows.
  Use explicit WASM bounds checks instead of the trap-handler path. This does
  not disable WASM, alter Chromium flags, or turn failed commands into success.
#>
[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$NodePath)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $NodePath -PathType Leaf)) {
  throw "Windows build Node is missing: $NodePath"
}
$flag = '--disable-wasm-trap-handler'
$originalOptions = $env:NODE_OPTIONS
try {
  # Append last so an inherited false value cannot silently undo the workaround.
  # Preserve caller options (including quoted paths), and avoid repeated appends.
  if (-not $env:NODE_OPTIONS -or $env:NODE_OPTIONS.TrimEnd() -notmatch '(?:^|\s)--disable-wasm-trap-handler$') {
    $env:NODE_OPTIONS = ("$originalOptions $flag").Trim()
  }
  # Validate against the bundled Node, not whichever node happens to be on PATH.
  # A real WASM instantiation also guards against accidentally disabling WASM.
  $probe = @'
const bytes = new Uint8Array([0,97,115,109,1,0,0,0]);
new WebAssembly.Instance(new WebAssembly.Module(bytes));
console.log('Build Node ' + process.version + ': WASM explicit bounds checks enabled');
'@
  & $NodePath -e $probe
  if ($LASTEXITCODE -ne 0) {
    throw "Windows build Node/WASM compatibility probe failed (exit $LASTEXITCODE)"
  }
} catch {
  $env:NODE_OPTIONS = $originalOptions
  throw
}
