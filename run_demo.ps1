param(
    [string]$OutputDirectory = (Join-Path $PSScriptRoot ('demo-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0,8))),
    [ValidateSet('stdlib', 'auto')][string]$Codecs = 'stdlib'
)
$ErrorActionPreference = 'Stop'
$demoScript = Join-Path $PSScriptRoot 'demo.py'
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 $demoScript --output $OutputDirectory --codecs $Codecs
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    & python $demoScript --output $OutputDirectory --codecs $Codecs
} else {
    throw 'Python 3.11 or later is required. No pip packages are needed.'
}
if ($LASTEXITCODE -ne 0) { throw "Demo failed with exit code $LASTEXITCODE" }
Write-Output "Demo and original-byte restoration verified: $OutputDirectory"
