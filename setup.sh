#!/usr/bin/env bash
# ============================================================================
#  Hanaai AI Engine — First-Time Setup (Linux: Ubuntu / Kali / Debian)
#  Linux equivalent of SETUP.bat + ULTRON_SETUP.py
# ============================================================================
set -u

# ── Colors ──────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'
BOLD='\033[1m'; NC='\033[0m'

ok()    { echo -e "${GREEN}[OK]${NC}   $*"; }
info()  { echo -e "${CYAN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }
sep()   { echo -e "${BOLD}===========================================================${NC}"; }

# ── Auto-detect project folder (script location) ────────────────────────────
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR" || { err "Cannot enter project folder: $PROJECT_DIR"; exit 1; }

sep
echo -e "${BOLD}${GREEN}   Hanaai AI Engine — First-Time Setup (Linux)${NC}"
sep
echo ""

# ── Detect distro / package manager ─────────────────────────────────────────
PKG_MGR=""
if   command -v apt-get >/dev/null 2>&1; then PKG_MGR="apt-get"
elif command -v apt     >/dev/null 2>&1; then PKG_MGR="apt"
else
    err "Only Debian-based distros (apt / apt-get) are supported."
    err "Found neither apt nor apt-get. Aborting."
    exit 1
fi
info "Package manager: $PKG_MGR"

# ── Check Python 3.10+ ──────────────────────────────────────────────────────
PYTHON_BIN=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PY_VER="$("$candidate" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)"
        PY_MAJOR="${PY_VER%%.*}"; PY_MINOR="${PY_VER#*.}"
        if [ "${PY_MAJOR:-0}" -gt 3 ] || { [ "${PY_MAJOR:-0}" -eq 3 ] && [ "${PY_MINOR:-0}" -ge 10 ]; }; then
            PYTHON_BIN="$candidate"
            ok "Python $PY_VER found ($candidate)."
            break
        fi
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    err "Python 3.10+ is required but was not found."
    info "Installing python3 via $PKG_MGR (requires sudo)…"
    sudo "$PKG_MGR" update
    sudo "$PKG_MGR" install -y python3 python3-venv python3-dev
    PYTHON_BIN="python3"
    PY_VER="$("$PYTHON_BIN" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)"
    if [ -z "$PY_VER" ]; then
        err "Python installation failed. Please install Python 3.10+ manually."
        exit 1
    fi
    ok "Python $PY_VER installed."
fi

# ── Install required Linux system packages ──────────────────────────────────
info "Installing required system packages (requires sudo)…"
sudo "$PKG_MGR" update -y

SYS_PKGS=(
    python3 python3-venv python3-dev python3-pip
    build-essential pkg-config cmake git curl wget
    ffmpeg
    portaudio19-dev libportaudio2 libasound2-dev libsndfile1
    espeak espeak-ng
    alsa-utils pulseaudio-utils
    libgl1 libglib2.0-0 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2
    libgbm1 libpango-1.0-0 libcairo2 libasound2
)

# Install each package; skip silently if a name is unavailable on this distro
for pkg in "${SYS_PKGS[@]}"; do
    sudo "$PKG_MGR" install -y "$pkg" 2>/dev/null || warn "Package '$pkg' not available on this distro — skipping."
done
ok "System packages installed."

# ── Create virtual environment ──────────────────────────────────────────────
VENV_DIR="$PROJECT_DIR/.venv"
if [ ! -d "$VENV_DIR" ]; then
    info "Creating virtual environment in .venv …"
    "$PYTHON_BIN" -m venv "$VENV_DIR" || { err "venv creation failed."; exit 1; }
    ok "Virtual environment created."
else
    ok "Virtual environment already exists."
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

# ── Upgrade pip ─────────────────────────────────────────────────────────────
info "Upgrading pip …"
python -m pip install --upgrade pip >/dev/null 2>&1 || warn "pip upgrade failed — continuing."
ok "pip ready."

# ── Install Python requirements ─────────────────────────────────────────────
if [ -f "$PROJECT_DIR/requirements.txt" ]; then
    info "Installing requirements.txt …"
    python -m pip install -r "$PROJECT_DIR/requirements.txt" || {
        err "Failed to install requirements.txt."
        err "Try:  source .venv/bin/activate && pip install -r requirements.txt"
        exit 1
    }
    ok "Python requirements installed."
else
    err "requirements.txt not found in $PROJECT_DIR"
    exit 1
fi

# PyAudio — required by the wake word service (SpeechRecognition microphone)
info "Installing PyAudio (for wake word microphone) …"
python -m pip install PyAudio 2>/dev/null || warn "PyAudio install failed — wake word may be unavailable."

# ── Install Playwright Chromium browser ─────────────────────────────────────
info "Installing Playwright Chromium (browser automation) …"
python -m playwright install chromium || warn "Playwright Chromium install failed — browser automation may be limited."
ok "Playwright Chromium installed."

# ── Initialize config/api_keys.json ─────────────────────────────────────────
CONFIG_DIR="$PROJECT_DIR/config"
mkdir -p "$CONFIG_DIR"
API_KEYS="$CONFIG_DIR/api_keys.json"
API_EXAMPLE="$CONFIG_DIR/api_keys.json.example"
if [ ! -f "$API_KEYS" ]; then
    if [ -f "$API_EXAMPLE" ]; then
        cp "$API_EXAMPLE" "$API_KEYS"
        ok "Copied api_keys.json.example → api_keys.json"
    else
        cat > "$API_KEYS" <<'JSON'
{
    "gemini_api_key": "YOUR_GEMINI_API_KEY_HERE",
    "os_system": "linux",
    "morning_brief_enabled": true,
    "assistant_name": "Hanaai AI",
    "user_name": "",
    "ui_color": "#00ff66"
}
JSON
        ok "Created default api_keys.json (os_system=linux)."
    fi
else
    ok "api_keys.json already exists."
fi

# ── Create missing folders ──────────────────────────────────────────────────
mkdir -p "$PROJECT_DIR/memory" "$PROJECT_DIR/actions" "$PROJECT_DIR/core"
mkdir -p "$PROJECT_DIR/dashboard"
mkdir -p "$HOME/.ultron"

# ── Write setup-complete marker ─────────────────────────────────────────────
echo "Setup completed on: $(date -Iseconds)" > "$PROJECT_DIR/.ultron_setup_complete"
ok "Wrote setup completion marker."

# ── Verify key imports ──────────────────────────────────────────────────────
info "Verifying Python imports …"
IMPORT_FAIL=0
for mod in google.genai sounddevice numpy PIL requests bs4 psutil cv2; do
    python -c "import $mod" 2>/dev/null || { warn "Import check failed: $mod"; IMPORT_FAIL=1; }
done
if [ "$IMPORT_FAIL" -eq 0 ]; then
    ok "All core imports verified."
else
    warn "Some imports failed — review the messages above."
fi

# ── Fix executable permissions ──────────────────────────────────────────────
chmod +x "$PROJECT_DIR"/*.sh 2>/dev/null
ok "Shell scripts marked executable."

# ── Display environment / status logs ───────────────────────────────────────
sep
echo -e "${BOLD}ENVIRONMENT STATUS${NC}"
sep
echo -e "  ${CYAN}OS${NC}        : $(uname -srm)"
echo -e "  ${CYAN}Distro${NC}    : $(lsb_release -ds 2>/dev/null || cat /etc/os-release 2>/dev/null | grep '^PRETTY_NAME' | cut -d= -f2 | tr -d '\"')"
echo -e "  ${CYAN}Python${NC}    : $("$PYTHON_BIN" --version 2>&1)"
echo -e "  ${CYAN}pip${NC}       : $(python -m pip --version 2>&1)"
echo -e "  ${CYAN}Venv${NC}      : $VENV_DIR"
echo -e "  ${CYAN}ffmpeg${NC}    : $(command -v ffmpeg || echo 'not found')"
echo -e "  ${CYAN}portaudio${NC} : $([ -f /usr/lib/x86_64-linux-gnu/libportaudio.so ] && echo 'present' || echo 'check pkg-config')"
echo -e "  ${CYAN}API key${NC}   : $(python -c 'import json;print("set" if json.load(open("config/api_keys.json")).get("gemini_api_key","").strip() not in ("","YOUR_GEMINI_API_KEY_HERE") else "NOT set")' 2>/dev/null || echo 'unknown')"
sep

sep
echo -e "${BOLD}${GREEN}   Hanaai AI SETUP COMPLETE!${NC}"
sep
echo ""
echo -e "  To launch Hanaai AI:"
echo -e "    ${BOLD}bash start_ultron.sh${NC}"
echo ""
echo -e "  Or manually:  ${BOLD}source .venv/bin/activate && python main.py${NC}"
echo ""
echo -e "  ${YELLOW}IMPORTANT:${NC} Add your Gemini API key in ${BOLD}config/api_keys.json${NC}"
echo -e "  Get a free key: https://aistudio.google.com/apikey"
echo ""
sep

read -r -p "Do you want to launch Hanaai AI now? [Enter=launch / N=exit]: " choice
if [ "${choice:-}" != "n" ] && [ "${choice:-}" != "N" ]; then
    info "Launching Hanaai AI …"
    exec python main.py
fi
