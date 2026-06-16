from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "frontend-next" / "src" / "components" / "marketing" / "anonymous-trial-panel.tsx"


def _source() -> str:
    return PANEL.read_text(encoding="utf-8")


def test_anonymous_trial_dialog_does_not_implicit_close_or_reset() -> None:
    src = _source()

    assert 'showCloseButton={false}' in src
    assert 'function handleOpenChange(next: boolean)' in src
    assert 'Ignore backdrop clicks and Escape' in src
    assert 'if (!next) resetPanel()' not in src
    assert 'eventDetails?.reason' not in src


def test_anonymous_trial_close_button_confirms_busy_work() -> None:
    src = _source()

    assert 'useConfirmDialog' in src
    assert "state.step === 'uploading' || state.step === 'processing'" in src
    assert 'async function requestClose()' in src
    assert '确认关闭上传窗口？' in src
    assert '当前视频仍在上传或处理中' in src
    assert 'onClick={() => void requestClose()}' in src


def test_anonymous_trial_minimize_keeps_progress_alive() -> None:
    src = _source()

    assert 'const [minimized, setMinimized] = useState(false)' in src
    assert 'function minimizePanel()' in src
    assert 'setOpen(false)' in src
    assert 'setMinimized(true)' in src
    assert 'function restorePanel()' in src
    assert 'renderMinimizedWidget()' in src
    assert '视频上传中' in src
    assert '上传已完成' in src
    assert '预览已完成' in src
