import platform as _platform
import subprocess as _subprocess

# ── Nuclear: force CREATE_NO_WINDOW on EVERY subprocess call on Windows ───────
# This patches Popen itself, so no per-file flag is needed anywhere.
if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)   # drop any stale/shared STARTUPINFO
            super().__init__(args, **kw)

    _subprocess.Popen = _Popen
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import re
import threading
import time
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

import sounddevice as sd
from google import genai
from google.genai import types
from ui import UltronUI, JarvisUI
from memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
)
from actions.system_monitor    import SystemMonitor, get_system_status
from actions.proactive         import ProactiveEngine
from memory.config_manager     import get_brief_enabled
from memory import brain as _brain
from memory import conversation_store as _conv_store
from core.ai_providers import reset_health

# ── Lazy-loaded action imports (loaded on first tool call, not at startup) ──
import importlib as _il
_ACTION_CACHE: dict = {}

def _import_action(name: str, module: str, attr: str):
    """Lazy-import an action module once, cache it."""
    if name not in _ACTION_CACHE:
        mod = _il.import_module(f"actions.{module}")
        _ACTION_CACHE[name] = getattr(mod, attr)
    return _ACTION_CACHE[name]


def _archive_turn(user_text: str, assistant_text: str) -> None:
    """
    Background task: archive a conversation turn to SQLite + auto-save
    memorable content. Runs in executor thread — non-blocking.
    """
    try:
        # 1. Archive the conversation (always)
        _conv_store.archive_turn(user_text, assistant_text)

        # 2. Auto-classify user message — persist if it's a stable memory type
        mem_type = _brain.classify(user_text)
        # Only save persistent types (NOT EPHEMERAL or SHORT_TERM)
        persistent_types = (
            "LONG_TERM", "PROJECT_MEMORY", "KNOWLEDGE",
            "PREFERENCE", "FACT", "DECISION", "INSTRUCTION",
        )
        if mem_type in persistent_types:
            snippet = user_text[:380]
            # Detect project name from user message
            project = ""
            for kw in ("website", "project", "app", "program"):
                if kw in user_text.lower():
                    project = kw
                    break
            rid = _brain.add_memory(
                mem_type, snippet, source="auto", project=project, verify=True
            )
            if rid > 0:
                print(f"[Memory] 💾 Auto-saved ({mem_type}): {snippet[:50]}...")
            else:
                print(f"[Memory] ❌ Failed to save: {snippet[:50]}...")

        # 3. Extract DevAgent project results from assistant response
        #    DevAgent returns structured info like "Project: X | Files: N | Entry: Y"
        #    Save this as PROJECT_MEMORY so it survives restart
        _extract_project_memory(assistant_text)
    except Exception as e:
        print(f"[Memory] Archive error: {e}")


def _extract_project_memory(assistant_text: str) -> None:
    """
    Detect DevAgent project results in assistant response and save to brain.db
    as PROJECT_MEMORY. This captures project name, type, files, entry point.
    """
    try:
        text = (assistant_text or "").strip()
        if not text:
            return

        # Pattern 1: DevAgent result format "Project: X | Files: N | Entry: Y"
        proj_match = re.search(r"[Pp]roject:\s*(\S+)", text)
        files_match = re.search(r"[Ff]iles:\s*(\d+)", text)
        entry_match = re.search(r"[Ee]ntry:\s*(\S+)", text)
        saved_match = re.search(r"[Ss]aved to:\s*(\S+)", text)

        # Pattern 2: Code helper / dev_agent result "Saved to: /path/to/file"
        if not proj_match and saved_match:
            # code_helper result — save as PROJECT_MEMORY with file path
            path = saved_match.group(1)
            content = f"Code file created: {path}"
            _brain.add_memory("PROJECT_MEMORY", content[:380],
                             source="dev_agent", project="code",
                             importance=4, verify=True)
            return

        if not proj_match:
            return

        proj_name = proj_match.group(1)
        file_count = files_match.group(1) if files_match else "?"
        entry = entry_match.group(1) if entry_match else "?"
        save_path = saved_match.group(1) if saved_match else ""

        # Detect project type from entry point
        if entry.endswith(".html"):
            proj_type = "website"
        elif entry.endswith(".py"):
            proj_type = "python_app"
        else:
            proj_type = "project"

        content = (f"Created project '{proj_name}' (type: {proj_type}). "
                   f"Entry: {entry}, Files: {file_count}. Path: {save_path}")

        # Also extract the user's original request context from recent conversation
        tags = f"{proj_type},dev_agent"
        rid = _brain.add_memory(
            "PROJECT_MEMORY", content[:380],
            source="dev_agent", project=proj_name,
            importance=7, confidence=8, tags=tags, verify=True
        )
        if rid > 0:
            print(f"[Memory] 🏗️ Project saved: {proj_name} ({proj_type}, {file_count} files)")
    except Exception as e:
        print(f"[Memory] Project extraction error: {e}")


def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"
LIVE_MODEL          = "models/gemini-2.5-flash-native-audio-preview-12-2025"
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 512

class ApiKeyMissing(Exception):
    """Raised when config/api_keys.json is missing, broken, or has no real key."""


_cached_api_key = None   # cached after first read — avoids re-reading file each reconnect
_cached_prompt  = None   # cached system prompt text

def _get_api_key() -> str:
    global _cached_api_key
    if _cached_api_key:
        return _cached_api_key
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            key = json.load(f)["gemini_api_key"]
    except (json.JSONDecodeError, KeyError, FileNotFoundError) as e:
        raise ApiKeyMissing(f"config/api_keys.json is missing or invalid: {e}") from e
    if not key or key.strip() in ("", "YOUR_GEMINI_API_KEY_HERE"):
        raise ApiKeyMissing("No API key set in config/api_keys.json")
    _cached_api_key = key
    return key


def _load_system_prompt() -> str:
    global _cached_prompt
    if _cached_prompt:
        return _cached_prompt
    try:
        _cached_prompt = PROMPT_PATH.read_text(encoding="utf-8")
        return _cached_prompt
    except Exception:
        return (
            "You are Hanaai AI, a highly intelligent AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

def _clean_transcript(text: str) -> str:    
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()

def _clean_chunk(text: str) -> str:
    """Like _clean_transcript but KEEPS leading/trailing spaces —
    needed for live streaming so words stay separated."""
    text = _CTRL_RE.sub("", text)
    return re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)

from core.tool_declarations import TOOL_DECLARATIONS

# --- Plugin system ---


