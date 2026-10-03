# Run only on an ephemeral GitHub-hosted Windows runner. Never use real accounts.
# Credentials remain SecureStrings in this process; no password command lines,
# task definitions, environment variables, files, or log output are generated.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Python,
    [Parameter(Mandatory = $true)][string]$Wheel,
    [Parameter(Mandatory = $true)][string]$BetaPack,
    [Parameter(Mandatory = $true)][string]$Output
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($env:GITHUB_ACTIONS -ne 'true' -or $env:RUNNER_OS -ne 'Windows' -or
    $env:RUNNER_ENVIRONMENT -ne 'github-hosted') {
    throw 'This account-creating exercise is restricted to ephemeral Windows CI.'
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'The CI orchestrator must be elevated to create disposable standard accounts.'
}

function Set-ExerciseAcl([string]$Path, [hashtable]$Users) {
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $grants = @{ 'S-1-5-18' = 'FullControl'; 'S-1-5-32-544' = 'FullControl' }
    foreach ($sid in $Users.Keys) { $grants[$sid] = $Users[$sid] }
    foreach ($sid in $grants.Keys) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($sid),
            [Security.AccessControl.FileSystemRights]$grants[$sid],
            [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit',
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        )
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function New-ExerciseAccount([string]$Name) {
    $bytes = [byte[]]::new(32)
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
    $secret = ConvertTo-SecureString ([Convert]::ToBase64String($bytes) + 'aA1!') -AsPlainText -Force
    [Array]::Clear($bytes, 0, $bytes.Length)
    $user = New-LocalUser -Name $Name -Password $secret -AccountNeverExpires -PasswordNeverExpires
    # Record immediately so a failed group change still removes the account.
    $script:accounts.Add($user)
    $script:secrets.Add($secret)
    Add-LocalGroupMember -SID 'S-1-5-32-545' -Member $user
    $adminMembers = @(Get-LocalGroupMember -SID 'S-1-5-32-544')
    if ($adminMembers.SID.Value -contains $user.SID.Value) {
        throw 'The disposable account unexpectedly belongs to Administrators.'
    }
    return [pscustomobject]@{
        Sid = $user.SID.Value
        Credential = [Management.Automation.PSCredential]::new("$env:COMPUTERNAME\$Name", $secret)
    }
}

function Start-Exercise([string]$Mode, $Account, $Peer, [string]$Work, [string]$PeerWork) {
    $arguments = @(
        '-I', $script:smokeScript, $Mode,
        '--root', $Work, '--expected-sid', $Account.Sid,
        '--peer-sid', $Peer.Sid, '--peer-root', $PeerWork,
        '--runtime', $script:installedRoot, '--pack', $script:stagedPack
    )
    # Only generated paths/SIDs/modes are passed. Passwords remain in PSCredential.
    $quoted = ($arguments | ForEach-Object { '"' + $_.Replace('"', '\"') + '"' }) -join ' '
    $process = Start-Process -FilePath $script:installedPython -ArgumentList $quoted `
        -Credential $Account.Credential -LoadUserProfile -UseNewEnvironment `
        -WorkingDirectory $Work -PassThru `
        -RedirectStandardOutput (Join-Path $Work 'stdout.txt') `
        -RedirectStandardError (Join-Path $Work 'stderr.txt')
    $script:processes.Add($process)
    return $process
}

function Read-ExerciseReport([string]$Work) {
    $path = Join-Path $Work 'result.json'
    if (-not (Test-Path -LiteralPath $path)) { throw 'No account report was produced.' }
    return Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
}

$accounts = [Collections.Generic.List[object]]::new()
$secrets = [Collections.Generic.List[object]]::new()
$processes = [Collections.Generic.List[object]]::new()
$exerciseId = [Guid]::NewGuid().ToString('N')
$stage = Join-Path $env:ProgramData "VeilCI-$exerciseId"
$outputPath = [IO.Path]::GetFullPath($Output)
if (Test-Path -LiteralPath $outputPath) { throw 'Output must be a new file.' }
$report = [ordered]@{
    schema = 1
    scope = 'windows_standard_user_local_cli_storage'
    state = 'fail'
    stage = 'provision'
    cleanup = 'not_run'
    client_journey = 'not_run'
    owner = $null
    outsider = $null
}
$ownerWork = $null
$outsiderWork = $null
try {
    $wheelPath = (Resolve-Path -LiteralPath $Wheel).Path
    $packPath = (Resolve-Path -LiteralPath $BetaPack).Path
    $pythonPath = (Resolve-Path -LiteralPath $Python).Path
    $ownerAccount = New-ExerciseAccount "veil_o_$($exerciseId.Substring(0, 10))"
    $outsiderAccount = New-ExerciseAccount "veil_x_$($exerciseId.Substring(0, 10))"
    New-Item -ItemType Directory -Path $stage | Out-Null
    $readers = @{ ($ownerAccount.Sid) = 'ReadAndExecute'; ($outsiderAccount.Sid) = 'ReadAndExecute' }
    Set-ExerciseAcl $stage $readers
    $ownerWork = Join-Path $stage 'owner'
    $outsiderWork = Join-Path $stage 'outsider'
    foreach ($work in @($ownerWork, $outsiderWork)) {
        New-Item -ItemType Directory -Path $work | Out-Null
    }
    Set-ExerciseAcl $ownerWork @{
        ($ownerAccount.Sid) = 'FullControl'; ($outsiderAccount.Sid) = 'ReadAndExecute'
    }
    Set-ExerciseAcl $outsiderWork @{
        ($outsiderAccount.Sid) = 'FullControl'; ($ownerAccount.Sid) = 'ReadAndExecute'
    }

    $report.stage = 'stage_installed_wheel'
    # Copy only a disposable Python runtime; never broaden ACLs on the runner's
    # real home, tool cache, checkout, or installed interpreter.
    $baseRoot = & $pythonPath -I -c 'import pathlib, sys; print(pathlib.Path(sys._base_executable).resolve().parent)'
    if ($LASTEXITCODE -ne 0) { throw 'Cannot locate the Python base runtime.' }
    $runtimeRoot = Join-Path $stage 'python'
    Copy-Item -LiteralPath $baseRoot.Trim() -Destination $runtimeRoot -Recurse
    $basePython = Join-Path $runtimeRoot 'python.exe'
    $installedRoot = Join-Path $stage 'installed'
    & uv venv --python $basePython $installedRoot 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create the staged environment.' }
    $installedPython = Join-Path $installedRoot 'Scripts\python.exe'
    & uv pip install --link-mode copy --python $installedPython "$wheelPath`[desktop`]" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot install the wheel and desktop extra.' }
    $smokeScript = Join-Path $stage 'windows_standard_user.py'
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'windows_standard_user.py') -Destination $smokeScript
    $stagedPack = Join-Path $stage 'beta.zip'
    Copy-Item -LiteralPath $packPath -Destination $stagedPack

    $report.stage = 'owner_checks'
    $ownerProcess = Start-Exercise 'owner' $ownerAccount $outsiderAccount $ownerWork $outsiderWork
    $deadline = [DateTime]::UtcNow.AddSeconds(240)
    while (-not (Test-Path -LiteralPath (Join-Path $ownerWork 'ready'))) {
        $ownerProcess.Refresh()
        if ($ownerProcess.HasExited) {
            $report.owner = Read-ExerciseReport $ownerWork
            throw 'Owner checks failed before the isolation probe.'
        }
        if ([DateTime]::UtcNow -gt $deadline) { throw 'Owner checks timed out.' }
        Start-Sleep -Milliseconds 100
    }

    $report.stage = 'outsider_checks'
    $outsiderProcess = Start-Exercise 'outsider' $outsiderAccount $ownerAccount $outsiderWork $ownerWork
    if (-not $outsiderProcess.WaitForExit(120000)) { throw 'Outsider checks timed out.' }
    $report.outsider = Read-ExerciseReport $outsiderWork
    if ($outsiderProcess.ExitCode -ne 0 -or $report.outsider.state -ne 'pass') {
        throw 'The cross-account isolation probe failed.'
    }
    Set-Content -LiteralPath (Join-Path $ownerWork 'finish') -Value 'pass' -Encoding utf8
    if (-not $ownerProcess.WaitForExit(60000)) { throw 'Owner completion timed out.' }
    $report.owner = Read-ExerciseReport $ownerWork
    if ($ownerProcess.ExitCode -ne 0 -or $report.owner.state -ne 'pass') {
        throw 'Owner completion failed.'
    }
    $report.stage = 'complete'
    $report.state = 'pass'
} catch {
    # Do not emit captured child output or exception details. The stage and
    # allowlisted per-check reports identify the failed boundary.
    $report.state = 'fail'
} finally {
    $cleanupFailed = $false
    foreach ($process in $processes) {
        try {
            $process.Refresh()
            if (-not $process.HasExited) {
                & taskkill /PID $process.Id /T /F 2>&1 | Out-Null
                if ($LASTEXITCODE -ne 0) { throw 'Process cleanup failed.' }
                $null = $process.WaitForExit(10000)
            }
            $process.Dispose()
        } catch { $cleanupFailed = $true }
    }
    # Preserve partial fixed-code progress if a peer or orchestration step failed.
    foreach ($entry in @(@('owner', $ownerWork), @('outsider', $outsiderWork))) {
        if ($null -ne $entry[1] -and $null -eq $report[$entry[0]]) {
            try { $report[$entry[0]] = Read-ExerciseReport $entry[1] } catch { }
        }
    }
    foreach ($account in $accounts) {
        # Windows may take a moment to release a just-exited logon profile.
        for ($attempt = 0; $attempt -lt 5; $attempt++) {
            try {
                $profile = Get-CimInstance Win32_UserProfile -Filter "SID='$($account.SID.Value)'"
                if ($null -ne $profile) {
                    if ($profile.Loaded) { throw 'Disposable profile is still loaded.' }
                    $profile | Remove-CimInstance
                }
                break
            } catch {
                if ($attempt -eq 4) { $cleanupFailed = $true }
                else { Start-Sleep -Milliseconds 500 }
            }
        }
        try { Remove-LocalUser -SID $account.SID } catch { $cleanupFailed = $true }
    }
    foreach ($secret in $secrets) { $secret.Dispose() }
    try {
        if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
    } catch { $cleanupFailed = $true }
    if ($cleanupFailed) {
        $report.cleanup = 'fail'
        $report.state = 'fail'
    } else {
        $report.cleanup = 'pass'
    }
}
$json = $report | ConvertTo-Json -Depth 8
$stream = [IO.File]::Open($outputPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
$writer = [IO.StreamWriter]::new($stream)
try { $writer.WriteLine($json) } finally { $writer.Dispose() }
Write-Output $json
if ($report.state -ne 'pass') { exit 1 }
