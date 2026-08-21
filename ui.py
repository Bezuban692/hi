from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

if platform.system() == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}

# ─────────────────────────────────────────────────────────────────────────────
# Hanaai AI WebEngine Anti-Flicker Environment Configuration
# MUST be set BEFORE importing PyQt6 modules or creating QApplication!
# ─────────────────────────────────────────────────────────────────────────────
os.environ["QTWEBENGINE_DISABLE_NO_SANDBOX"] = "1"
os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "1"
os.environ["QT_SCALE_FACTOR_ROUNDING_POLICY"] = "PassThrough"

_ui_chrome_flags = [
    "--enable-gpu-rasterization",
    "--enable-accelerated-2d-canvas",
    "--ignore-gpu-blocklist",
    "--disable-gpu-driver-bug-workarounds",
]
_render_mode = os.environ.get("HANAAI_RENDER_MODE", "auto").lower()
if _render_mode == "software":
    _ui_chrome_flags.append("--disable-gpu")
    os.environ["QT_OPENGL"] = "software"
elif _render_mode == "angle":
    _ui_chrome_flags.extend(["--use-gl=angle", "--use-angle=d3d11"])
elif _render_mode == "desktop_gl":
    _ui_chrome_flags.append("--use-gl=desktop")
    os.environ["QT_OPENGL"] = "desktop"

if "QTWEBENGINE_CHROMIUM_FLAGS" not in os.environ:
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(_ui_chrome_flags)

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWebEngineCore import QWebEngineSettings
    _WEBENGINE_OK = True
except ImportError:
    _WEBENGINE_OK = False

from PyQt6.QtCore import (
    Qt, QUrl, pyqtSignal, QCoreApplication, QTimer,
)
from PyQt6.QtGui import (
    QColor, QPalette, QSurfaceFormat,
)
from PyQt6.QtWidgets import (
    QApplication, QLabel, QMainWindow, QVBoxLayout, QWidget,
    QDialog, QLineEdit, QPushButton, QHBoxLayout, QMessageBox,
)

_qt_env_initialized = False

def _setup_qt_environment():
    global _qt_env_initialized
    if _qt_env_initialized:
        return
    _qt_env_initialized = True

    os.environ["QTWEBENGINE_DISABLE_NO_SANDBOX"] = "1"

    chrome_switches = [
        "--enable-gpu-rasterization",
        "--enable-accelerated-2d-canvas",
        "--enable-zero-copy",
        "--ignore-gpu-blocklist",
        "--disable-gpu-driver-bug-workarounds",
        "--disable-direct-composition",
    ]
    for switch in chrome_switches:
        if switch not in sys.argv:
            sys.argv.append(switch)

    try:
        if hasattr(Qt.ApplicationAttribute, "AA_ShareOpenGLContexts"):
            QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    except Exception as e:
        print(f"[Hanaai GUI] Qt attribute warning: {e}", file=sys.stderr)

    try:
        fmt = QSurfaceFormat()
        fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.DoubleBuffer)
        fmt.setSwapInterval(1)
        fmt.setDepthBufferSize(24)
        fmt.setStencilBufferSize(8)
        QSurfaceFormat.setDefaultFormat(fmt)
    except Exception as e:
        print(f"[Hanaai GUI] QSurfaceFormat warning: {e}", file=sys.stderr)

_setup_qt_environment()


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR   = _base_dir()
CONFIG_DIR = BASE_DIR / "config"
API_FILE   = CONFIG_DIR / "api_keys.json"


