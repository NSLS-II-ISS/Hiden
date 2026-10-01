#requires -Version 5.1
<#
.SYNOPSIS
Inspect or configure Windows Time on the MASsoft VM XF08ID-WS7.
.DESCRIPTION
Read-only by default. Applying changes requires an elevated PowerShell session
and -Apply -ExperimentStopped. The operator must first stop the MASsoft scan
and set the IOC's Acquire=0. This script cannot verify instrument state.
It does not change the time zone, VMware providers, firewall, or IOC settings.
.EXAMPLE
.\scripts\configure-massoft-time.ps1
.EXAMPLE
.\scripts\configure-massoft-time.ps1 -Apply -ExperimentStopped -WhatIf
.EXAMPLE
.\scripts\configure-massoft-time.ps1 -Apply -ExperimentStopped
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [switch]$Apply,
    [switch]$ExperimentStopped
)

function Test-MasSoftTimeAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-MasSoftW32Time {
    param([string[]]$Arguments)

    & w32tm.exe @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "w32tm $($Arguments -join ' ') failed (exit $LASTEXITCODE)."
    }
}

function Show-MasSoftTimeStatus {
    Write-Host '--- VM clock and Windows Time service ---'
    Get-Date -Format o
    Get-Service -Name W32Time | Format-List Name, Status, StartType

    foreach ($query in 'source', 'status', 'peers', 'configuration') {
        Write-Host "--- Windows Time $query ---"
        Invoke-MasSoftW32Time -Arguments @('/query', "/$query")
    }

    Write-Host '--- NTP offset samples (read-only) ---'
    Invoke-MasSoftW32Time -Arguments @(
        '/stripchart', '/computer:time1.nsls2.bnl.gov', '/dataonly', '/samples:5'
    )
    Write-Host 'Review source, leap indicator, last successful sync, and offsets.'
    Write-Host 'Command completion alone does not establish synchronization accuracy.'
}

function Invoke-MasSoftTime {
    [CmdletBinding(SupportsShouldProcess = $true)]
    param(
        [switch]$Apply,
        [switch]$ExperimentStopped
    )

    $ErrorActionPreference = 'Stop'
    if ($env:OS -ne 'Windows_NT' -or $env:COMPUTERNAME -ne 'XF08ID-WS7') {
        throw 'Run this only on the MASsoft Windows VM: XF08ID-WS7.'
    }
    if (-not (Test-MasSoftTimeAdministrator)) {
        throw 'Open PowerShell with Run as administrator.'
    }

    if ($Apply) {
        if (-not $ExperimentStopped) {
            throw 'Stop the MASsoft experiment, set Acquire=0, then pass -ExperimentStopped.'
        }
        if ((Get-CimInstance -ClassName Win32_ComputerSystem).PartOfDomain) {
            throw 'VM is now domain-joined. Consult support before overriding domain time.'
        }

        # Use the three beamline peers, not accelerator or SDCC fallback servers.
        $peers = 'time1.nsls2.bnl.gov,0x8 time2.nsls2.bnl.gov,0x8 time3.nsls2.bnl.gov,0x8'
        if (-not $PSCmdlet.ShouldProcess(
            'XF08ID-WS7 Windows Time',
            "Set automatic startup, configure NTP peers $peers, and resync (clock may jump)"
        )) {
            return
        }

        Write-Host '--- Previous configuration ---'
        Invoke-MasSoftW32Time -Arguments @('/query', '/configuration')
        try {
            Set-Service -Name W32Time -StartupType Automatic
            Start-Service -Name W32Time
            Invoke-MasSoftW32Time -Arguments @(
                '/config', "/manualpeerlist:$peers", '/syncfromflags:manual', '/update'
            )
            Restart-Service -Name W32Time
            Start-Sleep -Seconds 5
            Invoke-MasSoftW32Time -Arguments @('/resync', '/rediscover')
        }
        catch {
            throw "Time configuration did not complete. Changes already made were not rolled back. $($_.Exception.Message)"
        }

        Write-Host 'Resync command completed. Waiting before verification.'
        Start-Sleep -Seconds 15
    }

    Show-MasSoftTimeStatus
}

# Dot-sourcing loads functions only, so tests can replace OS calls without side effects.
if ($MyInvocation.InvocationName -ne '.') {
    Invoke-MasSoftTime @PSBoundParameters
}
