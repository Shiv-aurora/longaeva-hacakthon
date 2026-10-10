"""Immutable public source bytes. Curl uses the host's normal certificate trust."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"


def byte_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def fetch_https(url: str, user_agent: str = "UAL Counterfactual Lab research") -> bytes:
    if urlparse(url).scheme != "https" or "\n" in user_agent or "\r" in user_agent:
        raise ValueError("Source download requires HTTPS and a valid user agent")
    curl = shutil.which("curl")
    if not curl:
        raise ValueError("Install curl with normal certificate trust to retrieve public sources")
    with tempfile.TemporaryDirectory(prefix="ual-source-") as temporary:
        target = Path(temporary) / "download"
        try:
            subprocess.run([curl, "--fail", "--silent", "--show-error", "--location",
                            "--proto", "=https", "--proto-redir", "=https", "--max-time", "30",
                            "--user-agent", user_agent, "--output", str(target), url],
                           check=True, capture_output=True, timeout=35)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            detail = getattr(exc, "stderr", b"") or b""
            raise ValueError(f"Public source download failed: {detail.decode(errors='replace')[:300]}") from exc
        if target.stat().st_size > 100_000_000:
            raise ValueError("Source exceeds the 100 MB ingestion limit")
        return target.read_bytes()


def preserve_source(content: bytes, url: str, media_type: str, destination: Path = RAW) -> dict:
    if not content:
        raise ValueError("Cannot preserve an empty source")
    sha = byte_hash(content)
    target = destination / sha
    target.mkdir(parents=True, exist_ok=True)
    payload = target / "source.bin"
    if payload.exists():
        if byte_hash(payload.read_bytes()) != sha:
            raise ValueError("Immutable raw source was modified")
    else:
        with payload.open("xb") as stream:
            stream.write(content)
    metadata = {"sha256": sha, "source_url": url, "media_type": media_type,
                "bytes": len(content), "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                "access": "public source; source-specific redistribution terms still require review"}
    manifest = target / "source.json"
    if manifest.exists():
        existing = json.loads(manifest.read_text())
        if existing["sha256"] != sha or existing["bytes"] != len(content) or existing["source_url"] != url:
            raise ValueError("Immutable raw source metadata differs")
        metadata = existing
    else:
        with manifest.open("x") as stream:
            json.dump(metadata, stream, indent=2)
            stream.write("\n")
    return {**metadata, "path": str(payload)}


def read_source(sha: str, destination: Path = RAW) -> tuple[bytes, dict]:
    if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
        raise ValueError("Invalid source hash")
    target = destination / sha
    metadata = json.loads((target / "source.json").read_text())
    content = (target / "source.bin").read_bytes()
    if byte_hash(content) != sha or metadata["sha256"] != sha or metadata["bytes"] != len(content):
        raise ValueError("Immutable raw source failed checksum validation")
    return content, metadata
