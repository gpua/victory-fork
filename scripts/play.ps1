# Start a manual play session with the fork's cache and save settings.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Game,
    [string]$Cache = $env:VICTORY_CACHE_DIR,
    [string]$Data = $env:VICTORY_DATA_DIR,
    [switch]$BoundTextureUploads,
    [string]$Exe = "$PSScriptRoot\..\target\release\victory.exe"
)

$ErrorActionPreference = "Stop"
$settings = @{"VICTORY_CACHE_DIR" = $Cache; "VICTORY_DATA_DIR" = $Data}
if ($BoundTextureUploads) {
    $settings["VICTORY_TEXTURE_STAGE_MB"] = "32"
    $settings["VICTORY_TEXTURE_CATCHUP"] = "0"
}
$diagnostics = "VICTORY_KEYS", "VICTORY_CAPTURE", "VICTORY_CAPTURE_FRAMES", "VICTORY_DUMP_TARGETS"
$previous = @{}
try {
    foreach ($name in @($settings.Keys) + $diagnostics) {
        $previous[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
        [Environment]::SetEnvironmentVariable($name, $settings[$name], "Process")
    }
    & $Exe run $Game --execute
    $status = $LASTEXITCODE
}
finally {
    foreach ($name in $previous.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previous[$name], "Process")
    }
}
exit $status
