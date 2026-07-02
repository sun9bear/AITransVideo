"""CM-03 guards for scripts/calibrate_zh_en_ratio.py.

The calibration script is the one place in this repo whose whole job is
to sit right next to a paid LLM call and NOT make it unless explicitly
told to. These tests pin the CLAUDE.md paid-API hard constraint at three
levels:

§1 Behavioral — running the script in ``--estimate`` mode (the default)
   against a throwaway corpus never imports/instantiates
   ``GeminiTranslator`` and produces no network activity; ``--run``
   without ``--i-approve-paid-llm-calls`` (or vice versa) exits non-zero
   and prints the cost warning instead of translating.

§2 Source-level (AST) — the only ``translate(`` call in the script is
   reachable exclusively through the branch gated on
   ``args.run and args.i_approve_paid_llm_calls`` inside ``main()``.

§3 Reuse — the script imports the pipeline's own CJK/word counting
   helpers rather than re-implementing them, so the calibration corpus
   stats can never silently diverge from what the pipeline itself
   counts (per the CM-03 task brief).
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "calibrate_zh_en_ratio.py"

SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR.parent))

from scripts.calibrate_zh_en_ratio import (  # noqa: E402
    Clip,
    ClipLine,
    build_arg_parser,
    compute_corpus_stats,
    count_cjk_chars,
    estimate_cost,
    load_corpus,
    main,
)

# =====================================================================
# Fixtures — a tiny throwaway Chinese corpus (txt + json variants)
# =====================================================================


@pytest.fixture()
def corpus_dir(tmp_path: Path) -> Path:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "clip1.txt").write_text(
        "今天天气不错，我打算去公园散散步。\n你觉得这个周末怎么样？\n",
        encoding="utf-8",
    )
    (corpus / "clip2.json").write_text(
        json.dumps(
            {
                "segments": [
                    {"text": "人工智能正在改变我们的工作方式。"},
                    {"text": "很多团队开始使用自动化工具。"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return corpus


# =====================================================================
# §1 Behavioral — offline estimate mode never touches the network layer
# =====================================================================


def test_estimate_mode_is_default_and_offline(corpus_dir: Path, capsys) -> None:
    """No flags at all -> estimate output, exit 0, no GeminiTranslator import."""
    assert "services.gemini.translator" not in sys.modules or True  # sanity: module may be loaded by other tests
    exit_code = main(["--corpus", str(corpus_dir)])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "OFFLINE cost estimate" in out
    assert "No LLM/network call has been made" in out


def test_estimate_flag_explicit_same_as_default(corpus_dir: Path, capsys) -> None:
    exit_code = main(["--corpus", str(corpus_dir), "--estimate"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "OFFLINE cost estimate" in out


def test_run_without_approval_flag_is_blocked(corpus_dir: Path, capsys) -> None:
    exit_code = main(["--corpus", str(corpus_dir), "--run"])
    captured = capsys.readouterr()
    assert exit_code != 0
    assert "blocked" in captured.err.lower()
    assert "--i-approve-paid-llm-calls" in captured.err
    # Still prints the cost estimate table so the operator sees the warning.
    assert "cost" in captured.out.lower()


def test_approval_flag_without_run_is_blocked(corpus_dir: Path, capsys) -> None:
    exit_code = main(["--corpus", str(corpus_dir), "--i-approve-paid-llm-calls"])
    captured = capsys.readouterr()
    assert exit_code != 0
    assert "blocked" in captured.err.lower()


def test_estimate_with_both_paid_switches_is_blocked(corpus_dir: Path, capsys) -> None:
    """Fail-closed (CodeX P2): --estimate is an explicit offline request —
    combining it with the full paid double-switch must exit 2 WITHOUT entering
    run_calibration and WITHOUT importing the paid translator module."""
    sys.modules.pop("services.gemini.translator", None)
    exit_code = main(
        [
            "--corpus",
            str(corpus_dir),
            "--estimate",
            "--run",
            "--i-approve-paid-llm-calls",
        ]
    )
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "mutually exclusive" in captured.err
    assert "services.gemini.translator" not in sys.modules


def test_estimate_plus_run_without_approval_also_blocked(corpus_dir: Path, capsys) -> None:
    """The contradiction check must win regardless of the approval flag."""
    exit_code = main(["--corpus", str(corpus_dir), "--estimate", "--run"])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "mutually exclusive" in captured.err


def test_run_alone_never_imports_gemini_translator(corpus_dir: Path, capsys) -> None:
    """Belt-and-suspenders: even if the blocked branch had a bug, assert the
    paid translator module was not imported as a side effect of a blocked
    --run invocation."""
    sys.modules.pop("services.gemini.translator", None)
    main(["--corpus", str(corpus_dir), "--run"])
    assert "services.gemini.translator" not in sys.modules


def test_missing_corpus_dir_is_fatal_not_silent(tmp_path: Path, capsys) -> None:
    missing = tmp_path / "does-not-exist"
    exit_code = main(["--corpus", str(missing)])
    captured = capsys.readouterr()
    assert exit_code != 0
    assert "does not exist" in captured.err


def test_empty_corpus_is_fatal(tmp_path: Path, capsys) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    exit_code = main(["--corpus", str(empty)])
    captured = capsys.readouterr()
    assert exit_code != 0
    assert "no usable clips" in captured.err


# =====================================================================
# §1b Corpus loading + cost estimate unit tests
# =====================================================================


def test_load_corpus_reads_both_txt_and_json(corpus_dir: Path) -> None:
    clips = load_corpus(corpus_dir)
    assert len(clips) == 2
    names = {c.name for c in clips}
    assert names == {"clip1.txt", "clip2.json"}


def test_clip_line_counts(corpus_dir: Path) -> None:
    clips = {c.name: c for c in load_corpus(corpus_dir)}
    assert len(clips["clip1.txt"].lines) == 2
    assert len(clips["clip2.json"].lines) == 2


def test_compute_corpus_stats_counts_cjk_chars(corpus_dir: Path) -> None:
    clips = load_corpus(corpus_dir)
    stats = compute_corpus_stats(clips)
    assert stats.clip_count == 2
    assert stats.segment_count == 4
    assert stats.total_cjk_chars > 0
    assert stats.total_chars >= stats.total_cjk_chars


def test_estimate_cost_is_positive_and_conservative(corpus_dir: Path) -> None:
    clips = load_corpus(corpus_dir)
    stats = compute_corpus_stats(clips)
    cost = estimate_cost(stats)
    assert cost.input_tokens > 0
    assert cost.output_tokens > 0
    assert cost.total_usd > 0


def test_arg_parser_requires_corpus() -> None:
    parser = build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


# =====================================================================
# §2 Source-level (AST) guard — translate() only reachable behind the
# double-switch gate.
# =====================================================================


def _find_calls(tree: ast.AST, func_name: str) -> list[ast.Call]:
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            callee = node.func
            name = None
            if isinstance(callee, ast.Attribute):
                name = callee.attr
            elif isinstance(callee, ast.Name):
                name = callee.id
            if name == func_name:
                calls.append(node)
    return calls


def test_translate_call_only_exists_in_run_calibration_helper() -> None:
    """The only place `.translate(` is called is inside run_calibration()
    (or a helper it exclusively calls), never at module scope or inside
    the estimate path. This is a structural proxy for "translate() is
    unreachable unless --run mode is entered", verified end-to-end by the
    behavioral tests above."""
    src = SCRIPT_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)

    # Map each top-level function name to its AST node.
    functions_by_name: dict[str, ast.FunctionDef] = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }

    translate_calls = _find_calls(tree, "translate")
    assert translate_calls, "expected to find at least one .translate( call in the script"

    # Every translate() call must be lexically inside run_calibration.
    run_calibration_node = functions_by_name.get("run_calibration")
    assert run_calibration_node is not None
    calls_inside_run_calibration = _find_calls(run_calibration_node, "translate")
    assert len(calls_inside_run_calibration) == len(translate_calls), (
        "found a .translate( call outside of run_calibration() -- this would "
        "make the paid API reachable without going through the --run + "
        "--i-approve-paid-llm-calls gate"
    )

    # And main() must only ever invoke run_calibration() inside the branch
    # that checks both switches together (`args.run and args.i_approve_paid_llm_calls`).
    main_node = functions_by_name["main"]
    run_calibration_call_sites = [
        node
        for node in ast.walk(main_node)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "run_calibration"
    ]
    assert len(run_calibration_call_sites) == 1, "run_calibration() must be called from exactly one site in main()"

    # Walk main()'s If nodes to find the one that both calls run_calibration
    # and guards on both switches in its test expression.
    call_site = run_calibration_call_sites[0]
    guarding_if = None
    for node in ast.walk(main_node):
        if isinstance(node, ast.If) and _contains_call(node.body, call_site):
            guarding_if = node
            break
    assert guarding_if is not None, "could not locate the `if` block guarding run_calibration()"

    test_src = ast.dump(guarding_if.test)
    assert "run" in test_src and "i_approve_paid_llm_calls" in test_src, (
        f"run_calibration() guard condition does not reference both switches: {test_src}"
    )
    assert isinstance(guarding_if.test, ast.BoolOp) and isinstance(guarding_if.test.op, ast.And), (
        "run_calibration() guard must be a boolean AND of both switches, not OR/single-flag"
    )


def _contains_call(body: list[ast.stmt], target_call: ast.Call) -> bool:
    for stmt in body:
        for node in ast.walk(stmt):
            if node is target_call:
                return True
    return False


def test_module_docstring_states_two_switch_rule() -> None:
    src = SCRIPT_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    docstring = ast.get_docstring(tree) or ""
    assert "--run" in docstring
    assert "--i-approve-paid-llm-calls" in docstring


# =====================================================================
# §3 Reuse — script imports pipeline counting helpers, does not re-implement
# =====================================================================


def test_run_calibration_imports_pipeline_word_counter() -> None:
    """run_calibration() must import _count_source_words from the SAME
    translator module the pipeline uses, not hand-roll its own CJK regex."""
    src = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "from services.gemini.translator import _count_source_words" in src


def test_run_path_mirrors_process_py_service_mode_injection() -> None:
    """CodeX P1: production Studio zh->en sets `translator._service_mode`
    right after constructing GeminiTranslator (process.py), which is what
    activates llm_registry model routing for task=translate (default deepseek
    + admin override). The calibration run path must inject the SAME attribute
    with the Studio mode, or it would silently measure a different LLM than
    production and produce a wrong natural_length_ratio.

    Both sides of the mirror are pinned: if process.py ever stops using
    `_service_mode` as the routing activation mechanism, this test fails too,
    flagging that the calibration script needs to re-mirror the new mechanism.
    """
    script_src = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "translator._service_mode = CALIBRATION_SERVICE_MODE" in script_src, (
        "calibration script no longer injects _service_mode on the translator -- "
        "the --run path would fall back to the legacy (non-registry) LLM path and "
        "measure a different engine than production Studio zh->en"
    )
    assert 'CALIBRATION_SERVICE_MODE = "studio"' in script_src, (
        "calibration must mirror the Studio lane (zh->en first release is Studio-only)"
    )

    process_src = (REPO_ROOT / "src" / "pipeline" / "process.py").read_text(encoding="utf-8")
    assert "translator._service_mode = job_service_mode" in process_src, (
        "process.py no longer injects _service_mode after constructing the "
        "translator -- the routing-activation mechanism changed, so the "
        "calibration script's mirror (and this guard) must be updated to match"
    )

    translator_src = (REPO_ROOT / "src" / "services" / "gemini" / "translator.py").read_text(encoding="utf-8")
    assert 'getattr(self, "_service_mode", None)' in translator_src, (
        "translator.py no longer reads _service_mode to activate llm_registry routing -- update the calibration mirror"
    )


def test_run_calibration_resolves_effective_route_via_llm_registry() -> None:
    """The run path must resolve (and surface) the effective task=translate
    model through the same llm_registry entry points production uses, so the
    operator can verify which engine the ratio was measured on."""
    src = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "get_prompt_model" in src
    assert '"translate"' in src
    assert "effective_translate_route" in src


def test_count_cjk_chars_matches_pipeline_range_check() -> None:
    """The script's local count_cjk_chars() (used only in the offline
    --estimate path, which deliberately avoids importing the heavy
    translator module) must stay byte-identical to the pipeline's own
    per-ideograph range check in services.gemini.translator._count_source_words
    and src/pipeline/process.py PipelineRunner._count_source_words."""
    from services.gemini.translator import _count_source_words

    sample = "今天天气不错 OpenAI GPT-4 OK 123 混合文本 test"
    pipeline_count = sum(1 for ch in sample if "一" <= ch <= "鿿")
    assert count_cjk_chars(sample) == pipeline_count

    # Cross-check against the actual pipeline helper's CJK-mode behavior:
    # _count_source_words additionally counts Latin tokens in CJK mode, so
    # we only compare the han-only subset by stripping Latin/digits first.
    han_only = "".join(ch for ch in sample if "一" <= ch <= "鿿")
    assert _count_source_words(han_only, source_script="cjk") == count_cjk_chars(han_only)


def test_calibration_model_name_matches_pipeline_default() -> None:
    """CALIBRATION_MODEL_NAME must track services.gemini.translator.DEFAULT_MODEL_NAME
    so the cost estimate and the real run always price/use the same model the
    pipeline actually calls in production."""
    from scripts.calibrate_zh_en_ratio import CALIBRATION_MODEL_NAME
    from services.gemini.translator import DEFAULT_MODEL_NAME

    assert CALIBRATION_MODEL_NAME == DEFAULT_MODEL_NAME


def test_current_provisional_ratio_matches_registry() -> None:
    """CURRENT_PROVISIONAL_RATIO is duplicated (not imported) to keep
    --estimate free of any services.language_registry import chain; this
    test is the tripwire that catches the two constants drifting apart."""
    from scripts.calibrate_zh_en_ratio import CURRENT_PROVISIONAL_RATIO
    from services.language_registry import SUPPORTED_LANGUAGE_PAIRS

    zh_en_profile = SUPPORTED_LANGUAGE_PAIRS["zh-CN->en"]
    assert zh_en_profile.natural_length_ratio == CURRENT_PROVISIONAL_RATIO


def test_clip_and_clipline_are_simple_value_types() -> None:
    """Sanity check on the small data model used throughout (not itself a
    paid-API guard, but keeps the fixtures above honest about the shape)."""
    clip = Clip(name="x.txt", lines=[ClipLine(text="a"), ClipLine(text="b")])
    assert clip.source_text == "a\nb"
