from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from assistant.actions.base import ActionContext
from assistant.actions.registry import ActionRegistry
from assistant.cancellation_controller import CancelledError
from assistant.contracts import ActionResult
from assistant.tools import ToolResult
from assistant.tool_result_wrapper import wrap_tool_result
from assistant.tools import app_control, file_ops, system_control, web_control, window_control


@dataclass(slots=True)
class SystemPlugin:
    name: str = "system"

    def register(self, registry: ActionRegistry) -> None:
        registry.register("get_time", self._get_time, "Return the local system time.")
        registry.register(
            "open_url",
            self._open_url,
            "Open a URL in the browser.",
            required_params=("url",),
        )
        registry.register(
            "search_web",
            self._search_web,
            "Search the web in a browser tab.",
            required_params=("query",),
        )
        registry.register(
            "search_youtube",
            self._search_youtube,
            "Search for content on YouTube.",
            required_params=("query",),
        )
        registry.register(
            "play_youtube",
            self._play_youtube,
            "Open YouTube playback for a query or video URL.",
            required_params=("query",),
        )
        registry.register(
            "system_action",
            self._system_action,
            "Execute a system-level action like shutdown or volume control.",
            required_params=("action_type",),
        )

        registry.register(
            "open_app",
            self._open_app,
            "Launch a desktop application.",
            required_params=("app_name",),
        )
        registry.register(
            "focus_app",
            self._focus_app,
            "Bring an application window to the foreground.",
            required_params=("app_name",),
        )
        registry.register(
            "close_app",
            self._close_app,
            "Close an application window or process.",
            required_params=("app_name",),
        )
        registry.register(
            "create_file",
            self._create_file,
            "Create a text file in an allowed workspace path.",
            required_params=("name",),
        )
        registry.register(
            "overwrite_file",
            self._overwrite_file,
            "Overwrite an existing text file in an allowed workspace path.",
            required_params=("name", "content"),
        )
        registry.register(
            "read_file",
            self._read_file,
            "Read a text file from an allowed workspace path.",
            required_params=("name",),
        )
        registry.register(
            "delete_file",
            self._delete_file,
            "Delete a file from an allowed workspace path.",
            required_params=("name",),
        )
        registry.register(
            "set_volume",
            self._set_volume,
            "Adjust the system volume.",
            required_params=("value",),
        )
        registry.register(
            "open_explorer",
            self._open_explorer,
            "Open a folder in File Explorer.",
        )
        registry.register(
            "get_clipboard",
            self._get_clipboard,
            "Read the current clipboard text.",
        )
        registry.register(
            "set_clipboard",
            self._set_clipboard,
            "Replace the current clipboard text.",
            required_params=("text",),
        )
        registry.register(
            "minimize_window",
            self._minimize_window,
            "Minimize the active window.",
        )
        registry.register(
            "switch_window",
            self._switch_window,
            "Switch to a window matching a title.",
            required_params=("title",),
        )
        registry.register(
            "close_active_window",
            self._close_active_window,
            "Request that the active window closes.",
        )
        registry.register(
            "install_app",
            self._install_app,
            "Install an application using winget.",
            required_params=("app_name",),
        )
        registry.register(
            "remember_fact",
            self._remember_fact,
            "Persist a fact into long-term memory.",
            required_params=("content",),
        )
        registry.register("recall_memory", self._recall_memory, "Look up stored facts.")
        registry.register(
            "set_reminder",
            self._set_reminder,
            "Schedule a reminder event.",
            required_params=("message", "delay_seconds"),
        )
        registry.register("health_check", self._health_check, "Report runtime health and safety mode.")
        registry.register(
            "report_capabilities",
            self._report_capabilities,
            "Describe the current control-plane capabilities.",
        )

    @staticmethod
    def _get_time(params: dict[str, object], context: ActionContext) -> ActionResult:
        del params, context
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return ActionResult(success=True, message=f"The local time is {now}.", data={"time": now})

    @staticmethod
    def _truthy(value: object) -> bool:
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _should_simulate(params: dict[str, object], context: ActionContext | None = None) -> bool:
        return (
            bool(getattr(getattr(context, "settings", None), "simulate_actions", False))
            or SystemPlugin._truthy(params.get("simulate", False))
            or SystemPlugin._truthy(params.get("dry_run", False))
        )

    @staticmethod
    def _simulated_result(action: str, target: str, **data: object) -> ActionResult:
        return ActionResult(
            success=True,
            message=f"Prepared {action} for {target}.",
            data={**data, "simulated": True, "execution_state": "simulated"},
        )

    @staticmethod
    def _from_tool_result(
        action_name: str,
        result: ToolResult,
        *,
        target: str = "",
        **extra_data: object,
    ) -> ActionResult:
        return wrap_tool_result(
            action_name,
            result,
            target=target,
            **{**extra_data, "simulated": False, "execution_state": "executed"},
        )

    @staticmethod
    def _raise_if_cancelled(context: ActionContext, message: str = "Request cancelled.") -> None:
        token = getattr(context, "cancellation_token", None)
        if token is not None:
            token.raise_if_cancelled(message)

    @staticmethod
    def _run_cancellable_process(
        command: list[str],
        *,
        token,
    ) -> subprocess.CompletedProcess[str]:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if token is not None:
            token.track_subprocess(process)

        while True:
            if token is not None and token.is_cancelled():
                raise CancelledError("Cancelled the running system action.")
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                return subprocess.CompletedProcess(command, process.returncode, stdout or "", stderr or "")
            time.sleep(0.05)

    @staticmethod
    def _open_url(params: dict[str, object], context: ActionContext) -> ActionResult:
        url = str(params["url"]).strip()
        browser_app = str(params.get("browser_app", "")).strip()
        SystemPlugin._raise_if_cancelled(context, f"Cancelled opening {url}.")
        if SystemPlugin._should_simulate(params, context):
            return SystemPlugin._simulated_result(
                "open_url",
                url,
                url=web_control.normalize_url(url),
                browser_app=browser_app,
            )
        return SystemPlugin._from_tool_result(
            "open_url",
            web_control.open_url(url, browser_app=browser_app or None),
            target=url,
        )

    @staticmethod
    def _search_web(params: dict[str, object], context: ActionContext) -> ActionResult:
        query = str(params["query"]).strip()
        browser_app = str(params.get("browser_app", "")).strip()
        SystemPlugin._raise_if_cancelled(context, f"Cancelled searching for {query}.")
        if SystemPlugin._should_simulate(params, context):
            search_url = f"https://www.google.com/search?q={query.replace(' ', '+')}"
            return SystemPlugin._simulated_result(
                "search_web",
                query,
                query=query,
                search_url=search_url,
                browser_app=browser_app,
            )
        return SystemPlugin._from_tool_result(
            "search_web",
            web_control.search_google(query, browser_app=browser_app or None),
            target=query,
        )


    @staticmethod
    def _search_youtube(params: dict[str, object], context: ActionContext) -> ActionResult:
        query = str(params["query"]).strip()
        browser_app = str(params.get("browser_app", "")).strip()
        SystemPlugin._raise_if_cancelled(context, f"Cancelled YouTube search for {query}.")
        if SystemPlugin._should_simulate(params, context):
            search_url = f"https://www.youtube.com/results?search_query={query.replace(' ', '+')}"
            return SystemPlugin._simulated_result(
                "search_youtube",
                query,
                query=query,
                search_url=search_url,
                browser_app=browser_app,
            )
        return SystemPlugin._from_tool_result(
            "search_youtube",
            web_control.search_youtube(query, browser_app=browser_app or None),
            target=query,
        )


    @staticmethod
    def _play_youtube(params: dict[str, object], context: ActionContext) -> ActionResult:
        query = str(params["query"]).strip()
        browser_app = str(params.get("browser_app", "")).strip()
        video_url = str(params.get("video_url", "")).strip()
        SystemPlugin._raise_if_cancelled(context, f"Cancelled YouTube playback for {query}.")
        if SystemPlugin._should_simulate(params, context):
            target_url = video_url or f"https://www.youtube.com/results?search_query={query.replace(' ', '+')}"
            data = {
                "query": query,
                "url": target_url,
                "browser_app": browser_app,
                "playback_state": "requested",
            }
            if video_url:
                data["video_url"] = target_url
            else:
                data["search_url"] = target_url
            return SystemPlugin._simulated_result("play_youtube", query, **data)
        return SystemPlugin._from_tool_result(
            "play_youtube",
            web_control.play_youtube(query, browser_app=browser_app or None, video_url=video_url or None),
            target=query,
        )


    @staticmethod
    def _system_action(params: dict[str, object], context: ActionContext) -> ActionResult:
        action_type = str(params["action_type"]).strip().lower()
        SystemPlugin._raise_if_cancelled(context, f"Cancelled system action {action_type}.")

        if action_type not in {"shutdown", "restart", "reboot"}:
            return ActionResult(
                success=False,
                message=f"Unsupported system action: {action_type}",
                error="unsupported_system_action",
            )

        if not SystemPlugin._truthy(params.get("_confirmed_system_action", False)):
            return ActionResult(
                success=False,
                message="Confirmation required before running a system power action.",
                error="confirmation_required",
                data={"action_type": action_type, "needs_confirmation": True},
            )

        if SystemPlugin._should_simulate(params, context):
             return SystemPlugin._simulated_result("system_action", action_type)

        command = ["shutdown", "/r" if action_type in {"restart", "reboot"} else "/s", "/t", "0"]
        try:
            subprocess.Popen(command, shell=False)
        except OSError as exc:
            return ActionResult(
                success=False,
                message=f"Failed to execute system action {action_type}: {exc}",
                error="system_action_failed",
                data={"action_type": action_type},
            )
        return ActionResult(
            success=True,
            message=f"Executed system action: {action_type}",
            data={"action_type": action_type},
        )


    @staticmethod
    def _open_app(params: dict[str, object], context: ActionContext) -> ActionResult:
        app_name = str(params["app_name"]).strip().lower()
        SystemPlugin._raise_if_cancelled(context, f"Cancelled opening {app_name}.")

        if SystemPlugin._should_simulate(params, context):
            return SystemPlugin._simulated_result("open_app", app_name, app_name=app_name)

        return SystemPlugin._from_tool_result("open_app", app_control.open_app(app_name), target=app_name)


    @staticmethod
    def _focus_app(params: dict[str, object], context: ActionContext) -> ActionResult:
        app_name = str(params["app_name"]).strip()
        SystemPlugin._raise_if_cancelled(context, f"Cancelled focusing {app_name}.")
        if SystemPlugin._should_simulate(params, context):
            return SystemPlugin._simulated_result("focus_app", app_name, app_name=app_name)
        return SystemPlugin._from_tool_result(
            "focus_app",
            app_control.focus_app(app_name),
            target=app_name,
            app_name=app_name,
        )

    @staticmethod
    def _close_app(params: dict[str, object], context: ActionContext) -> ActionResult:
        app_name = str(params["app_name"]).strip()
        SystemPlugin._raise_if_cancelled(context, f"Cancelled closing {app_name}.")
        if SystemPlugin._should_simulate(params, context):
            return SystemPlugin._simulated_result("close_app", app_name, app_name=app_name)
        return SystemPlugin._from_tool_result(
            "close_app",
            app_control.close_app(app_name),
            target=app_name,
            app_name=app_name,
        )

    @staticmethod
    def _create_file(params: dict[str, object], context: ActionContext) -> ActionResult:
        name = str(params["name"]).strip()
        content = str(params.get("content", ""))
        SystemPlugin._raise_if_cancelled(context, f"Cancelled creating {name}.")
        if SystemPlugin._should_simulate(params, context):
            return SystemPlugin._simulated_result("create_file", name, path=name, content=content)
        return SystemPlugin._from_tool_result(
            "create_file",
            file_ops.create_file(name=name, content=content, project_root=context.settings.project_root),
            target=name,
        )

    @staticmethod
    def _overwrite_file(params: dict[str, object], context: ActionContext) -> ActionResult:
        name = str(params["name"]).strip()
        content = str(params["content"])
        SystemPlugin._raise_if_cancelled(context, f"Cancelled overwriting {name}.")
        if SystemPlugin._should_simulate(params, context):
            return SystemPlugin._simulated_result("overwrite_file", name, path=name, content=content)
        return SystemPlugin._from_tool_result(
            "overwrite_file",
            file_ops.overwrite_file(name=name, content=content, project_root=context.settings.project_root),
            target=name,
        )

    @staticmethod
    def _read_file(params: dict[str, object], context: ActionContext) -> ActionResult:
        SystemPlugin._raise_if_cancelled(context, f"Cancelled reading {params['name']}.")
        if SystemPlugin._should_simulate(params, context):
            name = str(params["name"]).strip()
            return SystemPlugin._simulated_result("read_file", name, path=name)
        result = file_ops.read_file(
            name=str(params["name"]).strip(),
            project_root=context.settings.project_root,
        )
        if result.success and "content" in result.data:
            result.message = f"{result.message}\n{result.data['content']}"
        return SystemPlugin._from_tool_result("read_file", result, target=str(params["name"]).strip())

    @staticmethod
    def _delete_file(params: dict[str, object], context: ActionContext) -> ActionResult:
        name = str(params["name"]).strip()
        SystemPlugin._raise_if_cancelled(context, f"Cancelled deleting {name}.")
        if SystemPlugin._should_simulate(params, context):
            return SystemPlugin._simulated_result("delete_file", name, path=name)
        return SystemPlugin._from_tool_result(
            "delete_file",
            file_ops.delete_file(name=name, project_root=context.settings.project_root),
            target=name,
        )

    @staticmethod
    def _set_volume(params: dict[str, object], context: ActionContext) -> ActionResult:
        value = str(params["value"]).strip()
        SystemPlugin._raise_if_cancelled(context, "Cancelled the volume change.")
        if SystemPlugin._should_simulate(params, context):
            return SystemPlugin._simulated_result("set_volume", value, value=value)
        return SystemPlugin._from_tool_result("set_volume", system_control.set_volume(value), target=value)

    @staticmethod
    def _open_explorer(params: dict[str, object], context: ActionContext) -> ActionResult:
        path = str(params.get("path", "project")).strip() or "project"
        SystemPlugin._raise_if_cancelled(context, f"Cancelled opening {path}.")
        if SystemPlugin._should_simulate(params, context):
            return SystemPlugin._simulated_result("open_explorer", path, path=path)
        return SystemPlugin._from_tool_result(
            "open_explorer",
            system_control.open_explorer(path=path, project_root=context.settings.project_root),
            target=path,
        )

    @staticmethod
    def _get_clipboard(params: dict[str, object], context: ActionContext) -> ActionResult:
        SystemPlugin._raise_if_cancelled(context, "Cancelled reading the clipboard.")
        if SystemPlugin._should_simulate(params, context):
            return SystemPlugin._simulated_result("get_clipboard", "clipboard", text="")
        result = system_control.get_clipboard()
        if result.success:
            result.message = f"{result.message}\n{result.data.get('text', '')}"
        return SystemPlugin._from_tool_result("get_clipboard", result, target="clipboard")

    @staticmethod
    def _set_clipboard(params: dict[str, object], context: ActionContext) -> ActionResult:
        text = str(params["text"])
        SystemPlugin._raise_if_cancelled(context, "Cancelled updating the clipboard.")
        if SystemPlugin._should_simulate(params, context):
            return SystemPlugin._simulated_result("set_clipboard", text, text=text)
        return SystemPlugin._from_tool_result("set_clipboard", system_control.set_clipboard(text), target=text)

    @staticmethod
    def _minimize_window(params: dict[str, object], context: ActionContext) -> ActionResult:
        SystemPlugin._raise_if_cancelled(context, "Cancelled minimizing the active window.")
        if SystemPlugin._should_simulate(params, context):
            return SystemPlugin._simulated_result("minimize_window", "active window")
        return SystemPlugin._from_tool_result("minimize_window", window_control.minimize_active_window(), target="active window")

    @staticmethod
    def _switch_window(params: dict[str, object], context: ActionContext) -> ActionResult:
        title = str(params["title"]).strip()
        SystemPlugin._raise_if_cancelled(context, f"Cancelled switching to {title}.")
        if SystemPlugin._should_simulate(params, context):
            return SystemPlugin._simulated_result("switch_window", title, title=title)
        return SystemPlugin._from_tool_result("switch_window", window_control.switch_window(title), target=title)

    @staticmethod
    def _close_active_window(params: dict[str, object], context: ActionContext) -> ActionResult:
        SystemPlugin._raise_if_cancelled(context, "Cancelled closing the active window.")
        if SystemPlugin._should_simulate(params, context):
            return SystemPlugin._simulated_result("close_active_window", "active window")
        return SystemPlugin._from_tool_result("close_active_window", window_control.close_active_window(), target="active window")

    @staticmethod
    def _install_app(params: dict[str, object], context: ActionContext) -> ActionResult:
        app_name = str(params["app_name"]).strip()
        token = getattr(context, "cancellation_token", None)
        SystemPlugin._raise_if_cancelled(context, f"Cancelled installing {app_name}.")
        if SystemPlugin._should_simulate(params, context):
            return SystemPlugin._simulated_result("install_app", app_name, app_name=app_name)

        completed = SystemPlugin._run_winget(
            [
                "install",
                "--exact",
                "--accept-package-agreements",
                "--accept-source-agreements",
                app_name,
            ],
            token=token,
        )
        output = (completed.stdout or completed.stderr or "").strip()
        if completed.returncode != 0:
            rollback = SystemPlugin._run_winget(
                ["uninstall", "--exact", "--accept-source-agreements", app_name],
                token=token,
            )
            rollback_output = (rollback.stdout or rollback.stderr or "").strip()
            rollback_succeeded = rollback.returncode == 0
            rollback_summary = (
                f"Rollback succeeded: {rollback_output or 'package uninstall completed.'}"
                if rollback_succeeded
                else f"Rollback attempted but failed: {rollback_output or 'package uninstall could not be completed.'}"
            )
            return ActionResult(
                success=False,
                message=f"Failed to install {app_name}. {output} {rollback_summary}".strip(),
                error="install_failed",
                data={
                    "app_name": app_name,
                    "returncode": completed.returncode,
                    "rollback_attempted": True,
                    "rollback_succeeded": rollback_succeeded,
                    "rollback_message": rollback_output or rollback_summary,
                },
            )

        return ActionResult(
            success=True,
            message=f"Installed {app_name}.",
            data={"app_name": app_name, "output": output, "simulated": False, "execution_state": "executed"},
        )

    @staticmethod
    def _run_winget(args: list[str], *, token=None) -> subprocess.CompletedProcess[str]:
        command = ["winget", *args]
        try:
            return SystemPlugin._run_cancellable_process(command, token=token)
        except OSError as exc:
            return subprocess.CompletedProcess(command, returncode=1, stdout="", stderr=str(exc))

    @staticmethod
    def _remember_fact(params: dict[str, object], context: ActionContext) -> ActionResult:
        content = str(params["content"]).strip()
        namespace = str(params.get("namespace", "system"))
        category = str(params.get("category", "fact"))
        SystemPlugin._raise_if_cancelled(context, "Cancelled updating memory.")
        context.memory.remember(namespace=namespace, content=content, category=category)
        return ActionResult(
            success=True,
            message=f"I'll remember that: {content}",
            data={"namespace": namespace, "category": category},
        )

    @staticmethod
    def _recall_memory(params: dict[str, object], context: ActionContext) -> ActionResult:
        query = str(params.get("query", "")).strip()
        namespace = str(params.get("namespace", "system"))
        SystemPlugin._raise_if_cancelled(context, "Cancelled the memory lookup.")
        records = context.memory.recall(query=query, namespace=namespace, limit=5)
        if not records:
            return ActionResult(
                success=True,
                message="I could not find anything relevant in memory yet.",
                data={"matches": []},
            )

        lines = [record["content"] for record in records]
        return ActionResult(
            success=True,
            message="Here is what I found in memory:\n- " + "\n- ".join(lines),
            data={"matches": records},
        )

    @staticmethod
    def _set_reminder(params: dict[str, object], context: ActionContext) -> ActionResult:
        message = str(params["message"]).strip()
        delay_seconds = max(0, int(params["delay_seconds"]))
        SystemPlugin._raise_if_cancelled(context, f"Cancelled the reminder for {message}.")
        due_at = datetime.now() + timedelta(seconds=delay_seconds)
        context.scheduler.schedule(
            run_at=due_at,
            topic="reminder.due",
            payload={"message": f"Reminder: {message}", "due_at": due_at.isoformat()},
        )
        return ActionResult(
            success=True,
            message=f"Reminder set for {due_at.strftime('%Y-%m-%d %H:%M:%S')}: {message}",
            data={"message": message, "due_at": due_at.isoformat()},
        )

    @staticmethod
    def _health_check(params: dict[str, object], context: ActionContext) -> ActionResult:
        del params
        SystemPlugin._raise_if_cancelled(context, "Cancelled the health check.")
        return ActionResult(
            success=True,
            message=(
                "Health OK. "
                f"safe_mode={context.settings.safe_mode}, "
                f"offline_mode={context.settings.offline_mode}, "
                f"simulate_actions={context.settings.simulate_actions}, "
                f"pending_reminders={context.scheduler.pending_count}"
            ),
            data={
                "safe_mode": context.settings.safe_mode,
                "offline_mode": context.settings.offline_mode,
                "simulate_actions": context.settings.simulate_actions,
                "pending_reminders": context.scheduler.pending_count,
            },
        )

    @staticmethod
    def _report_capabilities(params: dict[str, object], context: ActionContext) -> ActionResult:
        del params
        SystemPlugin._raise_if_cancelled(context, "Cancelled the capabilities report.")
        message = (
            "Current capabilities: open_url, search_web, open_app, focus_app, close_app, "
            "create_file, read_file, delete_file, set_volume, open_explorer, "
            "clipboard control, window control, reminders, memory, and best-effort runtime observation."
        )
        return ActionResult(success=True, message=message)
