"""Freeze the deployed PV/API contract before replacing the two implementations."""

import json
import subprocess
import sys
import tomllib
from pathlib import Path

from cap3 import RGAIOC
from massoft_client import MASsoftClient

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / "tests/fixtures/legacy_contract.json").read_text())


def test_preserves_deployed_pv_contract():
    ioc = RGAIOC(prefix="", mas_host="127.0.0.1")
    actual = {
        name: {
            "attr": pv.pvspec.attr,
            "dtype": pv.pvspec.dtype.__name__,
            "value": pv.value,
            "max_length": pv.max_length,
            "read_only": pv.pvspec.read_only,
            "precision": getattr(pv, "precision", None),
        }
        for name, pv in ioc.pvdb.items()
    }
    expected = {name: dict(spec) for name, spec in CONTRACT["pvs"].items()}
    # Guarded publication now requires the source millisecond counter by default.
    expected["XF:08IDB-SE{RGA:1}:DataMsFmt"]["value"] = 1
    assert {name: actual[name] for name in expected} == expected
    new_names = {
        "SourceStartUTC",
        "SourceMaxAge",
        "DataState",
        "SourceTime",
        "SourceAge",
        "HistoryRows",
        "QueueDepth",
        "DroppedRows",
        "PublishedRows",
    }
    assert actual.keys() - expected.keys() == {f"XF:08IDB-SE{{RGA:1}}:{name}" for name in new_names}
    assert set(CONTRACT["core_pvs"]) <= actual.keys()
    assert ioc.client_class is MASsoftClient


def test_archiver_manifest_matches_all_twenty_channels():
    ioc = RGAIOC(prefix="", mas_host="127.0.0.1")
    names = (ROOT / "archiver-pvs.txt").read_text().splitlines()
    expected = {getattr(ioc, f"{kind}{i}").pvname for kind in ("mass", "mid") for i in range(1, 21)}
    assert len(names) == len(set(names)) == 40
    assert set(names) == expected


def test_quality_manifest_contains_existing_pvs_without_duplicates():
    ioc = RGAIOC(prefix="", mas_host="127.0.0.1")
    names = (ROOT / "archiver-quality-pvs.txt").read_text().splitlines()
    assert len(names) == len(set(names))
    assert set(names) <= ioc.pvdb.keys()
    assert "XF:08IDB-SE{RGA:1}:DataState" in names
    assert "XF:08IDB-SE{RGA:1}:SourceStartUTC" in names


def test_single_client_implementation_retains_public_api():
    assert set(CONTRACT["client_api"]) <= set(dir(MASsoftClient))
    assert MASsoftClient.open_experiment_commands is MASsoftClient.open_experiment


def test_retired_implementations_are_removed():
    for filename in ("cap2.py", "cap2_aj2.py", "massoft_client_aj2.py"):
        assert not (ROOT / "hiden" / filename).exists()


def test_cli_help_exits_without_starting_ioc():
    result = subprocess.run(
        [sys.executable, str(ROOT / "hiden" / "cap3.py"), "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "--interfaces" in result.stdout
    assert "--mas-host" in result.stdout
    assert "--mas-port" in result.stdout


def test_pixi_uses_canonical_ioc_and_broadcast_binding():
    manifest = tomllib.loads((ROOT / "pixi.toml").read_text())
    task = manifest["tasks"]["ioc"]
    assert task["cmd"] == "python cap3.py"
    assert task["cwd"] == "hiden"
    assert manifest["activation"]["env"]["EPICS_CAS_INTF_ADDR_LIST"] == "0.0.0.0"
