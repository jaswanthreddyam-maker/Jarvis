"""Privacy Guard — filters and redacts sensitive data before online execution.

Ensures that PII, passwords, system tokens, or sensitive API keys
are stripped or blocked before ever leaving the local machine.
"""
from __future__ import annotations

import re
import logging

logger = logging.getLogger("Jarvis.PrivacyGuard")

class PrivacyGuard:
    def __init__(self):
        # Basic patterns for sensitive data
        self.patterns = [
            (re.compile(r'\b(?:\d[ -]*?){13,16}\b'), "[REDACTED_CREDIT_CARD]"),
            (re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,7}\b'), "[REDACTED_EMAIL]"),
            (re.compile(r'(?i)(password|passwd|pwd|secret|key|token)[\s:=]+([^\s]+)'), r'\1 [REDACTED]'),
            (re.compile(r'\b(?:\d{3}-\d{2}-\d{4}|\d{9})\b'), "[REDACTED_SSN]"),
        ]
        
        # Context-aware triggers that elevate a query's risk level
        self.context_triggers = ["login", "bank", "financial", "auth", "credentials", "private"]

    def _has_sensitive_context(self, text: str) -> bool:
        lower_text = text.lower()
        return any(trigger in lower_text for trigger in self.context_triggers)

    def is_safe(self, text: str) -> bool:
        """Check if text contains explicit high-risk system commands."""
        unsafe_keywords = ["sudo", "rm -rf", "format", "del /f", "diskpart"]
        lower_text = text.lower()
        if any(keyword in lower_text for keyword in unsafe_keywords):
            return False
            
        # If the query is heavily loaded with sensitive context but has no explicit pattern matches,
        # we still flag it as potentially unsafe for online reflection.
        if self._has_sensitive_context(text) and "send" in lower_text:
            logger.warning("Context-aware privacy block triggered.")
            return False
            
        return True

    def redact(self, text: str) -> str:
        """Redact known PII patterns from text."""
        redacted_text = text
        for pattern, replacement in self.patterns:
            redacted_text = pattern.sub(replacement, redacted_text)
        
        if redacted_text != text:
            logger.info("PrivacyGuard redacted sensitive patterns from input.")
            
        return redacted_text

guard = PrivacyGuard()
