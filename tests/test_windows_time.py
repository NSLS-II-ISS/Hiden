"""Exercise the VM time helper with mocked OS calls; never change a real clock."""

import json
import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "configure-massoft-time.ps1"


def shell_environment(**overrides):
    # A pytest launched from PS7 must not point Windows PS5 at incompatible PS7 modules.
    env = {key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"}
    env.update(overrides)
    return env


# Dot-source definitions, then replace every OS/network operation before invoking
# the entry point. Each test has a fresh PowerShell process and private environment.
HARNESS = r"""
$ErrorActionPreference = 'Stop'
. $env:HIDEN_TEST_SCRIPT
$script:Calls = [System.Collections.Generic.List[object]]::new()
function Record-Call {
    param($Name, $Arguments)
    $script:Calls.Add(@{name = $Name; arguments = @($Arguments)})
}
function Test-MasSoftTimeAdministrator {
    return $env:HIDEN_TEST_CASE -ne 'not_admin'
}
function Get-CimInstance {
    param($ClassName)
    return [pscustomobject]@{PartOfDomain = ($env:HIDEN_TEST_CASE -eq 'domain')}
}
function Get-Service {
    param($Name)
    return [pscustomobject]@{Name = $Name; Status = 'Running'; StartType = 'Automatic'}
}
function Set-Service {
    param($Name, $StartupType)
    Record-Call 'Set-Service' @($Name, $StartupType)
    if ($env:HIDEN_TEST_CASE -eq 'service_failure') { throw 'Service access denied' }
}
function Start-Service {
    param($Name)
    Record-Call 'Start-Service' @($Name)
}
function Restart-Service {
    param($Name)
    Record-Call 'Restart-Service' @($Name)
}
function Start-Sleep {
    param($Seconds)
    Record-Call 'Start-Sleep' @($Seconds)
}
function w32tm.exe {
    Record-Call 'w32tm' @($args)
    $global:LASTEXITCODE = 0
    $fail = switch ($env:HIDEN_TEST_CASE) {
        'config_failure' { '/config' }
        'resync_failure' { '/resync' }
        'query_failure' { '/query' }
        'stripchart_failure' { '/stripchart' }
        default { 'never' }
    }
    if ($args[0] -eq $fail) { $global:LASTEXITCODE = 42 }
}
$parameters = @{}
if ($env:HIDEN_TEST_CASE -notin @('check', 'cli_wrong_host', 'dot_source', 'query_failure')) {
    $parameters.Apply = $true
    $parameters.ExperimentStopped = $env:HIDEN_TEST_CASE -ne 'not_stopped'
}
if ($env:HIDEN_TEST_CASE -eq 'preview') { $parameters.WhatIf = $true }
$failure = $null
try {
    if ($env:HIDEN_TEST_CASE -eq 'cli_wrong_host') {
        & $env:HIDEN_TEST_SCRIPT
    }
    elseif ($env:HIDEN_TEST_CASE -ne 'dot_source') {
        Invoke-MasSoftTime @parameters
    }
}
catch { $failure = $_.Exception.Message }
$result = @{error = $failure; calls = @($script:Calls.ToArray())}
Write-Output ('RESULT_JSON:' + (ConvertTo-Json -InputObject $result -Depth 6 -Compress))
exit 0
"""


@pytest.fixture(scope="session", params=["powershell", "pwsh"])
def powershell(request):
    if os.name != "nt":
        pytest.skip("Windows time helper is Windows-only")
    executable = shutil.which(request.param)
    if not executable:
        pytest.skip(f"{request.param} is not installed")
    return executable


@lru_cache
def execution_policy(powershell):
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", "Get-ExecutionPolicy"],
        env=shell_environment(),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return result.stdout.strip()


def test_script_syntax(powershell):
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$tokens = $null; $parseErrors = $null; "
            "$null = [System.Management.Automation.Language.Parser]::ParseFile("
            "$env:HIDEN_TEST_SCRIPT, [ref]$tokens, [ref]$parseErrors); "
            "if ($parseErrors.Count) { $parseErrors | Format-List; exit 1 }",
        ],
        env=shell_environment(HIDEN_TEST_SCRIPT=str(SCRIPT)),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def run_case(powershell, case):
    policy = execution_policy(powershell)
    if policy in {"Restricted", "AllSigned"}:
        pytest.skip(f"{powershell} policy is {policy}; do not override local script policy")
    env = shell_environment(
        HIDEN_TEST_SCRIPT=str(SCRIPT),
        HIDEN_TEST_CASE=case,
        COMPUTERNAME="OTHER-HOST" if case in ("wrong_host", "cli_wrong_host") else "XF08ID-WS7",
        OS="Unix" if case == "wrong_os" else "Windows_NT",
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", HARNESS],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    lines = [line for line in result.stdout.splitlines() if line.startswith("RESULT_JSON:")]
    assert len(lines) == 1, result.stdout + result.stderr
    return json.loads(lines[0].removeprefix("RESULT_JSON:"))


def native_arguments(result):
    return [call["arguments"] for call in result["calls"] if call["name"] == "w32tm"]


def test_default_is_readonly(powershell):
    result = run_case(powershell, "check")
    assert result["error"] is None
    assert all(call["name"] == "w32tm" for call in result["calls"])
    assert native_arguments(result) == [
        ["/query", "/source"],
        ["/query", "/status"],
        ["/query", "/peers"],
        ["/query", "/configuration"],
        ["/stripchart", "/computer:time1.nsls2.bnl.gov", "/dataonly", "/samples:5"],
    ]


@pytest.mark.parametrize("case", ["preview", "dot_source"])
def test_no_os_operations(powershell, case):
    result = run_case(powershell, case)
    assert result == {"error": None, "calls": []}


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("wrong_host", "XF08ID-WS7"),
        ("cli_wrong_host", "XF08ID-WS7"),
        ("wrong_os", "XF08ID-WS7"),
        ("not_admin", "administrator"),
        ("not_stopped", "Stop the MASsoft experiment"),
        ("domain", "domain-joined"),
    ],
)
def test_safety_guards(powershell, case, message):
    result = run_case(powershell, case)
    assert message in result["error"]
    assert result["calls"] == []