def _read_full_config() -> dict:
    """Read api_keys.json config dict. Returns {} on any error."""
    try:
        return json.loads(API_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


_PLACEHOLDER_KEY = "YOUR_GEMINI_API_KEY_HERE"


def _needs_api_key() -> bool:
    """True if the config is missing, broken, or still has the placeholder key."""
    cfg = _read_full_config()
    key = str(cfg.get("gemini_api_key", "")).strip()
    return not key or key == _PLACEHOLDER_KEY


def _write_api_key(key: str) -> None:
    """Safely save the API key — always writes valid JSON, never hand-edited."""
    cfg = _read_full_config()
    if not cfg:
        cfg = {
            "os_system": "windows",
            "morning_brief_enabled": True,
            "assistant_name": "Hanaai AI",
            "user_name": "",
            "ui_color": "#00ff66",
        }
    cfg["gemini_api_key"] = key.strip()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    API_FILE.write_text(json.dumps(cfg, indent=4), encoding="utf-8")


class ApiKeyDialog(QDialog):
    """Popup asking for the Gemini API key — replaces manual Notepad editing."""

    def __init__(self, parent=None, error_message: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Hanaai AI — API Key Required")
        self.setFixedSize(460, 230)
        self.setModal(True)
        self.setStyleSheet("""
            QDialog { background-color: #0a0e18; }
            QLabel { color: #d8e0ec; font-size: 13px; }
            QLineEdit {
                background-color: #131b2e; color: #00ff66;
                border: 1px solid #2a3550; border-radius: 4px;
                padding: 8px; font-size: 13px;
            }
            QPushButton {
                background-color: #b00020; color: white;
                border: none; border-radius: 4px;
                padding: 8px 16px; font-size: 13px; font-weight: bold;
            }
            QPushButton:hover { background-color: #d4002a; }
            QPushButton#ghost { background-color: #232d45; }
            QPushButton#ghost:hover { background-color: #2f3b58; }
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        title = QLabel("Hanaai AI needs a Gemini API key to start")
        title.setStyleSheet("font-size: 15px; font-weight: bold; color: #ff4d4d;")
        layout.addWidget(title)

        info = QLabel("Get a free key at aistudio.google.com/apikey, then paste it below.")
        info.setWordWrap(True)
        layout.addWidget(info)

        if error_message:
            err = QLabel(error_message)
            err.setStyleSheet("color: #ff8080; font-size: 12px;")
            err.setWordWrap(True)
            layout.addWidget(err)

        self.input = QLineEdit()
        self.input.setPlaceholderText("Paste your Gemini API key here...")
        self.input.setEchoMode(QLineEdit.EchoMode.Password)
        self.input.returnPressed.connect(self._on_save)
        layout.addWidget(self.input)

        show_row = QHBoxLayout()
        show_row.addStretch()
        self.show_btn = QPushButton("Show")
        self.show_btn.setObjectName("ghost")
        self.show_btn.setCheckable(True)
        self.show_btn.setFixedWidth(70)
        self.show_btn.toggled.connect(self._toggle_visibility)
        show_row.addWidget(self.show_btn)
        layout.addLayout(show_row)

        btn_row = QHBoxLayout()
        quit_btn = QPushButton("Quit")
        quit_btn.setObjectName("ghost")
        quit_btn.clicked.connect(self._on_quit)
        save_btn = QPushButton("Save && Launch")
        save_btn.clicked.connect(self._on_save)
        btn_row.addWidget(quit_btn)
        btn_row.addWidget(save_btn)
        layout.addLayout(btn_row)

        self.result_key: str | None = None

    def _toggle_visibility(self, checked: bool):
        self.input.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )

    def _on_save(self):
        key = self.input.text().strip()
        if not key or key == _PLACEHOLDER_KEY:
            QMessageBox.warning(self, "Hanaai AI", "Please paste a real API key.")
            return
        self.result_key = key
        self.accept()

    def _on_quit(self):
        self.result_key = None
        self.reject()


class _RootShim:
    def __init__(self, app: QApplication):
        self._app = app
    def mainloop(self):
        self._app.exec()
    def protocol(self, *_):
        pass


class UltronWebWindow(QMainWindow):
    _state_sig = pyqtSignal(str)
    _log_sig = pyqtSignal(str)
    _content_sig = pyqtSignal(str, str)
    _stream_sig = pyqtSignal(str, str)     # (speaker, chunk) — live typing
    _stream_end_sig = pyqtSignal(str)      # (speaker) — finalize bubble
    _reconfig_sig = pyqtSignal()
    _camera_sig = pyqtSignal(bytes)
    # CRITICAL: runJavaScript() MUST run on the Qt main thread. Calling it
    # from daemon/executor threads = SIGTRAP core dump. Every JS touch from
    # other threads goes through these queued signals.
    _lock_sig = pyqtSignal(str)            # (mode) — 'setup' | 'login'
    _stage_sig = pyqtSignal(str)           # (stage) — 'pw' | 'face' | 'verify'
    _code_start_sig = pyqtSignal(str, str) # (filename, lang) — live editor
    _code_chunk_sig = pyqtSignal(str)      # (chunk) — streaming file text
    _code_end_sig = pyqtSignal(str)        # (path) — finalize code bubble
    _auth_sig = pyqtSignal(bool, str)      # (ok, msg) — lock screen result
    _camcmd_sig = pyqtSignal(bool)         # (start?) — camera stream on/off
    _js_sig = pyqtSignal(str)              # generic one-shot JS (toast etc.)

    def __init__(self, face_path: str = "face.png"):
        super().__init__()
        self.setWindowTitle("Hanaai AI — Next-Gen AI Operating System")
        self.resize(1280, 800)
        self.setMinimumSize(900, 600)

        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor("#020510"))
        self.setPalette(pal)
        self.setAutoFillBackground(True)

        self._muted = False
        self._ready = True
        self._assistant_name = _read_full_config().get("assistant_name", "Hanaai AI") or "Hanaai AI"
        self.on_text_command = None
        self.on_remote_clicked = None
        self.on_interrupt = None
        self.on_page_loaded = None   # called on every web page (re)load

        import time
        self._last_reload_time = 0.0

        if _WEBENGINE_OK:
            self._web = QWebEngineView(self)
            if hasattr(self._web, "page") and self._web.page():
                self._web.page().setBackgroundColor(QColor("#020510"))
                if hasattr(self._web.page(), "renderProcessTerminated"):
                    self._web.page().renderProcessTerminated.connect(self._on_render_process_terminated)
            self._web.titleChanged.connect(self._on_title_changed)
            # Re-sync security lock state every time the page (re)loads —
            # the page defaults to SETUP, Python corrects it to LOGIN when
            # already configured (covers WebEngine auto-reloads).
            self._web.loadFinished.connect(self._on_page_load_finished)
            settings = self._web.settings()
            settings.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
            settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
            settings.setAttribute(QWebEngineSettings.WebAttribute.Accelerated2dCanvasEnabled, True)
            settings.setAttribute(QWebEngineSettings.WebAttribute.WebGLEnabled, True)
            settings.setAttribute(QWebEngineSettings.WebAttribute.ScrollAnimatorEnabled, True)
            
            # --- FIX: Allowing CORS and Local Files for HTML WebGL ---
            settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
            settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)

            self.setCentralWidget(self._web)
            
            # --- Pointing to current directory app.html ---
            target_path = BASE_DIR / "dashboard" / "static" / "app.html"

            self._web.setUrl(QUrl.fromLocalFile(str(target_path)))
        else:
            container = QWidget()
            lbl = QLabel("Hanaai AI — Loading GUI...", container)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout = QVBoxLayout(container)
            layout.addWidget(lbl)
            self.setCentralWidget(container)

        self._state_sig.connect(self._on_state)
        self._log_sig.connect(self._on_log)
        self._content_sig.connect(self._on_content)
        self._stream_sig.connect(self._on_stream)
        self._stream_end_sig.connect(self._on_stream_end)
        self._reconfig_sig.connect(self._on_reconfig)
        self._lock_sig.connect(self._on_lock_mode)
        self._stage_sig.connect(self._on_stage)
        self._code_start_sig.connect(self._on_code_start)
        self._code_chunk_sig.connect(self._on_code_chunk)
        self._code_end_sig.connect(self._on_code_end)
        self._auth_sig.connect(self._on_auth_result)
        self._camcmd_sig.connect(self._on_cam_cmd)
        self._js_sig.connect(self._on_generic_js)

        # Grant camera/mic to the UI page — without this getUserMedia
        # silently fails and the camera panel shows nothing.
        if _WEBENGINE_OK and hasattr(self, "_web") and self._web.page():
            try:
                from PyQt6.QtWebEngineCore import QWebEnginePage
                self._web.page().featurePermissionRequested.connect(
                    self._on_feature_permission
                )
            except Exception as e:
                print(f"[Hanaai GUI] permission hook failed: {e}", file=sys.stderr)

        if _needs_api_key():
            self._ready = False
            QTimer.singleShot(300, lambda: self._on_reconfig(""))

    def _on_feature_permission(self, origin, feature):
        """Auto-grant camera/mic to our own UI page."""
        try:
            from PyQt6.QtWebEngineCore import QWebEnginePage
            grant = QWebEnginePage.PermissionPolicy.PermissionGrantedByUser
            self._web.page().setFeaturePermission(origin, feature, grant)
            print(f"[Hanaai GUI] ✅ Granted feature {feature} to UI", file=sys.stderr)
        except Exception as e:
            print(f"[Hanaai GUI] grant failed: {e}", file=sys.stderr)

    def _on_title_changed(self, title: str):
        if title.startswith("CMD:"):
            cmd = title[4:].strip()
            if cmd and callable(self.on_text_command):
                self.on_text_command(cmd)

    def _on_page_load_finished(self, ok: bool):
        """Fires on EVERY page load/reload — let main re-sync lock state."""
        try:
            if callable(self.on_page_loaded):
                self.on_page_loaded(ok)
        except Exception as e:
            print(f"[Hanaai GUI] page-loaded callback error: {e}", file=sys.stderr)

    def _on_render_process_terminated(self, termination_status, exit_code):
        import time
        now = time.time()
        print(f"[Hanaai GUI WARNING] WebEngine Render Process Terminated (status: {termination_status}, exit code: {exit_code}).", file=sys.stderr)
        if hasattr(self, "_web") and self._web and (now - getattr(self, "_last_reload_time", 0) > 5.0):
            self._last_reload_time = now
            self._web.reload()

    def _eval_js(self, js_code: str):
        if _WEBENGINE_OK and hasattr(self, "_web") and self._web.page():
            try:
                self._web.page().runJavaScript(js_code)
            except Exception as e:
                print(f"[Hanaai GUI] _eval_js warning: {e}", file=sys.stderr)

    def _on_state(self, state: str):
        js = f"if (typeof updateAIState === 'function') updateAIState('{state}');"
        self._eval_js(js)

    def _on_log(self, text: str):
        escaped = json.dumps(text)
        js = f"if (typeof addMemoryLog === 'function') addMemoryLog({escaped});"
        self._eval_js(js)

    def _on_content(self, title: str, text: str):
        escaped_title = json.dumps(title)
        escaped_text = json.dumps(text)
        js = f"if (typeof addChatMessage === 'function') addChatMessage({escaped_title}, {escaped_text});"
        self._eval_js(js)

    def _on_stream(self, speaker: str, chunk: str):
        """Live typing: append a transcription chunk to the streaming bubble."""
        escaped_speaker = json.dumps(speaker)
        escaped_chunk = json.dumps(chunk)
        js = (
            f"if (typeof streamChatMessage === 'function') "
            f"streamChatMessage({escaped_speaker}, {escaped_chunk});"
        )
        self._eval_js(js)

    def _on_stream_end(self, speaker: str):
        """Live typing: finalize the streaming bubble with a timestamp."""
        escaped_speaker = json.dumps(speaker)
        js = f"if (typeof streamChatEnd === 'function') streamChatEnd({escaped_speaker});"
        self._eval_js(js)

    # ── Thread-safe JS bridges (queued signals → main thread) ────────────
    def show_lock_mode(self, mode: str):
        """SAFE from any thread: show lock screen in setup/login mode."""
        self._lock_sig.emit(mode)

    def show_stage(self, stage: str):
        """SAFE from any thread: advance setup wizard (pw|face|verify)."""
        self._stage_sig.emit(stage)

    def stream_code_start(self, filename: str, lang: str):
        """SAFE from any thread: begin live code bubble in chat."""
        self._code_start_sig.emit(filename[:60], (lang or "FILE")[:12].upper())

    def stream_code_chunk(self, chunk: str):
        """SAFE from any thread: append streaming file text."""
        self._code_chunk_sig.emit(chunk)

    def stream_code_end(self, path: str):
        """SAFE from any thread: finalize code bubble with save path."""
        self._code_end_sig.emit(path[:160])

    def auth_result(self, ok: bool, msg: str = ""):
        """SAFE from any thread: login result → lock screen."""
        self._auth_sig.emit(ok, msg)

    def set_camera_stream(self, start: bool):
        """SAFE from any thread: start/stop the HTML camera preview."""
        self._camcmd_sig.emit(start)

    def eval_js_threadsafe(self, js: str):
        """SAFE from any thread: run a one-shot JS snippet."""
        self._js_sig.emit(js)

    def _on_lock_mode(self, mode: str):
        escaped = json.dumps(mode)
        js = f"if (typeof lockMode === 'function') lockMode({escaped});"
        self._eval_js(js)

    def _on_stage(self, stage: str):
        escaped = json.dumps(stage)
        js = f"if (typeof setupStage === 'function') setupStage({escaped});"
        self._eval_js(js)

    def _on_code_start(self, filename: str, lang: str):
        f = json.dumps(filename); l = json.dumps(lang)
        js = (f"if (typeof streamCodeStart === 'function') "
              f"streamCodeStart({f}, {l});")
        self._eval_js(js)

    def _on_code_chunk(self, chunk: str):
        c = json.dumps(chunk)
        js = f"if (typeof streamCodeChunk === 'function') streamCodeChunk({c});"
        self._eval_js(js)

    def _on_code_end(self, path: str):
        p = json.dumps(path)
        js = f"if (typeof streamCodeEnd === 'function') streamCodeEnd({p});"
        self._eval_js(js)

    def _on_auth_result(self, ok: bool, msg: str):
        ok_js = "true" if ok else "false"
        escaped = json.dumps(msg)
        js = (
            f"if (typeof authResult === 'function') "
            f"authResult({ok_js}, {escaped});"
        )
        self._eval_js(js)

    def _on_cam_cmd(self, start: bool):
        fn = "startCameraStream" if start else "stopCameraStream"
        js = f"if (typeof {fn} === 'function') {fn}();"
        self._eval_js(js)

    def _on_generic_js(self, js: str):
        self._eval_js(js)

    def _on_reconfig(self, error_message: str = ""):
        dlg = ApiKeyDialog(self, error_message)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_key:
            _write_api_key(dlg.result_key)
            self._assistant_name = _read_full_config().get("assistant_name", "Hanaai AI") or "Hanaai AI"
            self._ready = True
        else:
            QApplication.quit()

    def _toggle_mute(self):
        self._muted = not self._muted
        state = "MUTED" if self._muted else "LISTENING"
        self._on_state(state)

    def notify_phone_connected(self):
        self.eval_js_threadsafe(
            "if (typeof showToast === 'function') showToast('PHONE CONNECTED', 'Remote device paired');"
        )

    def start_camera_stream(self):
        # Thread-safe: HTML camera preview via queued signal
        self.set_camera_stream(True)

    def stop_camera_stream(self):
        self.set_camera_stream(False)


class UltronUI:
    def __init__(self, face_path: str = "face.png", size=None):
        _setup_qt_environment()
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setStyle("Fusion")
        self._win = UltronWebWindow(face_path)
        self._win.show()
        self.root = _RootShim(self._app)

    @property
    def muted(self) -> bool:
        return self._win._muted

    @muted.setter
    def muted(self, v: bool):
        if v != self._win._muted:
            self._win._toggle_mute()

    @property
    def current_file(self) -> str | None:
        return None

    @property
    def on_text_command(self):
        return self._win.on_text_command

    @on_text_command.setter
    def on_text_command(self, cb):
        self._win.on_text_command = cb

    @property
    def on_remote_clicked(self):
        return self._win.on_remote_clicked

    @on_remote_clicked.setter
    def on_remote_clicked(self, cb):
        self._win.on_remote_clicked = cb

    @property
    def on_interrupt(self):
        return self._win.on_interrupt

    @on_interrupt.setter
    def on_interrupt(self, cb):
        self._win.on_interrupt = cb

    def notify_phone_connected(self) -> None:
        self._win.notify_phone_connected()

    def set_state(self, state: str):
        self._win._state_sig.emit(state)

    def write_log(self, text: str):
        self._win._log_sig.emit(text)

    def wait_for_api_key(self):
        while not self._win._ready:
            time.sleep(0.1)

    def show_content(self, title: str, text: str):
        """Thread-safe: display content in the UI."""
        self._win._content_sig.emit(title[:48], text[:4000])

    def auth_result(self, ok: bool, msg: str = ""):
        """Thread-safe: secure-boot login result → lock screen."""
        self._win.auth_result(ok, msg)

    def _eval_lock_mode(self, mode: str):
        """Thread-safe: show lock screen (setup/login mode)."""
        self._win.show_lock_mode(mode)

    def set_page_loaded_callback(self, fn):
        """Register a callback fired on every web page (re)load."""
        self._win.on_page_loaded = fn

    def setup_stage(self, stage: str):
        """Thread-safe: advance the setup wizard (pw|face|verify)."""
        self._win.show_stage(stage)

    def stream_code_start(self, filename: str, lang: str):
        """Thread-safe: live code editor bubble starts."""
        self._win.stream_code_start(filename, lang)

    def stream_code_chunk(self, chunk: str):
        """Thread-safe: live code editor text chunk."""
        self._win.stream_code_chunk(chunk)

    def stream_code_end(self, path: str):
        """Thread-safe: live code editor done."""
        self._win.stream_code_end(path)

    def stream_content(self, speaker: str, chunk: str):
        """Thread-safe: live-type a speech chunk into the chat bubble."""
        self._win._stream_sig.emit(speaker[:48], chunk[:2000])

    def stream_end(self, speaker: str):
        """Thread-safe: finalize the live streaming bubble."""
        self._win._stream_end_sig.emit(speaker[:48])

    def prompt_reconfig(self):
        """Thread-safe: show API key setup overlay if needed."""
        self._win._ready = False
        self._win._reconfig_sig.emit()

    def show_camera_frame(self, img_bytes: bytes):
        """Camera frames are handled by the HTML getUserMedia panel — no-op here."""
        pass

    def start_camera_stream(self) -> None:
        self._win.start_camera_stream()

    def stop_camera_stream(self) -> None:
        self._win.stop_camera_stream()

    @property
    def assistant_name(self) -> str:
        return self._win._assistant_name

    def start_speaking(self):
        self.set_state("SPEAKING")

    def stop_speaking(self):
        if not self.muted:
            self.set_state("LISTENING")


# Backward compatibility alias
JarvisUI = UltronUI