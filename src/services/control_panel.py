import cgi
import ast
import copy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlparse
from uuid import uuid4
import webbrowser

from core.exceptions import IngestionError, StateError, TTSConfigurationError, TranslationConfigurationError
from modules.ingestion.intake import AuthoritativeIntakeBuilder, AuthoritativeIntakeRequest
from modules.media_understanding.models import (
    MediaSourceKind,
    REAL_AUTHORITATIVE_MEDIA_SOURCE_KINDS,
    SKELETON_AUTHORITATIVE_MEDIA_SOURCE_KINDS,
    describe_authoritative_flow,
)
from modules.translation.providers import TranslationProviderSelectionConfig
from services import config_loader
from services.project_state_summary import build_project_state_summary
from services.tts_provider import TTSProviderSelectionConfig
from services.voice_clone import VoiceCloneConfig, VoiceCloneConfigurationError
from services.voice_registry import SpeakerVoiceProfile, VoiceRegistry, VoiceResolver


CONTROL_PANEL_DEFAULT_HOST = "127.0.0.1"
CONTROL_PANEL_DEFAULT_PORT = 8765
CONTROL_PANEL_TITLE = "AIVideoTrans 本地工作台"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAIN_PY_PATH = PROJECT_ROOT / "main.py"
WORKBENCH_RUN_TIMEOUT_SECONDS = 600
WORKBENCH_PROVIDER_MODES = ("mock", "real")
WORKBENCH_UPLOAD_AUDIO_EXTENSIONS = (".wav", ".wave")
WORKBENCH_UPLOADED_AUDIO_ROOT = PROJECT_ROOT / "voice_bank" / "input_audio"
MASKED_SECRET_DISPLAY_VALUE = "已配置（已脱敏）"
SENSITIVE_CONFIG_FIELD_PATHS = (
    ("translation", "api_key"),
    ("tts", "api_key"),
    ("voice_clone", "api_key"),
)


def build_control_panel_snapshot(
    *,
    config_path: Path | None = None,
    registry_path: Path | None = None,
    last_workbench_run: dict[str, object] | None = None,
) -> dict[str, object]:
    project_config = config_loader.load_project_local_config(config_path)
    effective_config_path = project_config.path
    editable_config = _sanitize_config_sections_for_display(
        config_loader.build_editable_project_local_config_payload(project_config)
    )
    resolved_registry_path, resolved_registry_path_source = _resolve_voice_registry_path(
        project_config,
        override_registry_path=registry_path,
    )

    return {
        "meta": {
            "title": CONTROL_PANEL_TITLE,
            "config_path": str(effective_config_path),
            "config_exists": effective_config_path.exists(),
            "config_error": project_config.error,
            "registry_path": str(resolved_registry_path),
            "registry_path_source": resolved_registry_path_source,
            "editable_sections": list(config_loader.EDITABLE_PROJECT_LOCAL_CONFIG_SECTIONS),
        },
        "config": {
            "sections": {
                section_name: editable_config.get(section_name, {})
                for section_name in config_loader.EDITABLE_PROJECT_LOCAL_CONFIG_SECTIONS
            }
        },
        "diagnostics": {
            "paths": _build_path_diagnostics(project_config),
            "media_understanding": _build_media_understanding_diagnostic(),
            "translation": _build_translation_diagnostic(effective_config_path),
            "tts": _build_tts_diagnostic(effective_config_path),
            "voice_clone": _build_voice_clone_diagnostic(effective_config_path),
            "voice_registry": _build_voice_registry_diagnostic(
                project_config,
                resolved_registry_path,
                resolved_registry_path_source,
            ),
        },
        "voice_registry": _build_voice_registry_snapshot(resolved_registry_path),
        "workbench": _build_workbench_snapshot(last_workbench_run),
    }


def save_control_panel_sections(
    section_overrides: dict[str, object],
    *,
    config_path: Path | None = None,
) -> dict[str, object]:
    loaded_config = config_loader.load_project_local_config(config_path)
    restored_overrides = _restore_sensitive_config_values_for_save(section_overrides, loaded_config)
    config_loader.save_project_local_config_sections(restored_overrides, config_path=config_path)
    return build_control_panel_snapshot(config_path=config_path)


def set_control_panel_default_voice(
    *,
    speaker_id: str,
    voice_id: str,
    config_path: Path | None = None,
    registry_path: Path | None = None,
) -> dict[str, object]:
    project_config = config_loader.load_project_local_config(config_path)
    resolved_registry_path, _ = _resolve_voice_registry_path(
        project_config,
        override_registry_path=registry_path,
    )
    registry = VoiceRegistry(str(resolved_registry_path))
    registry.set_default_voice(speaker_id, voice_id)
    return build_control_panel_snapshot(
        config_path=config_path,
        registry_path=resolved_registry_path,
    )


def run_control_panel_default_demo() -> dict[str, object]:
    return _execute_workbench_command(
        ["default_demo"],
        command_args=[],
    )


def run_control_panel_local_audio_demo(
    *,
    local_audio_path: str,
    translation_mode: str = "mock",
    tts_mode: str = "mock",
) -> dict[str, object]:
    normalized_audio_path = local_audio_path.strip()
    if not normalized_audio_path:
        return _build_failed_workbench_result(
            run_kind="local_audio_demo",
            command=[str(sys.executable), str(MAIN_PY_PATH), "local-audio-demo"],
            error_message="local-audio-demo 需要提供本地音频路径。",
        )
    try:
        normalized_audio_path = _normalize_local_audio_intake_path(normalized_audio_path)
    except IngestionError as exc:
        return _build_failed_workbench_result(
            run_kind="local_audio_demo",
            command=[str(sys.executable), str(MAIN_PY_PATH), "local-audio-demo"],
            error_message=str(exc),
        )
    normalized_translation_mode = _normalize_workbench_provider_mode(translation_mode)
    normalized_tts_mode = _normalize_workbench_provider_mode(tts_mode)
    return _execute_workbench_command(
        ["local_audio_demo"],
        command_args=[
            "local-audio-demo",
            normalized_audio_path,
            normalized_translation_mode,
            normalized_tts_mode,
        ],
        input_path=normalized_audio_path,
    )


def run_control_panel_uploaded_local_audio_demo(
    *,
    uploaded_filename: str,
    uploaded_file_bytes: bytes,
    translation_mode: str = "mock",
    tts_mode: str = "mock",
) -> dict[str, object]:
    normalized_filename = Path(uploaded_filename or "").name.strip()
    command = [str(sys.executable), str(MAIN_PY_PATH), "local-audio-demo"]
    if not normalized_filename:
        return _build_failed_workbench_result(
            run_kind="local_audio_demo",
            command=command,
            error_message="工作台上传需要提供音频文件名。",
        )

    normalized_extension = Path(normalized_filename).suffix.lower()
    if normalized_extension not in WORKBENCH_UPLOAD_AUDIO_EXTENSIONS:
        allowed_extensions = ", ".join(WORKBENCH_UPLOAD_AUDIO_EXTENSIONS)
        return _build_failed_workbench_result(
            run_kind="local_audio_demo",
            command=command,
            error_message=(
                "当前工作台上传仅支持现有 local-audio-demo 边界内的 WAV/WAVE 音频。"
                f" 支持格式：{allowed_extensions}。收到：{normalized_extension or '<no extension>'}"
            ),
        )

    if not uploaded_file_bytes:
        return _build_failed_workbench_result(
            run_kind="local_audio_demo",
            command=command,
            error_message="上传文件为空，无法运行 local-audio-demo。",
        )

    try:
        saved_audio_path = _save_uploaded_workbench_audio(
            filename=normalized_filename,
            file_bytes=uploaded_file_bytes,
        )
    except OSError as exc:
        return _build_failed_workbench_result(
            run_kind="local_audio_demo",
            command=command,
            error_message=f"上传文件保存失败：{exc}",
        )

    return run_control_panel_local_audio_demo(
        local_audio_path=str(saved_audio_path),
        translation_mode=translation_mode,
        tts_mode=tts_mode,
    )