def test_apply_order_and_peer_argument(powershell):
    result = run_case(powershell, "apply")
    assert result["error"] is None
    assert [call["name"] for call in result["calls"][:8]] == [
        "w32tm",
        "Set-Service",
        "Start-Service",
        "w32tm",
        "Restart-Service",
        "Start-Sleep",
        "w32tm",
        "Start-Sleep",
    ]
    assert result["calls"][1]["arguments"] == ["W32Time", "Automatic"]
    assert result["calls"][5]["arguments"] == [5]
    assert result["calls"][7]["arguments"] == [15]
    assert native_arguments(result)[:3] == [
        ["/query", "/configuration"],
        [
            "/config",
            "/manualpeerlist:time1.nsls2.bnl.gov,0x8 "
            "time2.nsls2.bnl.gov,0x8 time3.nsls2.bnl.gov,0x8",
            "/syncfromflags:manual",
            "/update",
        ],
        ["/resync", "/rediscover"],
    ]
    assert native_arguments(result)[3:] == native_arguments(run_case(powershell, "check"))


@pytest.mark.parametrize(
    ("case", "last_command", "error"),
    [
        ("service_failure", "/query", "Service access denied"),
        ("config_failure", "/config", "exit 42"),
        ("resync_failure", "/resync", "exit 42"),
    ],
)
def test_apply_failure_stops_without_claiming_rollback(powershell, case, last_command, error):
    result = run_case(powershell, case)
    assert error in result["error"]
    assert "not rolled back" in result["error"]
    assert native_arguments(result)[-1][0] == last_command
    if case != "resync_failure":
        assert "Restart-Service" not in [call["name"] for call in result["calls"]]


@pytest.mark.parametrize("case", ["query_failure", "stripchart_failure"])
def test_verification_errors_are_not_hidden(powershell, case):
    result = run_case(powershell, case)
    assert "exit 42" in result["error"]
    if case == "query_failure":
        assert native_arguments(result) == [["/query", "/source"]]
