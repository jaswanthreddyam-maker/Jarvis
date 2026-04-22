import os
import subprocess
import webbrowser
from bs4 import BeautifulSoup
import requests
import ast
from .registry import registry

@registry.register("search_web")
async def search_web(query: str) -> str:
    try:
        url = f"https://html.duckduckgo.com/html/?q={query}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        response = requests.get(url, headers=headers)
        soup = BeautifulSoup(response.text, 'html.parser')
        results = ""
        for a in soup.find_all('a', class_='result__snippet', limit=3):
            results += a.text + "\n"
        return results if results else "No results found."
    except Exception as e:
        return f"Search failed: {e}"

@registry.register("open_app")
async def open_app(app_name: str) -> str:
    try:
        subprocess.Popen(["start", app_name], shell=True)
        return f"Opened {app_name}"
    except Exception as e:
        return f"Failed to open app: {e}"

@registry.register("open_url")
async def open_url(url: str) -> str:
    try:
        webbrowser.open(url)
        return f"Opened {url}"
    except Exception as e:
        return f"Failed to open url: {e}"

@registry.register("file_op")
async def file_op(operation: str, path: str, content: str = "") -> str:
    try:
        if operation == "read":
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        elif operation == "write":
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Wrote to {path}"
        else:
            return f"Unknown operation: {operation}"
    except Exception as e:
        return f"File operation failed: {e}"

def check_unsafe_code(code: str) -> bool:
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in ["os", "sys", "subprocess"]:
                        return False
            elif isinstance(node, ast.ImportFrom):
                if node.module in ["os", "sys", "subprocess"]:
                    return False
        return True
    except SyntaxError:
        return False

@registry.register("run_code")
async def run_code(code: str) -> str:
    if not check_unsafe_code(code):
        return "Error: Unsafe code detected. Imports of os, sys, and subprocess are disabled in this sandbox."
    try:
        result = subprocess.run(["python", "-c", code], capture_output=True, text=True, timeout=10)
        return result.stdout + result.stderr
    except Exception as e:
        return f"Code execution failed: {e}"
