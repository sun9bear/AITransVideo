"""PII 注入守卫：fact sheet 不得出现以下字面量。"""
import sys
import json
import subprocess
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parent.parent
          / "scripts" / "smart_shadow_eval_collector.py")
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "smart_shadow_eval"

# These must NOT appear in any fact sheet output:
PII_LITERALS = [
    "贝基·奎克",          # Chinese personal name
    "沃伦·巴菲特",         # Chinese personal name
    "13800138000",        # Phone number
    "$19,100,000",        # Financial figure
    "abc@example.com",    # Email
    "我们今天的嘉宾是埃隆·马斯克",  # Chinese full sentence (cn_text)
]


def test_no_pii_in_fact_sheet(tmp_path):
    out_dir = tmp_path / "out"
    subprocess.run(
        [sys.executable, str(SCRIPT),
         "--jobs-root", str(FIXTURES / "jobs"),
         "--projects-root", str(FIXTURES / "projects"),
         "--out-dir", str(out_dir)],
        check=True, capture_output=True
    )
    facts = (out_dir / "facts.jsonl").read_text(encoding="utf-8")
    for lit in PII_LITERALS:
        assert lit not in facts, f"PII leak: {lit!r} found in facts.jsonl"
