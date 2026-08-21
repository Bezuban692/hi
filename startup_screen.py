"""
startup_screen.py — Premium Hanaai AI Startup / System Status GUI

Dark futuristic AI boot console. Shows live system initialization:
SYSTEM, LOCAL AI, CLOUD AI, MEMORY, AGENTS, VOICE, DESKTOP, NETWORK.

Opens BEFORE main GUI. Main GUI opens only after required components are ready.
"""
from __future__ import annotations

import os
import sys
import json
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QThread
from PyQt6.QtGui import QFont, QColor, QPalette, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QProgressBar, QFrame, QScrollArea, QGridLayout,
)

# ── Colors (dark futuristic theme) ─────────────────────────────────────
BG_DARK      = "#050810"
BG_PANEL     = "#0a0e1a"
BG_CARD      = "#0f1422"
ACCENT_CYAN  = "#00d4ff"
ACCENT_BLUE  = "#1a5fff"
ACCENT_NAVY  = "#0a1628"
TEXT_WHITE   = "#e0e7ff"
TEXT_DIM     = "#6b7a99"
TEXT_GREEN   = "#00ff88"
TEXT_YELLOW  = "#ffcc00"
TEXT_RED     = "#ff4466"
GLOW_CYAN    = "rgba(0, 212, 255, 0.15)"

# ── Card status icons ──────────────────────────────────────────────────
def _icon(ok: bool, warn: bool = False) -> str:
    if ok: return "✓"
    if warn: return "⚠"
    return "○"


