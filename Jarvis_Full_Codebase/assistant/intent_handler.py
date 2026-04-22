import os
import webbrowser
import re
import shutil
import logging
import json
import time
import uuid
from dataclasses import dataclass, field
from difflib import get_close_matches, SequenceMatcher
from typing import List, Dict, Optional, Any, Tuple, Set
from enum import Enum

logger = logging.getLogger("Jarvis.ProductionAgent")

# --- 6. FAILURE CLASSIFICATION ---
class FailureType(Enum):
    NONE = "NONE"
    APP_NOT_FOUND = "APP_NOT_FOUND"
    NETWORK_ERROR = "NETWORK_ERROR"
    INTENT_ERROR = "INTENT_ERROR"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    USER_INTERRUPT = "USER_INTERRUPT"
    CONFIRM_TIMEOUT = "CONFIRM_TIMEOUT"

# --- 5. EXECUTION AUDIT TRAIL ---
@dataclass
class AuditStep:
    session_id: str
    step_id: int
    intent: str
    target: str
    timestamp: float
    status: str
    failure_type: FailureType = FailureType.NONE

# --- 3. SESSION-BASED CONTEXT ---
@dataclass
class ContextItem:
    app: str
    timestamp: float

@dataclass
class SessionContext:
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    apps: List[ContextItem] = field(default_factory=list)
    last_intent: Optional[str] = None
    last_query: Optional[str] = None
    pending_action: Optional[dict] = None
    opened_by_jarvis: Set[str] = field(default_factory=set)
    audit_trail: List[AuditStep] = field(default_factory=list)
    
    def add_audit(self, intent: str, target: str, status: str, failure_type: FailureType = FailureType.NONE):
        step = AuditStep(
            session_id=self.session_id,
            step_id=len(self.audit_trail) + 1,
            intent=intent,
            target=target,
            timestamp=time.time(),
            status=status,
            failure_type=failure_type
        )
        self.audit_trail.append(step)

# --- 7. ALIAS MEMORY CONTROL ---
class AliasManager:
    def __init__(self, file_path: str, max_aliases: int = 50):
        self.path = file_path
        self.max_aliases = max_aliases
        self.aliases = self._load()

    def _load(self) -> Dict[str, str]:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r") as f:
                    return json.load(f)
            except (OSError, json.JSONDecodeError): pass
        return {"wapp": "whatsapp", "yt": "youtube"}

    def add(self, alias: str, target: str):
        self.aliases[alias] = target
        # Pruning strategy: Remove oldest entries if limit exceeded
        if len(self.aliases) > self.max_aliases:
            keys = list(self.aliases.keys())
            for i in range(len(keys) - self.max_aliases):
                del self.aliases[keys[i]]
        self.save()

    def save(self):
        with open(self.path, "w") as f:
            json.dump(self.aliases, f)

# Global Managers
ALIAS_FILE = os.path.join(os.path.dirname(__file__), "aliases.json")
alias_mgr = AliasManager(ALIAS_FILE)
sessions: Dict[str, SessionContext] = {}

def get_session(session_id: Optional[str] = None) -> SessionContext:
    if not session_id or session_id not in sessions:
        new_session = SessionContext()
        sessions[new_session.session_id] = new_session
        return new_session
    return sessions[session_id]

# --- 2. REAL-TIME INTERRUPT SYSTEM ---
interrupt_flag = False

def check_interrupt():
    global interrupt_flag
    if interrupt_flag:
        raise InterruptedError("Global interrupt triggered.")

# --- CORE INTENT LOGIC ---

