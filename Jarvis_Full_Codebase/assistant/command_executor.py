"""Safe Command Execution Engine.

Detects OS, validates commands, maps package managers, and executes safely.
Blocks destructive patterns like rm -rf, mkfs, etc.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import ctypes
import logging
import time
import sys
from typing import Any

from assistant.trust_manager import trust_manager

logger = logging.getLogger("Jarvis.CommandExecutor")

# Known package managers by OS
PACKAGE_MANAGERS = {
    "Windows": "winget",
    "Linux": "apt", # Or pacman, dnf (simplified for now)
    "Darwin": "brew"
}

# Strict Allowlist for executables
ALLOWED_EXECUTABLES = {
    "winget", "apt", "apt-get", "brew", "echo", "start", "python", 
    "ls", "dir", "mkdir", "git", "npm", "pip", "node"
}

# Blocked argument flags
BLOCKED_ARGS = {
    "-c", "--eval", "-e", "--allow-all", "--no-verify"
}

# Dangerous command patterns
BLOCKED_PATTERNS = [
    re.compile(r"\brm\s+-rf\b", re.I),
    re.compile(r"\bmkfs\b", re.I),
    re.compile(r"\bformat\b", re.I),
    re.compile(r"\bdel\s+/f\s+/s\s+/q\b", re.I),
    re.compile(r">\s*/dev/sda", re.I),
    re.compile(r"\bdd\s+if=.*of=/dev/", re.I),
]

class CommandExecutor:
    
    def __init__(self) -> None:
        self.os_name = platform.system()
        self.pkg_manager = PACKAGE_MANAGERS.get(self.os_name, "unknown")

    def _is_admin(self) -> bool:
        """Detect if the current process has administrative/root privileges."""
        try:
            if self.os_name == "Windows":
                return ctypes.windll.shell32.IsUserAnAdmin() != 0
            else:
                return os.geteuid() == 0
        except Exception:
            return False

    def _requires_admin(self, command: str) -> bool:
        """Detect if a command inherently requires elevated privileges."""
        cmd_lower = command.lower()
        if "apt-get install" in cmd_lower or "apt install" in cmd_lower:
            return True
        if self.os_name == "Windows" and "winget install" in cmd_lower and "--machine" in cmd_lower:
            return True
        # For our purposes, block format/disk ops entirely, but if allowed they would require admin
        return False

    def relaunch_as_admin(self) -> bool:
        """Attempt to relaunch the current Jarvis process with Administrator privileges."""
        if self._is_admin():
            return True
            
        try:
            if self.os_name == "Windows":
                # Uses ShellExecuteW to prompt UAC
                ret = ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", sys.executable, " ".join(sys.argv), None, 1
                )
                return int(ret) > 32 # ShellExecute returns > 32 on success
            else:
                logger.warning("Auto-relaunch as root not supported on Linux/Mac. Please run via sudo.")
                return False
        except Exception as e:
            logger.error("Failed to relaunch as admin: %s", e)
            return False

    def classify_error(self, stderr: str) -> str:
        """Categorize errors for better failure handling and UX."""
        err_lower = stderr.lower()
        if "permission denied" in err_lower or "run as administrator" in err_lower or "access is denied" in err_lower:
            return "PERMISSION_ERROR"
        if "not found" in err_lower or "no packages found" in err_lower or "unrecognized" in err_lower:
            return "MISSING_PACKAGE"
        if "timeout" in err_lower or "timed out" in err_lower or "connection refused" in err_lower:
            return "NETWORK_ERROR"
        return "UNKNOWN_ERROR"

    def is_safe(self, command: str) -> tuple[bool, str]:
        """Check if a command is safe to execute using allowlists and blocklists."""
        cmd_stripped = command.strip()
        if not cmd_stripped:
            return False, "Empty command"
            
        if self._requires_admin(command) and not self._is_admin():
            return False, "This command requires Administrator/Root privileges, which Jarvis currently lacks."
            
        # Parse executable
        executable = cmd_stripped.split()[0].lower()
        if executable not in ALLOWED_EXECUTABLES:
            return False, f"Executable '{executable}' is not in the allowlist."
            
        args_str = command[len(executable):].strip()
        args_list = args_str.split()
        
        # Check blocked arguments
        for arg in args_list:
            if arg in BLOCKED_ARGS:
                return False, f"Blocked argument flag detected: {arg}"
                
        # Context-specific argument validation
        if executable == "python" and ("-c" in args_list or "-m" in args_list):
            # Only allow script files
            return False, "Python must be run with a specific script file, not -c or -m flags."
            
        if executable == "npm" and len(args_list) > 1 and args_list[0] == "run":
            # Very strict NPM check
            known_npm_scripts = {"start", "build", "test", "dev"}
            if args_list[1] not in known_npm_scripts:
                return False, f"Unknown npm script: {args_list[1]}"

        # Check blocked patterns
        for pattern in BLOCKED_PATTERNS:
            if pattern.search(command):
                return False, f"Command matches blocked destructive pattern: {pattern.pattern}"
                
        # Check system critical paths (simplistic)
        if "C:\\Windows\\System32" in command and "del" in command.lower():
            return False, "Modifying System32 is forbidden."
            
        if self.os_name == "Linux" and "/etc/" in command and "rm" in command:
            return False, "Modifying /etc/ is forbidden."

        return True, "Safe"

    def dry_run(self, command: str) -> str:
        """Simulate execution and return a step-by-step plan."""
        safe, reason = self.is_safe(command)
        if not safe:
            return f"[DRY-RUN BLOCKED] {reason}"
            
        executable = command.split()[0].lower()
        args = command[len(executable):].strip()
        
        plan = f"Execution Plan for `{command}`:\n"
        plan += f"1. Verified executable '{executable}' against allowlist.\n"
        plan += f"2. Scanned arguments '{args}' against destructive patterns (Passed).\n"
        plan += f"3. OS Target: {self.os_name}\n"
        if executable in PACKAGE_MANAGERS.values() or executable == "apt-get":
            plan += f"4. Will attempt to install/manage packages securely via {executable}.\n"
        else:
            plan += f"4. Will spawn subprocess '{executable}' with standard privileges.\n"
            
        return plan

    def generate_install_command(self, app_name: str) -> str | None:
        """Generate the appropriate package manager command."""
        if self.pkg_manager == "winget":
            # Just a heuristic, winget search usually requires exact ID but install works with names if unique
            return f"winget install --exact {app_name} --accept-source-agreements --accept-package-agreements"
        elif self.pkg_manager == "apt":
            return f"sudo apt-get install -y {app_name}"
        elif self.pkg_manager == "brew":
            return f"brew install {app_name}"
        return None

    def generate_uninstall_command(self, app_name: str) -> str | None:
        """Generate the appropriate package manager uninstall command for rollback."""
        if self.pkg_manager == "winget":
            return f"winget uninstall {app_name}"
        elif self.pkg_manager == "apt":
            return f"sudo apt-get remove -y {app_name}"
        elif self.pkg_manager == "brew":
            return f"brew uninstall {app_name}"
        return None
        
    def verify_execution(self, command: str, output: str) -> bool:
        """Verify that the execution actually succeeded beyond just returncode 0."""
        output_lower = output.lower()
        fail_markers = ["not found", "error", "failed", "unrecognized", "cannot find", "no packages found"]
        
        if any(marker in output_lower for marker in fail_markers) and "successfully" not in output_lower:
            return False
            
        # System state checks: If installing an app, verify its binary exists
        if "install" in command.lower():
            # Extract likely app name
            parts = command.split()
            # E.g., 'winget install --exact git ...'
            app_name = None
            for i, p in enumerate(parts):
                if p in ["install", "add"] and i + 1 < len(parts):
                    # Skip flags
                    idx = i + 1
                    while idx < len(parts) and parts[idx].startswith("-"):
                        idx += 1
                    if idx < len(parts):
                        app_name = parts[idx]
                        break
                        
            if app_name:
                # Handle PATH propagation delays with timed intervals
                for attempt in range(5):
                    time.sleep(1) # wait 1s per interval
                    if shutil.which(app_name):
                        logger.info("System state verified: Binary %s found in PATH.", app_name)
                        return True
                    logger.debug("Binary %s not found on attempt %d", app_name, attempt + 1)
                
                # If we couldn't verify the binary, but output implies success, we cautiously accept it
                logger.info("Could not verify binary '%s' in PATH after 5s, relying on command output.", app_name)
            
        return True

    def execute(self, command: str) -> tuple[bool, str]:
        """Execute a validated command."""
        safe, reason = self.is_safe(command)
        if not safe:
            logger.warning("Blocked unsafe command: %s (%s)", command, reason)
            return False, f"Execution blocked: {reason}"
            
        logger.info("Executing safe command: %s", command)
        try:
            # Safer subprocess execution with process isolation where possible
            if self.os_name == "Windows":
                # CREATE_NEW_PROCESS_GROUP allows sending CTRL_BREAK_EVENT
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                creationflags = 0
                
            process = subprocess.Popen(
                command, 
                shell=True, 
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=creationflags
            )
            
            try:
                out, err = process.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                logger.error("Command timed out: %s", command)
                # Safely kill the hung process and all its children with fallback force-kill
                try:
                    if self.os_name == "Windows":
                        process.send_signal(subprocess.signal.CTRL_BREAK_EVENT)
                        # Fallback strict taskkill for detached trees
                        subprocess.run(f"taskkill /F /T /PID {process.pid}", shell=True, capture_output=True)
                    else:
                        process.kill()
                        subprocess.run(f"kill -9 {process.pid}", shell=True, capture_output=True)
                except Exception as kill_err:
                    logger.error("Error during force-kill: %s", kill_err)
                    
                process.communicate() # flush pipes
                return False, "Execution timed out and process was forcefully killed."
            
            if process.returncode == 0:
                logger.info("Command succeeded.")
                return True, out.strip()
            else:
                logger.error("Command failed: %s", err.strip())
                err_class = self.classify_error(err.strip() or out.strip())
                return False, f"[{err_class}] {err.strip()}"
                
        except Exception as e:
            logger.error("Command error: %s", e)
            return False, str(e)

# Singleton
command_executor = CommandExecutor()
