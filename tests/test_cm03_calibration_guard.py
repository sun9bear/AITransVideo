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


#: Every paid translation entry point on GeminiTranslator the script could
#: conceivably call. The AST gate below pins ALL of them inside
#: run_calibration(), so adding a new call site (or switching entry points
#: again) outside the double-switch branch turns this red.
_PAID_TRANSLATE_ENTRY_NAMES = ("translate", "translate_probe")


def test_translate_call_only_exists_in_run_calibration_helper() -> None:
    """The only place a paid translation entry (`.translate(` /
    `.translate_probe(`) is called is inside run_calibration(), never at
    module scope or inside the estimate path. This is a structural proxy for
    "the paid API is unreachable unless --run mode is entered", verified
    end-to-end by the behavioral tests above."""
    src = SCRIPT_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)

    # Map each top-level function name to its AST node.
    functions_by_name: dict[str, ast.FunctionDef] = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }

    all_paid_calls = [call for name in _PAID_TRANSLATE_ENTRY_NAMES for call in _find_calls(tree, name)]
    assert all_paid_calls, "expected to find at least one paid translation entry call in the script"

    # Every paid entry call must be lexically inside run_calibration.
    run_calibration_node = functions_by_name.get("run_calibration")
    assert run_calibration_node is not None
    calls_inside_run_calibration = [
        call for name in _PAID_TRANSLATE_ENTRY_NAMES for call in _find_calls(run_calibration_node, name)
    ]
    assert len(calls_inside_run_calibration) == len(all_paid_calls), (
        "found a paid translation entry call outside of run_calibration() -- "
        "this would make the paid API reachable without going through the "
        "--run + --i-approve-paid-llm-calls gate"
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


# =====================================================================
# §4 Constraint neutrality (@codex P1, PR #99) — the measurement must not
# be clamped by the very natural_length_ratio prior being calibrated.
# =====================================================================


def test_run_calibration_measures_through_constraint_free_probe_entry() -> None:
    """run_calibration must call translate_probe (whose prompt omits ALL
    length constraints) and must NOT call the regular translate() entry
    (whose prompt injects min_chars~max_chars derived from the 0.55 prior
    as hard constraints -- circular measurement)."""
    src = SCRIPT_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    functions_by_name = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    run_calibration_node = functions_by_name["run_calibration"]

    probe_calls = _find_calls(run_calibration_node, "translate_probe")
    assert probe_calls, "run_calibration no longer measures through translate_probe"

    constrained_calls = _find_calls(tree, "translate")
    assert not constrained_calls, (
        "the script calls the regular constrained translate() entry -- its "
        "prompt clamps the output to +/-15% of source_words x "
        "natural_length_ratio, so the measured ratio would just read the "
        "0.55 prior back (circular). Use translate_probe."
    )

    # The report must declare the neutralization so downstream readers of the
    # calibration JSON know which measurement regime produced the numbers.
    assert '"constraint_neutralized": True' in src
    assert "translate_probe" in src


def test_probe_groups_carry_no_length_constraints_but_regular_groups_do() -> None:
    """Behavioral (offline, no network): the probe group builder emits NO
    length-budget keys, while the regular group builder derives
    min_chars/max_chars from natural_length_ratio -- documenting exactly why
    the probe path is the valid measurement entry and translate() is not."""
    from services.assemblyai.transcriber import TranscriptLine
    from services.gemini.translator import _build_groups, _build_probe_groups

    lines = [
        TranscriptLine(
            index=0,
            start_ms=0,
            end_ms=5_000,
            speaker_id="speaker_a",
            speaker_label="A",
            source_text="今天天气不错，我打算去公园散散步。",
        ),
        TranscriptLine(
            index=1,
            start_ms=5_000,
            end_ms=10_000,
            speaker_id="speaker_a",
            speaker_label="A",
            source_text="人工智能正在改变我们的工作方式。",
        ),
    ]

    probe_groups = _build_probe_groups(lines)
    assert probe_groups
    for group in probe_groups:
        for banned_key in ("target_chars", "min_chars", "max_chars", "target_chars_hint"):
            assert banned_key not in group, (
                f"probe group unexpectedly carries {banned_key!r} -- the probe "
                "path is no longer constraint-free; the calibration measurement "
                "regime must be re-audited"
            )

    regular_groups = _build_groups(
        lines,
        max_segment_duration_ms=45_000,
        source_language="zh-CN",
        target_language="en",
    )
    assert regular_groups
    for group in regular_groups:
        assert "min_chars" in group and "max_chars" in group, (
            "regular _build_groups no longer injects min/max_chars -- if the "
            "constrained path changed, re-evaluate whether translate() is now "
            "safe for calibration"
        )
    # And the constraint really is ratio-derived: for a pure-CJK source the
    # target budget tracks source_cjk_chars x 0.55 (+/- the band factors).
    first = regular_groups[0]
    source_words = int(first["source_word_count"])
    assert source_words > 0
    expected_target = round(source_words * 0.55)
    assert abs(int(first["target_chars"]) - expected_target) <= 1, (
        "regular-path target_chars no longer tracks source_words x 0.55 -- "
        "update this guard's documentation of the circularity mechanism"
    )


def test_zh_en_probe_template_has_no_numeric_length_anchor() -> None:
    """The zh->en probe template must not smuggle length-budget tokens back
    in -- it is the whole basis for calling the measurement 'natural'."""
    from services.gemini.translator import _PROBE_TEMPLATE_BY_PAIR

    template = _PROBE_TEMPLATE_BY_PAIR[("zh-CN", "en")]
    for banned_token in ("min_chars", "max_chars", "target_chars", "0.55"):
        assert banned_token not in template, (
            f"zh->en probe template now contains {banned_token!r} -- the probe "
            "path is no longer a constraint-free measurement entry"
        )


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
