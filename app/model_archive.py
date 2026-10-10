"""Preserve our own model source and replay only locally registered bundles."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
# Hosted functions keep executable receipts in writable, ephemeral storage.
RUNTIME_DATA_ROOT = Path(os.environ.get("UAL_RUNTIME_DIR", "/tmp/ual-twin")) if os.environ.get("VERCEL") == "1" else ROOT / "data"
ARCHIVE_ROOT = RUNTIME_DATA_ROOT / "model_code"
STUDY_CACHE_ROOT = RUNTIME_DATA_ROOT / "convergence"
SCHEMA = "model-code-0.1.0"
LEGACY_CORE_FILES = {
    "monthly": ("monthly.py", "fleet.py", "evidence.py", "data.py"),
    "failure_search": ("failure_search.py", "monthly.py", "fleet.py", "evidence.py", "data.py"),
    "target_inversion": ("target_inversion.py", "failure_search.py", "monthly.py", "fleet.py", "evidence.py", "data.py"),
}
CORE_FILES = {kind: (*names, "cash_bridge.py") for kind, names in LEGACY_CORE_FILES.items()}
CORE_FILES["convergence"] = ("convergence.py", "monthly.py", "fleet.py", "evidence.py", "data.py", "cash_bridge.py")
LEGACY_CORE_FILES["convergence"] = CORE_FILES["convergence"]  # no earlier convergence file set


def digest(value):
    # Browser JSON.stringify changes integral floats to integers.
    def canonical(item):
        if isinstance(item, float) and item.is_integer():
            return int(item)
        if isinstance(item, dict):
            return {k: canonical(v) for k, v in item.items()}
        if isinstance(item, list):
            return [canonical(v) for v in item]
        return item
    return hashlib.sha256(json.dumps(canonical(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def environment():
    return {"python": platform.python_version(), "numpy": version("numpy"), "duckdb": version("duckdb"),
            "system": platform.system(), "machine": platform.machine()}


def deterministic(result):
    clean = {k: v for k, v in result.items() if k not in {"runtime_seconds", "replay_code"}}
    if "summary" in clean:
        clean["summary"] = {k: v for k, v in clean["summary"].items() if k != "runtime_seconds"}
    return clean


def _read_json(path):
    def invalid(value):
        raise ValueError(f"Non-finite JSON value: {value}")
    return json.loads(Path(path).read_text(), parse_constant=invalid)


def _names(kind, core_files=None):
    if kind not in CORE_FILES:
        raise ValueError("Unsupported model code kind")
    names = CORE_FILES[kind] if core_files is None else tuple(core_files)
    if names not in (CORE_FILES[kind], LEGACY_CORE_FILES[kind]):
        raise ValueError("Model code file list differs from the fixed allowlist")
    return (*("app/" + n for n in names), "app/__init__.py", "replay_worker.py")


@dataclass(frozen=True)
class SourceCapture:
    kind: str
    checkout: Path
    sources: tuple[tuple[str, bytes], ...]

    @property
    def code_sha256(self):
        sources = dict(self.sources)
        return hashlib.sha256(b"".join(sources["app/" + n] for n in CORE_FILES[self.kind])).hexdigest()

    def assert_unchanged(self):
        if capture_source(self.kind, self.checkout).sources != self.sources:
            raise ValueError("Model source changed after import; restart the server or command before running")


def capture_source(kind, checkout=ROOT):
    """Call immediately after importing model modules, before serving runs."""
    checkout = Path(checkout).resolve()
    sources = []
    for name in _names(kind):
        path = checkout / ("app/replay_worker.py" if name == "replay_worker.py" else name)
        if path.is_symlink():
            raise ValueError("Model source symlinks are not supported")
        sources.append((name, path.read_bytes()))
    return SourceCapture(kind, checkout, tuple(sources))


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("Invalid model code bundle identifier")
    return value


def _atomic_json(path, value):
    """Publish complete immutable metadata, including concurrent first writers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".register-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write("\n")
        try:
            os.link(temporary, path)
        except FileExistsError:
            if _read_json(path) != value:
                raise ValueError("Existing immutable model registration differs")
    finally:
        Path(temporary).unlink(missing_ok=True)


