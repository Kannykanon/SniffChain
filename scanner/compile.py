"""Compiles contracts on demand and caches ABI + bytecode in build/.

Artifacts are committed, so a server never needs solc: an artifact is reused as long as the hash of
the contract sources it was built from still matches (file timestamps don't survive a git clone).
"""
import hashlib
import json
from pathlib import Path

SOLC_VERSION = "0.8.26"
ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = ROOT / "contracts"


def _sources_hash() -> str:
    h = hashlib.sha256(SOLC_VERSION.encode())
    for p in sorted(CONTRACTS.rglob("*.sol")):
        h.update(p.relative_to(CONTRACTS).as_posix().encode())
        h.update(p.read_bytes().replace(b"\r\n", b"\n"))  # same hash on Windows and Linux checkouts
    return h.hexdigest()


def load_artifact(name: str = "HoneypotSimulator", source: str = "HoneypotSimulator.sol") -> dict:
    artifact_path = ROOT / "build" / f"{name}.json"
    sources_hash = _sources_hash()
    if artifact_path.exists():
        artifact = json.loads(artifact_path.read_text())
        if artifact.get("sources_sha256") == sources_hash:
            return artifact

    import solcx  # only needed when contracts changed
    if SOLC_VERSION not in {str(v) for v in solcx.get_installed_solc_versions()}:
        solcx.install_solc(SOLC_VERSION)
    out = solcx.compile_files(
        [str(CONTRACTS / source)],
        output_values=["abi", "bin", "bin-runtime"],
        solc_version=SOLC_VERSION,
        optimize=True,
        optimize_runs=200,
        via_ir=True,
        base_path=str(CONTRACTS),
        allow_paths=[str(CONTRACTS)],
    )
    c = next(v for k, v in out.items() if k.endswith(f":{name}"))
    artifact = {"abi": c["abi"], "bytecode": "0x" + c["bin"], "runtime": "0x" + c["bin-runtime"],
                "solc": SOLC_VERSION, "sources_sha256": sources_hash}
    artifact_path.parent.mkdir(exist_ok=True)
    artifact_path.write_text(json.dumps(artifact, indent=2))
    return artifact
