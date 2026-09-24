"""Compiles contracts on demand and caches ABI + bytecode in build/."""
import json
from pathlib import Path

import solcx

SOLC_VERSION = "0.8.26"
ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = ROOT / "contracts"


def load_artifact(name: str = "HoneypotSimulator", source: str = "HoneypotSimulator.sol") -> dict:
    src = CONTRACTS / source
    artifact_path = ROOT / "build" / f"{name}.json"
    newest_source = max(p.stat().st_mtime for p in CONTRACTS.rglob("*.sol"))
    if artifact_path.exists() and artifact_path.stat().st_mtime >= newest_source:
        return json.loads(artifact_path.read_text())

    if SOLC_VERSION not in {str(v) for v in solcx.get_installed_solc_versions()}:
        solcx.install_solc(SOLC_VERSION)
    out = solcx.compile_files(
        [str(src)],
        output_values=["abi", "bin", "bin-runtime"],
        solc_version=SOLC_VERSION,
        optimize=True,
        optimize_runs=200,
        via_ir=True,
        base_path=str(CONTRACTS),
        allow_paths=[str(CONTRACTS)],
    )
    c = next(v for k, v in out.items() if k.endswith(f":{name}"))
    artifact = {"abi": c["abi"], "bytecode": "0x" + c["bin"], "runtime": "0x" + c["bin-runtime"]}
    artifact_path.parent.mkdir(exist_ok=True)
    artifact_path.write_text(json.dumps(artifact, indent=2))
    return artifact