def archive_source(captured: SourceCapture, archive_root=ARCHIVE_ROOT):
    captured.assert_unchanged()
    source_map = dict(captured.sources)
    descriptor = {"schema": SCHEMA, "kind": captured.kind, "core_files": list(CORE_FILES[captured.kind]),
        "code_sha256": captured.code_sha256, "environment": environment(),
        "files": {n: hashlib.sha256(b).hexdigest() for n, b in source_map.items()}}
    bundle_id = digest(descriptor)
    root = Path(archive_root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / bundle_id
    temporary = Path(tempfile.mkdtemp(prefix=".bundle-", dir=root))
    try:
        for name, content in captured.sources:
            path = temporary / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        (temporary / "descriptor.json").write_text(json.dumps(descriptor, indent=2) + "\n")
        try:
            temporary.rename(target)
        except OSError:
            if not target.is_dir():
                raise
            # Existing content must match before registering/reusing it.
            _verify_files(target, descriptor)
        # Only our explicit capture operation registers executable bundles.
        _atomic_json(root / ".registered" / (bundle_id + ".json"), descriptor)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    verify_bundle(bundle_id, captured.kind, archive_root=root)
    return {"schema": SCHEMA, "kind": captured.kind, "bundle_id": bundle_id,
            "code_sha256": captured.code_sha256}


def _verify_files(target, descriptor):
    if target.is_symlink() or _read_json(target / "descriptor.json") != descriptor:
        raise ValueError("Model code descriptor differs from its local registration")
    if descriptor.get("schema") != SCHEMA or set(descriptor.get("files", {})) != set(_names(descriptor["kind"], descriptor.get("core_files", []))):
        raise ValueError("Model code file list differs from the fixed allowlist")
    if tuple(descriptor["core_files"]) not in (CORE_FILES[descriptor["kind"]], LEGACY_CORE_FILES[descriptor["kind"]]):
        raise ValueError("Model code concatenation order differs")
    for name, sha in descriptor["files"].items():
        path = target / name
        if path.is_symlink() or path.parent.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            raise ValueError("Model code source checksum differs")
    actual = hashlib.sha256(b"".join((target / "app" / n).read_bytes() for n in descriptor["core_files"])).hexdigest()
    if actual != descriptor["code_sha256"]:
        raise ValueError("Model code legacy checksum differs")
    # No extra app modules can influence imports; bytecode caching is disabled.
    allowed = set(descriptor["files"]) | {"descriptor.json"}
    if any(p.relative_to(target).as_posix() not in allowed for p in target.rglob("*") if p.is_file()):
        raise ValueError("Unexpected executable bundle file")


def verify_bundle(bundle_id, kind, archive_root=ARCHIVE_ROOT):
    bundle_id = _identifier(bundle_id)
    if kind not in CORE_FILES:
        raise ValueError("Unsupported model code kind")
    root = Path(archive_root)
    registration = root / ".registered" / (bundle_id + ".json")
    if not registration.is_file() or registration.is_symlink():
        raise ValueError("Model code bundle is not locally registered; run JSON cannot supply executable code")
    descriptor = _read_json(registration)
    if digest(descriptor) != bundle_id or descriptor.get("kind") != kind:
        raise ValueError("Model code registration checksum or kind differs")
    _verify_files(root / bundle_id, descriptor)
    if descriptor["environment"] != environment():
        raise ValueError("Replay environment differs; use the recorded Python, NumPy, DuckDB, OS and architecture")
    return descriptor


def attach_archive(result, receipt):
    m = result["manifest"]
    if m["code_sha256"] != receipt["code_sha256"]:
        raise ValueError("Executed model and archived source differ")
    return {**result, "replay_code": {**receipt, "result_sha256": digest(deterministic(result))}}


def run_preserved(captured, operation, archive_root=ARCHIVE_ROOT):
    receipt = archive_source(captured, archive_root)
    result = operation()
    captured.assert_unchanged()
    return attach_archive(result, receipt)


def _find_legacy(kind, manifest, root):
    for path in sorted((root / ".registered").glob("*.json")):
        descriptor = _read_json(path)
        if descriptor.get("kind") == kind and descriptor.get("code_sha256") == manifest["code_sha256"]:
            verify_bundle(path.stem, kind, root)
            return path.stem
    return None


def replay_saved(path, kind, archive_root=ARCHIVE_ROOT):
    """Run data chooses a known bundle, never a path, module or source payload."""
    saved = _read_json(path)
    root = Path(archive_root)
    m = saved["manifest"]
    receipt = saved.get("replay_code")
    if receipt is not None:
        if set(receipt) != {"schema", "kind", "bundle_id", "code_sha256", "result_sha256"} or receipt["schema"] != SCHEMA or receipt["kind"] != kind:
            raise ValueError("Replay code receipt differs from its fixed contract")
        bundle_id = _identifier(receipt["bundle_id"])
        if receipt["code_sha256"] != m["code_sha256"] or receipt["result_sha256"] != digest(deterministic(saved)):
            raise ValueError("Saved result checksum or model code receipt differs")
    else:
        bundle_id = _find_legacy(kind, m, root)
        if bundle_id is None:
            # Legacy migration is allowed only when our own current source still
            # matches. Missing earlier versions cannot be reconstructed by hash.
            captured = capture_source(kind)
            if captured.code_sha256 != m["code_sha256"]:
                raise ValueError("Saved model code differs and its original executable source is unavailable")
            bundle_id = archive_source(captured, root)["bundle_id"]
    descriptor = verify_bundle(bundle_id, kind, root)
    if descriptor["code_sha256"] != m["code_sha256"] or m["python_version"] != descriptor["environment"]["python"] or m["numpy_version"] != descriptor["environment"]["numpy"]:
        raise ValueError("Saved manifest code or environment differs from registered source")
    worker_path = str(root.resolve() / bundle_id / "replay_worker.py")
    command = [sys.executable, "-I", "-B", worker_path, kind]
    if os.environ.get("VERCEL") == "1":
        # Vercel injects installed dependencies outside Python's normal prefix.
        # Isolated replay admits only the package roots already loaded by this
        # process, never paths or executable code supplied in a run record.
        package_roots = sorted({str(Path(sys.modules[name].__file__).resolve().parent.parent)
                                for name in ("numpy", "duckdb")})
        bootstrap = "import json,runpy,sys; sys.path.extend(json.loads(sys.argv[1])); target=sys.argv[2]; sys.argv=sys.argv[2:]; runpy.run_path(target,run_name='__main__')"
        command = [sys.executable, "-I", "-B", "-c", bootstrap, json.dumps(package_roots), worker_path, kind]
    try:
        worker = subprocess.run(command,
            input=json.dumps(saved, allow_nan=False), capture_output=True, text=True, cwd=root.resolve() / bundle_id,
            timeout=1800 if kind == "convergence" else 120, check=False)
    except subprocess.TimeoutExpired as e:
        raise ValueError(f"Archived replay exceeded the {1800 if kind == 'convergence' else 120}-second limit") from e
    if worker.returncode:
        raise ValueError("Archived replay failed: " + worker.stderr.strip()[-1500:])
    result = json.loads(worker.stdout)
    return attach_archive(result, {"schema": SCHEMA, "kind": kind, "bundle_id": bundle_id,
                                   "code_sha256": descriptor["code_sha256"]})


def _study_cache_key(reference_run_id, controls, code_sha256):
    return digest({"schema": "verified-study-0.1.0", "reference_run_id": _identifier(reference_run_id),
                   "convergence": controls, "code_sha256": _identifier(code_sha256), "environment": environment()})


def replay_and_register_study(path, cache_root=STUDY_CACHE_ROOT, archive_root=ARCHIVE_ROOT):
    """Only a complete isolated replay can admit a study to the local cache."""
    path = Path(path)
    original = path.read_bytes()
    saved = _read_json(path)
    replayed = replay_saved(path, "convergence", archive_root)
    if path.read_bytes() != original or deterministic(saved) != deterministic(replayed):
        raise ValueError("Study changed during verification or differs from its complete replay")
    # Keep the verified artifact's original runtime, so repeated registration is
    # immutable even though replay duration changes.
    saved = {**saved, "replay_code": replayed["replay_code"]}
    manifest = saved["manifest"]
    study_id = _identifier(manifest["run_id"])
    root = Path(cache_root)
    target = root / (study_id + ".json")
    if target.exists():
        existing = _read_json(target)
        if target.is_symlink() or deterministic(existing) != deterministic(saved) or existing.get("replay_code") != saved["replay_code"]:
            raise ValueError("Existing cached study differs from its verified replay")
        saved = existing
    else:
        _atomic_json(target, saved)
    registration = {"schema": "verified-study-0.1.0", "study_id": study_id,
                    "reference_run_id": manifest["reference_run_id"], "convergence": manifest["convergence"],
                    "code_sha256": manifest["code_sha256"], "environment": environment(),
                    "replay_code": saved["replay_code"], "file_sha256": hashlib.sha256(target.read_bytes()).hexdigest()}
    key = _study_cache_key(manifest["reference_run_id"], manifest["convergence"], manifest["code_sha256"])
    _atomic_json(root / ".verified" / (key + ".json"), registration)
    return saved


def read_verified_study(reference_run_id, controls, code_sha256, cache_root=STUDY_CACHE_ROOT, archive_root=ARCHIVE_ROOT):
    """Reuse only an unchanged, replay-verified study of these exact inputs."""
    key = _study_cache_key(reference_run_id, controls, code_sha256)
    root = Path(cache_root)
    index = root / ".verified" / (key + ".json")
    if not index.exists():
        return None
    try:
        registration = _read_json(index)
        if index.is_symlink() or set(registration) != {"schema", "study_id", "reference_run_id", "convergence", "code_sha256", "environment", "replay_code", "file_sha256"}:
            raise ValueError("Verified study registration differs")
        if registration["schema"] != "verified-study-0.1.0" or registration["environment"] != environment() or _study_cache_key(registration["reference_run_id"], registration["convergence"], registration["code_sha256"]) != key:
            raise ValueError("Verified study inputs, code or environment differ")
        target = root / (_identifier(registration["study_id"]) + ".json")
        content = target.read_bytes()
        if target.is_symlink() or hashlib.sha256(content).hexdigest() != registration["file_sha256"]:
            raise ValueError("Verified study file checksum differs")
        saved = json.loads(content)
        receipt = saved["replay_code"]
        manifest = saved["manifest"]
        if receipt != registration["replay_code"] or receipt["result_sha256"] != digest(deterministic(saved)) or manifest["run_id"] != registration["study_id"] or manifest["reference_run_id"] != reference_run_id or digest(manifest["convergence"]) != digest(controls) or manifest["code_sha256"] != code_sha256:
            raise ValueError("Verified study result or receipt differs")
        descriptor = verify_bundle(receipt["bundle_id"], "convergence", archive_root)
        if descriptor["code_sha256"] != code_sha256:
            raise ValueError("Verified study registered source differs")
        return saved
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("Verified study cache is incomplete or invalid") from error
