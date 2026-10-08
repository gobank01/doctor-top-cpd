$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 "$ProjectDir/run.py" @args
} else {
    & python "$ProjectDir/run.py" @args
}
exit $LASTEXITCODE