def parse_intent(text: str, session_id: Optional[str] = None) -> List[dict]:
    """
    Production-Grade Parser: Handles 4. Proactive Disambiguation and session isolation.
    """
    ctx = get_session(session_id)
    normalized = text.lower().strip()
    
    # 1. Strict Confirmation Check
    if normalized in ["yes", "confirm", "yes do it"]:
        if ctx.pending_action:
            if time.time() - ctx.pending_action["timestamp"] < 10:
                return [{"intent": "execute_pending", "data": ctx.pending_action["data"], "confidence": 1.0}]
            else:
                ctx.pending_action = None
                return [{"intent": "error", "type": FailureType.CONFIRM_TIMEOUT, "message": "Confirmation expired."}]

    # Disambiguation Logic: Find all possible intents
    possible_intents = []
    
    if any(k in normalized for k in ["youtube", "yt", "video", "play"]):
        possible_intents.append({"intent": "search_youtube", "confidence": 0.9})
    if any(k in normalized for k in ["google", "search", "find"]):
        possible_intents.append({"intent": "search_google", "confidence": 0.8})
    if any(k in normalized for k in ["open", "launch", "start"]):
        possible_intents.append({"intent": "open_app", "confidence": 0.85})
    if any(k in normalized for k in ["shutdown", "restart"]):
        possible_intents.append({"intent": "system_action", "confidence": 1.0})

    # 4. Proactive Disambiguation: If multiple valid paths, return options
    if len(possible_intents) > 1 and "search" in normalized:
        return [{"intent": "disambiguate", "options": possible_intents, "raw": text}]

    if not possible_intents:
        return [{"intent": "search_google", "confidence": 0.4, "raw": text}]

    # Process most likely intent
    best = max(possible_intents, key=lambda x: x["confidence"])
    words = normalized.split()
    target = ""
    
    if best["intent"] in ["open_app", "search_youtube"]:
        candidates = [w for w in words if len(w) > 2 and w not in ["search", "open", "youtube"]]
        if candidates:
            raw_target = candidates[-1]
            target = alias_mgr.aliases.get(raw_target, raw_target)
            # Safe alias learning threshold (0.85)
            matches = get_close_matches(target, ["whatsapp", "chrome", "notepad", "vlc", "spotify"], n=1, cutoff=0.85)
            if matches and target != matches[0]:
                alias_mgr.add(target, matches[0])
                target = matches[0]

    return [{
        "intent": best["intent"],
        "target": target,
        "query": " ".join(words),
        "confidence": best["confidence"],
        "session_id": ctx.session_id,
        "raw": text
    }]

def execute_command(intent_data: dict) -> Tuple[bool, str, FailureType]:
    """
    Executes command with 5. Audit Trail and 6. Failure Classification.
    """
    ctx = get_session(intent_data.get("session_id"))
    intent = intent_data.get("intent")
    target = intent_data.get("target")
    
    check_interrupt() # 2. Real-time check

    if intent == "disambiguate":
        return False, f"Multiple options found: {', '.join([o['intent'] for o in intent_data['options']])}", FailureType.INTENT_ERROR

    if intent == "system_action":
        ctx.pending_action = {"data": intent_data, "timestamp": time.time()}
        return False, "Confirmation required (10s limit).", FailureType.NONE

    if intent == "execute_pending":
        return execute_command(intent_data["data"])

    try:
        if intent == "open_app" and target:
            path = shutil.which(target)
            if not path:
                ctx.add_audit(intent, target, "FAILED", FailureType.APP_NOT_FOUND)
                return False, f"App {target} not found.", FailureType.APP_NOT_FOUND
            
            os.startfile(path)
            ctx.opened_by_jarvis.add(target)
            ctx.add_audit(intent, target, "SUCCESS")
            return True, f"Launched {target}", FailureType.NONE

        elif intent == "search_youtube":
            webbrowser.open(f"https://www.youtube.com/results?search_query={intent_data['query'].replace(' ', '+')}")
            ctx.add_audit(intent, "youtube", "SUCCESS")
            return True, "YouTube search opened.", FailureType.NONE

    except Exception as e:
        ctx.add_audit(intent, target or "unknown", "FAILED", FailureType.PERMISSION_DENIED)
        return False, str(e), FailureType.PERMISSION_DENIED

    return False, "Failed", FailureType.INTENT_ERROR

def split_commands(text: str) -> List[str]:
    """Splits multi-command input by common separators."""
    # Split by 'and', 'then', or 'followed by'
    delimiters = r"\s+and\s+|\s+then\s+|\s+followed by\s+|[,;]"
    commands = [c.strip() for c in re.split(delimiters, text, flags=re.IGNORECASE) if c.strip()]
    return commands if commands else [text]