def run_control_panel_server(
    *,
    host: str = CONTROL_PANEL_DEFAULT_HOST,
    port: int = CONTROL_PANEL_DEFAULT_PORT,
    config_path: Path | None = None,
    registry_path: Path | None = None,
) -> None:
    server = create_control_panel_server(
        host=host,
        port=port,
        config_path=config_path,
        registry_path=registry_path,
    )
    control_panel_url = f"http://{host}:{port}"
    print(f"{CONTROL_PANEL_TITLE} 已启动：http://{host}:{port}")
    print(f"配置文件：{server.config_path}")
    print(f"音色注册表：{server.registry_path}")
    _open_control_panel_browser(control_panel_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止本地工作台。")
    finally:
        server.server_close()


def create_control_panel_server(
    *,
    host: str = CONTROL_PANEL_DEFAULT_HOST,
    port: int = CONTROL_PANEL_DEFAULT_PORT,
    config_path: Path | None = None,
    registry_path: Path | None = None,
) -> ThreadingHTTPServer:
    handler_class = _build_control_panel_handler(
        config_path=config_path,
        registry_path=registry_path,
    )
    server = ThreadingHTTPServer((host, port), handler_class)
    project_config = config_loader.load_project_local_config(config_path)
    resolved_registry_path, _ = _resolve_voice_registry_path(
        project_config,
        override_registry_path=registry_path,
    )
    server.config_path = str(project_config.path)  # type: ignore[attr-defined]
    server.registry_path = str(resolved_registry_path)  # type: ignore[attr-defined]
    server.last_workbench_run = None  # type: ignore[attr-defined]
    return server


def render_control_panel_html() -> str:
    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AIVideoTrans 本地工作台</title>
  <style>
    :root {
      --bg: #f6efe3;
      --panel: #fff9f0;
      --ink: #1c2a2a;
      --muted: #5e6a68;
      --accent: #d97231;
      --accent-soft: #f3d2b6;
      --line: #d6c4ad;
      --ok: #1f7a58;
      --warn: #9f5f00;
      --bad: #b53a2d;
      --shadow: 0 16px 32px rgba(28, 42, 42, 0.08);
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(217, 114, 49, 0.14), transparent 28%),
        linear-gradient(180deg, #f9f3e9 0%, var(--bg) 100%);
    }

    header {
      padding: 32px 24px 16px;
      border-bottom: 1px solid rgba(28, 42, 42, 0.08);
    }

    header h1 {
      margin: 0 0 8px;
      font-family: "IBM Plex Serif", Georgia, serif;
      font-size: 32px;
      line-height: 1.15;
    }

    header p {
      margin: 0;
      color: var(--muted);
      max-width: 880px;
    }

    main {
      padding: 20px 24px 40px;
      display: grid;
      gap: 20px;
    }

    .hero-grid,
    .section-grid {
      display: grid;
      gap: 20px;
    }

    .hero-grid {
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    }

    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
      box-shadow: var(--shadow);
      padding: 18px;
    }

    .panel h2,
    .panel h3 {
      margin: 0 0 12px;
      font-family: "IBM Plex Serif", Georgia, serif;
    }

    .meta-list,
    .diagnostic-list,
    .voice-list {
      display: grid;
      gap: 10px;
      margin: 0;
      padding: 0;
      list-style: none;
    }

    .meta-list li,
    .diagnostic-list li {
      padding: 10px 12px;
      border-radius: 12px;
      background: rgba(255, 255, 255, 0.68);
      border: 1px solid rgba(214, 196, 173, 0.88);
    }

    .section-grid {
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    }

    textarea {
      width: 100%;
      min-height: 260px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: #fffdf8;
      color: var(--ink);
      padding: 14px;
      font-family: "IBM Plex Mono", Consolas, monospace;
      font-size: 13px;
      line-height: 1.45;
      resize: vertical;
    }

    button {
      border: none;
      border-radius: 999px;
      padding: 11px 16px;
      font: inherit;
      cursor: pointer;
      background: var(--accent);
      color: white;
    }

    button.secondary {
      background: transparent;
      color: var(--ink);
      border: 1px solid var(--line);
    }

    .toolbar {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 14px;
    }

    .status {
      min-height: 24px;
      font-size: 14px;
      color: var(--muted);
    }

    .status.ok { color: var(--ok); }
    .status.warn { color: var(--warn); }
    .status.bad { color: var(--bad); }

    .pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 12px;
      background: var(--accent-soft);
      color: var(--ink);
      margin-right: 6px;
      margin-bottom: 6px;
    }

    .voice-card {
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: rgba(255, 255, 255, 0.72);
    }

    .voice-card header {
      padding: 0;
      border: 0;
      margin-bottom: 12px;
    }

    .voice-card h3 {
      margin-bottom: 6px;
    }

    .voice-card p {
      margin: 0;
      color: var(--muted);
    }

    select {
      width: 100%;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid var(--line);
      margin-top: 10px;
      background: #fffdf8;
      color: var(--ink);
      font: inherit;
    }

    input[type="text"],
    input[type="file"] {
      width: 100%;
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid var(--line);
      margin-top: 10px;
      background: #fffdf8;
      color: var(--ink);
      font: inherit;
    }

    .form-grid {
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      margin-top: 12px;
    }

    .workbench-result {
      margin-top: 16px;
      display: grid;
      gap: 12px;
    }

    .empty {
      padding: 18px;
      border-radius: 14px;
      border: 1px dashed var(--line);
      color: var(--muted);
      text-align: center;
    }

    code {
      font-family: "IBM Plex Mono", Consolas, monospace;
      font-size: 12px;
    }

    @media (max-width: 720px) {
      header { padding: 24px 18px 12px; }
      main { padding: 16px 18px 28px; }
      textarea { min-height: 220px; }
    }
  </style>
