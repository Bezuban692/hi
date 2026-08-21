#!/usr/bin/env python3
"""
Linux Requirements Checker for Hanaai AI Engine.
Verifies system packages, Python version, virtual environment,
pip dependencies, audio subsystem, and project integrity.
Run standalone:  python linux_requirements_check.py
"""

import importlib.util
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

GREEN  = "\033[0;32m"
RED    = "\033[0;31m"
YELLOW = "\033[1;33m"
CYAN   = "\033[0;36m"
BOLD   = "\033[1m"
NC     = "\033[0m"

PASS = 0
FAIL = 0
WARN = 0


def ok(msg):   global PASS; PASS += 1; print(f"{GREEN}[OK]{NC}   {msg}")
def fail(msg): global FAIL; FAIL += 1; print(f"{RED}[FAIL]{NC} {msg}")
def warn(msg): global WARN; WARN += 1; print(f"{YELLOW}[WARN]{NC} {msg}")
def info(msg): print(f"{CYAN}[INFO]{NC} {msg}")
def sep():    print(f"{BOLD}{'=' * 60}{NC}")


def check_os():
    sep()
    print(f"{BOLD}OPERATING SYSTEM{NC}")
    sep()
    os_name = platform.system()
    if os_name == "Linux":
        ok(f"OS: {platform.platform()}")
    else:
        fail(f"Unsupported OS: {os_name} (this checker is for Linux only)")

    # Distro
    try:
        distro = subprocess.run(
            ["lsb_release", "-ds"], capture_output=True, text=True, timeout=3
        ).stdout.strip()
        ok(f"Distro: {distro}")
    except Exception:
        try:
            with open("/etc/os-release") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        ok(f"Distro: {line.strip().split('=',1)[1].strip('\"')}")
                        break
        except Exception:
            warn("Could not detect distro.")


def check_python():
    sep()
    print(f"{BOLD}PYTHON{NC}")
    sep()
    ver = sys.version_info
    if ver >= (3, 10):
        ok(f"Python {ver.major}.{ver.minor}.{ver.micro} — meets minimum (3.10+)")
    else:
        fail(f"Python {ver.major}.{ver.minor}.{ver.micro} — requires 3.10+")


def check_venv():
    sep()
    print(f"{BOLD}VIRTUAL ENVIRONMENT{NC}")
    sep()
    in_venv = hasattr(sys, "real_prefix") or (
        hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix
    )
    if in_venv:
        ok(f"Active venv: {sys.prefix}")
    else:
        warn("No virtual environment active. Recommended: source .venv/bin/activate")