class UltronLive:

    def __init__(self, ui: UltronUI):
        self.ui             = ui
        self._asst_name     = "Hanaai AI"   # updated each session from config
        self.session              = None
        self.audio_in_queue       = None
        self.out_queue            = None
        self._loop                = None
        self._is_speaking         = False
        self._speaking_lock       = threading.Lock()
        self._phone_active        = False   # True while phone mic is streaming; pauses PC mic
        self._pending_vision       = None    # (img_bytes, mime_type, question, angle) to inject after tool response
        self._vision_cam_active    = False   # True if camera was opened for vision → auto-close after response
        self._vision_close_pending = False   # True after vision injected; next turn_complete closes camera
        self._vision_last_time     = 0.0     # monotonic time of last screen_process call (cooldown guard)
        self._vision_busy          = False   # True while a vision capture/inject cycle is in flight
        self._interrupted          = False   # True while draining audio after user interrupt
        self._authenticated        = False   # Secure-boot: unlocks after face/password
        self._auth_event           = threading.Event()  # gates the AI session start
        self._tasks: dict = {}                # name → {pct, detail} live progress
        self._tasks_lock = threading.Lock()
        self._pending_cmds: list = []         # messages typed before session ready
        self._audio_got_this_turn = False     # voice came this turn? (else espeak fallback)
        self._user_location: str = ""         # "Peshawar, PK" via ipinfo at startup
        self._turn_done_event: asyncio.Event | None = None
        self._dashboard     = None
        self._briefing_sent    = False          # morning briefing fires once per process
        self._sys_monitor      = SystemMonitor()  # persistent cooldown state
        self._proactive        = ProactiveEngine()
        self._last_user_speech = time.monotonic()  # updated on every user utterance
        self.ui.on_text_command   = self._on_text_command
        self.ui.on_remote_clicked = self._make_remote_key
        self.ui.on_interrupt      = self.interrupt

        # ── SECURE BOOT: re-sync lock state on EVERY page (re)load ──────
        # The page defaults to the SETUP wizard; if security is already
        # configured (or owner already logged in) Python corrects it here.
        def _resync_lock(_ok=True):
            try:
                if self._authenticated:
                    # already verified — keep the app unlocked after reloads
                    self.ui.eval_js_threadsafe(
                        "if (typeof lockHide === 'function') lockHide();")
                    return
                from core import security as _sec
                mode = "setup" if not _sec.is_setup() else "login"
                self.ui._eval_lock_mode(mode)
            except Exception:
                pass
        try:
            self.ui.set_page_loaded_callback(_resync_lock)
            # immediate + backup timer for the first load
            _resync_lock()
            threading.Timer(2.5, _resync_lock).start()
            from core import security as _sec0
            print(f"[Security] 🔐 {'LOGIN' if _sec0.is_setup() else 'SETUP'} mode — "
                  f"AI stays offline until owner verifies")
        except Exception as _e:
            print(f"[Security] ⚠️ lock init failed: {_e}")
            self._authenticated = True
            self._auth_event.set()  # fail-open on internal error

        # Always-on live screen monitoring — Hanaai "sees" the screen from
        # the moment she starts. Any screen question answers from this cache.
        try:
            _start_live = _import_action("start_live_screen", "screen_processor", "start_live_screen")
            _start_live()
        except Exception as _e:
            print(f"[Vision] ⚠️ Live screen start failed: {_e}")

    def task_update(self, name: str, pct: int, detail: str = "") -> None:
        """Track a running task's progress — shown when user asks 'kitna hua?'."""
        with self._tasks_lock:
            if pct >= 100:
                self._tasks.pop(name, None)
                line = f"TASK[{name}]: ✅ COMPLETE {detail}".strip()
            else:
                self._tasks[name] = {"pct": int(pct), "detail": detail}
                line = f"TASK[{name}]: {int(pct)}% {detail}".strip()
        try:
            self.ui.write_log(line)
        except Exception:
            pass

    def tasks_snapshot(self) -> str:
        with self._tasks_lock:
            if not self._tasks:
                return "No tasks currently running."
            lines = []
            for name, t in self._tasks.items():
                lines.append(f"- {name}: {t['pct']}% — {t['detail']}")
            return "ACTIVE TASKS:\n" + "\n".join(lines)

    def _make_remote_key(self):
        """Called from Qt main thread when user presses Remote Control."""
        if self._dashboard is None:
            self.ui.write_log(
                "SYS: Dashboard unavailable. "
                "Run: pip install fastapi \"uvicorn[standard]\" cryptography"
            )
            return None
        key    = self._dashboard.new_key()
        url    = self._dashboard.get_url()
        manual = self._dashboard.get_manual_url()
        return url, key, f"{url}/auto-login?key={key}", manual

    def _on_text_command(self, text: str):
        if not text:
            return
        clean_text = str(text).strip()
        if clean_text in ("/toggle_mic", "toggle_mic", "mute", "unmute"):
            if clean_text == "mute":
                self.ui.muted = True
            elif clean_text == "unmute":
                self.ui.muted = False
            else:
                self.ui.muted = not self.ui.muted
            new_state = "MUTED" if self.ui.muted else "LISTENING"
            self.set_app_state(new_state)
            self.ui.write_log(f"SYS: Microphone {'MUTED (OFF)' if self.ui.muted else 'UNMUTED (ON)'}.")
            return
        # UI camera preview holds the V4L2 device — Python must release/grab
        # around it (Linux allows only ONE consumer of /dev/video0).
        if clean_text == "/camera_ui_on":
            try:
                _rel = _import_action("release_camera", "face_manager", "release_camera_pub")
                _rel()
                self.ui.write_log("SYS: Camera device → UI preview (Python released)")
            except Exception:
                pass
            return
        if clean_text == "/camera_ui_off":
            self.ui.write_log("SYS: Camera device → Python available")
            return
        # Settings: change default city (weather/local answers follow it)
        if clean_text.startswith("/set_city "):
            city = clean_text[len("/set_city "):].strip()
            if city:
                threading.Thread(target=self._set_city, args=(city,), daemon=True).start()
            return
        # ── SECURE BOOT auth commands (handled before Gemini, never spoken) ──
        if clean_text.startswith("/auth_"):
            self._handle_auth_command(clean_text)
            return
        # Block everything else until the owner authenticates
        if not self._authenticated:
            self.ui.auth_result(False, "Login first — face or password")
            return
        # Session still connecting (first seconds after login)? QUEUE the
        # message instead of silently dropping it — flushed on connect.
        if not self._loop or not self.session:
            with self._tasks_lock:
                self._pending_cmds.append(clean_text)
            if len(self._pending_cmds) <= 1:  # note once, not per message
                self.ui.show_content(
                    "Hanaai AI",
                    "AI abhi online ho rahi hai jaan — 5 second me message deliver kar dungi 💖")
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": clean_text}]},
                turn_complete=True
            ),
            self._loop
        )

    def _flush_pending_cmds(self) -> None:
        """Send any messages typed while the session was connecting."""
        with self._tasks_lock:
            pending = self._pending_cmds[:]
            self._pending_cmds.clear()
        for text in pending:
            try:
                asyncio.run_coroutine_threadsafe(
                    self.session.send_client_content(
                        turns={"parts": [{"text": text}]},
                        turn_complete=True
                    ),
                    self._loop
                )
            except Exception:
                pass

    def _grant_access(self, reason: str) -> None:
        """Full unlock: UI + AI session both start."""
        self._authenticated = True
        self._auth_event.set()
        self.ui.auth_result(True, reason)
        self.ui.write_log(f"[Security] ✅ ACCESS GRANTED — {reason}")

    def _handle_auth_command(self, cmd: str) -> None:
        """Secure-boot wizard commands. Runs in Qt/executor threads —
        heavy work (face worker) goes to background threads."""
        import threading as _th

        def _bg(fn):
            _th.Thread(target=fn, daemon=True).start()

        try:
            from core import security as _sec
        except Exception:
            return

        # STEP 1: set password
        if cmd.startswith("/auth_setup_pw "):
            pw = cmd[len("/auth_setup_pw "):].strip()
            ok, msg = _sec.set_password(pw)
            if ok:
                self.ui.write_log(f"[Security] {msg}")
                self.ui.setup_stage("face")       # advance wizard → STEP 2
            else:
                self.ui.auth_result(False, msg)

        # STEP 2: enroll face (worker subprocess, ~10-20s)
        elif cmd == "/auth_setup_face":
            def _enroll():
                self._sys_monitor.set_ai_task(True)
                try:
                    from actions.face_manager import enroll_face
                    result = enroll_face("saad")
                    _sec.mark_face_enrolled("enrolled" in result)
                    self.ui.write_log(f"[Security] {result}")
                    if "enrolled" in result:
                        self.ui.setup_stage("verify")   # advance → STEP 3
                    else:
                        self.ui.auth_result(False, result)  # stay on step 2
                finally:
                    self._sys_monitor.set_ai_task(False)
            _bg(_enroll)

        elif cmd in ("/auth_skip_face", "/auth_done"):
            self.ui.setup_stage("verify")           # skip → STEP 3

        # STEP 3: verify password to finish setup
        elif cmd.startswith("/auth_verify_pw "):
            pw = cmd[len("/auth_verify_pw "):].strip()
            ok, msg = _sec.verify_password(pw)
            if ok:
                self._grant_access(f"Setup complete — khush aamdeed Saad!")
            else:
                self.ui.write_log(f"[Security] ⛔ Verify denied: {msg}")
                self.ui.auth_result(False, msg)

        # LOGIN (subsequent runs): face
        elif cmd == "/auth_face":
            def _scan():
                self._sys_monitor.set_ai_task(True)
                try:
                    ok, msg = _sec.face_unlock()
                    if ok:
                        self._grant_access(f"Face matched — {msg}")
                    else:
                        self.ui.write_log(f"[Security] ⛔ Face denied: {msg}")
                        self.ui.auth_result(False, msg)
                finally:
                    self._sys_monitor.set_ai_task(False)
            _bg(_scan)

        # LOGIN (subsequent runs): password
        elif cmd.startswith("/auth_pw "):
            pw = cmd[len("/auth_pw "):].strip()
            ok, msg = _sec.verify_password(pw)
            if ok:
                self._grant_access("Password sahi — welcome back Saad!")
            else:
                self.ui.write_log(f"[Security] ⛔ Password denied: {msg}")
                self.ui.auth_result(False, msg)

    def set_app_state(self, state: str):
        self.ui.set_state(state)
        if self._dashboard:
            try:
                loop = self._loop or asyncio.get_event_loop()
                if loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        self._dashboard.broadcast({"type": "state", "state": state}), loop
                    )
            except Exception:
                pass

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        if value:
            self.set_app_state("SPEAKING")
        elif not self.ui.muted:
            self.set_app_state("LISTENING")

    def interrupt(self) -> None:
        """Stop JARVIS mid-speech: drain queued audio and open mic immediately."""
        self._interrupted = True
        q = self.audio_in_queue
        if q:
            drained = 0
            while True:
                try:
                    q.get_nowait()
                    drained += 1
                except Exception:
                    break
            if drained:
                print(f"[Hanaai] ✋ Interrupted — {drained} audio chunks discarded")
        self.set_speaking(False)
        if self._turn_done_event:
            self._turn_done_event.clear()
        self.ui.write_log("SYS: Interrupted — listening...")

    def speak(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"Sir, {tool_name} encountered an error. {short}")

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        # Reset provider health for new session
        reset_health()

        # Load customization from config
        try:
            _cfg = json.loads(open(API_CONFIG_PATH, encoding="utf-8").read())
            self._asst_name = (_cfg.get("assistant_name") or "JARVIS").strip()
            _user_name = (_cfg.get("user_name") or "").strip()
        except Exception:
            self._asst_name = "JARVIS"
            _user_name = ""

        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        # ── CRITICAL: Also load brain.db profile (SQLite persistent memory) ──
        try:
            _brain.import_from_json()  # sync long_term.json → brain.db on first run
            brain_profile = _brain.get_profile_context()
        except Exception:
            brain_profile = ""
        # ── CRITICAL: Load recent conversation history (last 15 turns) ──
        # This gives Gemini context of what user said/did recently
        try:
            recent_convos = _conv_store.get_recent_turns(limit=15)
        except Exception:
            recent_convos = ""
        sys_prompt = _load_system_prompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        # Identity injection — overrides any hardcoded name in prompt.txt
        _addr = (f"ADDRESS: Always call the user '{_user_name}'."
                 if _user_name
                 else "ADDRESS: When speaking Turkish → always say \"efendim\". "
                      "When speaking English → say \"sir\". Never mix languages.")
        identity_ctx = (
            f"[IDENTITY]\n"
            f"Your name is {self._asst_name}. "
            f"Always refer to yourself as {self._asst_name}.\n"
            f"{_addr}\n\n"
        )

        voice_instruction = (
            "[CRITICAL VOICE & PERSONALITY DIRECTIVE]\n"
            "YOU ARE HANAAI AI, A WARM, FRIENDLY, AND INTELLIGENT AI ASSISTANT.\n"
            "YOU MUST SPEAK FLUENT, NATURAL URDU (ROMAN URDU / URDU SCRIPT).\n"
            "USE A NATURAL, SOFT, HUMAN-LIKE FEMALE VOICE.\n"
            "SPEAK SMOOTHLY WITH WARM, EMOTIONALLY EXPRESSIVE INTONATION.\n"
            "KEEP A PLEASANT, CALM PACING WITH NATURAL PAUSES.\n"
            "CLEAR PRONUNCIATION. NEVER SOUND ROBOTIC OR FLAT.\n"
            "ALWAYS REPLY IN URDU UNLESS THE USER EXPLICITLY ASKS FOR ENGLISH.\n\n"
        )

        parts = [voice_instruction, time_ctx, identity_ctx]
        if mem_str:
            parts.append(mem_str)
        if brain_profile:
            parts.append(brain_profile)
        if recent_convos:
            parts.append(recent_convos)
        if self._user_location:
            parts.append(f"[USER LOCATION — real-time, use as default city] {self._user_location}")
        parts.append(sys_prompt)

        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": TOOL_DECLARATIONS}],
            session_resumption=types.SessionResumptionConfig(),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name="Aoede"
                    )
                )
            ),
        )

    async def _handle_open_app(self, args, loop):
        _fn = _import_action("open_app", "open_app", "open_app")
        r = await loop.run_in_executor(None, lambda: _fn(parameters=args, response=None, player=self.ui))
        return r or f"Opened {args.get('app_name')}."

    async def _handle_weather_report(self, args, loop):
        _fn = _import_action("weather_report", "weather_report", "weather_action")
        r = await loop.run_in_executor(None, lambda: _fn(parameters=args, player=self.ui))
        return r or "Weather delivered."

    async def _handle_browser_control(self, args, loop):
        _fn = _import_action("browser_control", "browser_control", "browser_control")
        r = await loop.run_in_executor(None, lambda: _fn(parameters=args, player=self.ui))
        return r or "Done."

    async def _handle_file_controller(self, args, loop):
        _fn = _import_action("file_controller", "file_controller", "file_controller")
        r = await loop.run_in_executor(None, lambda: _fn(parameters=args, player=self.ui))
        return r or "Done."

    async def _handle_send_message(self, args, loop):
        _fn = _import_action("send_message", "send_message", "send_message")
        r = await loop.run_in_executor(None, lambda: _fn(parameters=args, response=None, player=self.ui, session_memory=None))
        return r or f"Message sent to {args.get('receiver')}."

    async def _handle_reminder(self, args, loop):
        _fn = _import_action("reminder", "reminder", "reminder")
        r = await loop.run_in_executor(None, lambda: _fn(parameters=args, response=None, player=self.ui))
        return r or "Reminder set."

    async def _handle_youtube_video(self, args, loop):
        _fn = _import_action("youtube_video", "youtube_video", "youtube_video")
        r = await loop.run_in_executor(None, lambda: _fn(parameters=args, response=None, player=self.ui))
        return r or "Done."

    async def _handle_screen_process(self, args, loop):
        import time as _t_mod
        _now = _t_mod.monotonic()
        _cooldown = 4.0  # seconds — covers echo window after speaking ends
        if self._vision_busy or (_now - self._vision_last_time) < _cooldown:
            _wait = max(0, _cooldown - (_now - self._vision_last_time))
            print(f"[Vision] ⏳ Cooldown active ({_wait:.1f}s remaining) — ignoring duplicate call")
            return "Vision is still processing the previous request. I will not call this again."
        else:
            self._vision_busy      = True
            self._vision_last_time = _now
            # Suppress system-monitor emergency alerts during vision cycle
            self._sys_monitor.set_ai_task(True)
            angle     = args.get("angle", "screen").lower()
            user_text = args.get("text", "What do you see?")
            if angle == "camera":
                # Free the device first — the UI preview (browser) may be
                # holding the V4L2 camera. Linux allows ONE consumer.
                try:
                    self.ui.stop_camera_stream()
                except Exception:
                    pass
                _cap_cam = _import_action("_capture_camera", "screen_processor", "_capture_camera")
                img_b, mime_t = await loop.run_in_executor(None, _cap_cam)
                self.ui.start_camera_stream()
                self._vision_cam_active = True
                print(f"[Vision] 📷 Camera: {len(img_b):,} bytes")
                # FACE RECOGNITION — who is in front of the camera?
                _face_note = ""
                try:
                    _id_fn = _import_action("identify_from_camera", "face_manager", "identify_from_camera")
                    who = await loop.run_in_executor(None, _id_fn)
                    print(f"[Face] 👤 {who}")
                    if who.startswith("saad"):
                        _face_note = ("[FACE_RECOGNITION: This is SAAD — the owner. "
                                      "Greet him as owner and answer his question.] ")
                    elif who == "UNKNOWN_PERSON":
                        _face_note = ("[FACE_RECOGNITION: UNKNOWN PERSON — this is NOT Saad! "
                                      "Clearly tell the owner that someone else is in front of the camera.] ")
                    elif who == "NO_FACE":
                        _face_note = "[FACE_RECOGNITION: no face visible.] "
                except Exception as _e:
                    print(f"[Face] ⚠️ identify failed: {_e}")
                _stall = "camera"
                self._pending_vision = (img_b, mime_t, _face_note + user_text, angle)
                return (
                    f"[VISION_ACTIVE] {_stall.capitalize()} captured. "
                    f"Immediately say ONE short natural sentence in the user's own language, "
                    f"telling them you are looking at their {_stall} right now. "
                    f"Do NOT describe or guess content — the actual image arrives in the NEXT message."
                )
            else:
                # ── FAST PATH: OCR latest live screenshot via generateContent ──
                # Live API downscales injected images (small text unreadable);
                # generateContent returns exact text → Gemini speaks it directly.
                report = ""
                try:
                    _ocr_fn = _import_action("read_screen_text", "screen_processor", "read_screen_text")
                    report = await loop.run_in_executor(None, lambda: _ocr_fn(user_text))
                except Exception as _e:
                    print(f"[Vision] ⚠️ OCR fast-path failed: {_e}")
                if report:
                    self._vision_busy = False
                    self._vision_last_time = _t_mod.monotonic()
                    self._sys_monitor.set_ai_task(False)
                    return (
                        "[SCREEN CONTENT REPORT — captured from live screen just now]\n"
                        f"{report}\n\n"
                        "Now answer the user in Roman Urdu using this report. "
                        "Read important text EXACTLY as written (commands, file names, titles). "
                        "Do NOT call screen_process again for this question."
                    )
                # Fallback: old image-injection flow
                _cap_scr = _import_action("_capture_screen", "screen_processor", "_capture_screen")
                img_b, mime_t = await loop.run_in_executor(None, _cap_scr)
                print(f"[Vision] 🖥️  Screen: {len(img_b):,} bytes")
                _stall = "screen"
                self._pending_vision = (img_b, mime_t, user_text, angle)
                return (
                    f"[VISION_ACTIVE] {_stall.capitalize()} captured. "
                    f"Immediately say ONE short natural sentence in the user's own language, "
                    f"telling them you are looking at their {_stall} right now. "
                    f"Do NOT describe or guess content — the actual image arrives in the NEXT message."
                )

    async def _handle_close_camera(self, args, loop):
        self.ui.stop_camera_stream()
        return "Camera closed."

    async def _handle_research_agent(self, args, loop):
        """Live research: real browser tabs + human word-by-word typing."""
        self._sys_monitor.set_ai_task(True)
        topic = (args.get("topic") or args.get("query") or "research").strip()
        self.task_update(f"research:{topic[:20]}", 3, "starting")
        try:
            _fn = _import_action("research_agent", "research_agent", "research_agent")
            _prog = lambda pct, d: self.task_update(f"research:{topic[:20]}", pct, d)
            r = await loop.run_in_executor(
                None, lambda: _fn(parameters=args, ui=self.ui, progress=_prog))
            self.task_update(f"research:{topic[:20]}", 100, topic)
            await self._announce(f"[TASK_DONE] Research task on '{topic}' just completed: {r}")
            return r or "Done."
        finally:
            self._sys_monitor.set_ai_task(False)

    def _set_city(self, city: str) -> None:
        """Save default city (or auto-detect via IP) for weather & local answers."""
        try:
            from utils.env import save_config_key
            if city.lower() in ("auto", "detect", "ip"):
                import urllib.request
                req = urllib.request.Request("https://ipinfo.io/json",
                                             headers={"User-Agent": "HanaaiAI/1.0"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    city = json.loads(r.read().decode()).get("city", "") or "auto"
            save_config_key("user_city", city)
            self._user_location = city
            self.ui.eval_js_threadsafe(
                f"if (typeof setLocation === 'function') setLocation('{city}');")
            self.ui.write_log(f"SYS: Default city set → {city} (weather ab isi ka batayegi)")
            try:
                _brain.add_memory(memory_type="FACT",
                    content=f"USER DEFAULT CITY: {city} — set from Settings. Weather/local answers use this.",
                    source="settings", importance=10, tags="location,city,default")
            except Exception:
                pass
        except Exception as e:
            print(f"[Location] set_city failed: {e}")

    async def _fetch_user_location(self) -> None:
        """ipinfo.io → city/country; saved to brain + used for weather."""
        try:
            import urllib.request
            # Settings me saved city hai? wahi use karo (IP detect skip)
            _saved = (load_config() or {}).get("user_city", "")
            if _saved and _saved.lower() not in ("auto", ""):
                self._user_location = _saved
                self.ui.write_log(f"SYS: Saved location — {_saved}")
            else:
                def _get():
                    req = urllib.request.Request("https://ipinfo.io/json",
                                                 headers={"User-Agent": "HanaaiAI/1.0"})
                    with urllib.request.urlopen(req, timeout=8) as r:
                        return json.loads(r.read().decode())
                info = await asyncio.to_thread(_get)
                city = info.get("city", ""); region = info.get("region", "")
                country = info.get("country", ""); loc = info.get("loc", "")
                if city:
                    self._user_location = f"{city}, {country}"
                    self.ui.write_log(f"SYS: Location detected — {self._user_location} ({loc})")
                    try:
                        _brain.add_memory(memory_type="FACT",
                            content=f"USER LOCATION: {city}, {region}, {country} (coords {loc}) — auto-detected via IP.",
                            source="location", importance=9, tags="location,city")
                    except Exception:
                        pass
        except Exception as e:
            print(f"[Location] ⚠️ {e}")

        # Update the settings UI + greet owner with location/time/weather
        try:
            _loc = self._user_location or "unknown"
            self.ui.eval_js_threadsafe(
                f"if (typeof setLocation === 'function') setLocation('{_loc.split(',')[0]}');")
        except Exception:
            pass

        if self._user_location and not getattr(self, "_greeted", False):
            self._greeted = True
            try:
                city = self._user_location.split(",")[0].strip()
                _wx = await asyncio.to_thread(
                    _import_action, "weather_controller", "weather_controller", "weather_controller")
                report = await asyncio.to_thread(
                    lambda: _wx({"action": "current", "city": city}))
                import re as _re
                temp = _re.search(r"Temperature:\s*([\d.]+)", report or "")
                hum = _re.search(r"💧(\d+)%|humidity:\s*(\d+)", report or "", _re.I)
                cond = (report or "").split("\n")
                cond_line = next((l for l in cond if "°C" not in l and l.strip() and not
                                  l.startswith(("📍", "🌡", "💨"))), "")
                _t = temp.group(1) if temp else "?"
                _h = (hum.group(1) or hum.group(2)) if hum else "?"
                await self.session.send_client_content(
                    turns={"parts": [{"text":
                        f"[STARTUP_GREET] You just came online. Greet Saad warmly (Roman Urdu, GF-tone) "
                        f"in 2-3 lines: current time, city {_loc}, temperature {_t}°C, humidity {_h}%, "
                        f"weather: {cond_line[:60]}. Then offer help. Do NOT call any tools."}]},
                    turn_complete=True)
            except Exception as e:
                print(f"[Greet] ⚠️ {e}")

    async def _announce(self, text: str) -> None:
        """Send a short system note to Gemini (safe — user asked for updates)."""
        if not self.session:
            return
        try:
            await self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True,
            )
        except Exception:
            pass

    async def _handle_live_editor(self, args, loop):
        """Live file editor: create + open in Kate + stream into chat."""
        self._sys_monitor.set_ai_task(True)
        fname = (args.get("filename") or "file").strip()
        self.task_update(f"file:{fname[:20]}", 30, "writing")
        try:
            _fn = _import_action("live_editor", "live_editor", "live_editor")
            r = await loop.run_in_executor(None, lambda: _fn(parameters=args, ui=self.ui))
            self.task_update(f"file:{fname[:20]}", 100, fname)
            await self._announce(f"[TASK_DONE] File task '{fname}' completed: {r}")
            return r or "Done."
        finally:
            self._sys_monitor.set_ai_task(False)

    async def _handle_task_status(self, args, loop):
        """Progress of all running tasks — user asks 'kitna hua?'."""
        return self.tasks_snapshot()

    async def _handle_face_manager(self, args, loop):
        self._sys_monitor.set_ai_task(True)
        try:
            action = (args.get("action") or "").strip().lower()
            # Enroll/identify need the V4L2 device — the UI browser preview
            # may be holding it (Linux = ONE consumer). Free it first.
            if action in ("enroll", "identify"):
                try:
                    self.ui.stop_camera_stream()
                except Exception:
                    pass
            _fn = _import_action("face_manager", "face_manager", "face_manager")
            r = await loop.run_in_executor(None, lambda: _fn(parameters=args, player=self.ui))
            return r or "Done."
        finally:
            self._sys_monitor.set_ai_task(False)

    async def _handle_computer_settings(self, args, loop):
        _fn = _import_action("computer_settings", "computer_settings", "computer_settings")
        r = await loop.run_in_executor(None, lambda: _fn(parameters=args, response=None, player=self.ui))
        return r or "Done."

    async def _handle_maps_controller(self, args, loop):
        _fn = _import_action("maps_controller", "maps_controller", "maps_controller")
        r = await loop.run_in_executor(None, lambda: _fn(parameters=args, player=self.ui))
        return r or "Done."

    async def _handle_weather_controller(self, args, loop):
        _fn = _import_action("weather_controller", "weather_controller", "weather_controller")
        r = await loop.run_in_executor(None, lambda: _fn(parameters=args, player=self.ui))
        return r or "Done."

    async def _handle_email_sender(self, args, loop):
        _fn = _import_action("email_sender", "email_sender", "email_sender")
        r = await loop.run_in_executor(None, lambda: _fn(parameters=args, player=self.ui))
        return r or "Done."

    async def _handle_database_controller(self, args, loop):
        _fn = _import_action("database_controller", "database_controller", "database_controller")
        r = await loop.run_in_executor(None, lambda: _fn(parameters=args, player=self.ui))
        return r or "Done."

    async def _handle_desktop_control(self, args, loop):
        _fn = _import_action("desktop", "desktop", "desktop_control")
        r = await loop.run_in_executor(None, lambda: _fn(parameters=args, player=self.ui))
        return r or "Done."

    async def _handle_code_helper(self, args, loop):
        # Mark AI-intensive task so system monitor suppresses false alarms
        self._sys_monitor.set_ai_task(True)
        try:
            _fn = _import_action("code_helper", "code_helper", "code_helper")
            r = await loop.run_in_executor(None, lambda: _fn(parameters=args, player=self.ui, speak=self.speak))
            return r or "Done."
        finally:
            self._sys_monitor.set_ai_task(False)

    async def _handle_dev_agent(self, args, loop):
        # Mark AI-intensive task so system monitor suppresses false alarms
        self._sys_monitor.set_ai_task(True)
        try:
            _fn = _import_action("dev_agent", "dev_agent", "dev_agent")
            r = await loop.run_in_executor(None, lambda: _fn(parameters=args, player=self.ui, speak=self.speak))
            return r or "Done."
        finally:
            self._sys_monitor.set_ai_task(False)

    async def _handle_web_search(self, args, loop):
        _fn = _import_action("web_search", "web_search", "web_search")
        r = await loop.run_in_executor(None, lambda: _fn(parameters=args, player=self.ui))
        result = r or "Done."
        # Mirror results to the on-screen content panel
        _mode = args.get("mode", "search")
        if r and not r.startswith("No results") and not r.startswith("Search failed"):
            _query = args.get("query") or ", ".join(args.get("items", []))
            _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
            self.ui.show_content(_label, r)
        return result

    async def _handle_file_processor(self, args, loop):
        if not args.get("file_path") and self.ui.current_file:
            args["file_path"] = self.ui.current_file
        _fn = _import_action("file_processor", "file_processor", "file_processor")
        r = await loop.run_in_executor(
            None,
            lambda: _fn(parameters=args, player=self.ui, speak=self.speak)
        )
        return r or "Done."

    async def _handle_computer_control(self, args, loop):
        _fn = _import_action("computer_control", "computer_control", "computer_control")
        r = await loop.run_in_executor(None, lambda: _fn(parameters=args, player=self.ui))
        return r or "Done."

    async def _handle_game_updater(self, args, loop):
        _fn = _import_action("game_updater", "game_updater", "game_updater")
        r = await loop.run_in_executor(None, lambda: _fn(parameters=args, player=self.ui, speak=self.speak))
        return r or "Done."

    async def _handle_flight_finder(self, args, loop):
        _fn = _import_action("flight_finder", "flight_finder", "flight_finder")
        r = await loop.run_in_executor(None, lambda: _fn(parameters=args, player=self.ui))
        return r or "Done."

    async def _handle_system_status(self, args, loop):
        r = await loop.run_in_executor(None, get_system_status)
        return str(r)

    async def _handle_shutdown(self, args, loop):
        self.ui.write_log("SYS: Shutdown requested.")
        self.speak("Goodbye, sir.")
        def _shutdown():
            import time, os
            time.sleep(1)
            os._exit(0)
        threading.Thread(target=_shutdown, daemon=True).start()
        return "Done."

    async def _handle_terminal_command(self, args, loop):
        import subprocess
        from utils.env import get_base_dir
        cmd = (args.get("command") or "").strip()
        if not cmd:
            return "No command provided."
        cwd = args.get("cwd") or str(get_base_dir())
        timeout = int(args.get("timeout") or 30)

        # Safety: block destructive commands
        low = cmd.lower()
        if any(b in low for b in ("rm -rf /", "mkfs", "dd if=", ":(){", "shutdown", "halt", "init 0")):
            return f"For safety, this command is blocked: {cmd}"

        def _run():
            try:
                r = subprocess.run(
                    cmd, shell=True, cwd=cwd, capture_output=True,
                    text=True, timeout=timeout,
                )
                out = (r.stdout or "").strip()
                err = (r.stderr or "").strip()
                result = []
                if out: result.append(out)
                if err: result.append(f"[stderr] {err}")
                result.append(f"[exit {r.returncode}]")
                return "\n".join(result)[:4000]
            except subprocess.TimeoutExpired:
                return f"Command timed out after {timeout}s."
            except Exception as e:
                return f"Command failed: {e}"

        r = await loop.run_in_executor(None, _run)
        if self.ui:
            self.ui.write_log(f"[terminal] {cmd[:60]}")
        return r

    async def _handle_recall_memory(self, args, loop):
        """Search persistent memory (brain.db) + conversation_store for relevant past info."""
        query = (args.get("query") or "").strip()
        if not query:
            return "No search query provided."

        # Vague "recent past" queries — user wants history, not keyword match.
        # e.g. "recent activities", "what did we do yesterday", "kal kia tha"
        _vague = any(w in query.lower() for w in (
            "recent", "yesterday", "last time", "before", "what did we",
            "kal", "pichle", "pehle", "activities", "history", "aaj", "today",
        ))

        def _search():
            lines = []
            # 1) Search brain.db (persistent structured memories, FTS5)
            try:
                results = _brain.search_memory(query, limit=5)
                for r in results:
                    tag = f" [{r.get('tags')}]" if r.get("tags") else ""
                    proj = f" (project: {r.get('project')})" if r.get("project") else ""
                    lines.append(f"- (memory) {r.get('content')}{proj}{tag}")
            except Exception:
                pass
            # 2) Search conversation_store (chat history, keyword LIKE)
            try:
                from memory import conversation_store as _cs
                convos = _cs.search_conversations(query, limit=5)
                for c in convos:
                    role = "User" if c["role"] == "user" else "Hanaai"
                    lines.append(f"- ({role} said) {c['content']}")
            except Exception:
                pass
            # 3) Vague query fallback: return recent conversation history
            #    (the user's real intent is "what happened recently")
            if not lines and _vague:
                try:
                    from memory import conversation_store as _cs
                    recent = _cs.get_recent_turns(limit=15)
                    if recent:
                        lines.append("[RECENT ACTIVITY — most recent 15 turns]")
                        lines.append(recent)
                except Exception:
                    pass
            if not lines:
                return "No relevant memories found."
            return "\n".join(lines)
        return await loop.run_in_executor(None, _search)

    async def _handle_memory_command(self, args, loop):
        """Handle explicit remember/forget/recall/export commands."""
        action = (args.get("action") or "").strip().lower()
        def _run():
            if action == "remember":
                content = (args.get("content") or "").strip()
                if not content:
                    return "Nothing to remember."
                mem_type = _brain.classify(content)
                tags = (args.get("tags") or "").strip()
                project = (args.get("project") or "").strip()
                rid = _brain.add_memory(mem_type, content, project=project,
                                        tags=tags, confidence=9, importance=8, verify=True)
                if rid < 0:
                    return "Sorry sir, memory save failed. Please try again."
                try:
                    update_memory({"notes": {content[:40]: {"value": content}}})
                except Exception:
                    pass
                return f"Remembered ({mem_type}): {content}"
            elif action == "forget":
                match = (args.get("content") or args.get("query") or "").strip()
                if not match:
                    return "Specify what to forget."
                n = _brain.forget_memory(match)
                return f"Forgot {n} memor{'y' if n==1 else 'ies'} matching: {match}"
            elif action == "recall":
                query = (args.get("query") or args.get("content") or "").strip()
                if not query:
                    return "No search query."
                results = _brain.search_memory(query, limit=5)
                if not results:
                    return "No relevant memories found."
                return "\n".join(f"- {r.get('content')}" for r in results)
            elif action == "export_txt":
                return _brain.export_txt()
            elif action == "export_pdf":
                return _brain.export_pdf()
            elif action == "backup":
                return _brain.backup()
            elif action == "stats":
                s = _brain.get_stats()
                return (f"Total memories: {s.get('total',0)} | "
                        f"By type: {s.get('by_type',{})} | "
                        f"Projects: {s.get('projects',[])}")
            else:
                return (f"Unknown memory action: {action}. "
                        f"Valid: remember, forget, recall, export_txt, export_pdf, backup, stats")
        return await loop.run_in_executor(None, _run)

    TOOL_REGISTRY = {
        "open_app": _handle_open_app,
        "weather_report": _handle_weather_report,
        "browser_control": _handle_browser_control,
        "file_controller": _handle_file_controller,
        "send_message": _handle_send_message,
        "reminder": _handle_reminder,
        "youtube_video": _handle_youtube_video,
        "screen_process": _handle_screen_process,
        "close_camera": _handle_close_camera,
        "computer_settings": _handle_computer_settings,
        "desktop_control": _handle_desktop_control,
        "code_helper": _handle_code_helper,
        "dev_agent": _handle_dev_agent,
        "web_search": _handle_web_search,
        "file_processor": _handle_file_processor,
        "computer_control": _handle_computer_control,
        "game_updater": _handle_game_updater,
        "flight_finder": _handle_flight_finder,
        "system_status": _handle_system_status,
        "shutdown_ultron": _handle_shutdown,
        "shutdown_jarvis": _handle_shutdown,
        "terminal_command": _handle_terminal_command,
        "recall_memory": _handle_recall_memory,
        "memory_command": _handle_memory_command,
        "maps_controller": _handle_maps_controller,
        "weather_controller": _handle_weather_controller,
        "email_sender": _handle_email_sender,
        "database_controller": _handle_database_controller,
        "face_manager": _handle_face_manager,
        "live_editor": _handle_live_editor,
        "research_agent": _handle_research_agent,
        "task_status": _handle_task_status,
    }

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})

        # SECURE BOOT: no tools until the owner authenticates
        if not self._authenticated:
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": (
                    "SYSTEM LOCKED: Owner authentication pending. "
                    "Tell the user to complete face scan or password login on the lock screen."
                )}
            )

        print(f"[JARVIS] 🔧 {name}  {args}")
        self.set_app_state("THINKING")
        # Animate the matching employee card in the company UI
        self.ui.write_log(f"TOOL_EXEC:{name}")

        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if not self.ui.muted:
                self.set_app_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        loop   = asyncio.get_event_loop()
        result = "Done."

        try:
            handler = self.TOOL_REGISTRY.get(name)
            if handler:
                result = await handler(self, args, loop)
            else:
                result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)

        if not self.ui.muted:
            self.set_app_state("LISTENING")

        print(f"[Hanaai] 📤 {name} → {str(result)[:80]}")
        # Animate employee card → done state in the company UI
        self.ui.write_log(f"TOOL_DONE:{name}")
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result}
        )

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            await self.session.send_realtime_input(media=msg)

    async def _listen_audio(self):
        print("[Hanaai] 🎤 Mic started")
        loop = asyncio.get_event_loop()

        def callback(indata, frames, time_info, status):
            with self._speaking_lock:
                ultron_speaking = self._is_speaking
            if not ultron_speaking and not self.ui.muted and not self._phone_active:
                data = indata.tobytes()
                loop.call_soon_threadsafe(
                    self.out_queue.put_nowait,
                    {"data": data, "mime_type": "audio/pcm"}
                )

        # ── FIX: paInvalidSampleRate — try multiple sample rates ──────
        _mic_opened = False
        for _try_sr in (SEND_SAMPLE_RATE, 48000, 44100, 32000):
            try:
                with sd.InputStream(
                    samplerate=_try_sr,
                    channels=CHANNELS,
                    dtype="int16",
                    blocksize=CHUNK_SIZE,
                    callback=callback,
                ):
                    print(f"[Hanaai] 🎤 Mic stream open @ {_try_sr}Hz")
                    _mic_opened = True
                    # Event-driven keep-alive: long sleep, wake on cancel/error.
                    while True:
                        await asyncio.sleep(3600)   # idle keep-alive
            except Exception as e:
                err_str = str(e).lower()
                if "invalidsampl" in err_str or "samplerate" in err_str:
                    print(f"[Hanaai] ⚠️ Sample rate {_try_sr}Hz not supported, trying next...")
                    continue
                elif "no hardware" in err_str or "device unavailable" in err_str:
                    print(f"[Hanaai] ⚠️ No microphone available — continuing in text mode")
                    # Don't raise — let text mode work
                    while True:
                        await asyncio.sleep(3600)
                else:
                    print(f"[Hanaai] ❌ Mic: {e}")
                    raise
        if not _mic_opened:
            print(f"[Hanaai] ⚠️ Mic could not open at any sample rate — text mode only")
            while True:
                await asyncio.sleep(3600)

    async def _receive_audio(self):
        print("[Hanaai] 👂 Recv started")
        out_buf, in_buf = [], []
        _BUF_LIMIT = 200   # bounded — prevent unbounded memory growth on long turns

        try:
            while True:
                async for response in self.session.receive():

                    if response.data:
                        if self._interrupted:
                            pass  # discard: interrupted
                        else:
                            self._audio_got_this_turn = True  # voice came — no TTS fallback
                            if self._turn_done_event and self._turn_done_event.is_set():
                                self._turn_done_event.clear()
                            # Split into ~50 ms chunks so interrupt() stops audio within 50 ms
                            # (24000 Hz × 2 bytes/sample × 0.05 s = 2400 bytes per slice)
                            _audio_data = response.data
                            _SLICE = 2400
                            for _i in range(0, len(_audio_data), _SLICE):
                                self.audio_in_queue.put_nowait(_audio_data[_i : _i + _SLICE])

                    if response.server_content:
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            chunk = _clean_chunk(sc.output_transcription.text)
                            txt = chunk.strip()
                            if txt and txt != (out_buf[-1] if out_buf else ""):
                                out_buf.append(txt)
                                if len(out_buf) > _BUF_LIMIT:
                                    out_buf = out_buf[-_BUF_LIMIT:]
                                # LIVE: Hanaai's words appear in chat as she speaks
                                self.ui.stream_content(self._asst_name, chunk)

                        if sc.input_transcription and sc.input_transcription.text:
                            chunk = _clean_chunk(sc.input_transcription.text)
                            txt = chunk.strip()
                            if txt:
                                in_buf.append(txt)
                                if len(in_buf) > _BUF_LIMIT:
                                    in_buf = in_buf[-_BUF_LIMIT:]
                                self._last_user_speech = time.monotonic()
                                self.set_app_state("THINKING")
                                # LIVE: user's words appear in chat as they speak
                                self.ui.stream_content("You", chunk)

                        if sc.turn_complete:
                            if self._turn_done_event:
                                self._turn_done_event.set()

                            # If this turn_complete ends an interrupted response, clear the
                            # flag and skip all further processing for that turn.
                            if self._interrupted:
                                self._interrupted = False
                                in_buf  = []
                                out_buf = []
                                # Finalize any partially-streamed bubbles
                                self.ui.stream_end("You")
                                self.ui.stream_end(self._asst_name)
                                continue

                            full_in = " ".join(in_buf).strip()
                            if full_in:
                                self.ui.write_log(f"You: {full_in}")
                                # Finalize the live-streamed user bubble (timestamp)
                                self.ui.stream_end("You")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "user",
                                        "text": full_in,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            in_buf = []

                            full_out = " ".join(out_buf).strip()
                            if full_out:
                                self.ui.write_log(f"{self._asst_name}: {full_out}")
                                # Finalize the live-streamed Hanaai bubble (timestamp)
                                self.ui.stream_end(self._asst_name)
                                # VOICE FALLBACK: server sent text but no audio →
                                # speak locally via espeak-ng so the user ALWAYS hears a reply
                                if not self._audio_got_this_turn and not self.ui.muted:
                                    _say = full_out[:400]
                                    asyncio.create_task(asyncio.to_thread(
                                        subprocess.run,
                                        ["espeak-ng", "-v", "hi", "-s", "150", _say],
                                        capture_output=True,
                                    ))
                                self._audio_got_this_turn = False
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "jarvis",
                                        "text": full_out,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            out_buf = []

                            # ── Archive conversation + auto-classify memory ─────
                            if full_in and full_out:
                                asyncio.create_task(asyncio.to_thread(
                                    _archive_turn, full_in, full_out
                                ))

                            # Vision injection: model finished tool-response turn → now send the image
                            if self._pending_vision and self.session:
                                import base64 as _b64
                                img_b, mime_t, question, angle = self._pending_vision
                                self._pending_vision = None
                                b64 = _b64.b64encode(img_b).decode("ascii")
                                print(f"[Vision] 📤 {len(img_b):,} bytes (angle={angle}) → main session")

                                # Inject the OS window/app list — screenshot only shows
                                # the TOP window (Hanaai is usually fullscreen), so Gemini
                                # needs this to know what ELSE is open on the PC.
                                _ctx = ""
                                if angle == "screen":
                                    try:
                                        _apps_fn = _import_action(
                                            "get_running_apps", "screen_processor", "get_running_apps"
                                        )
                                        _apps = await asyncio.to_thread(_apps_fn)
                                        if _apps:
                                            _ctx = (
                                                "[SCREEN CONTEXT — apps currently open on this PC "
                                                "(some may be BEHIND the visible window)]\n"
                                                f"{_apps}\n\n"
                                            )
                                    except Exception as _e:
                                        print(f"[Vision] ⚠️ app list failed: {_e}")

                                await self.session.send_client_content(
                                    turns={"parts": [
                                        {"inline_data": {"mime_type": mime_t, "data": b64}},
                                        {"text": _ctx + question},
                                    ]},
                                    turn_complete=True,
                                )
                                # Mark next turn_complete behaviour depending on angle
                                if self._vision_cam_active:
                                    # Camera: keep busy until JARVIS finishes speaking the answer
                                    self._vision_cam_active    = False
                                    self._vision_close_pending = True
                                else:
                                    # Screen-only: no camera to close; release busy flag now
                                    self._vision_busy = False
                                    self._sys_monitor.set_ai_task(False)
                            elif self._vision_close_pending:
                                # This turn_complete IS the vision answer — close camera + release busy flag
                                self._vision_close_pending = False
                                self._vision_busy = False
                                self._sys_monitor.set_ai_task(False)
                                async def _cam_close():
                                    await asyncio.sleep(2.0)
                                    self.ui.stop_camera_stream()
                                asyncio.create_task(_cam_close())

                    if response.tool_call:
                        fn_responses = []
                        for fc in response.tool_call.function_calls:
                            print(f"[Hanaai] 📞 {fc.name}")
                            fr = await self._execute_tool(fc)
                            fn_responses.append(fr)
                        await self.session.send_tool_response(
                            function_responses=fn_responses
                        )
        except Exception as e:
            print(f"[Hanaai] ❌ Recv: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[Hanaai] 🔊 Play started")

        def _make_stream():
            """Speaker stream with sample-rate fallback (PipeWire quirks)."""
            last = None
            for sr in (RECEIVE_SAMPLE_RATE, 48000, 44100):
                try:
                    s = sd.RawOutputStream(
                        samplerate=sr, channels=CHANNELS,
                        dtype="int16", blocksize=CHUNK_SIZE,
                    )
                    s.start()
                    print(f"[Hanaai] 🔊 Speaker @ {sr}Hz")
                    return s
                except Exception as e:
                    last = e
            raise last

        stream = _make_stream()
        first_chunk_logged = False
        write_fails = 0

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self.audio_in_queue.get(),
                        timeout=0.02
                    )
                except asyncio.TimeoutError:
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                    ):
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                    continue
                self.set_speaking(True)
                if not first_chunk_logged:
                    first_chunk_logged = True
                    print(f"[Hanaai] 🔊 First audio chunk ({len(chunk):,} bytes) → speaker")
                try:
                    await asyncio.to_thread(stream.write, chunk)
                except (RuntimeError, asyncio.CancelledError):
                    break   # executor shutting down — exit cleanly
                except Exception as e:
                    # Dead/stream-lost speaker (device change, PipeWire restart):
                    # recreate ONCE instead of killing the whole TaskGroup.
                    write_fails += 1
                    print(f"[Hanaai] ⚠️ Speaker write failed ({write_fails}): {e}")
                    if write_fails > 3:
                        break
                    try:
                        stream.stop(); stream.close()
                    except Exception:
                        pass
                    try:
                        stream = _make_stream()
                    except Exception as e2:
                        print(f"[Hanaai] ❌ Speaker recreate failed: {e2}")
                        break
        except Exception as e:
            print(f"[Hanaai] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            try:
                stream.stop(); stream.close()
            except Exception:
                pass

    # ── Morning briefing ────────────────────────────────────────────────────────

    async def _send_startup_briefing(self) -> None:
        """
        Startup briefing:
          Instant greeting & status report (no news prefetching).
        """
        memory   = load_memory()
        identity = memory.get("identity", {})

        def _val(k: str) -> str:
            e = identity.get(k, {})
            return (e.get("value", "") if isinstance(e, dict) else str(e)).strip()

        lang = _val("language")
        name = _val("name")
        time_str = datetime.now().strftime("%H:%M")

        await asyncio.sleep(0.3)
        if not self.session:
            return

        # ── Instant greeting & status ─────────────────────────────────────────
        lang_clause = f" Respond in {lang}." if lang else ""
        name_clause = f" Address the user as {name}." if name else ""
        p1 = (
            f"Say 'Assalamualaikum sir' first, then mention it is {time_str}, state that systems and HUD HUNNY are fully operational, "
            f"and ask how you can assist today. One or two short sentences only. Do not call any tools.{lang_clause}{name_clause}"
        )

        # Clear the turn-done event
        if self._turn_done_event:
            self._turn_done_event.clear()

        await self.session.send_client_content(
            turns={"parts": [{"text": p1}]},
            turn_complete=True,
        )
        self.ui.write_log("SYS: Startup briefing greeting sent.")

    # ── System monitor ──────────────────────────────────────────────────────────

    async def _run_system_monitor(self) -> None:
        """Background system monitor — VISUAL ONLY.

        FIX: [SYSTEM_ALERT] text turns INTERRUPTED Gemini mid-task (user asked
        for a file → CPU spiked during generation → alert hijacked the turn →
        task never ran → alert→think→spike→alert loop). The monitor now only
        updates the UI (state + log). It NEVER sends text to the session.
        """
        emergency_active = False
        while True:
            try:
                await asyncio.sleep(5.0)   # 5s sampling interval
                status = await asyncio.to_thread(self._sys_monitor.check_emergency)
            except (RuntimeError, asyncio.CancelledError, OSError) as e:
                # Event loop shutting down or network failure — stop gracefully
                print(f"[Monitor] Background check aborted (loop shutting down?): {e}")
                return
            except Exception as e:
                print(f"[Monitor] check_emergency error: {e}")
                await asyncio.sleep(10.0)  # back off on error
                continue

            is_90 = status.get("is_emergency_90", False)
            closed = status.get("closed", [])
            cpu = status.get("cpu", 0)
            ram = status.get("ram", 0)

            if is_90 and not emergency_active:
                emergency_active = True
                self.ui.set_state("EMERGENCY")
                self.ui.write_log(f"SYS: High load (CPU: {cpu}%, RAM: {ram}%).")
            elif not is_90 and emergency_active:
                emergency_active = False
                self.ui.set_state("LISTENING" if not self.ui.muted else "MUTED")
                self.ui.write_log("SYS: System load normalized.")

            if closed:
                app_names = ", ".join(closed).replace(".exe", "")
                self.ui.write_log(f"SYS: Overload — closed heavy apps: {app_names}.")

            # NOTE: no `check()` alert loop — the chatty CPU warnings caused
            # the interrupt-spam. Users see load in the footer bar instead.

    # ── Proactive mode ──────────────────────────────────────────────────────────

    async def _run_proactive_mode(self) -> None:
        """
        Background task: periodically checks if the user has been silent long enough,
        then hands time + memory context to Gemini so it can decide what (if anything)
        to say proactively. No hardcoded rules — Gemini makes the call.
        """
        while True:
            await asyncio.sleep(90)   # evaluate every 90s — less idle CPU, still timely

            if not self.session:
                continue

            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue

            if not self._proactive.should_trigger(self._last_user_speech):
                continue

            self._proactive.mark_triggered()

            try:
                memory = await asyncio.to_thread(load_memory)
                prompt = self._proactive.build_prompt(memory)
                await self.session.send_client_content(
                    turns={"parts": [{"text": prompt}]},
                    turn_complete=True,
                )
                self.ui.write_log("SYS: Proactive check-in.")
            except Exception as e:
                print(f"[Proactive] ⚠️ {e}")

    # ── Phone audio relay ────────────────────────────────────────────────────────

    async def _relay_phone_audio(self) -> None:
        """Forward phone mic PCM chunks from dashboard queue into the Gemini Live session."""
        q = self._dashboard._phone_audio_queue
        while True:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # No audio for 1 s → phone mic inactive, give PC mic back
                self._phone_active = False
                continue
            self._phone_active = True   # phone is streaming — silence PC mic
            with self._speaking_lock:
                speaking = self._is_speaking
            if not speaking and not self.ui.muted:
                try:
                    self.out_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass

    def _on_phone_connected(self) -> None:
        self.ui.write_log("SYS: Phone connected via Remote Dashboard.")
        self.ui.notify_phone_connected()

    # ── dashboard command relay ─────────────────────────────────────────────

    async def _process_dashboard_commands(self) -> None:
        import base64
        while True:
            try:
                item = await asyncio.wait_for(
                    self._dashboard._command_queue.get(), timeout=0.02
                )
                if not item:
                    continue
                for _ in range(80):
                    if self.session:
                        break
                    await asyncio.sleep(0.05)
                if self.session:
                    if isinstance(item, dict) and item.get("type") == "image":
                        b64_str = item.get("data", "")
                        mime = item.get("mime", "image/jpeg")
                        img_bytes = base64.b64decode(b64_str)
                        self.ui.write_log("SYS: Image received. Hanaai AI analyzing image...")
                        await self.session.send_realtime_input(media={"data": img_bytes, "mime_type": mime})
                        await self.session.send_client_content(
                            turns={"parts": [{"text": "Please analyze this attached image in full detail, describe every visual element, and explain what it represents."}]},
                            turn_complete=True,
                        )
                    else:
                        text = str(item).strip()
                        if text:
                            if text in ("/toggle_mic", "toggle_mic", "mute", "unmute"):
                                if text == "mute":
                                    self.ui.muted = True
                                elif text == "unmute":
                                    self.ui.muted = False
                                else:
                                    self.ui.muted = not self.ui.muted
                                new_state = "MUTED" if self.ui.muted else "LISTENING"
                                self.set_app_state(new_state)
                                self.ui.write_log(f"SYS: Microphone {'MUTED (OFF)' if self.ui.muted else 'UNMUTED (ON)'}.")
                                continue
                            await self.session.send_client_content(
                                turns={"parts": [{"text": text}]},
                                turn_complete=True,
                            )
                            self.ui.write_log(f"[Web]: {text}")
                else:
                    print(f"[Dashboard] Dropped item (no session)")
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                print(f"[Dashboard] Command error: {e}")
                await asyncio.sleep(0.1)

    # ── main loop ───────────────────────────────────────────────────────────

    async def run(self):
        self._loop = asyncio.get_event_loop()

        # SECURE BOOT: the AI (session, mic, voice) stays completely OFF
        # until the owner finishes face/password verification.
        if not self._authenticated:
            print("[Security] ⏳ AI OFFLINE — waiting for owner login...")
            await asyncio.to_thread(self._auth_event.wait)
            print("[Security] ✅ Owner verified — AI coming online")

        # Start dashboard (optional — needs: pip install fastapi "uvicorn[standard]" cryptography)
        try:
            from dashboard.server import DashboardServer, PORT
            import webbrowser
            self._dashboard = DashboardServer()
            self._dashboard.set_connect_callback(self._on_phone_connected)
            asyncio.create_task(self._dashboard.serve())
            asyncio.create_task(self._process_dashboard_commands())
            # webbrowser.open(f"http://127.0.0.1:{PORT}")
        except Exception as e:
            print(f"[Dashboard] Disabled: {e}")
            self._dashboard = None

        while True:
            try:
                print("[Hanaai] Connecting...")
                self.set_app_state("THINKING")
                config = self._build_config()

                # Fresh client on every reconnect — avoids stale HTTP session state
                client = genai.Client(
                    api_key=_get_api_key(),
                    http_options={"api_version": "v1beta"}
                )

                async with (
                    client.aio.live.connect(model=LIVE_MODEL, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session          = session
                    self.audio_in_queue   = asyncio.Queue()
                    self.out_queue        = asyncio.Queue(maxsize=200)
                    self._turn_done_event = asyncio.Event()

                    # Reset transient state that must not carry over from a previous session
                    self._pending_vision       = None
                    self._vision_cam_active    = False  
                    self._vision_close_pending = False
                    self._vision_busy          = False
                    self._vision_last_time     = 0.0
                    self._interrupted          = False

                    print("[Hanaai] Connected.")
                    self.set_app_state("LISTENING")
                    self.ui.write_log("SYS: Hanaai AI online.")
                    # Real-time location (ipinfo) → weather & local answers
                    tg.create_task(self._fetch_user_location())
                    # Deliver any messages typed while we were connecting
                    self._flush_pending_cmds()

                    if self._dashboard:
                        await self._dashboard.broadcast({"type": "status", "state": "active"})

                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self._run_system_monitor())
                    tg.create_task(self._run_proactive_mode())
                    if self._dashboard:
                        tg.create_task(self._relay_phone_audio())

                    # Wake Word or Morning briefing — fires once per process launch
                    if not self._briefing_sent:
                        self._briefing_sent = True
                        if "--wake-word" in sys.argv:
                            tg.create_task(self.session.send_client_content(
                                turns={"parts": [{"text": "wake up hanaai"}]},
                                turn_complete=True
                            ))
                        elif get_brief_enabled():
                            tg.create_task(self._send_startup_briefing())

            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except BaseException as e:
                # Catches both Exception and BaseExceptionGroup (Python 3.11+
                # TaskGroup raises BaseExceptionGroup when tasks are cancelled
                # externally, which `except Exception` would miss, letting the
                # exception escape the while-loop and causing asyncio.run() to
                # start shutdown — resulting in "executor after shutdown" errors).
                err_str = str(e)
                print(f"[JARVIS] Error ({type(e).__name__}): {e}")
                traceback.print_exc()

                # Invalid / missing / broken API key — stop hammering the API, prompt re-configuration
                if (
                    isinstance(e, ApiKeyMissing)
                    or "API key not valid" in err_str
                    or "1007" in err_str
                ):
                    self.ui.write_log("ERR: API key missing or invalid — please re-enter your key.")
                    self.ui.set_state("SLEEPING")
                    self.ui.prompt_reconfig()
                    while not self.ui._win._ready:
                        await asyncio.sleep(1)
                    print("[JARVIS] New API key saved — reconnecting...")
                    global _cached_api_key
                    _cached_api_key = None   # force re-read of new key on next _get_api_key()
                    _conn_backoff = 3
                    continue

                # Network / timeout errors — log clearly and back off
                is_net_err = any(k in err_str for k in (
                    "TimeoutError", "timed out", "getaddrinfo", "CancelledError",
                    "ConnectionRefusedError", "OSError", "Cannot connect",
                ))
                if is_net_err:
                    _conn_backoff = min(getattr(self, "_conn_backoff", 3) * 2, 60)
                    self._conn_backoff = _conn_backoff
                    self.ui.write_log(
                        f"NET: Bağlantı kurulamadı — {_conn_backoff}s sonra tekrar deneniyor. "
                        "(VPN gerekiyor olabilir)"
                    )
                else:
                    self._conn_backoff = 3
            finally:
                self.session = None

            self.set_speaking(False)
            self.set_app_state("SLEEPING")

            if self._dashboard:
                await self._dashboard.broadcast({"type": "status", "state": "sleeping"})

            delay = getattr(self, "_conn_backoff", 3)
            print(f"[Hanaai] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)

JarvisLive = UltronLive


import socket

_single_instance_sock = None

def _ensure_single_instance():
    global _single_instance_sock
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 39152))
        _single_instance_sock = sock
    except OSError:
        print("[Hanaai] ⚠️ Hanaai AI is already running in another process! Exiting duplicate instance.", file=sys.stderr)
        sys.exit(0)

def main():
    _ensure_single_instance()

    # ── Show startup status screen FIRST (premium boot console) ──────
    startup_results = {}
    try:
        from startup_screen import run_startup
        print("[Hanaai] ┌─────────────────────────────────────┐")
        print("[Hanaai] │  HANAAI AI — SYSTEM INITIALIZATION   │")
        print("[Hanaai] └─────────────────────────────────────┘")
        startup_results = run_startup()
        print(f"[Hanaai] Startup complete:")
        print(f"  Ollama: {startup_results.get('ollama', False)}")
        print(f"  Qwen:   {startup_results.get('qwen', False)}")
        print(f"  Llama:  {startup_results.get('llama', False)}")
        print(f"  Brain:  {startup_results.get('brain', False)}")
        print(f"  Mic:    {startup_results.get('mic', False)}")
        print(f"  Net:    {startup_results.get('net', False)}")
    except Exception as e:
        print(f"[Hanaai] Startup screen skipped ({e}) — launching directly")

    ui = UltronUI("face.png")

    def runner():
        ui.wait_for_api_key()
        ultron = UltronLive(ui)
        try:
            asyncio.run(ultron.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()

if __name__ == "__main__":
    main()