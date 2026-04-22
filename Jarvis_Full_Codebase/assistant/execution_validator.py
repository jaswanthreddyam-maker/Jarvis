from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from assistant.contracts import ActionResult, StepDefinition, StepState, TaskPlan, TaskState
from assistant.tools import app_control


_URL_SURFACES = {
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "open.spotify.com": "spotify",
    "spotify.com": "spotify",
    "github.com": "github",
    "reddit.com": "reddit",
    "mail.google.com": "gmail",
    "google.com": "google",
}


def _normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _surface_from_url(url: str) -> str:
    lowered = _normalize(url)
    for hint, surface in _URL_SURFACES.items():
        if hint in lowered:
            return surface
    return ""


def _surface_from_step(step: StepDefinition | StepState) -> str:
    params = dict(step.params)
    target = _normalize(step.target)
    url = _normalize(str(params.get("url") or params.get("search_url") or ""))
    if _normalize(step.action) in {"search_youtube", "play_youtube"}:
        return "youtube"
    if url:
        return _surface_from_url(url)
    return target


class ExecutionValidator:
    """Validates step completion, active context, and final goal state."""

    def __init__(self, memory, observer: object | None = None) -> None:
        self._memory = memory
        self._observer = observer

    def verify_step(
        self,
        step: StepState,
        result: ActionResult,
    ) -> tuple[bool, str, dict[str, Any]]:
        metadata: dict[str, Any] = {"verified": False}
        if not result.success:
            return False, result.message, metadata

        simulated = bool(result.data.get("simulated"))
        action = _normalize(step.action)
        runtime_observation = self._runtime_observation(
            action=action,
            result_data=result.data,
            expected_state={},
            simulated=simulated,
        )

        # Contradiction Handling: Observed state overrides action success when confidence is high
        if runtime_observation and runtime_observation.get("confidence", 0) > 0.7:
            observed_failure, failure_reason = self._detect_contradiction(step, runtime_observation)
            if observed_failure:
                return False, f"Contradiction: {failure_reason}", metadata

        if runtime_observation:
            metadata["runtime_observation"] = runtime_observation
            self._merge_observation_into_result(step, result, runtime_observation)

        verifier = getattr(self, f"_verify_{action}", None)
        if verifier is None:
            verified = True
            reason = step.verification or "action result accepted"
        else:
            verified, reason = verifier(step, result, simulated=simulated)

        active_app, active_source = self._validated_active_app(step, result, simulated=simulated)
        if active_app:
            metadata["active_app"] = active_app
            metadata["active_app_source"] = active_source

        metadata["verified"] = verified
        metadata["verification_message"] = reason
        metadata["verification_mode"] = "simulated" if simulated else "executed"
        return verified, reason, metadata

    def _detect_contradiction(self, step: StepState, observation: dict[str, Any]) -> tuple[bool, str]:
        """Check if high-confidence observation contradicts action success."""
        action = _normalize(step.action)
        observed_app = _normalize(observation.get("active_app", ""))
        
        if action == "open_app":
            target = _normalize(step.target or str(step.params.get("app_name", "")))
            if target and observed_app and target not in observed_app:
                # If we are 100% sure a different app is focused and target isn't running
                if not app_control.is_app_running(target):
                    return True, f"Action reported success but {target} is not running."
        
        if action == "close_app":
            target = _normalize(step.target or str(step.params.get("app_name", "")))
            if target and app_control.is_app_running(target):
                return True, f"Action reported success but {target} is still running."
                
        return False, ""

    def define_goal_state(self, plan: TaskPlan) -> dict[str, Any]:
        if not plan.steps:
            return {}

        final_step = plan.steps[-1]
        action = _normalize(final_step.action)
        goal_state: dict[str, Any] = {
            "action": action,
            "requires_verified_final_step": True,
        }

        if action == "search_youtube":
            query = str(final_step.params.get("query") or final_step.target).strip()
            goal_state["platform"] = "youtube"
            goal_state["active_app"] = "youtube"
            goal_state["state"] = "results_loaded"
            goal_state["query"] = query
            goal_state["url_contains"] = "youtube.com/results"
        elif action == "play_youtube":
            query = str(final_step.params.get("query") or final_step.target).strip()
            goal_state["platform"] = "youtube"
            goal_state["active_app"] = "youtube"
            goal_state["state"] = "video_playing"
            goal_state["query"] = query
            goal_state["url_contains"] = "youtube.com/watch"
        elif action == "open_url":
            url = str(final_step.params.get("url", "")).strip()
            if url:
                goal_state["url_contains"] = url.lower()
            goal_state["state"] = "page_loaded"
            surface = _surface_from_step(final_step)
            if surface:
                goal_state["platform"] = surface
                goal_state["active_app"] = surface
        elif action == "search_web":
            query = str(final_step.params.get("query") or final_step.target).strip()
            goal_state["platform"] = "google"
            goal_state["state"] = "results_loaded"
            goal_state["query"] = query
            goal_state["url_contains"] = "google.com/search"
            browser_app = _normalize(str(final_step.params.get("browser_app", "")))
            if browser_app:
                goal_state["browser_app"] = browser_app
        elif action in {"open_app", "focus_app"}:
            target_app = _normalize(str(final_step.params.get("app_name") or final_step.target))
            if target_app:
                goal_state["state"] = "app_running"
                goal_state["active_app"] = target_app
        elif action == "close_app":
            target_app = _normalize(str(final_step.params.get("app_name") or final_step.target))
            if target_app:
                goal_state["state"] = "app_closed"
                goal_state["closed_app"] = target_app

        return goal_state

    def goal_progress(
        self,
        plan: TaskPlan,
        *,
        task: TaskState,
        workflow_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        if not task.steps:
            return {
                "achieved": not bool(plan.steps),
                "completed_steps": 0,
                "total_steps": len(plan.steps),
                "checks": {},
                "message": "No execution steps were required." if not plan.steps else "No task steps were recorded.",
            }

        final_step = task.steps[-1]
        final_result = dict(final_step.result)
        goal_state = dict(plan.goal_state or self.define_goal_state(plan))
        runtime_observation = self._runtime_observation(
            action=str(goal_state.get("action", "")),
            result_data=final_result,
            expected_state=goal_state,
            simulated=bool(final_result.get("simulated")),
        )
        if runtime_observation:
            final_result = self._merged_result_data(final_result, runtime_observation)
        observation_payload = dict(runtime_observation or final_result.get("runtime_observation") or {})
        observation_mode = _normalize(str(observation_payload.get("observation_mode", "")))
        if observation_mode == "runtime":
            observed_url = _normalize(str(observation_payload.get("url") or final_result.get("observed_url") or ""))
            observed_app = _normalize(
                str(
                    observation_payload.get("active_app")
                    or final_result.get("observed_active_app")
                    or ""
                )
            )
            observed_browser = _normalize(
                str(
                    observation_payload.get("browser_app")
                    or final_result.get("observed_browser_app")
                    or ""
                )
            )
            observed_playback_state = _normalize(
                str(
                    observation_payload.get("playback_state")
                    or final_result.get("observed_playback_state")
                    or ""
                )
            )
            observed_window_title = str(
                observation_payload.get("window_title") or final_result.get("window_title", "")
            ).strip()
        else:
            observed_url = _normalize(
                str(final_result.get("url") or final_result.get("search_url") or final_result.get("observed_url") or "")
            )
            observed_app = _normalize(
                str(
                    workflow_snapshot.get("active_app")
                    or final_result.get("active_app")
                    or final_result.get("observed_active_app")
                    or final_result.get("app_name")
                    or final_result.get("browser_app")
                    or ""
                )
            )
            observed_browser = _normalize(str(final_result.get("browser_app") or final_result.get("observed_browser_app") or ""))
            observed_playback_state = _normalize(
                str(final_result.get("playback_state") or final_result.get("observed_playback_state") or "")
            )
            observed_window_title = str(final_result.get("window_title", "")).strip()
        expected_app = _normalize(str(goal_state.get("active_app", "")))
        expected_browser = _normalize(str(goal_state.get("browser_app", "")))
        required_url = _normalize(str(goal_state.get("url_contains", "")))
        expected_query = str(goal_state.get("query", "")).strip()
        expected_query_fragment = quote_plus(expected_query).lower()
        state = _normalize(str(goal_state.get("state", "")))
        if observation_mode == "runtime":
            normalized_window_title = _normalize(observed_window_title)
            expected_surface = _surface_from_url(required_url)
            confirmed_surface = False
            if expected_app and observed_app == expected_app:
                confirmed_surface = True
            elif expected_browser and observed_browser == expected_browser and not expected_app:
                confirmed_surface = True
            elif expected_app and expected_app in normalized_window_title:
                confirmed_surface = True
            elif expected_surface and expected_surface in normalized_window_title:
                confirmed_surface = True
            elif expected_surface and expected_surface in observed_url:
                confirmed_surface = True

            prepared_url = _normalize(
                str(final_result.get("url") or final_result.get("search_url") or final_result.get("observed_url") or "")
            )
            if confirmed_surface and prepared_url:
                if not observed_url:
                    observed_url = prepared_url
                elif required_url and required_url not in observed_url:
                    observed_url = prepared_url
                elif expected_query_fragment and expected_query_fragment not in observed_url:
                    observed_url = prepared_url

            if confirmed_surface and not observed_browser:
                observed_browser = _normalize(
                    str(final_result.get("browser_app") or final_result.get("observed_browser_app") or "")
                )

        checks: dict[str, bool] = {
            "final_step_verified": not goal_state.get("requires_verified_final_step") or bool(final_result.get("verified")),
        }
        if expected_app:
            checks["active_app"] = observed_app == expected_app
        if expected_browser:
            checks["browser_app"] = observed_browser == expected_browser
        if required_url:
            checks["url_contains"] = required_url in observed_url
        if expected_query_fragment and state in {"results_loaded", "video_playing"}:
            checks["query_matches"] = expected_query_fragment in observed_url
        if state == "video_playing":
            checks["video_playing"] = (
                observed_playback_state == "playing"
                and ("youtube.com/watch" in observed_url or "youtu.be/" in observed_url)
            )
        elif state == "results_loaded":
            checks["results_loaded"] = bool(observed_url) and checks.get("url_contains", True)
        elif state == "page_loaded":
            checks["page_loaded"] = bool(observed_url)
        elif state == "app_running" and expected_app:
            checks["app_running"] = observed_app == expected_app

        closed_app = _normalize(str(goal_state.get("closed_app", "")))
        if closed_app:
            checks["app_closed"] = not app_control.is_app_running(closed_app)

        achieved = all(checks.values()) if checks else True
        first_failed = next((name for name, ok in checks.items() if not ok), "")
        message = "Goal state achieved." if achieved else self._goal_failure_message(first_failed, goal_state)
        return {
            "achieved": achieved,
            "completed_steps": sum(1 for step in task.steps if step.status == "completed"),
            "total_steps": len(task.steps),
            "goal_state": goal_state,
            "observed": {
                "active_app": observed_app,
                "browser_app": observed_browser,
                "url": observed_url,
                "playback_state": observed_playback_state,
                "window_title": observed_window_title,
                "observation_mode": str(observation_payload.get("observation_mode", "")),
            },
            "checks": checks,
            "message": message,
        }

    def goal_achieved(
        self,
        plan: TaskPlan,
        *,
        task: TaskState,
        workflow_snapshot: dict[str, Any],
    ) -> tuple[bool, str]:
        if not plan.steps:
            return True, "No execution steps were required."

        if not task.steps:
            return False, "The plan did not produce any executable task steps."

        # Temporal Verification: Re-check state with retries before confirming failure
        import time
        max_retries = 3
        retry_delay = 1.0
        
        for attempt in range(max_retries):
            progress = self.goal_progress(plan, task=task, workflow_snapshot=workflow_snapshot)
            if progress["achieved"]:
                return True, str(progress["message"])
            
            # If confidence is low, wait and retry
            obs = progress.get("observed", {})
            confidence = obs.get("confidence", 0) if isinstance(obs, dict) else 0
            if confidence < 0.6 and attempt < max_retries - 1:
                time.sleep(retry_delay * (attempt + 1))
                continue
            else:
                break

        return bool(progress["achieved"]), str(progress["message"])

    def _verify_remember_fact(self, step: StepState, result: ActionResult, *, simulated: bool) -> tuple[bool, str]:
        del result, simulated
        query = step.target or str(step.params.get("content", "")).strip()
        namespace = str(step.params.get("namespace", "system"))
        records = self._memory.recall(query=query, namespace=namespace, limit=1)
        return bool(records), step.verification or "memory lookup"

    @staticmethod
    def _verify_recall_memory(step: StepState, result: ActionResult, *, simulated: bool) -> tuple[bool, str]:
        del step, simulated
        return "matches" in result.data, "memory lookup completed"

    @staticmethod
    def _verify_open_url(step: StepState, result: ActionResult, *, simulated: bool) -> tuple[bool, str]:
        del simulated
        expected_url = str(step.params.get("url", "")).strip().lower()
        observed_url = str(result.data.get("url", "")).strip().lower()
        if not observed_url:
            return False, step.verification or "url missing"
        if expected_url and expected_url != observed_url:
            return False, "The opened URL did not match the requested target."
        return True, step.verification or "url prepared"

    @staticmethod
    def _verify_search_web(step: StepState, result: ActionResult, *, simulated: bool) -> tuple[bool, str]:
        del simulated
        query = str(step.params.get("query", "")).strip()
        search_url = str(result.data.get("search_url", "")).strip().lower()
        expected_query = quote_plus(query)
        if "google.com/search" not in search_url:
            return False, "The web search URL was not prepared."
        if expected_query and expected_query.lower() not in search_url:
            return False, "The search URL does not contain the requested query."
        return True, step.verification or "search URL prepared"

    @staticmethod
    def _verify_search_youtube(step: StepState, result: ActionResult, *, simulated: bool) -> tuple[bool, str]:
        del simulated
        query = str(step.params.get("query", "")).strip()
        search_url = str(result.data.get("search_url", "")).strip().lower()
        expected_query = quote_plus(query)
        if "youtube.com/results" not in search_url:
            return False, "The YouTube search URL was not prepared."
        if expected_query and expected_query.lower() not in search_url:
            return False, "The YouTube search URL does not contain the requested query."
        return True, step.verification or "YouTube search prepared"

    @staticmethod
    def _verify_play_youtube(step: StepState, result: ActionResult, *, simulated: bool) -> tuple[bool, str]:
        del simulated
        query = str(step.params.get("query", "")).strip()
        observed_url = str(result.data.get("video_url") or result.data.get("url") or "").strip().lower()
        expected_query = quote_plus(query).lower()
        playback_state = _normalize(str(result.data.get("playback_state", "")))
        if not observed_url:
            return False, "No YouTube playback URL was prepared."
        if expected_query and expected_query not in observed_url and not result.data.get("video_url"):
            return False, "The YouTube playback request does not contain the requested query."
        if playback_state != "playing" or ("youtube.com/watch" not in observed_url and "youtu.be/" not in observed_url):
            return False, "YouTube playback was requested, but a playing video was not verified."
        return True, step.verification or "YouTube playback verified"

    @staticmethod
    def _verify_open_app(step: StepState, result: ActionResult, *, simulated: bool) -> tuple[bool, str]:
        app_name = _normalize(str(result.data.get("app_name") or step.params.get("app_name") or step.target))
        if simulated:
            return bool(app_name), "simulated application launch accepted"
        if app_control.is_app_running(app_name):
            return True, step.verification or "application launched"
        return False, "The application launch could not be confirmed."

    @staticmethod
    def _verify_focus_app(step: StepState, result: ActionResult, *, simulated: bool) -> tuple[bool, str]:
        app_name = _normalize(str(result.data.get("app_name") or step.params.get("app_name") or step.target))
        if simulated:
            return bool(app_name), "simulated focus accepted"
        if bool(result.data.get("window_title")) or app_control.is_app_running(app_name):
            return True, step.verification or "application focused"
        return bool(result.data.get("app_name")), "focus request acknowledged"

    @staticmethod
    def _verify_close_app(step: StepState, result: ActionResult, *, simulated: bool) -> tuple[bool, str]:
        app_name = _normalize(str(result.data.get("app_name") or step.params.get("app_name") or step.target))
        if simulated:
            return bool(app_name), "simulated close accepted"
        if not app_control.is_app_running(app_name):
            return True, step.verification or "application closed"
        return False, "The application still appears to be running."

    @staticmethod
    def _verify_create_file(step: StepState, result: ActionResult, *, simulated: bool) -> tuple[bool, str]:
        return ExecutionValidator._verify_path_exists(step, result, simulated=simulated)

    @staticmethod
    def _verify_overwrite_file(step: StepState, result: ActionResult, *, simulated: bool) -> tuple[bool, str]:
        return ExecutionValidator._verify_path_exists(step, result, simulated=simulated)

    @staticmethod
    def _verify_path_exists(step: StepState, result: ActionResult, *, simulated: bool) -> tuple[bool, str]:
        del step
        path = str(result.data.get("path", "")).strip()
        if simulated:
            return bool(path), "simulated file update accepted"
        return bool(path) and Path(path).exists(), "file exists"

    @staticmethod
    def _verify_read_file(step: StepState, result: ActionResult, *, simulated: bool) -> tuple[bool, str]:
        del step
        path = str(result.data.get("path", "")).strip()
        if simulated:
            return bool(path), "simulated file read accepted"
        return bool(path) and "content" in result.data, "file contents returned"

    @staticmethod
    def _verify_delete_file(step: StepState, result: ActionResult, *, simulated: bool) -> tuple[bool, str]:
        del step
        path = str(result.data.get("path", "")).strip()
        if simulated:
            return bool(path), "simulated delete accepted"
        return bool(path) and not Path(path).exists(), "file deleted"

    @staticmethod
    def _verify_set_volume(step: StepState, result: ActionResult, *, simulated: bool) -> tuple[bool, str]:
        del step, simulated
        return bool(result.message.strip()), "volume command completed"

    @staticmethod
    def _verify_open_explorer(step: StepState, result: ActionResult, *, simulated: bool) -> tuple[bool, str]:
        del step, simulated
        return bool(result.message.strip()), "explorer opened"

    @staticmethod
    def _verify_get_clipboard(step: StepState, result: ActionResult, *, simulated: bool) -> tuple[bool, str]:
        del step, simulated
        return "text" in result.data, "clipboard text returned"

    @staticmethod
    def _verify_set_clipboard(step: StepState, result: ActionResult, *, simulated: bool) -> tuple[bool, str]:
        del step, simulated
        return bool(result.message.strip()), "clipboard text updated"

    @staticmethod
    def _verify_minimize_window(step: StepState, result: ActionResult, *, simulated: bool) -> tuple[bool, str]:
        del step, simulated
        return bool(result.message.strip()), "window minimized"

    @staticmethod
    def _verify_switch_window(step: StepState, result: ActionResult, *, simulated: bool) -> tuple[bool, str]:
        del step, simulated
        return bool(result.data.get("window_title")) or bool(result.message.strip()), "window switched"

    @staticmethod
    def _verify_close_active_window(step: StepState, result: ActionResult, *, simulated: bool) -> tuple[bool, str]:
        del step, simulated
        return bool(result.message.strip()), "close request sent"

    @staticmethod
    def _verify_set_reminder(step: StepState, result: ActionResult, *, simulated: bool) -> tuple[bool, str]:
        del step, simulated
        return bool(result.data.get("due_at")), "reminder scheduled"

    @staticmethod
    def _verify_get_time(step: StepState, result: ActionResult, *, simulated: bool) -> tuple[bool, str]:
        del step, simulated
        return bool(result.data.get("time")), "local time returned"

    @staticmethod
    def _verify_health_check(step: StepState, result: ActionResult, *, simulated: bool) -> tuple[bool, str]:
        del step, simulated
        return "offline_mode" in result.data, "health payload returned"

    @staticmethod
    def _verify_report_capabilities(step: StepState, result: ActionResult, *, simulated: bool) -> tuple[bool, str]:
        del step, simulated
        return bool(result.message.strip()), "capabilities reported"

    @staticmethod
    def _verify_install_app(step: StepState, result: ActionResult, *, simulated: bool) -> tuple[bool, str]:
        if simulated:
            return True, "simulated install accepted"
        app_name = _normalize(str(result.data.get("app_name") or step.params.get("app_name") or step.target))
        return bool(app_name) and bool(result.message.strip()), step.verification or "installation completed"

    @staticmethod
    def _validated_active_app(
        step: StepState,
        result: ActionResult,
        *,
        simulated: bool,
    ) -> tuple[str, str]:
        data = dict(result.data)
        action = _normalize(step.action)

        url = str(data.get("search_url") or data.get("url") or step.params.get("url") or "").strip()
        surface = _surface_from_url(url)
        if surface:
            return surface, "url"

        browser_app = _normalize(str(data.get("browser_app") or step.params.get("browser_app") or ""))
        if browser_app and (simulated or app_control.is_app_running(browser_app)):
            return browser_app, "system_state"

        app_name = _normalize(str(data.get("app_name") or step.params.get("app_name") or step.target))
        if action in {"open_app", "focus_app"} and app_name and (simulated or app_control.is_app_running(app_name)):
            return app_name, "system_state"

        return "", ""

    def _runtime_observation(
        self,
        *,
        action: str,
        result_data: dict[str, Any],
        expected_state: dict[str, Any],
        simulated: bool,
    ) -> dict[str, Any]:
        if simulated:
            return {}
        observer = self._observer
        if observer is None or not hasattr(observer, "capture"):
            return {}
        try:
            observation = observer.capture(
                action=action,
                result_data=result_data,
                expected_state=expected_state,
                simulated=simulated,
            )
        except Exception:
            return {}
        return observation if isinstance(observation, dict) else {}

    @staticmethod
    def _merge_observation_into_result(
        step: StepState,
        result: ActionResult,
        observation: dict[str, Any],
    ) -> None:
        merged = ExecutionValidator._merged_result_data(dict(result.data), observation)
        if step.action == "search_web" and merged.get("observed_url"):
            merged.setdefault("search_url", merged["observed_url"])
        elif step.action == "search_youtube" and merged.get("observed_url"):
            merged.setdefault("search_url", merged["observed_url"])
        elif step.action in {"open_url", "play_youtube"} and merged.get("observed_url"):
            merged.setdefault("url", merged["observed_url"])
        if merged.get("observed_playback_state"):
            merged.setdefault("playback_state", merged["observed_playback_state"])
        if merged.get("observed_active_app"):
            merged.setdefault("active_app", merged["observed_active_app"])
        result.data.update(merged)

    @staticmethod
    def _merged_result_data(result_data: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
        merged = dict(result_data)
        merged["runtime_observation"] = observation
        if observation.get("window_title"):
            merged.setdefault("window_title", observation["window_title"])
        if observation.get("active_app"):
            merged.setdefault("observed_active_app", observation["active_app"])
        if observation.get("browser_app"):
            merged.setdefault("observed_browser_app", observation["browser_app"])
        if observation.get("url"):
            merged.setdefault("observed_url", observation["url"])
        if observation.get("playback_state"):
            merged.setdefault("observed_playback_state", observation["playback_state"])
        return merged

    @staticmethod
    def _goal_failure_message(failed_check: str, goal_state: dict[str, Any]) -> str:
        state = _normalize(str(goal_state.get("state", "")))
        if failed_check == "final_step_verified":
            return "The final step completed, but its result was not verified."
        if failed_check == "active_app":
            expected = str(goal_state.get("active_app") or "the expected app")
            return f"The goal state expected {expected}, but the active context did not confirm it."
        if failed_check == "browser_app":
            expected = str(goal_state.get("browser_app") or "the expected browser")
            return f"The goal state expected browser {expected}, but the result did not confirm it."
        if failed_check == "url_contains":
            if state == "video_playing":
                return "I could not confirm that a YouTube video watch page opened."
            return "The expected goal URL was not confirmed by the final step result."
        if failed_check == "query_matches":
            return "The final URL did not confirm the requested query."
        if failed_check == "video_playing":
            return "I could not confirm that YouTube video playback actually started."
        if failed_check == "app_closed":
            return f"{goal_state.get('closed_app', 'The app')} still appears to be running."
        return "The final observed state did not match the requested goal."

    def suggest_recovery(self, progress: dict[str, Any]) -> str:
        """Planner Integration: Suggest recovery strategies based on confidence."""
        obs = progress.get("observed", {})
        confidence = obs.get("confidence", 0)
        
        if confidence < 0.3:
            return "low_confidence_retry"
        if not progress.get("checks", {}).get("active_app", True):
            return "refocus_target_app"
        if not progress.get("checks", {}).get("url_contains", True):
            return "reopen_url"
        return "replan_task"