</head>
<body>
  <header>
    <h1>AIVideoTrans 本地工作台</h1>
    <p>
      这是一个保持最小边界的本地工作台：用于编辑分区配置、查看 provider 就绪状态、
      管理 speaker 音色资产、检查 authoritative media path，并触发当前已有的 demo 运行入口与本地音频上传入口，
      同时不改动 workflow 主干。
    </p>
  </header>
  <main>
    <section class="hero-grid">
      <div class="panel">
        <h2>工作区</h2>
        <ul class="meta-list" id="meta-list"></ul>
      </div>
      <div class="panel">
        <h2>Provider 诊断</h2>
        <div id="diagnostics-grid" class="diagnostic-list"></div>
      </div>
    </section>

    <section class="panel">
      <h2>配置分区</h2>
      <p class="status" id="config-status">正在加载配置...</p>
      <div class="toolbar">
        <button id="save-config-button" type="button">保存配置</button>
        <button id="refresh-button" class="secondary" type="button">刷新</button>
      </div>
      <div class="section-grid" id="config-sections"></div>
    </section>

    <section class="panel">
      <h2>音色注册表</h2>
      <p class="status" id="registry-status">正在加载注册表...</p>
      <div id="voice-registry-summary"></div>
      <div class="voice-list" id="voice-list"></div>
    </section>

    <section class="panel">
      <h2>工作台运行</h2>
      <p class="status" id="workbench-status">就绪。</p>
      <div class="toolbar">
        <button id="run-default-demo-button" type="button">运行默认 Demo</button>
      </div>
      <div class="form-grid">
        <div>
          <label for="local-audio-path">本地音频路径</label>
          <input id="local-audio-path" type="text" placeholder="D:\\path\\to\\input.wav" />
        </div>
        <div>
          <label for="local-audio-file">上传本地音频（WAV/WAVE）</label>
          <input id="local-audio-file" type="file" accept=".wav,.wave,audio/wav" />
        </div>
        <div>
          <label for="translation-mode">翻译模式</label>
          <select id="translation-mode">
            <option value="mock" selected>mock</option>
            <option value="real">real</option>
          </select>
        </div>
        <div>
          <label for="tts-mode">TTS 模式</label>
          <select id="tts-mode">
            <option value="mock" selected>mock</option>
            <option value="real">real</option>
          </select>
        </div>
      </div>
      <p class="status">
        当前上传入口仅支持本地单个 WAV/WAVE 文件。上传后会先保存到项目目录，再复用现有 local-audio-demo。
      </p>
      <div class="toolbar">
        <button id="run-local-audio-demo-button" type="button">运行本地音频 Demo</button>
        <button id="upload-local-audio-demo-button" class="secondary" type="button">上传并运行本地音频 Demo</button>
      </div>
      <div id="workbench-last-run" class="workbench-result"></div>
    </section>
  </main>

  <script>
    const editableSections = ["paths", "translation", "tts", "voice_clone", "voice_registry"];
    const sectionDisplayNames = {
      paths: "paths（路径）",
      translation: "translation（翻译）",
      tts: "tts（配音）",
      voice_clone: "voice_clone（音色克隆）",
      voice_registry: "voice_registry（音色注册表）"
    };
    const diagnosticDisplayNames = {
      media_understanding: "media_understanding（媒体理解）",
      translation: "translation（翻译）",
      tts: "tts（配音）",
      voice_clone: "voice_clone（音色克隆）",
      voice_registry: "voice_registry（音色注册表）",
      paths: "paths（路径）"
    };
    const workbenchFieldLabels = {
      output_root: "输出根目录",
      input_path: "输入路径",
      source_kind: "source kind",
      locator: "source locator",
      video_title: "video title",
      media_authoritative_path_kind: "authoritative 路径类型",
      cn_text_produced: "已产出中文翻译文本"
    };
    const stageDisplayNames = {
      media_understanding: "media_understanding（媒体理解）",
      translation: "translation（翻译）",
      alignment: "alignment（对齐）",
      draft: "draft（草稿）",
      tts: "tts（配音）"
    };
    const resolutionSourceLabels = {
      speaker_default_cloned: "speaker 默认 cloned",
      speaker_default_builtin: "speaker 默认 builtin",
      project_default_builtin: "项目默认 builtin",
      env_fallback: "环境变量回退",
      unresolved: "未解析"
    };
    const verificationStatusLabels = {
      verified: "已验证",
      failed: "验证失败",
      unverified: "未验证",
      pending: "待验证"
    };
    const voiceTypeLabels = {
      builtin: "builtin",
      cloned: "cloned"
    };
    const statusLabels = {
      success: "成功",
      failed: "失败",
      done: "完成",
      running: "运行中",
      skipped: "跳过",
      pending: "待处理"
    };
    const executionModeLabels = {
      fresh_run: "fresh_run",
      provider_run: "provider_run",
      restored: "restored",
      cached: "cached",
      mock: "mock"
    };
    let latestSnapshot = null;

    async function fetchSnapshot() {
      const response = await fetch("/api/state");
      const payload = await response.json();
      if (!response.ok || !payload.ok) {
        throw new Error(payload.error || "加载工作台状态失败。");
      }
      latestSnapshot = payload.snapshot;
      renderSnapshot(payload.snapshot);
    }

    function renderSnapshot(snapshot) {
      renderMeta(snapshot.meta);
      renderDiagnostics(snapshot.diagnostics);
      renderSections(snapshot.config.sections);
      renderVoiceRegistry(snapshot.voice_registry);
      renderWorkbench(snapshot.workbench);
      setStatus("config-status", "配置已加载。", "ok");
      setStatus("registry-status", "注册表已加载。", "ok");
    }

    function renderMeta(meta) {
      const list = document.getElementById("meta-list");
      list.innerHTML = "";
      const rows = [
        `配置文件：${meta.config_path}`,
        `配置文件存在：${formatDisplayValue(meta.config_exists)}`,
        `注册表路径：${meta.registry_path}`,
        `注册表路径来源：${meta.registry_path_source || "default"}`
      ];
      if (meta.config_error) {
        rows.push(`配置错误：${meta.config_error}`);
      }
      for (const text of rows) {
        const item = document.createElement("li");
        item.textContent = text;
        list.appendChild(item);
      }
    }

    function renderDiagnostics(diagnostics) {
      const container = document.getElementById("diagnostics-grid");
      container.innerHTML = "";
      const sections = [
        ["media_understanding", diagnostics.media_understanding],
        ["translation", diagnostics.translation],
        ["tts", diagnostics.tts],
        ["voice_clone", diagnostics.voice_clone],
        ["voice_registry", diagnostics.voice_registry],
        ["paths", diagnostics.paths]
      ];
      for (const [name, payload] of sections) {
        const item = document.createElement("div");
        item.className = "voice-card";
        const title = document.createElement("h3");
        title.textContent = diagnosticDisplayNames[name] || name;
        item.appendChild(title);
        const summary = document.createElement("p");
        summary.textContent = payload.summary || "";
        item.appendChild(summary);
        const details = document.createElement("div");
        details.style.marginTop = "10px";
        for (const [key, value] of Object.entries(payload)) {
          if (key === "summary") {
            continue;
          }
          const chip = document.createElement("span");
          chip.className = "pill";
          chip.textContent = `${formatFieldLabel(key)}：${formatDisplayValue(value)}`;
          details.appendChild(chip);
        }
        item.appendChild(details);
        container.appendChild(item);
      }
    }

    function renderSections(sections) {
      const container = document.getElementById("config-sections");
      if (container.childElementCount === 0) {
        for (const sectionName of editableSections) {
          const panel = document.createElement("div");
          panel.className = "panel";
          const heading = document.createElement("h3");
          heading.textContent = sectionDisplayNames[sectionName] || sectionName;
          panel.appendChild(heading);
          const textarea = document.createElement("textarea");
          textarea.id = `section-${sectionName}`;
          panel.appendChild(textarea);
          container.appendChild(panel);
        }
      }
      for (const sectionName of editableSections) {
        const textarea = document.getElementById(`section-${sectionName}`);
        textarea.value = JSON.stringify(sections[sectionName] || {}, null, 2);
      }
    }

    function renderVoiceRegistry(voiceRegistry) {
      const summary = document.getElementById("voice-registry-summary");
      summary.innerHTML = "";
      const summaryText = document.createElement("p");
      summaryText.innerHTML = `<code>${voiceRegistry.path}</code> | speaker 数：${voiceRegistry.speaker_count} | 注册表存在：${formatDisplayValue(voiceRegistry.exists)}`;
      summary.appendChild(summaryText);

      const projectDefault = voiceRegistry.project_default_builtin_voice;
      if (projectDefault) {
        const chip = document.createElement("span");
        chip.className = "pill";
        chip.textContent = `项目默认 builtin：${projectDefault.voice_id}`;
        summary.appendChild(chip);
      }

      const container = document.getElementById("voice-list");
      container.innerHTML = "";
      if (!voiceRegistry.speakers.length) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "当前还没有 speaker 音色记录。请先用已有的 voice-registry CLI 命令注册音色，再刷新这个页面。";
        container.appendChild(empty);
        return;
      }

      for (const speaker of voiceRegistry.speakers) {
        const card = document.createElement("div");
        card.className = "voice-card";
        const header = document.createElement("header");
        header.innerHTML =
          `<h3>${speaker.speaker_id}</h3><p>${speaker.speaker_name || "未命名 speaker"} | 默认：${speaker.default_voice_id || "无"} (${speaker.default_voice_type || "暂无"})</p>`;
        card.appendChild(header);

        const sourceChip = document.createElement("span");
        sourceChip.className = "pill";
        sourceChip.textContent = `解析来源：${formatResolutionSource(speaker.resolution_source)}`;
        card.appendChild(sourceChip);

        const select = document.createElement("select");
        select.id = `voice-select-${speaker.speaker_id}`;
        for (const voice of speaker.voices) {
          const option = document.createElement("option");
          option.value = voice.voice_id;
          option.textContent = `${voice.voice_id} | ${formatVoiceType(voice.voice_type)} | ${voice.label} | ${formatVerificationStatus(voice.verification_status)}`;
          option.selected = voice.voice_id === speaker.default_voice_id;
          select.appendChild(option);
        }
        card.appendChild(select);

        const voicesMeta = document.createElement("div");
        voicesMeta.style.marginTop = "12px";
        for (const voice of speaker.voices) {
          const meta = document.createElement("div");
          meta.className = "pill";
          const defaultMarker = voice.voice_id === speaker.default_voice_id ? "默认" : "音色";
          const verifiedAt = voice.last_verified_at || "从未";
          meta.textContent = `${defaultMarker}：${voice.voice_id} | ${formatVerificationStatus(voice.verification_status)} | 验证时间：${verifiedAt}`;
          voicesMeta.appendChild(meta);
        }
        card.appendChild(voicesMeta);

        const button = document.createElement("button");
        button.type = "button";
        button.style.marginTop = "10px";
        button.textContent = "设为默认";
        button.addEventListener("click", async () => {
          try {
            await postJson("/api/voice-registry/set-default", {
              speaker_id: speaker.speaker_id,
              voice_id: select.value
            });
            await fetchSnapshot();
            setStatus("registry-status", `已更新 ${speaker.speaker_id} 的默认音色。`, "ok");
          } catch (error) {
            setStatus("registry-status", error.message, "bad");
          }
        });
        card.appendChild(button);
        container.appendChild(card);
      }
    }

    function renderWorkbench(workbench) {
      const container = document.getElementById("workbench-last-run");
      container.innerHTML = "";
      if (!workbench || !workbench.last_run) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.textContent = "当前还没有运行记录。请使用上面的按钮运行默认 Demo 或 local-audio-demo。";
        container.appendChild(empty);
        return;
      }

      const run = workbench.last_run;
      const summaryCard = document.createElement("div");
      summaryCard.className = "voice-card";
      summaryCard.innerHTML =
        `<h3>最近一次运行：${run.run_kind}</h3><p>状态：${formatStatusValue(run.status)} | 草稿路径：${run.draft_path || "暂无"}</p>`;
      container.appendChild(summaryCard);

      const resultMeta = document.createElement("div");
      for (const [label, value] of [
        ["output_root", run.output_root],
        ["input_path", run.input_path],
        ["media_authoritative_path_kind", run.provider_mode_summary?.media_authoritative_path_kind],
        ["cn_text_produced", run.provider_mode_summary?.cn_text_produced]
      ]) {
        if (value === undefined || value === null || value === "") {
          continue;
        }
        const chip = document.createElement("span");
        chip.className = "pill";
        chip.textContent = `${workbenchFieldLabels[label] || label}：${formatDisplayValue(value)}`;
        resultMeta.appendChild(chip);
      }
      const sourceContext = run.source_context || run.result_summary?.source_context || {};
      for (const [label, value] of [
        ["source_kind", sourceContext.source_kind],
        ["locator", sourceContext.locator],
        ["video_title", sourceContext.video_title]
      ]) {
        if (value === undefined || value === null || value === "") {
          continue;
        }
        const chip = document.createElement("span");
        chip.className = "pill";
        chip.textContent = `${workbenchFieldLabels[label] || label}：${formatDisplayValue(value)}`;
        resultMeta.appendChild(chip);
      }
      container.appendChild(resultMeta);

      const stageCard = document.createElement("div");
      stageCard.className = "voice-card";
      const stageTitle = document.createElement("h3");
      stageTitle.textContent = "阶段执行";
      stageCard.appendChild(stageTitle);
      const projectStateSummary = run.project_state_summary || {};
      if (projectStateSummary.overall_status || projectStateSummary.latest_stage_name) {
        const projectStateMeta = document.createElement("p");
        const latestStageLabel = projectStateSummary.latest_stage_label
          || stageDisplayNames[projectStateSummary.latest_stage_name]
          || projectStateSummary.latest_stage_name
          || "暂无";
        const overallStatusLabel = projectStateSummary.overall_status_label
          || formatStatusValue(projectStateSummary.overall_status)
          || "暂无";
        projectStateMeta.textContent =
          `项目状态：${overallStatusLabel} | 最新阶段：${latestStageLabel}`;
        stageCard.appendChild(projectStateMeta);
      }
      const stageEntries = Array.isArray(projectStateSummary.stages) && projectStateSummary.stages.length
        ? projectStateSummary.stages
        : Object.entries(run.stage_execution_summary || {}).map(([stageName, stagePayload]) => ({
            name: stageName,
            ...(stagePayload || {})
          }));
      for (const stagePayload of stageEntries) {
        const stageName = stagePayload.name || "unknown";
        const chip = document.createElement("span");
        chip.className = "pill";
        const stageLabel = stagePayload.label || stageDisplayNames[stageName] || stageName;
        const stageStatus = stagePayload.status_label || formatStatusValue(stagePayload.status) || "暂无";
        chip.textContent =
          `${stageLabel}：${stageStatus} / ${formatExecutionMode(stagePayload.execution_mode) || "暂无"}`;
        stageCard.appendChild(chip);
      }
      container.appendChild(stageCard);

      if (run.error) {
        const errorCard = document.createElement("div");
        errorCard.className = "voice-card";
        errorCard.innerHTML = `<h3>最近一次错误</h3><p>${run.error}</p>`;
        container.appendChild(errorCard);
      }
    }

    async function postJson(url, payload) {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const responsePayload = await response.json();
      if (!response.ok || !responsePayload.ok) {
        throw new Error(responsePayload.error || "请求失败。");
      }
      latestSnapshot = responsePayload.snapshot;
      return responsePayload.snapshot;
    }

    async function postFormData(url, formData) {
      const response = await fetch(url, {
        method: "POST",
        body: formData
      });
      const responsePayload = await response.json();
      if (!response.ok || !responsePayload.ok) {
        throw new Error(responsePayload.error || "上传请求失败。");
      }
      latestSnapshot = responsePayload.snapshot;
      return responsePayload.snapshot;
    }

    function setStatus(elementId, message, statusClass) {
      const node = document.getElementById(elementId);
      node.textContent = message;
      node.className = `status ${statusClass || ""}`.trim();
    }

    async function saveConfigSections() {
      try {
        const sections = {};
        for (const sectionName of editableSections) {
          const rawText = document.getElementById(`section-${sectionName}`).value;
          sections[sectionName] = JSON.parse(rawText);
        }
        const snapshot = await postJson("/api/config", { sections });
        renderSnapshot(snapshot);
        setStatus("config-status", "配置已保存到 autodub.local.json。", "ok");
      } catch (error) {
        setStatus("config-status", error.message, "bad");
      }
    }

    document.getElementById("save-config-button").addEventListener("click", saveConfigSections);
    document.getElementById("run-default-demo-button").addEventListener("click", async () => {
      try {
        setStatus("workbench-status", "正在运行默认 Demo...", "warn");
        const snapshot = await postJson("/api/workbench/run-demo", {});
        renderSnapshot(snapshot);
        const lastRun = snapshot.workbench?.last_run;
        setStatus(
          "workbench-status",
          lastRun?.status === "success" ? "默认 Demo 已完成。" : (lastRun?.error || "默认 Demo 失败。"),
          lastRun?.status === "success" ? "ok" : "bad"
        );
      } catch (error) {
        setStatus("workbench-status", error.message, "bad");
      }
    });
    document.getElementById("run-local-audio-demo-button").addEventListener("click", async () => {
      try {
        setStatus("workbench-status", "正在运行 local-audio-demo...", "warn");
        const snapshot = await postJson("/api/workbench/run-local-audio-demo", {
          local_audio_path: document.getElementById("local-audio-path").value,
          translation_mode: document.getElementById("translation-mode").value,
          tts_mode: document.getElementById("tts-mode").value
        });
        renderSnapshot(snapshot);
        const lastRun = snapshot.workbench?.last_run;
        setStatus(
          "workbench-status",
          lastRun?.status === "success" ? "local-audio-demo 已完成。" : (lastRun?.error || "local-audio-demo 失败。"),
          lastRun?.status === "success" ? "ok" : "bad"
        );
      } catch (error) {
        setStatus("workbench-status", error.message, "bad");
      }
    });
    document.getElementById("upload-local-audio-demo-button").addEventListener("click", async () => {
      const fileInput = document.getElementById("local-audio-file");
      const selectedFile = fileInput.files && fileInput.files[0];
      if (!selectedFile) {
        setStatus("workbench-status", "请先选择一个本地 WAV/WAVE 音频文件。", "bad");
        return;
      }
      try {
        setStatus("workbench-status", "正在上传并运行 local-audio-demo...", "warn");
        const formData = new FormData();
        formData.append("audio_file", selectedFile);
        formData.append("translation_mode", document.getElementById("translation-mode").value);
        formData.append("tts_mode", document.getElementById("tts-mode").value);
        const snapshot = await postFormData("/api/workbench/upload-and-run-local-audio-demo", formData);
        renderSnapshot(snapshot);
        const lastRun = snapshot.workbench?.last_run;
        if (lastRun?.input_path) {
          document.getElementById("local-audio-path").value = lastRun.input_path;
        }
        fileInput.value = "";
        setStatus(
          "workbench-status",
          lastRun?.status === "success"
            ? "上传音频并运行 local-audio-demo 已完成。"
            : (lastRun?.error || "上传音频并运行 local-audio-demo 失败。"),
          lastRun?.status === "success" ? "ok" : "bad"
        );
      } catch (error) {
        setStatus("workbench-status", error.message, "bad");
      }
    });
    document.getElementById("refresh-button").addEventListener("click", async () => {
      try {
        await fetchSnapshot();
      } catch (error) {
        setStatus("config-status", error.message, "bad");
        setStatus("registry-status", error.message, "bad");
        setStatus("workbench-status", error.message, "bad");
      }
    });

    function formatFieldLabel(key) {
      return workbenchFieldLabels[key] || key;
    }

    function formatDisplayValue(value) {
      if (Array.isArray(value)) {
        return value.join(", ");
      }
      if (typeof value === "boolean") {
        return value ? "是" : "否";
      }
      if (value === undefined || value === null || value === "") {
        return "暂无";
      }
      return String(value);
    }

    function formatResolutionSource(source) {
      return resolutionSourceLabels[source] || source || "暂无";
    }

    function formatVerificationStatus(status) {
      return verificationStatusLabels[status] || status || "暂无";
    }

    function formatVoiceType(voiceType) {
      return voiceTypeLabels[voiceType] || voiceType || "暂无";
    }

    function formatStatusValue(status) {
      return statusLabels[status] || status || "暂无";
    }

    function formatExecutionMode(executionMode) {
      return executionModeLabels[executionMode] || executionMode || "暂无";
    }

    fetchSnapshot().catch((error) => {
      setStatus("config-status", error.message, "bad");
      setStatus("registry-status", error.message, "bad");
      setStatus("workbench-status", error.message, "bad");
    });
  </script>
