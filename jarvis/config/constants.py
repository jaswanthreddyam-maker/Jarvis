from __future__ import annotations

from jarvis.core.context import ToolDefinition


KNOWN_URLS: dict[str, str] = {
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "gmail": "https://mail.google.com",
    "github": "https://github.com",
    "spotify": "https://open.spotify.com",
    "reddit": "https://www.reddit.com",
    "stackoverflow": "https://stackoverflow.com",
    "stack overflow": "https://stackoverflow.com",
    "chatgpt": "https://chat.openai.com",
    "openai": "https://openai.com",
    "gemini": "https://gemini.google.com",
    "bing": "https://www.bing.com",
    "wikipedia": "https://www.wikipedia.org",
    "twitter": "https://twitter.com",
    "facebook": "https://www.facebook.com",
}

KNOWN_FOLDERS = {"desktop", "documents", "downloads", "music", "pictures", "videos", "home", "project"}
BROWSER_APPS = {"chrome", "google chrome", "comet", "edge", "microsoft edge", "firefox"}
SYSTEM_ACTIONS = {"shutdown", "restart", "reboot"}

def load_user_aliases() -> None:
    import json
    from pathlib import Path
    try:
        alias_file = Path("user_aliases.json")
        if alias_file.exists():
            with open(alias_file, "r") as f:
                data = json.load(f)
                if "urls" in data:
                    KNOWN_URLS.update(data["urls"])
                if "folders" in data:
                    KNOWN_FOLDERS.update(data["folders"])
                if "apps" in data:
                    BROWSER_APPS.update(data["apps"])
    except Exception:
        pass

load_user_aliases()



