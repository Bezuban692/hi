#!/usr/bin/env bash
# ============================================================================
#  Hanaai AI Engine — Wake Word Listener (Linux: Ubuntu / Kali / Debian)
#  Linux equivalent of Start_ULTRON_Wake_Word.bat
#  Listens for "wake up hanaai" and auto-launches main.py.
# ============================================================================
set -u

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'
BOLD='\033[1m'; NC='\033[0m'

ok()   { echo -e "${GREEN}[OK]${NC}   $*"; }
info() { echo -e "${CYAN}[INFO]${NC} $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*"; }
sep()  { echo -e "${BOLD}===========================================================${NC}"; }

# ── Auto-detect project folder ──────────────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR" || { err "Cannot enter project folder: $PROJECT_DIR"; exit 1; }

sep
echo -e "${BOLD}${GREEN}   Hanaai AI — Wake Word Listener (Linux)${NC}"
sep

# ── First-time launch: run setup if marker missing ──────────────────────────
if [ ! -f "$PROJECT_DIR/.ultron_setup_complete" ]; then
    info "First-time launch detected. Running setup first …"
    echo ""
    bash "$PROJECT_DIR/setup.sh"
    RC=$?
    if [ "$RC" -ne 0 ]; then
        err "Setup failed (exit code $RC). Check the messages above."
        exit "$RC"
    fi
fi

# ── Activate virtual environment ────────────────────────────────────────────
VENV_DIR="$PROJECT_DIR/.venv"
if [ -f "$VENV_DIR/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    ok "Virtual environment activated."
else
    warn "Virtual environment not found — using system Python."
fi

# ── Check microphone / audio ─────────────────────────────────────────────────
info "Checking audio subsystem …"
if command -v arecord >/dev/null 2>&1; then
    ok "ALSA (arecord) available."
else
    warn "arecord not found — microphone may not work. Install: sudo apt install alsa-utils"
fi

if command -v pactl >/dev/null 2>&1; then
    ok "PulseAudio (pactl) available."
else
    warn "pactl not found — PulseAudio may be needed for microphone access."
    warn "Install: sudo apt install pulseaudio-utils"
fi

# ── Launch wake service ─────────────────────────────────────────────────────
echo ""
info 'Launching Wake Word Listener ("wake up hanaai") …'
echo ""
python wake_service.py

echo ""
info "Wake service stopped."