def check_system_packages():
    sep()
    print(f"{BOLD}SYSTEM PACKAGES (apt){NC}")
    sep()

    packages = [
        "python3", "python3-venv", "python3-dev", "build-essential",
        "ffmpeg", "portaudio19-dev", "libportaudio2",
        "libasound2-dev", "libsndfile1",
        "espeak", "espeak-ng",
        "alsa-utils", "pulseaudio-utils",
        "git", "curl", "wget", "pkg-config", "cmake",
        "libgl1", "libnss3", "libnspr4",
        "libatk1.0-0", "libatk-bridge2.0-0",
        "libcups2", "libdrm2", "libxkbcommon0",
        "libxcomposite1", "libxdamage1", "libxrandr2",
        "libgbm1", "libpango-1.0-0", "libcairo2",
    ]

    for pkg in packages:
        result = subprocess.run(
            ["dpkg", "-s", pkg], capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0 and "Status: install ok installed" in result.stdout:
            ok(pkg)
        else:
            warn(f"{pkg} — not installed (sudo apt install {pkg})")


def check_audio():
    sep()
    print(f"{BOLD}AUDIO / VOICE${NC}")
    sep()

    if shutil.which("arecord"):
        ok("ALSA (arecord)")
    else:
        fail("arecord not found — microphone won't work. Install: sudo apt install alsa-utils")

    if shutil.which("pactl"):
        ok("PulseAudio (pactl)")
    else:
        warn("pactl not found — install: sudo apt install pulseaudio-utils")

    if shutil.which("aplay"):
        ok("ALSA playback (aplay)")
    else:
        warn("aplay not found")

    if shutil.which("espeak"):
        ok("eSpeak TTS")
    else:
        warn("espeak not found — some voice features may be limited")

    # Check PyAudio (import-only, no install)
    if importlib.util.find_spec("pyaudio"):
        ok("PyAudio module")
    else:
        warn("PyAudio not importable — wake word service needs: pip install PyAudio")

    if importlib.util.find_spec("speech_recognition"):
        ok("SpeechRecognition module")
    else:
        fail("SpeechRecognition not importable — pip install SpeechRecognition")


def check_python_deps():
    sep()
    print(f"{BOLD}PYTHON DEPENDENCIES{NC}")
    sep()

    modules = {
        "PyQt6":                "PyQt6",
        "PyQt6.QtWebEngineWidgets": "PyQt6-WebEngine",
        "sounddevice":          "sounddevice",
        "numpy":                "numpy",
        "PIL":                  "pillow",
        "requests":             "requests",
        "bs4":                  "beautifulsoup4",
        "duckduckgo_search":   "duckduckgo-search",
        "pyautogui":            "pyautogui",
        "pyperclip":            "pyperclip",
        "cv2":                  "opencv-python",
        "mss":                  "mss",
        "psutil":               "psutil",
        "send2trash":           "send2trash",
        "pptx":                 "python-pptx",
        "youtube_transcript_api": "youtube-transcript-api",
        "fastapi":              "fastapi",
        "uvicorn":              "uvicorn",
        "cryptography":         "cryptography",
        "edge_tts":             "edge-tts",
        "miniaudio":            "miniaudio",
        "soundfile":            "soundfile",
        "playwright":           "playwright",
        "google.genai":         "google-genai / google-generativeai",
    }

    for mod, pip_name in modules.items():
        if importlib.util.find_spec(mod):
            ok(mod)
        else:
            fail(f"{mod} ({pip_name}) — pip install {pip_name}")


def check_project_integrity():
    sep()
    print(f"{BOLD}PROJECT INTEGRITY{NC}")
    sep()

    base = Path(__file__).resolve().parent
    markers = [
        "main.py", "ui.py", "requirements.txt",
        "core/tts.py", "actions/browser_control.py", "dashboard/server.py",
    ]
    for marker in markers:
        if (base / marker).exists():
            ok(marker)
        else:
            fail(f"Missing: {marker}")

    config_json = base / "config" / "api_keys.json"
    if config_json.exists():
        ok("config/api_keys.json exists")
        try:
            import json
            data = json.loads(config_json.read_text(encoding="utf-8"))
            key = data.get("gemini_api_key", "")
            if key and key.strip() not in ("", "YOUR_GEMINI_API_KEY_HERE"):
                ok("Gemini API key is set")
            else:
                warn("Gemini API key not configured — edit config/api_keys.json")
            os_sys = data.get("os_system", "")
            if os_sys.lower() in ("linux", ""):
                ok(f"os_system = '{os_sys or 'auto'}'")
            else:
                warn(f"os_system = '{os_sys}' — on Linux this should be 'linux'")
        except Exception as e:
            fail(f"config/api_keys.json parse error: {e}")
    else:
        fail("config/api_keys.json missing — run setup.sh")


def main():
    sep()
    print(f"{BOLD}{CYAN}  Hanaai AI Engine — Linux Requirements Check{NC}")
    sep()
    print("")

    check_os()
    check_python()
    check_venv()
    check_system_packages()
    check_audio()
    check_python_deps()
    check_project_integrity()

    sep()
    print(f"{BOLD}RESULTS{NC}")
    sep()
    print(f"  {GREEN}{PASS} passed{NC}  {YELLOW}{WARN} warnings{NC}  {RED}{FAIL} failures{NC}")
    print("")

    if FAIL > 0:
        print(f"{RED}Some checks failed. Fix the issues above, then re-run.{NC}")
        print(f"{RED}Tip: bash setup.sh will resolve most dependency issues.{NC}")
    else:
        print(f"{GREEN}All checks passed! Run: bash start_ultron.sh{NC}")

    sep()
    sys.exit(0 if FAIL == 0 else 1)


if __name__ == "__main__":
    main()
