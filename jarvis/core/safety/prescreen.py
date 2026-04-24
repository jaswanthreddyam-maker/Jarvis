from __future__ import annotations

import re


class SafetyPreScreen:
    def __init__(self) -> None:
        self._blacklisted_patterns = [
            re.compile(r"rm\s+-rf\s+/", re.IGNORECASE),
            re.compile(r"drop\s+table\s+", re.IGNORECASE),
            re.compile(r"mkfs\.", re.IGNORECASE),
            # Add more dangerous commands or known exploits as needed
        ]
        
        self._sensitive_data_patterns = [
            # Example: basic SSN or credit card structure prevention
            re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        ]

    def check(self, text: str) -> tuple[bool, str | None]:
        """Returns (is_safe, reason_if_not)"""
        if not text:
            return True, None
            
        for pattern in self._blacklisted_patterns:
            if pattern.search(text):
                return False, "Input matches blacklisted pattern."
                
        for pattern in self._sensitive_data_patterns:
            if pattern.search(text):
                return False, "Input contains potentially sensitive data."
                
        return True, None
