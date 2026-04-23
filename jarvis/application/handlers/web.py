from __future__ import annotations

from jarvis.application.handlers.shared import raise_if_cancelled, should_simulate, simulated_result, tool_result
from jarvis.core.tools import ToolRegistry, ToolResult
from jarvis.infrastructure.system_control import open_app as system_tools
from jarvis.infrastructure.web import browser as web_tools


def register(registry: ToolRegistry) -> None:
    registry.register("get_time", get_time, "Return the local system time.")
    registry.register("open_url", open_url, "Open a URL in the browser.", required_params=("url",))
    registry.register("search_web", search_web, "Search the web in a browser tab.", required_params=("query",))
    registry.register("search_youtube", search_youtube, "Search for content on YouTube.", required_params=("query",))
    registry.register("play_youtube", play_youtube, "Open YouTube playback for a query.", required_params=("query",))


def get_time(params: dict[str, object], context) -> ToolResult:
    del params, context
    return tool_result("get_time", system_tools.get_time(), target="local time")


def open_url(params: dict[str, object], context) -> ToolResult:
    url = str(params["url"]).strip()
    browser_app = str(params.get("browser_app", "")).strip()
    raise_if_cancelled(context, f"Cancelled opening {url}.")
    if should_simulate(params, context):
        return simulated_result("open_url", url, url=web_tools.normalize_url(url), browser_app=browser_app)
    return tool_result("open_url", web_tools.open_url(url, browser_app=browser_app or None), target=url)


def search_web(params: dict[str, object], context) -> ToolResult:
    query = str(params["query"]).strip()
    browser_app = str(params.get("browser_app", "")).strip()
    raise_if_cancelled(context, f"Cancelled searching for {query}.")
    if should_simulate(params, context):
        return simulated_result(
            "search_web",
            query,
            query=query,
            search_url=f"https://www.google.com/search?q={query.replace(' ', '+')}",
            browser_app=browser_app,
        )
    return tool_result("search_web", web_tools.search_web(query, browser_app=browser_app or None), target=query)


def search_youtube(params: dict[str, object], context) -> ToolResult:
    query = str(params["query"]).strip()
    browser_app = str(params.get("browser_app", "")).strip()
    raise_if_cancelled(context, f"Cancelled YouTube search for {query}.")
    if should_simulate(params, context):
        return simulated_result(
            "search_youtube",
            query,
            query=query,
            search_url=f"https://www.youtube.com/results?search_query={query.replace(' ', '+')}",
            browser_app=browser_app,
        )
    return tool_result("search_youtube", web_tools.search_youtube(query, browser_app=browser_app or None), target=query)


def play_youtube(params: dict[str, object], context) -> ToolResult:
    query = str(params["query"]).strip()
    browser_app = str(params.get("browser_app", "")).strip()
    video_url = str(params.get("video_url", "")).strip()
    raise_if_cancelled(context, f"Cancelled YouTube playback for {query}.")
    if should_simulate(params, context):
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
        return simulated_result("play_youtube", query, **data)
    return tool_result(
        "play_youtube",
        web_tools.play_youtube(query, browser_app=browser_app or None, video_url=video_url or None),
        target=query,
    )
