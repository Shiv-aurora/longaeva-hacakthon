#!/usr/bin/env bash
# Build a judge-reviewable source ZIP from committed HEAD, never include secrets or ignored raw archives.
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
OUT="${1:-$(dirname "$ROOT")/longaeva-stage2-delivery}"
mkdir -p "$OUT"
SHA="$(git rev-parse HEAD)"
SHORT_SHA="$(git rev-parse --short=12 HEAD)"
ZIP="$OUT/UAL_Counterfactual_Engine_${SHORT_SHA}_SOURCE.zip"

if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
  echo "WARNING: your working tree has uncommitted or untracked changes."
  echo "Only committed HEAD content would be included. Commit your final changes first."
  git status --short
  exit 2
fi

git archive --format=zip --output="$ZIP" HEAD
python3 - "$ZIP" <<'PY'
from pathlib import Path
from hashlib import sha256
import sys
path = Path(sys.argv[1])
checksum = sha256(path.read_bytes()).hexdigest()
path.with_suffix(path.suffix + ".sha256").write_text(checksum + "  " + path.name + "\n")
print("Archive:", path)
print("SHA256:", checksum)
PY

cat > "$OUT/READ_BEFORE_SUBMITTING.txt" <<EOF
UAL COUNTERFACTUAL ENGINE - STAGE 2

Repository: https://github.com/Shiv-aurora/longaeva-hacakthon
Commit: $SHA

This source ZIP contains tracked code, documentation, small public inputs and tests.
It excludes original large public archives, generated research caches, saved runs,
and local registered model-code bundles. See data/README.md and methods documents.
Open STAGE2_SUBMISSION.md first.

Local launch:
  python3 -m venv .venv
  source .venv/bin/activate
  python -m pip install -r requirements.txt
  python scripts/build_monthly_data.py
  python -m uvicorn app.server:app --host 127.0.0.1 --port 8000
  Open http://127.0.0.1:8000/monthly

Submit the ZIP alongside the Stage 2 technical PDF and optional demo video.
Verify any separate research archive before distribution; no private materials.
EOF

echo "Stage 2 source package ready at: $OUT"
echo "Include your report PDF and recorded demo from local workspace."
