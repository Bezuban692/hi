#!/usr/bin/env bash
# ============================================================================
#  Hanaai AI Engine — Launcher (Linux: Ubuntu / Kali / Debian)
#  Features:
#    - Checks/starts Ollama service
#    - Checks configured Llama 3.2 model — downloads with REAL progress if missing
#    - Skips download if model already exists (fast launch)
#    - Launches Hanaai AI
# ============================================================================
set -u

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'
BOLD='\033[1m'; NC='\033[0m'

ok()   { echo -e "${GREEN}[OK]${NC}   $*"; }
info() { echo -e "${CYAN}[INFO]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*"; }
sep()  { echo -e "${BOLD}===========================================================${NC}"; }

# ── Auto-detect project folder ──────────────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR" || { err "Cannot enter project folder: $PROJECT_DIR"; exit 1; }

sep
echo -e "${BOLD}${GREEN}   Hanaai AI Engine — Launcher (Linux)${NC}"
sep
echo ""

# ── First-time launch: run setup if marker missing ──────────────────────────
if [ ! -f "$PROJECT_DIR/.ultron_setup_complete" ]; then
    info "First-time launch detected. Running setup …"
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

# ============================================================================
#  LOCAL AI MODEL SETUP (Ollama + Llama 3.2)
# ============================================================================
LOCAL_MODEL="llama3.2"

sep
echo -e "${BOLD}HANAAI AI — LOCAL MODEL SETUP${NC}"
sep

# ── Step 1: Check if Ollama is installed ────────────────────────────────────
if ! command -v ollama >/dev/null 2>&1; then
    warn "Ollama not installed. Local offline AI will be unavailable."
    warn "Install with:  curl -fsSL https://ollama.com/install.sh | sh"
    echo ""
    info "Continuing with cloud AI only..."
else
    ok "Ollama found: $(command -v ollama)"

    # ── Step 2: Ensure Ollama service is running ───────────────────────────
    if curl -s --max-time 2 http://localhost:11434/api/tags >/dev/null 2>&1; then
        ok "Ollama service is running."
    else
        info "Starting Ollama service..."
        # Start ollama serve in background (detached)
        nohup ollama serve > /tmp/ollama_hanaai.log 2>&1 &
        OLLAMA_PID=$!
        # Wait up to 15 seconds for it to come up
        for i in $(seq 1 15); do
            if curl -s --max-time 1 http://localhost:11434/api/tags >/dev/null 2>&1; then
                ok "Ollama service started."
                break
            fi
            sleep 1
        done
        if ! curl -s --max-time 2 http://localhost:11434/api/tags >/dev/null 2>&1; then
            warn "Ollama service did not start in time. Local AI unavailable."
        fi
    fi

    # ── Step 3: Check if models already downloaded (skip if GUI handles it) ─
    if curl -s --max-time 2 http://localhost:11434/api/tags >/dev/null 2>&1; then
        MODEL_EXISTS=$(curl -s --max-time 3 http://localhost:11434/api/tags 2>/dev/null \
                       | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    models = [m.get('name','').split(':')[0] for m in data.get('models', [])]
    has_qwen = any('qwen2.5-coder' in m for m in models)
    has_llama = 'llama3.2' in models
    print('both' if has_qwen and has_llama else 'partial' if (has_qwen or has_llama) else 'none')
except Exception:
    print('none')
" 2>/dev/null || echo "none")

        if [ "$MODEL_EXISTS" = "both" ]; then
            ok "Models ready: qwen2.5-coder:7b + llama3.2"
            ok "Local AI ready."
        elif [ "$MODEL_EXISTS" = "partial" ]; then
            info "Some models present — startup GUI will download missing ones."
        else
            info "No models found — startup GUI will download them."
        fi
    fi
fi

echo ""
sep
echo -e "${BOLD}${GREEN}   Launching Hanaai AI...${NC}"
sep
echo ""

# ── Launch Hanaai AI ──────────────────────────────────────────────────────
python main.py
RC=$?

if [ "$RC" -ne 0 ]; then
    echo ""
    err "Hanaai AI closed with an error (code $RC)."
    info "Review the output above. Re-run setup with: bash setup.sh"
    exit "$RC"
fi

ok "Hanaai AI exited cleanly."