</body>
</html>
"""


def _build_control_panel_handler(
    *,
    config_path: Path | None,
    registry_path: Path | None,
):
    class ControlPanelHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed_path = urlparse(self.path)
            try:
                if parsed_path.path == "/":
                    self._send_html(HTTPStatus.OK, render_control_panel_html())
                    return
                if parsed_path.path == "/api/state":
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "ok": True,
                            "snapshot": build_control_panel_snapshot(
                                config_path=config_path,
                                registry_path=registry_path,
                                last_workbench_run=getattr(self.server, "last_workbench_run", None),
                            ),
                        },
                    )
                    return
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Route not found."})
            except (StateError, TranslationConfigurationError, TTSConfigurationError, VoiceCloneConfigurationError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            except Exception as exc:  # pragma: no cover - defensive handler boundary
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})

        def do_POST(self) -> None:  # noqa: N802
            parsed_path = urlparse(self.path)
            try:
                if parsed_path.path == "/api/workbench/upload-and-run-local-audio-demo":
                    payload = self._read_multipart_upload_payload()
                    run_result = run_control_panel_uploaded_local_audio_demo(
                        uploaded_filename=str(payload.get("uploaded_filename", "")),
                        uploaded_file_bytes=payload.get("uploaded_file_bytes", b""),
                        translation_mode=str(payload.get("translation_mode", "mock")),
                        tts_mode=str(payload.get("tts_mode", "mock")),
                    )
                    self.server.last_workbench_run = run_result  # type: ignore[attr-defined]
                    snapshot = build_control_panel_snapshot(
                        config_path=config_path,
                        registry_path=registry_path,
                        last_workbench_run=run_result,
                    )
                    self._send_json(HTTPStatus.OK, {"ok": True, "snapshot": snapshot, "run": run_result})
                    return

                payload = self._read_json_payload()
                if parsed_path.path == "/api/config":
                    snapshot = save_control_panel_sections(
                        payload.get("sections", {}),
                        config_path=config_path,
                    )
                    self._send_json(HTTPStatus.OK, {"ok": True, "snapshot": snapshot})
                    return
                if parsed_path.path == "/api/voice-registry/set-default":
                    snapshot = set_control_panel_default_voice(
                        speaker_id=str(payload.get("speaker_id", "")),
                        voice_id=str(payload.get("voice_id", "")),
                        config_path=config_path,
                        registry_path=registry_path,
                    )
                    self._send_json(HTTPStatus.OK, {"ok": True, "snapshot": snapshot})
                    return
                if parsed_path.path == "/api/workbench/run-demo":
                    run_result = run_control_panel_default_demo()
                    self.server.last_workbench_run = run_result  # type: ignore[attr-defined]
                    snapshot = build_control_panel_snapshot(
                        config_path=config_path,
                        registry_path=registry_path,
                        last_workbench_run=run_result,
                    )
                    self._send_json(HTTPStatus.OK, {"ok": True, "snapshot": snapshot, "run": run_result})
                    return
                if parsed_path.path == "/api/workbench/run-local-audio-demo":
                    run_result = run_control_panel_local_audio_demo(
                        local_audio_path=str(payload.get("local_audio_path", "")),
                        translation_mode=str(payload.get("translation_mode", "mock")),
                        tts_mode=str(payload.get("tts_mode", "mock")),
                    )
                    self.server.last_workbench_run = run_result  # type: ignore[attr-defined]
                    snapshot = build_control_panel_snapshot(
                        config_path=config_path,
                        registry_path=registry_path,
                        last_workbench_run=run_result,
                    )
                    self._send_json(HTTPStatus.OK, {"ok": True, "snapshot": snapshot, "run": run_result})
                    return
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Route not found."})
            except (json.JSONDecodeError, ValueError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            except (StateError, TranslationConfigurationError, TTSConfigurationError, VoiceCloneConfigurationError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
            except Exception as exc:  # pragma: no cover - defensive handler boundary
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})

        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def _read_json_payload(self) -> dict[str, object]:
            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"
            loaded = json.loads(raw_body.decode("utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("JSON request body must be an object.")
            return loaded

        def _read_multipart_upload_payload(self) -> dict[str, object]:
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type.lower():
                raise ValueError("上传接口需要 multipart/form-data 请求。")
            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_length) if content_length > 0 else b""
            form = cgi.FieldStorage(
                fp=io.BytesIO(raw_body),
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": content_type,
                    "CONTENT_LENGTH": str(content_length),
                },
                keep_blank_values=True,
            )
            try:
                audio_field = form["audio_file"]
            except KeyError as exc:
                raise ValueError("请先选择一个本地音频文件再上传。") from exc

            if isinstance(audio_field, list):
                audio_field = audio_field[0]

            uploaded_filename = str(getattr(audio_field, "filename", "") or "")
            uploaded_file = getattr(audio_field, "file", None)
            if not uploaded_filename or uploaded_file is None:
                raise ValueError("上传接口未收到有效的音频文件。")

            uploaded_file_bytes = uploaded_file.read()
            if not isinstance(uploaded_file_bytes, bytes):
                uploaded_file_bytes = str(uploaded_file_bytes).encode("utf-8")

            return {
                "uploaded_filename": uploaded_filename,
                "uploaded_file_bytes": uploaded_file_bytes,
                "translation_mode": str(form.getfirst("translation_mode", "mock")),
                "tts_mode": str(form.getfirst("tts_mode", "mock")),
            }

        def _send_json(self, status_code: HTTPStatus, payload: dict[str, object]) -> None:
            serialized_payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(serialized_payload)))
            self.end_headers()
            self.wfile.write(serialized_payload)

        def _send_html(self, status_code: HTTPStatus, html: str) -> None:
            encoded_html = html.encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded_html)))
            self.end_headers()
            self.wfile.write(encoded_html)

    return ControlPanelHandler


def _build_path_diagnostics(project_config: config_loader.ProjectLocalConfig) -> dict[str, object]:
    voice_bank_root, voice_bank_root_source = config_loader.resolve_path_value(
        config=project_config,
        config_key_paths=(("paths", "voice_bank_root"),),
    )
    voice_source_audio_root, voice_source_audio_root_source = config_loader.resolve_path_value(
        config=project_config,
        config_key_paths=(("paths", "voice_source_audio_root"),),
    )
    voice_registry_path, voice_registry_path_source = config_loader.resolve_path_value(
        env_keys=["AUTODUB_TTS_VOICE_REGISTRY_PATH"],
        config=project_config,
        config_key_paths=(
            ("voice_registry", "registry_path"),
            ("tts", "voice_registry_path"),
            ("paths", "voice_registry_path"),
        ),
    )
    voice_verification_root, voice_verification_root_source = config_loader.resolve_path_value(
        config=project_config,
        config_key_paths=(("paths", "voice_verification_root"),),
    )
    if voice_verification_root is None:
        voice_verification_root = str(
            (project_config.path.parent / "voice_bank" / "verification_audio").resolve(strict=False)
        )
        voice_verification_root_source = "default"
    return {
        "summary": "当前基于配置解析出的本地资产路径。",
        "voice_bank_root": voice_bank_root,
        "voice_bank_root_source": voice_bank_root_source,
        "voice_source_audio_root": voice_source_audio_root,
        "voice_source_audio_root_source": voice_source_audio_root_source,
        "voice_registry_path": voice_registry_path,
        "voice_registry_path_source": voice_registry_path_source,
        "voice_verification_root": voice_verification_root,
        "voice_verification_root_source": voice_verification_root_source,
    }


def _sanitize_config_sections_for_display(editable_config: dict[str, object]) -> dict[str, object]:
    sanitized_config = copy.deepcopy(editable_config)
    for section_name, field_name in SENSITIVE_CONFIG_FIELD_PATHS:
        section_payload = sanitized_config.get(section_name)
        if not isinstance(section_payload, dict):
            continue
        raw_value = section_payload.get(field_name)
        if isinstance(raw_value, str) and raw_value.strip():
            section_payload[field_name] = MASKED_SECRET_DISPLAY_VALUE
            continue
        section_payload[field_name] = None
    return sanitized_config


def _restore_sensitive_config_values_for_save(
    section_overrides: dict[str, object],
    loaded_config: config_loader.ProjectLocalConfig,
) -> dict[str, object]:
    restored_overrides = copy.deepcopy(section_overrides)
    for section_name, field_name in SENSITIVE_CONFIG_FIELD_PATHS:
        section_payload = restored_overrides.get(section_name)
        if not isinstance(section_payload, dict):
            continue
        if field_name not in section_payload:
            continue

        submitted_value = section_payload.get(field_name)
        if submitted_value == MASKED_SECRET_DISPLAY_VALUE:
            existing_value = loaded_config.get_section(section_name).get(field_name)
            if isinstance(existing_value, str) and existing_value.strip():
                section_payload[field_name] = existing_value
            else:
                section_payload[field_name] = None
            continue

        if isinstance(submitted_value, str) and not submitted_value.strip():
            section_payload[field_name] = None
    return restored_overrides


def _build_media_understanding_diagnostic() -> dict[str, object]:
    real_authoritative_inputs = [
        kind.value
        for kind in (
            MediaSourceKind.TRANSCRIPT,
            MediaSourceKind.ATTRIBUTED_TRANSCRIPT,
            MediaSourceKind.LOCAL_SRT,
            MediaSourceKind.LOCAL_AUDIO,
        )
        if kind in REAL_AUTHORITATIVE_MEDIA_SOURCE_KINDS
    ]
    skeleton_authoritative_inputs = [
        kind.value
        for kind in (MediaSourceKind.LOCAL_VIDEO,)
        if kind in SKELETON_AUTHORITATIVE_MEDIA_SOURCE_KINDS
    ]
    all_authoritative_inputs = real_authoritative_inputs + skeleton_authoritative_inputs
    return {
        "summary": "当前 media_understanding 的 authoritative 输入路径及其下游桥接方式。",
        "authoritative_inputs": all_authoritative_inputs,
        "real_authoritative_inputs": real_authoritative_inputs,
        "skeleton_authoritative_inputs": skeleton_authoritative_inputs,
        "transcript_flow": describe_authoritative_flow(MediaSourceKind.TRANSCRIPT),
        "local_srt_flow": describe_authoritative_flow(MediaSourceKind.LOCAL_SRT),
        "local_audio_flow": describe_authoritative_flow(MediaSourceKind.LOCAL_AUDIO),
        "local_video_flow": describe_authoritative_flow(MediaSourceKind.LOCAL_VIDEO),
    }


def _build_translation_diagnostic(config_path: Path) -> dict[str, object]:
    selection = TranslationProviderSelectionConfig.from_env(config_path=config_path)
    config = selection.real
    validation_error: str | None = None
    can_run_selected_mode = selection.mode == "mock"
    if selection.mode == "real":
        try:
            config.validate()
            can_run_selected_mode = True
        except TranslationConfigurationError as exc:
            validation_error = str(exc)
            can_run_selected_mode = False
    summary = config.build_diagnostic_summary()
    return {
        "summary": "翻译 provider 就绪状态。",
        "selected_mode": selection.mode,
        "config_source": summary["config_source"],
        "api_key_source_type": summary["api_key_source_type"],
        "provider_name": config.provider_name,
        "enabled": config.enabled,
        "can_run_selected_mode": can_run_selected_mode,
        "validation_error": validation_error,
    }


def _build_tts_diagnostic(config_path: Path) -> dict[str, object]:
    selection = TTSProviderSelectionConfig.from_env(config_path=config_path)
    config = selection.real
    validation_error: str | None = None
    can_run_selected_mode = selection.mode == "mock"
    if selection.mode == "real":
        try:
            config.validate()
            can_run_selected_mode = True
        except TTSConfigurationError as exc:
            validation_error = str(exc)
            can_run_selected_mode = False
    summary = config.build_diagnostic_summary()
    return {
        "summary": "TTS provider 就绪状态。",
        "selected_mode": selection.mode,
        "config_source": summary["config_source"],
        "api_key_source_type": summary["api_key_source_type"],
        "provider_name": config.provider_name,
        "enabled": config.enabled,
        "can_run_selected_mode": can_run_selected_mode,
        "validation_error": validation_error,
    }


def _build_voice_clone_diagnostic(config_path: Path) -> dict[str, object]:
    config = VoiceCloneConfig.from_env(config_path=config_path)
    validation_error: str | None = None
    try:
        config.validate()
        can_run = True
    except VoiceCloneConfigurationError as exc:
        validation_error = str(exc)
        can_run = False
    summary = config.build_diagnostic_summary()
    return {
        "summary": "显式 voice-clone 命令就绪状态。",
        "config_source": summary["config_source"],
        "api_key_source_type": summary["api_key_source_type"],
        "provider_name": config.provider_name,
        "enabled": config.enabled,
        "can_run": can_run,
        "validation_error": validation_error,
    }


def _build_voice_registry_diagnostic(
    project_config: config_loader.ProjectLocalConfig,
    registry_path: Path,
    registry_path_source: str,
) -> dict[str, object]:
    exists = registry_path.exists()
    load_error: str | None = None
    speaker_count = 0
    try:
        speaker_count = len(_build_voice_registry_snapshot(registry_path)["speakers"])
    except StateError as exc:
        load_error = str(exc)
    provider_name, provider_name_source = config_loader.resolve_text_value(
        env_keys=["AUTODUB_TTS_PROVIDER_NAME"],
        config=project_config,
        config_key_paths=(("voice_registry", "provider_name"), ("tts", "provider_name")),
    )
    return {
        "summary": "音色注册表路径与 speaker 绑定可见性。",
        "path": str(registry_path),
        "path_source": registry_path_source,
        "provider_name": provider_name or "minimax_tts",
        "provider_name_source": provider_name_source,
        "exists": exists,
        "speaker_count": speaker_count,
        "load_error": load_error,
    }


def _build_voice_registry_snapshot(registry_path: Path) -> dict[str, object]:
    registry = VoiceRegistry(str(registry_path))
    resolver = VoiceResolver(registry)
    registry_data = registry.load()
    speakers_payload = registry_data.get("speakers", {})
    speakers: list[dict[str, object]] = []
    if isinstance(speakers_payload, dict):
        for speaker_id in sorted(speakers_payload.keys(), key=str):
            speaker_payload = speakers_payload.get(speaker_id)
            if not isinstance(speaker_payload, dict):
                continue
            profile = SpeakerVoiceProfile.from_dict(str(speaker_id), speaker_payload)
            resolution = resolver.resolve(profile.speaker_id)
            speakers.append(
                {
                    "speaker_id": profile.speaker_id,
                    "speaker_name": profile.speaker_name,
                    "default_voice_id": profile.default_voice_id,
                    "default_voice_type": profile.default_voice_type,
                    "resolution_source": resolution.source,
                    "voices": [
                        {
                            "voice_id": voice.voice_id,
                            "voice_type": voice.voice_type,
                            "provider": voice.provider,
                            "label": voice.label,
                            "created_at": voice.created_at,
                            "source_audio_path": voice.source_audio_path,
                            "notes": voice.notes,
                            "verification_status": voice.verification_status,
                            "last_verified_at": voice.last_verified_at,
                            "last_verification_success": voice.last_verification_success,
                            "last_verification_audio_path": voice.last_verification_audio_path,
                            "last_verification_error": voice.last_verification_error,
                        }
                        for voice in profile.voices
                    ],
                }
            )

    project_default_builtin_voice = registry.get_project_default_builtin_voice()
    return {
        "path": str(registry_path),
        "exists": registry_path.exists(),
        "speaker_count": len(speakers),
        "project_default_builtin_voice": (
            project_default_builtin_voice.to_dict()
            if project_default_builtin_voice is not None
            else None
        ),
        "speakers": speakers,
    }


def _build_workbench_snapshot(last_workbench_run: dict[str, object] | None) -> dict[str, object]:
    return {
        "summary": (
            "最小本地工作台：用于查看配置、音色资产、authoritative path，并一键触发 demo。"
        ),
        "supported_actions": [
            {
                "id": "default_demo",
                "label": "默认 Mock Demo",
                "description": "运行当前已有的默认 mock demo 路径。",
            },
            {
                "id": "local_audio_demo",
                "label": "本地音频 Demo",
                "description": "使用本地 WAV/WAVE 路径与 translation/TTS 模式运行 local-audio-demo。",
            },
            {
                "id": "upload_local_audio_demo",
                "label": "上传音频并运行",
                "description": (
                    "上传单个本地 WAV/WAVE 文件到 voice_bank/input_audio/，"
                    "再复用现有 local-audio-demo。"
                ),
            },
        ],
        "last_run": _sanitize_workbench_run(last_workbench_run),
    }


def _build_project_state_summary_from_stage_execution_summary(
    stage_execution_summary: object,
) -> dict[str, object] | None:
    if not isinstance(stage_execution_summary, dict):
        return None

    synthesized_stages: dict[str, dict[str, object]] = {}
    for stage_name, stage_payload in stage_execution_summary.items():
        if not isinstance(stage_payload, dict):
            continue
        payload: dict[str, object] = {}
        execution_mode = stage_payload.get("execution_mode")
        if execution_mode is not None:
            payload["execution_mode"] = execution_mode
        artifact_count = stage_payload.get("artifact_count")
        if artifact_count is not None:
            payload["artifacts"] = {"file_count": artifact_count}
        synthesized_stages[str(stage_name)] = {
            "status": stage_payload.get("status"),
            "payload": payload,
        }

    if not synthesized_stages:
        return None
    return build_project_state_summary({"stages": synthesized_stages})


def _sanitize_workbench_run(last_workbench_run: dict[str, object] | None) -> dict[str, object] | None:
    if not isinstance(last_workbench_run, dict):
        return None

    stage_execution_summary = last_workbench_run.get("stage_execution_summary")
    fallback_project_state_summary = _build_project_state_summary_from_stage_execution_summary(
        stage_execution_summary
    )
    project_state_summary = last_workbench_run.get("project_state_summary")
    sanitized_project_state_summary: dict[str, object] | None = None
    source_project_state_summary = (
        project_state_summary if isinstance(project_state_summary, dict) else fallback_project_state_summary
    )
    if isinstance(source_project_state_summary, dict):
        stages = source_project_state_summary.get("stages")
        sanitized_project_state_summary = {
            "overall_status": source_project_state_summary.get("overall_status"),
            "overall_status_label": source_project_state_summary.get("overall_status_label"),
            "latest_stage_name": source_project_state_summary.get("latest_stage_name"),
            "latest_stage_label": source_project_state_summary.get("latest_stage_label"),
            "latest_stage_status": source_project_state_summary.get("latest_stage_status"),
            "latest_stage_status_label": source_project_state_summary.get("latest_stage_status_label"),
            "stage_count": source_project_state_summary.get("stage_count"),
            "completed_stage_count": source_project_state_summary.get("completed_stage_count"),
            "running_stage_count": source_project_state_summary.get("running_stage_count"),
            "failed_stage_count": source_project_state_summary.get("failed_stage_count"),
            "stages": [
                {
                    "name": stage_payload.get("name"),
                    "label": stage_payload.get("label"),
                    "status": stage_payload.get("status"),
                    "status_label": stage_payload.get("status_label"),
                    "execution_mode": stage_payload.get("execution_mode"),
                    "summary": stage_payload.get("summary"),
                }
                for stage_payload in stages
                if isinstance(stage_payload, dict)
            ]
            if isinstance(stages, list)
            else [],
        }

    sanitized_stage_execution_summary: dict[str, object] | None = None
    if isinstance(stage_execution_summary, dict):
        sanitized_stage_execution_summary = {
            stage_name: {
                "status": stage_payload.get("status"),
                "execution_mode": stage_payload.get("execution_mode"),
                "cn_text_produced": stage_payload.get("cn_text_produced"),
                "authoritative_input_used": stage_payload.get("authoritative_input_used"),
                "authoritative_path_kind": stage_payload.get("authoritative_path_kind"),
            }
            for stage_name, stage_payload in stage_execution_summary.items()
            if isinstance(stage_payload, dict)
        }

    result_summary = last_workbench_run.get("result_summary")
    source_context = last_workbench_run.get("source_context")
    if not isinstance(source_context, dict) and isinstance(result_summary, dict):
        nested_source_context = result_summary.get("source_context")
        if isinstance(nested_source_context, dict):
            source_context = nested_source_context
    sanitized_source_context: dict[str, object] | None = None
    if isinstance(source_context, dict):
        sanitized_source_context = {
            "source_kind": source_context.get("source_kind"),
            "locator": source_context.get("locator"),
            "video_title": source_context.get("video_title"),
        }

    return {
        "run_kind": last_workbench_run.get("run_kind"),
        "status": last_workbench_run.get("status"),
        "command": last_workbench_run.get("command"),
        "input_path": last_workbench_run.get("input_path"),
        "draft_path": last_workbench_run.get("draft_path"),
        "output_root": last_workbench_run.get("output_root"),
        "provider_mode_summary": last_workbench_run.get("provider_mode_summary"),
        "result_summary": result_summary,
        "source_context": sanitized_source_context,
        "project_state_summary": sanitized_project_state_summary,
        "stage_execution_summary": sanitized_stage_execution_summary,
        "error": last_workbench_run.get("error"),
        "stdout_excerpt": last_workbench_run.get("stdout_excerpt"),
        "stderr_excerpt": last_workbench_run.get("stderr_excerpt"),
    }


def _resolve_voice_registry_path(
    project_config: config_loader.ProjectLocalConfig,
    *,
    override_registry_path: Path | None,
) -> tuple[Path, str]:
    if override_registry_path is not None:
        return override_registry_path.expanduser().resolve(strict=False), "override"
    configured_registry_path, configured_registry_path_source = config_loader.resolve_path_value(
        env_keys=["AUTODUB_TTS_VOICE_REGISTRY_PATH"],
        config=project_config,
        config_key_paths=(
            ("voice_registry", "registry_path"),
            ("tts", "voice_registry_path"),
            ("paths", "voice_registry_path"),
        ),
    )
    if configured_registry_path is not None:
        return Path(configured_registry_path).expanduser().resolve(strict=False), (
            configured_registry_path_source or "default"
        )
    default_registry_path = project_config.path.parent / "voice_registry.json"
    return default_registry_path.resolve(strict=False), "default"


def _open_control_panel_browser(control_panel_url: str) -> None:
    try:
        if sys.platform.startswith("win") and hasattr(os, "startfile"):
            os.startfile(control_panel_url)
            return
    except Exception:
        pass
    try:
        webbrowser.open(control_panel_url, new=2)
    except Exception:
        # Browser launch is best-effort; the local server should still start even if the desktop shell refuses it.
        return


def _execute_workbench_command(
    run_kind_aliases: list[str],
    *,
    command_args: list[str],
    input_path: str | None = None,
) -> dict[str, object]:
    command = [str(sys.executable), str(MAIN_PY_PATH), *command_args]
    try:
        completed = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=WORKBENCH_RUN_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _build_failed_workbench_result(
            run_kind=run_kind_aliases[0],
            command=command,
            input_path=input_path,
            error_message=f"工作台运行超时（{WORKBENCH_RUN_TIMEOUT_SECONDS} 秒）。",
        )
    except OSError as exc:
        return _build_failed_workbench_result(
            run_kind=run_kind_aliases[0],
            command=command,
            input_path=input_path,
            error_message=f"工作台运行启动失败：{exc}",
        )
    parsed_summary = _parse_workbench_cli_output(completed.stdout)
    if completed.returncode == 0:
        return {
            "run_kind": run_kind_aliases[0],
            "status": "success",
            "command": command,
            "input_path": input_path,
            "draft_path": parsed_summary.get("draft_path"),
            "output_root": _read_nested_value(parsed_summary.get("result_summary"), "output_root"),
            "provider_mode_summary": parsed_summary.get("provider_mode_summary"),
            "result_summary": parsed_summary.get("result_summary"),
            "project_state_summary": parsed_summary.get("project_state_summary"),
            "stage_execution_summary": parsed_summary.get("stage_execution_summary"),
            "stdout_excerpt": _build_output_excerpt(completed.stdout),
            "stderr_excerpt": _build_output_excerpt(completed.stderr),
            "error": None,
        }
    return _build_failed_workbench_result(
        run_kind=run_kind_aliases[0],
        command=command,
        input_path=input_path,
        error_message=_extract_workbench_failure_message(completed),
        stdout=completed.stdout,
        stderr=completed.stderr,
        parsed_summary=parsed_summary,
    )


def _build_failed_workbench_result(
    *,
    run_kind: str,
    command: list[str],
    error_message: str,
    input_path: str | None = None,
    stdout: str | None = None,
    stderr: str | None = None,
    parsed_summary: dict[str, object] | None = None,
) -> dict[str, object]:
    summary = parsed_summary or {}
    return {
        "run_kind": run_kind,
        "status": "failed",
        "command": command,
        "input_path": input_path,
        "draft_path": summary.get("draft_path"),
        "output_root": _read_nested_value(summary.get("result_summary"), "output_root"),
        "provider_mode_summary": summary.get("provider_mode_summary"),
        "result_summary": summary.get("result_summary"),
        "project_state_summary": summary.get("project_state_summary"),
        "stage_execution_summary": summary.get("stage_execution_summary"),
        "stdout_excerpt": _build_output_excerpt(stdout or ""),
        "stderr_excerpt": _build_output_excerpt(stderr or ""),
        "error": error_message,
    }


def _normalize_workbench_provider_mode(raw_mode: str) -> str:
    normalized_mode = raw_mode.strip().lower()
    if normalized_mode not in WORKBENCH_PROVIDER_MODES:
        raise ValueError(f"工作台不支持的 provider mode：{raw_mode}")
    return normalized_mode


def _normalize_local_audio_intake_path(local_audio_path: str) -> str:
    media_source = AuthoritativeIntakeBuilder().build(
        AuthoritativeIntakeRequest(
            kind=MediaSourceKind.LOCAL_AUDIO,
            locator=local_audio_path,
        )
    )
    assert media_source.locator is not None
    return media_source.locator


def _save_uploaded_workbench_audio(*, filename: str, file_bytes: bytes) -> Path:
    normalized_extension = Path(filename).suffix.lower()
    normalized_stem = _slugify_workbench_upload_stem(Path(filename).stem)
    target_directory = WORKBENCH_UPLOADED_AUDIO_ROOT.expanduser().resolve(strict=False)
    target_directory.mkdir(parents=True, exist_ok=True)
    target_path = target_directory / f"{normalized_stem}_{uuid4().hex[:8]}{normalized_extension}"
    target_path.write_bytes(file_bytes)
    return target_path.resolve(strict=False)


def _slugify_workbench_upload_stem(raw_stem: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_stem.strip())
    sanitized = sanitized.strip("._-")
    return sanitized or "uploaded_audio"


def _parse_workbench_cli_output(stdout: str) -> dict[str, object]:
    parsed: dict[str, object] = {}
    draft_path_match = re.search(r"^Draft scaffold written to:\s*(.+)$", stdout, flags=re.MULTILINE)
    if draft_path_match:
        parsed["draft_path"] = draft_path_match.group(1).strip()

    section_headers = {
        "Run context:": "run_context",
        "Provider mode summary:": "provider_mode_summary",
        "Source context:": "source_context",
        "Run result summary:": "result_summary",
        "Project state summary:": "project_state_summary",
        "Stage execution summary:": "stage_execution_summary",
    }
    lines = stdout.splitlines()
    for header, output_key in section_headers.items():
        block_lines = _extract_printed_section_block(lines, header, section_headers.keys())
        if not block_lines:
            continue
        try:
            parsed[output_key] = ast.literal_eval("\n".join(block_lines))
        except (SyntaxError, ValueError):
            continue
    return parsed


def _extract_printed_section_block(
    lines: list[str],
    header: str,
    all_headers: object,
) -> list[str]:
    try:
        start_index = lines.index(header) + 1
    except ValueError:
        return []

    header_set = {str(item) for item in all_headers}
    collected: list[str] = []
    for line in lines[start_index:]:
        if line in header_set:
            break
        collected.append(line)
    return [line for line in collected if line.strip()]


def _extract_workbench_failure_message(completed: subprocess.CompletedProcess[str]) -> str:
    stderr_text = (completed.stderr or "").strip()
    if stderr_text:
        return stderr_text.splitlines()[-1]
    stdout_text = (completed.stdout or "").strip()
    if stdout_text:
        return stdout_text.splitlines()[-1]
    return f"工作台命令失败，退出码 {completed.returncode}。"


def _build_output_excerpt(raw_text: str, *, max_lines: int = 20) -> str | None:
    normalized_lines = [line.rstrip() for line in raw_text.splitlines() if line.strip()]
    if not normalized_lines:
        return None
    return "\n".join(normalized_lines[-max_lines:])


def _read_nested_value(payload: object, key: str) -> object | None:
    if not isinstance(payload, dict):
        return None
    return payload.get(key)
