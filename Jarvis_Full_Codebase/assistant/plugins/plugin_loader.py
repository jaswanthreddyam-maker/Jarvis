import os
import importlib
import inspect
from .interface import PluginInterface
from ..actions.registry import registry

PLUGIN_DIR = os.path.dirname(__file__)

def load_plugins():
    plugins = []
    for filename in os.listdir(PLUGIN_DIR):
        if filename.endswith(".py") and filename not in ["__init__.py", "plugin_loader.py", "interface.py"]:
            module_name = f"plugins.{filename[:-3]}"
            try:
                module = importlib.import_module(f"assistant.{module_name}")
                for name, obj in inspect.getmembers(module, inspect.isclass):
                    if issubclass(obj, PluginInterface) and obj != PluginInterface:
                        plugin_instance = obj()
                        plugin_instance.activate()
                        tools = plugin_instance.get_tools()
                        for tool_name, tool_func in tools.items():
                            registry.register(tool_name)(tool_func)
                        plugins.append(plugin_instance)
                        print(f"Loaded plugin: {obj.__name__}")
            except Exception as e:
                print(f"Failed to load plugin {filename}: {e}")
    return plugins
