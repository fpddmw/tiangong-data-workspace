# Host bridge only. Keep the real host identity; stdin is data, never shell code.
param([string]$Workspace)
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$hookEventInput = [Console]::In.ReadToEnd()
$hookWorkspace = if ($Workspace) { $Workspace } else { Split-Path -Parent $PSScriptRoot }
$hookActorId = [Environment]::GetEnvironmentVariable('CODEX_THREAD_ID')
if (-not $hookActorId) { throw 'Actual CODEX_THREAD_ID is unavailable; no parent identity fallback.' }
if ($hookWorkspace -match '^\\\\(?:wsl\$|wsl\.localhost)\\([^\\]+)\\(.*)$') {
    $hookDistribution = $Matches[1]
    $hookLinuxRoot = '/' + $Matches[2].Replace('\', '/')
    $hookBridgePath = Join-Path $PSScriptRoot 'harness_hook.py'
    $hookPrefix = '^\\\\(?:wsl\$|wsl\.localhost)\\' + [regex]::Escape($hookDistribution) + '\\(.*)$'
    if ($hookBridgePath -notmatch $hookPrefix) { throw 'Hook implementation and workspace must use the same WSL distribution.' }
    $hookLinuxScript = '/' + $Matches[1].Replace('\', '/')
    $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $hookEventInput | wsl.exe -d $hookDistribution --cd $hookLinuxRoot --exec env "CODEX_THREAD_ID=$hookActorId" python3 -B $hookLinuxScript --root $hookLinuxRoot
    exit $LASTEXITCODE
}
throw 'This host bridge requires the original WSL workspace; no alternate checkout is selected.'