def build_tool_catalog() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(
            name="emergency_stop",
            description="Cancel all running tasks immediately and reset the execution pipeline safely.",
            examples=("emergency stop", "stop everything"),
            verification="confirm all active tasks were cancelled",
        ),
        ToolDefinition(
            name="open_app",
            description="Launch a desktop application by name.",
            required_params=("app_name",),
            examples=("open chrome", "launch notepad"),
            verification="confirm the application launched",
        ),
        ToolDefinition(
            name="focus_app",
            description="Bring an application window to the foreground.",
            required_params=("app_name",),
            examples=("focus chrome", "switch to vscode"),
            verification="confirm the application was focused",
        ),
        ToolDefinition(
            name="close_app",
            description="Close a desktop application.",
            required_params=("app_name",),
            examples=("close spotify",),
            verification="confirm the close request was sent",
            risk="medium",
        ),
        ToolDefinition(
            name="close_active_window",
            description="Close the currently focused window.",
            examples=("close active window",),
            verification="confirm the close request was sent",
            risk="medium",
        ),
        ToolDefinition(
            name="minimize_window",
            description="Minimize the active window.",
            examples=("minimize window",),
            verification="confirm the active window was minimized",
        ),
        ToolDefinition(
            name="switch_window",
            description="Switch to a window by title.",
            required_params=("title",),
            examples=("switch to settings",),
            verification="confirm the window switched",
        ),
        ToolDefinition(
            name="open_url",
            description="Open a site or URL in the browser.",
            required_params=("url",),
            examples=("open youtube", "open github.com"),
            verification="confirm the URL was prepared",
            context_param="browser_app",
            preferred_surface="browser",
        ),
        ToolDefinition(
            name="search_web",
            description="Search the web for a query.",
            required_params=("query",),
            examples=("search python automation",),
            verification="confirm the search URL was prepared",
            context_param="browser_app",
            preferred_surface="browser",
        ),
        ToolDefinition(
            name="search_youtube",
            description="Search YouTube for a query.",
            required_params=("query",),
            examples=("search youtube for ai videos",),
            verification="confirm the YouTube search URL was prepared",
            preferred_surface="youtube",
            opens_surface="youtube",
            prelude_action="open_url",
            prelude_target="youtube",
            prelude_params={"url": "https://www.youtube.com"},
            prelude_description="Open youtube in the browser.",
        ),
        ToolDefinition(
            name="play_youtube",
            description="Start YouTube playback for a query.",
            required_params=("query",),
            examples=("play python tutorials on youtube",),
            verification="confirm YouTube video playback started",
            preferred_surface="youtube",
            opens_surface="youtube",
            prelude_action="open_url",
            prelude_target="youtube",
            prelude_params={"url": "https://www.youtube.com"},
            prelude_description="Open youtube in the browser.",
        ),
        ToolDefinition(
            name="create_file",
            description="Create a file in an allowed workspace path.",
            required_params=("name",),
            examples=("create a file named notes.txt",),
            verification="confirm the file was created",
        ),
        ToolDefinition(
            name="overwrite_file",
            description="Replace the contents of an existing file.",
            required_params=("name", "content"),
            examples=("overwrite file notes.txt with hello",),
            verification="confirm the file was overwritten",
            risk="high",
        ),
        ToolDefinition(
            name="read_file",
            description="Read a text file.",
            required_params=("name",),
            examples=("read file notes.txt",),
            verification="confirm file contents were returned",
        ),
        ToolDefinition(
            name="delete_file",
            description="Delete a file.",
            required_params=("name",),
            examples=("delete file notes.txt",),
            verification="confirm the file was deleted",
            risk="high",
        ),
        ToolDefinition(
            name="open_explorer",
            description="Open a known folder in File Explorer.",
            examples=("show downloads folder",),
            verification="confirm explorer opened",
        ),
        ToolDefinition(
            name="set_volume",
            description="Adjust the system volume.",
            required_params=("value",),
            examples=("increase volume", "mute volume"),
            verification="confirm the volume command was sent",
        ),
        ToolDefinition(
            name="get_clipboard",
            description="Read the clipboard.",
            examples=("what is on the clipboard",),
            verification="confirm clipboard text was returned",
        ),
        ToolDefinition(
            name="set_clipboard",
            description="Replace the clipboard text.",
            required_params=("text",),
            examples=("copy hello to clipboard",),
            verification="confirm clipboard text was updated",
        ),
        ToolDefinition(
            name="system_action",
            description="Run a system power action like shutdown or restart.",
            required_params=("action_type",),
            examples=("shutdown computer",),
            verification="confirm the system action was dispatched",
            risk="high",
        ),
        ToolDefinition(
            name="install_app",
            description="Install an application using the platform package manager.",
            required_params=("app_name",),
            examples=("install git",),
            verification="confirm installation completed",
            risk="high",
        ),
        ToolDefinition(
            name="remember_fact",
            description="Store a fact in long-term memory.",
            required_params=("content",),
            examples=("remember that my favorite editor is vscode",),
            verification="confirm the fact is stored in memory",
        ),
        ToolDefinition(
            name="save_preference",
            description="Store a structured user preference in long-term memory.",
            required_params=("key", "value"),
            examples=("save preference preferred_browser = chrome",),
            verification="confirm the preference is stored in memory",
        ),
        ToolDefinition(
            name="recall_memory",
            description="Recall previously stored memory.",
            examples=("what do you remember about editor",),
            verification="confirm memory lookup completed",
        ),
        ToolDefinition(
            name="set_reminder",
            description="Schedule a reminder event.",
            required_params=("message", "delay_seconds"),
            examples=("remind me to stretch in 5 minutes",),
            verification="confirm the reminder is scheduled",
        ),
        ToolDefinition(
            name="get_time",
            description="Return the local system time.",
            examples=("what time is it",),
            verification="confirm the local time was returned",
        ),
        ToolDefinition(
            name="health_check",
            description="Return assistant runtime health.",
            examples=("health check",),
            verification="confirm the runtime health payload was returned",
        ),
        ToolDefinition(
            name="report_capabilities",
            description="List the currently available capabilities.",
            examples=("what can you do",),
            verification="confirm the capabilities report was returned",
        ),
    )
