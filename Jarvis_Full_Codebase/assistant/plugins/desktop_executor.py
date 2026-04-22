from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

try:
    import pyautogui
    pyautogui.FAILSAFE = True
except ImportError:
    pyautogui = None  # type: ignore[assignment]

logger = logging.getLogger("Jarvis.DesktopExecutor")


# ─── Data types ──────────────────────────────────────────────────────
@dataclass(slots=True)
class StepResult:
    """Result of a single desktop step execution."""
    success: bool
    message: str
    step_index: int = 0
    action: str = ""


@dataclass(slots=True)
class ExecutionReport:
    """Complete report for a desktop workflow execution."""
    success: bool
    step_results: list[StepResult] = field(default_factory=list)
    interrupted: bool = False
    window_mismatch: bool = False

    @property
    def log_lines(self) -> list[str]:
        return [r.message for r in self.step_results]


# ─── Desktop Executor ────────────────────────────────────────────────
class DesktopExecutor:
    """
    Low-level executor for desktop automation workflows.

    Design principles:
        - No raw absolute coordinates — uses relative moves or image detection.
        - Validates the active window before every step.
        - Supports fixed delays and condition-based waits (window title, URL).
        - Interrupt-safe: calling `interrupt()` stops execution cleanly
          between steps.
        - Emits per-step callbacks so the UI layer can display progress.
    """

    def __init__(self) -> None:
        self._interrupted = threading.Event()
        self._last_active_window: str = ""
        self._step_callback: Callable[[int, int, str, str], None] | None = None
        # Assets directory for image-detection clicks
        self._assets_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            os.pardir,
            os.pardir,
            "assets",
        )

    # ── Public controls ──────────────────────────────────────────────
    def interrupt(self) -> None:
        """Signal the executor to stop after the current step."""
        self._interrupted.set()

    def reset(self) -> None:
        """Clear the interrupt flag so a new workflow can run."""
        self._interrupted.clear()

    @property
    def is_interrupted(self) -> bool:
        return self._interrupted.is_set()

    def set_step_callback(
        self, callback: Callable[[int, int, str, str], None]
    ) -> None:
        """
        Register a callback fired before each step.
        Signature: callback(step_index, total_steps, action, description)
        """
        self._step_callback = callback

    # ── Window management ────────────────────────────────────────────
    def get_active_window(self) -> str:
        """Return the title of the current foreground window (Windows OS)."""
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value

            if title and "Jarvis" not in title and title != "overlay":
                self._last_active_window = title
                return title
            elif self._last_active_window:
                return f"{self._last_active_window} (Jarvis focused)"
            return "Unknown Window"
        except Exception:
            return "Unknown Window"

    def _validate_window(self, expected: str) -> tuple[bool, str]:
        """
        Verify that the active window matches *expected*.
        Returns (ok, current_title).
        """
        current = self.get_active_window()
        if expected.lower() not in current.lower():
            return False, current
        return True, current

    # ── Wait primitives ──────────────────────────────────────────────
    def wait_fixed(self, seconds: float) -> StepResult:
        """Block for a fixed number of seconds, interruptible."""
        end = time.time() + seconds
        while time.time() < end:
            if self._interrupted.is_set():
                return StepResult(
                    success=False, message="Wait interrupted."
                )
            time.sleep(min(0.25, end - time.time()))
        return StepResult(success=True, message=f"Waited {seconds:.1f}s.")

    def wait_for_window(
        self, title: str, timeout: float = 10.0, poll: float = 0.5
    ) -> StepResult:
        """Poll until a window whose title contains *title* is in the foreground."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._interrupted.is_set():
                return StepResult(
                    success=False, message="Window wait interrupted."
                )
            if title.lower() in self.get_active_window().lower():
                return StepResult(
                    success=True, message=f"Window '{title}' detected."
                )
            time.sleep(poll)
        return StepResult(
            success=False,
            message=f"Timeout ({timeout}s) waiting for window '{title}'.",
        )

    def wait_for_url(
        self, url_fragment: str, timeout: float = 10.0, poll: float = 0.5
    ) -> StepResult:
        """
        Poll until the active window title contains *url_fragment*.

        Note: Browsers typically include URLs/domains in their title bar.
        For deeper URL inspection, an accessibility bridge or browser
        extension would be needed — this is a best-effort heuristic.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._interrupted.is_set():
                return StepResult(
                    success=False, message="URL wait interrupted."
                )
            current = self.get_active_window()
            if url_fragment.lower() in current.lower():
                return StepResult(
                    success=True,
                    message=f"URL fragment '{url_fragment}' detected in '{current}'.",
                )
            time.sleep(poll)
        return StepResult(
            success=False,
            message=f"Timeout ({timeout}s) waiting for URL containing '{url_fragment}'.",
        )

    # ── Core execution ───────────────────────────────────────────────
    def execute(
        self,
        steps: list[dict[str, Any]],
        expected_window: str | None = None,
    ) -> ExecutionReport:
        """
        Execute a list of desktop action steps.

        Each step is a dict with at least an ``"action"`` key.
        Supported actions:
            type            — type text via keyboard
            press           — hotkey combo (e.g. "ctrl,c")
            click           — click at the current cursor position
            move_rel        — move cursor relative to its current position
            click_image     — find an image on screen and click its center
            scroll          — scroll the mouse wheel
            wait            — fixed delay
            wait_for_window — wait until a window title matches
            wait_for_url    — wait until window title contains URL fragment
        """
        self.reset()
        report = ExecutionReport(success=True)
        total = len(steps)

        for idx, step in enumerate(steps):
            # ── Interrupt check ──
            if self._interrupted.is_set():
                report.step_results.append(
                    StepResult(
                        success=False,
                        message="Execution interrupted cleanly.",
                        step_index=idx,
                        action="interrupt",
                    )
                )
                report.interrupted = True
                report.success = False
                break

            # ── Window validation ──
            window_check = expected_window or step.get("expected_window")
            if window_check:
                ok, current = self._validate_window(window_check)
                if not ok:
                    msg = (
                        f"Window mismatch at step {idx}: expected "
                        f"'{window_check}', got '{current}'. Aborting."
                    )
                    report.step_results.append(
                        StepResult(
                            success=False,
                            message=msg,
                            step_index=idx,
                            action="validation",
                        )
                    )
                    report.window_mismatch = True
                    report.success = False
                    break

            action = step.get("action", "")

            # ── Step callback for UI feedback ──
            desc = step.get("description", action)
            if self._step_callback:
                try:
                    self._step_callback(idx, total, action, desc)
                except Exception as cb_err:
                    logger.warning("Step callback error: %s", cb_err)

            # ── Pre-wait ──
            pre_wait = step.get("pre_wait")
            if pre_wait:
                wait_result = self._handle_wait_condition(pre_wait)
                if not wait_result.success:
                    wait_result.step_index = idx
                    wait_result.action = f"pre_wait({action})"
                    report.step_results.append(wait_result)
                    report.success = False
                    break

            # ── Execute the action ──
            try:
                result = self._dispatch_action(idx, step)
            except Exception as exc:
                result = StepResult(
                    success=False,
                    message=f"Error at step {idx} ('{action}'): {exc}",
                    step_index=idx,
                    action=action,
                )

            report.step_results.append(result)
            if not result.success:
                report.success = False
                break

            # ── Post-wait ──
            post_wait = step.get("post_wait")
            if post_wait:
                wait_result = self._handle_wait_condition(post_wait)
                if not wait_result.success:
                    wait_result.step_index = idx
                    wait_result.action = f"post_wait({action})"
                    report.step_results.append(wait_result)
                    report.success = False
                    break

        return report

    # ── Action dispatch ──────────────────────────────────────────────
    _DESTRUCTIVE_COMBOS = {
        frozenset({"ctrl", "alt", "del"}),
        frozenset({"alt", "f4"}),
    }

    _BLOCKED_SOLO_KEYS = frozenset()

    def _dispatch_action(self, idx: int, step: dict[str, Any]) -> StepResult:
        """Route a single step to the right handler."""
        action = step.get("action", "")

        if action == "type":
            return self._action_type(idx, step)
        if action == "press":
            return self._action_press(idx, step)
        if action == "click":
            return self._action_click(idx, step)
        if action == "move_rel":
            return self._action_move_rel(idx, step)
        if action == "click_image":
            return self._action_click_image(idx, step)
        if action == "scroll":
            return self._action_scroll(idx, step)
        if action == "wait":
            delay = float(step.get("delay", 1.0))
            r = self.wait_fixed(delay)
            r.step_index = idx
            r.action = action
            return r
        if action == "wait_for_window":
            r = self.wait_for_window(
                step.get("title", ""),
                float(step.get("timeout", 10.0)),
            )
            r.step_index = idx
            r.action = action
            return r
        if action == "wait_for_url":
            r = self.wait_for_url(
                step.get("url_fragment", ""),
                float(step.get("timeout", 10.0)),
            )
            r.step_index = idx
            r.action = action
            return r

        return StepResult(
            success=False,
            message=f"Unknown action '{action}' at step {idx}.",
            step_index=idx,
            action=action,
        )

    # ── Individual actions ───────────────────────────────────────────
    def _action_type(self, idx: int, step: dict) -> StepResult:
        text = step.get("text", "")
        interval = float(step.get("interval", 0.03))
        pyautogui.write(text, interval=interval)
        return StepResult(True, f"Typed: '{text}'", idx, "type")

    def _action_press(self, idx: int, step: dict) -> StepResult:
        keys_raw = step.get("keys", "")
        keys = [k.strip().lower() for k in keys_raw.split(",")]

        # Safety: block destructive combos
        key_set = frozenset(keys)
        for combo in self._DESTRUCTIVE_COMBOS:
            if combo.issubset(key_set):
                return StepResult(
                    False,
                    f"Blocked destructive key combo: {keys_raw}.",
                    idx,
                    "press",
                )

        pyautogui.hotkey(*keys)
        return StepResult(True, f"Pressed: {keys_raw}", idx, "press")

    def _action_click(self, idx: int, step: dict) -> StepResult:
        button = step.get("button", "left")
        clicks = int(step.get("clicks", 1))
        pyautogui.click(button=button, clicks=clicks)
        return StepResult(True, f"Clicked {button} ({clicks}x)", idx, "click")

    def _action_move_rel(self, idx: int, step: dict) -> StepResult:
        dx = int(step.get("x", 0))
        dy = int(step.get("y", 0))
        duration = float(step.get("duration", 0.25))
        pyautogui.moveRel(dx, dy, duration=duration)
        return StepResult(True, f"Moved relative ({dx}, {dy})", idx, "move_rel")

    def _action_click_image(self, idx: int, step: dict) -> StepResult:
        image_name = step.get("image_name", "")
        confidence = float(step.get("confidence", 0.8))

        # Resolve path relative to assets dir
        img_path = image_name
        if not os.path.isabs(image_name):
            candidate = os.path.normpath(
                os.path.join(self._assets_dir, image_name)
            )
            if os.path.exists(candidate):
                img_path = candidate

        if not os.path.exists(img_path):
            return StepResult(
                False,
                f"Image file '{image_name}' not found.",
                idx,
                "click_image",
            )

        loc = pyautogui.locateCenterOnScreen(img_path, confidence=confidence)
        if loc is None:
            return StepResult(
                False,
                f"Image '{image_name}' not visible on screen.",
                idx,
                "click_image",
            )

        pyautogui.click(loc)
        return StepResult(
            True, f"Clicked image '{image_name}' at {loc}", idx, "click_image"
        )

    def _action_scroll(self, idx: int, step: dict) -> StepResult:
        amount = int(step.get("amount", -3))
        pyautogui.scroll(amount)
        return StepResult(True, f"Scrolled {amount}", idx, "scroll")

    # ── Wait condition handler ───────────────────────────────────────
    def _handle_wait_condition(self, cond: dict[str, Any]) -> StepResult:
        """Dispatch a wait condition dict."""
        wtype = cond.get("wait_type", "none")
        if wtype == "none":
            return StepResult(True, "No wait required.")
        if wtype == "fixed_delay":
            return self.wait_fixed(float(cond.get("value", 1.0)))
        if wtype == "window_title":
            return self.wait_for_window(
                cond.get("value", ""),
                float(cond.get("timeout", 10.0)),
                float(cond.get("poll_interval", 0.5)),
            )
        if wtype == "url_contains":
            return self.wait_for_url(
                cond.get("value", ""),
                float(cond.get("timeout", 10.0)),
                float(cond.get("poll_interval", 0.5)),
            )
        return StepResult(False, f"Unknown wait type: {wtype}")