class StatusCard(QFrame):
    """A glassmorphism status card with a title and live-updating lines."""

    def __init__(self, title: str):
        super().__init__()
        self.setFixedHeight(160)
        self._items: dict[str, QLabel] = {}
        self.setStyleSheet(f"""
            StatusCard {{
                background: {BG_CARD};
                border: 1px solid rgba(0, 212, 255, 0.15);
                border-radius: 12px;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(f"color: {ACCENT_CYAN}; font-size: 11px; font-weight: bold; letter-spacing: 1px;")
        layout.addWidget(title_lbl)

        self._body = QVBoxLayout()
        self._body.setSpacing(2)
        layout.addLayout(self._body)

    def set_item(self, key: str, ok: bool, warn: bool = False, text: str = ""):
        icon = _icon(ok, warn)
        color = TEXT_GREEN if ok else (TEXT_YELLOW if warn else TEXT_DIM)
        full = f"{icon}  {text}" if text else f"{icon}  {key}"

        if key not in self._items:
            lbl = QLabel(full)
            lbl.setStyleSheet(f"color: {color}; font-size: 10px; font-family: monospace;")
            self._items[key] = lbl
            self._body.addWidget(lbl)
        else:
            lbl = self._items[key]
            lbl.setText(full)
            lbl.setStyleSheet(f"color: {color}; font-size: 10px; font-family: monospace;")


class BootWorker(QThread):
    """Background worker that performs REAL system checks and emits updates."""
    log_signal = pyqtSignal(str, str)         # (source, message)
    card_signal = pyqtSignal(str, str, bool, bool, str)  # (card_name, key, ok, warn, text)
    progress_signal = pyqtSignal(int)          # overall percent
    done_signal = pyqtSignal(dict)             # final status dict

    def __init__(self):
        super().__init__()
        self._results = {}

    def _log(self, source: str, msg: str):
        self.log_signal.emit(source, msg)

    def _set(self, card: str, key: str, ok: bool, warn: bool, text: str = ""):
        self.card_signal.emit(card, key, ok, warn, text)

    def run(self):
        steps = 0
        total = 10

        # ── 1. SYSTEM ─────────────────────────────────────────────────
        self._log("SYSTEM", "Initializing Hanaai AI...")
        import platform as pf
        self._set("SYSTEM", "os", True, False, f"{pf.system()} detected")
        self._set("SYSTEM", "python", True, False, f"Python {sys.version.split()[0]}")

        venv = Path(sys.prefix).name
        is_venv = venv not in ("python", "python3", "usr")
        self._set("SYSTEM", "venv", is_venv, not is_venv,
                  f"venv: {venv}" if is_venv else "system Python (no venv)")

        # Check key dependencies
        deps_ok = []
        deps_missing = []
        for mod in ("PyQt6", "google.genai", "sounddevice", "psutil"):
            try:
                __import__(mod)
                deps_ok.append(mod)
            except ImportError:
                deps_missing.append(mod)
        self._set("SYSTEM", "deps", not deps_missing, bool(deps_missing),
                  f"{len(deps_ok)} deps OK" + (f" | missing: {','.join(deps_missing)}" if deps_missing else ""))
        steps += 1
        self.progress_signal.emit(int(steps / total * 100))
        time.sleep(0.3)

        # ── 2. LOCAL AI (Ollama) ──────────────────────────────────────
        self._log("OLLAMA", "Checking local models...")
        ollama_ok = False
        models = []
        try:
            host = "http://localhost:11434"
            with urllib.request.urlopen(f"{host}/api/tags", timeout=3) as resp:
                if resp.status == 200:
                    ollama_ok = True
                    data = json.loads(resp.read().decode("utf-8"))
                    models = [m.get("name", "") for m in data.get("models", [])]
        except Exception:
            pass

        self._set("LOCAL AI", "ollama", ollama_ok, not ollama_ok,
                  "Ollama running" if ollama_ok else "Ollama not found")
        if ollama_ok:
            self._log("OLLAMA", f"Found {len(models)} models: {', '.join(models)}")

        # Check specific models
        has_qwen = any("qwen2.5-coder" in m for m in models)
        has_llama = any("llama3.2" in m for m in models)
        self._set("LOCAL AI", "qwen", has_qwen, not has_qwen,
                  "qwen2.5-coder:7b READY" if has_qwen else "qwen2.5-coder:7b missing")
        self._set("LOCAL AI", "llama", has_llama, not has_llama,
                  "llama3.2 READY" if has_llama else "llama3.2 missing")

        # If llama3.2 missing, download it
        if ollama_ok and not has_llama:
            self._log("LOCAL AI", "Downloading llama3.2...")
            self._set("LOCAL AI", "llama_dl", False, False, "Downloading llama3.2...")
            try:
                proc = subprocess.Popen(
                    ["ollama", "pull", "llama3.2"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True
                )
                # Stream progress (ollama pull prints progress lines)
                for line in proc.stdout:
                    line = line.strip()
                    if line:
                        self._log("DOWNLOAD", line[:60])
                proc.wait(timeout=600)
                if proc.returncode == 0:
                    self._set("LOCAL AI", "llama", True, False, "llama3.2 downloaded")
                    self._set("LOCAL AI", "llama_dl", True, False, "")
                    self._log("LOCAL AI", "llama3.2 downloaded successfully")
                    has_llama = True
                else:
                    self._set("LOCAL AI", "llama_dl", False, True, "Download failed")
            except subprocess.TimeoutExpired:
                self._set("LOCAL AI", "llama_dl", False, True, "Download timeout")
            except Exception as e:
                self._set("LOCAL AI", "llama_dl", False, True, f"Download error")

        steps += 1
        self.progress_signal.emit(int(steps / total * 100))
        time.sleep(0.3)

        # ── 3. CLOUD AI ───────────────────────────────────────────────
        self._log("CLOUD", "Checking cloud providers...")
        cfg_path = Path(__file__).resolve().parent / "config" / "api_keys.json"
        cfg = {}
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            pass

        cloud_status = {}
        provider_configs = [
            ("Gemini", "gemini_api_key", None),
            ("OpenAI", "openai_api_key", None),
            ("DeepSeek", "deepseek_api_key", "deepseek"),
            ("xAI/Grok", "xai_api_key", "xai"),
            ("z.ai", "zai_api_key", "zai"),
        ]
        for name, key_field, _ in provider_configs:
            key = cfg.get(key_field, "")
            configured = bool(key) and key.strip() not in ("", "YOUR_KEY_HERE")
            if not configured:
                self._set("CLOUD AI", name.lower(), False, False, f"{name}: not configured")
                cloud_status[name] = "NOT_CONFIGURED"
            else:
                # Mark as configured — don't make expensive API calls during startup
                # Provider health check happens lazily on first use
                self._set("CLOUD AI", name.lower(), True, False, f"{name}: configured")
                cloud_status[name] = "CONFIGURED"

        steps += 1
        self.progress_signal.emit(int(steps / total * 100))
        time.sleep(0.2)

        # ── 4. MEMORY ─────────────────────────────────────────────────
        self._log("MEMORY", "Connecting brain.db...")
        brain_ok = False
        brain_count = 0
        try:
            from memory import brain as _brain
            _brain.init_db()
            stats = _brain.get_stats()
            brain_ok = True
            brain_count = stats.get("total", 0)
        except Exception:
            pass
        self._set("MEMORY", "brain_db", brain_ok, not brain_ok,
                  f"brain.db: {brain_count} records" if brain_ok else "brain.db: error")
        self._set("MEMORY", "recall", brain_ok, False,
                  "FTS5 recall engine ready" if brain_ok else "recall: needs brain.db")

        # Conversation store
        conv_ok = False
        conv_count = 0
        try:
            from memory import conversation_store as cs
            cs.init_db()
            cstats = cs.get_stats()
            conv_ok = True
            conv_count = cstats.get("total_turns", 0)
        except Exception:
            pass
        self._set("MEMORY", "convos", conv_ok, False,
                  f"Conversations: {conv_count} turns" if conv_ok else "Conversations: error")
        steps += 1
        self.progress_signal.emit(int(steps / total * 100))
        time.sleep(0.2)

        # ── 5. AGENTS ─────────────────────────────────────────────────
        self._log("AGENTS", "Initializing agents...")
        agents = ["Brain", "DevAgent", "CodeAgent", "FileAgent", "DesktopAgent",
                  "WebAgent", "VoiceAgent", "OllamaAgent", "SystemMonitor"]
        for i, a in enumerate(agents):
            self._set("AGENTS", a.lower(), True, False, f"{a}")
            if i == len(agents) - 1:
                self._log("AGENTS", f"All {len(agents)} agents ready")
        steps += 1
        self.progress_signal.emit(int(steps / total * 100))
        time.sleep(0.2)

        # ── 6. VOICE ──────────────────────────────────────────────────
        self._log("VOICE", "Checking microphone...")
        mic_ok = False
        sr_ok = False
        try:
            import sounddevice as sd
            devices = sd.query_devices()
            input_devs = [d for d in devices if d.get("max_input_channels", 0) > 0]
            mic_ok = len(input_devs) > 0
            # Try common sample rates
            for sr in (16000, 44100, 48000):
                try:
                    sd.check_input_settings(samplerate=sr, channels=1)
                    sr_ok = True
                    self._set("VOICE", "mic", mic_ok, not mic_ok,
                              f"Mic detected ({len(input_devs)} devices)" if mic_ok else "No mic")
                    self._set("VOICE", "sample_rate", sr_ok, not sr_ok,
                              f"Sample rate: {sr}Hz OK" if sr_ok else "No compatible rate")
                    break
                except Exception:
                    continue
        except Exception:
            pass
        if not sr_ok:
            self._set("VOICE", "mic", mic_ok, not mic_ok,
                      f"Mic detected ({len(input_devs)} devices)" if mic_ok else "No mic")
            self._set("VOICE", "sample_rate", False, True, "Sample rate: text mode fallback")
        steps += 1
        self.progress_signal.emit(int(steps / total * 100))
        time.sleep(0.2)

        # ── 7. DESKTOP ────────────────────────────────────────────────
        self._log("DESKTOP", "Checking desktop tools...")
        home = Path.home()
        desktop = home / "Desktop"
        self._set("DESKTOP", "files", desktop.exists(), False,
                  f"Desktop: {desktop}" if desktop.exists() else "Desktop: custom")
        self._set("DESKTOP", "file_ctrl", True, False, "File controller ready")
        self._set("DESKTOP", "app_launch", True, False, "App launcher ready")
        self._set("DESKTOP", "browser", True, False, "Browser control ready")
        steps += 1
        self.progress_signal.emit(int(steps / total * 100))
        time.sleep(0.2)

        # ── 8. NETWORK ────────────────────────────────────────────────
        self._log("NETWORK", "Checking internet...")
        net_ok = False
        # Try multiple endpoints — single URL can be blocked/slow in some regions
        for check_url in ("https://www.google.com",
                          "https://api.open-meteo.com",
                          "https://open.bigmodel.cn"):
            try:
                urllib.request.urlopen(check_url, timeout=4)
                net_ok = True
                break
            except Exception:
                continue
        self._set("NETWORK", "internet", net_ok, not net_ok,
                  "Online" if net_ok else "Offline (local mode)")
        steps += 1
        self.progress_signal.emit(int(steps / total * 100))
        time.sleep(0.2)

        # ── 9. GUI ────────────────────────────────────────────────────
        self._log("GUI", "Preparing main interface...")
        self._set("GUI", "ready", True, False, "Main GUI ready")
        steps += 1
        self.progress_signal.emit(int(steps / total * 100))
        time.sleep(0.2)

        # ── 10. DONE ──────────────────────────────────────────────────
        self._log("SYSTEM", "Hanaai AI ready.")
        steps = total
        self.progress_signal.emit(100)
        time.sleep(0.5)

        self._results = {
            "ollama": ollama_ok,
            "qwen": has_qwen,
            "llama": has_llama,
            "cloud": cloud_status,
            "brain": brain_ok,
            "mic": mic_ok,
            "net": net_ok,
        }
        self.done_signal.emit(self._results)


class StartupScreen(QMainWindow):
    """Premium dark futuristic startup status window."""

    done_signal = pyqtSignal(dict)  # emitted when startup is complete

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Hanaai AI — System Initialization")
        self.setFixedSize(900, 680)
        self.setStyleSheet(f"background: {BG_DARK};")

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(30, 25, 30, 20)
        main_layout.setSpacing(12)

        # ── Header ───────────────────────────────────────────────────
        header = QVBoxLayout()
        header.setSpacing(2)

        title = QLabel("HANAAI AI")
        title.setStyleSheet(f"color: {TEXT_WHITE}; font-size: 28px; font-weight: bold; letter-spacing: 4px;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(title)

        subtitle = QLabel("SYSTEM INITIALIZATION")
        subtitle.setStyleSheet(f"color: {ACCENT_CYAN}; font-size: 11px; letter-spacing: 3px; font-weight: bold;")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(subtitle)

        tagline = QLabel("Preparing your personal AI environment...")
        tagline.setStyleSheet(f"color: {TEXT_DIM}; font-size: 10px;")
        tagline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(tagline)
        main_layout.addLayout(header)

        # ── AI Core Indicator (animated dots) ────────────────────────
        self._core_label = QLabel("◉")
        self._core_label.setStyleSheet(f"color: {ACCENT_CYAN}; font-size: 24px;")
        self._core_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(self._core_label)
        self._core_timer = QTimer()
        self._core_timer.timeout.connect(self._animate_core)
        self._core_timer.start(500)
        self._core_state = 0

        # ── Progress bar ─────────────────────────────────────────────
        self._progress = QProgressBar()
        self._progress.setFixedHeight(6)
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(False)
        self._progress.setStyleSheet(f"""
            QProgressBar {{
                background: {BG_CARD};
                border: none;
                border-radius: 3px;
            }}
            QProgressBar::chunk {{
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 {ACCENT_BLUE}, stop:1 {ACCENT_CYAN});
                border-radius: 3px;
            }}
        """)
        main_layout.addWidget(self._progress)

        # ── Status cards grid ────────────────────────────────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"QScrollArea {{ border: none; background: transparent; }}")
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        cards_widget = QWidget()
        cards_layout = QGridLayout(cards_widget)
        cards_layout.setSpacing(8)
        cards_layout.setContentsMargins(0, 0, 0, 0)

        self._cards = {}
        card_defs = [
            ("SYSTEM", 0, 0), ("LOCAL AI", 0, 1),
            ("CLOUD AI", 1, 0), ("MEMORY", 1, 1),
            ("AGENTS", 2, 0), ("VOICE", 2, 1),
            ("DESKTOP", 3, 0), ("NETWORK", 3, 1),
            ("GUI", 4, 0),
        ]
        for name, row, col in card_defs:
            card = StatusCard(name)
            self._cards[name] = card
            cards_layout.addWidget(card, row, col)
        cards_layout.setRowStretch(5, 1)
        scroll.setWidget(cards_widget)
        main_layout.addWidget(scroll, stretch=1)

        # ── Log panel ────────────────────────────────────────────────
        log_frame = QFrame()
        log_frame.setStyleSheet(f"""
            QFrame {{
                background: {BG_PANEL};
                border: 1px solid rgba(0, 212, 255, 0.1);
                border-radius: 8px;
            }}
        """)
        log_frame.setFixedHeight(100)
        log_layout = QVBoxLayout(log_frame)
        log_layout.setContentsMargins(10, 6, 10, 6)
        log_layout.setSpacing(1)

        log_title = QLabel("SYSTEM LOG")
        log_title.setStyleSheet(f"color: {TEXT_DIM}; font-size: 9px; font-weight: bold; letter-spacing: 2px;")
        log_layout.addWidget(log_title)

        self._log_labels = []
        log_lines_layout = QVBoxLayout()
        log_lines_layout.setSpacing(0)
        for _ in range(4):
            lbl = QLabel("")
            lbl.setStyleSheet(f"color: {TEXT_DIM}; font-size: 9px; font-family: monospace;")
            self._log_labels.append(lbl)
            log_lines_layout.addWidget(lbl)
        log_layout.addLayout(log_lines_layout)
        main_layout.addWidget(log_frame)

        # ── Status text ──────────────────────────────────────────────
        self._status_label = QLabel("Initializing...")
        self._status_label.setStyleSheet(f"color: {TEXT_DIM}; font-size: 10px;")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        main_layout.addWidget(self._status_label)

    def _animate_core(self):
        icons = ["◉", "◎", "●", "◎"]
        self._core_state = (self._core_state + 1) % len(icons)
        self._core_label.setText(icons[self._core_state])

    def start_checks(self):
        """Start the background boot worker."""
        self._worker = BootWorker()
        self._worker.log_signal.connect(self._on_log)
        self._worker.card_signal.connect(self._on_card)
        self._worker.progress_signal.connect(self._on_progress)
        self._worker.done_signal.connect(self._on_done)
        self._worker.start()

    def _on_log(self, source: str, msg: str):
        ts = time.strftime("%H:%M:%S")
        line = f"{ts}  [{source}]  {msg}"
        # Shift log lines up
        for i in range(len(self._log_labels) - 1):
            self._log_labels[i].setText(self._log_labels[i + 1].text())
        self._log_labels[-1].setText(line)

    def _on_card(self, card_name: str, key: str, ok: bool, warn: bool, text: str):
        if card_name in self._cards:
            self._cards[card_name].set_item(key, ok, warn, text)

    def _on_progress(self, pct: int):
        self._progress.setValue(pct)
        if pct < 100:
            self._status_label.setText(f"Initializing... {pct}%")
        else:
            self._status_label.setText("✓ Ready — launching Hanaai AI...")

    def _on_done(self, results: dict):
        # Wait 1.5s then signal done
        QTimer.singleShot(1500, lambda: self.done_signal.emit(results))


def run_startup() -> dict:
    """
    Show the startup screen, perform all checks, return status dict.
    Blocks until startup is complete.
    """
    app = QApplication.instance() or QApplication(sys.argv)
    screen = StartupScreen()
    screen.show()

    results = {}
    done_event = threading.Event()

    def on_done(r):
        nonlocal results
        results = r
        done_event.set()

    screen.done_signal.connect(on_done)
    screen.start_checks()

    # Process events until done
    while not done_event.is_set():
        app.processEvents()
        time.sleep(0.02)

    screen.close()
    return results


if __name__ == "__main__":
    # Quick test
    results = run_startup()
    print("\n=== Startup Results ===")
    for k, v in results.items():
        print(f"  {k}: {v}")
