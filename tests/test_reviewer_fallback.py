import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path


def _load_reviewer_module():
    path = Path("scripts/agents/reviewer_aryabhatta.py")
    spec = importlib.util.spec_from_file_location("reviewer_aryabhatta", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_unknown_prior_comment_is_non_blocking_advisory() -> None:
    reviewer = _load_reviewer_module()

    finding = reviewer.comment_to_finding(
        {
            "id": "prior-msg:unknown@example.com",
            "subject": "Re: [PATCH] subsystem: example review",
        },
        [],
    )

    assert finding["status"] == "closed"
    assert finding["severity"] == "low"
    assert finding["source_comment_id"] == "prior-msg:unknown@example.com"
    assert "Fallback prior comment mapping unavailable" in finding["title"]
    assert "non-blocking advisory" in " ".join(finding["evidence"])


def test_reviewer_wrapper_agent_exec_does_not_pass_output_schema(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    qgenie = bin_dir / "qgenie"
    qgenie.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1 $2 $3" == "agent exec --help" ]]; then
  exit 0
fi
if [[ "$1 $2" == "agent exec" ]]; then
  out=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --output-schema)
        echo "unexpected --output-schema" >&2
        exit 64
        ;;
      --output-last-message)
        out="$2"
        shift 2
        ;;
      *)
        shift
        ;;
    esac
  done
  cat >/dev/null
  printf '{"findings": []}\n' > "$out"
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    qgenie.chmod(0o755)

    findings = tmp_path / "findings.json"
    review = tmp_path / "review.md"
    watch = tmp_path / "watch"
    watch.mkdir()
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env.get('PATH', '')}",
            "A2A_FINDINGS_FILE": str(findings),
            "A2A_REVIEW_FILE": str(review),
            "A2A_WATCH_PATH": str(watch),
            "A2A_STABLE_MODE": "1",
        }
    )

    result = subprocess.run(
        ["bash", "scripts/agents/reviewer_llm_native.sh"],
        cwd=Path.cwd(),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    payload = json.loads(findings.read_text(encoding="utf-8"))
    assert payload == {"findings": []}
    assert "unexpected --output-schema" not in result.stderr
