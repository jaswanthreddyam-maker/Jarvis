class PluginInterface:
    def activate(self):
        """Called when plugin is activated."""
        raise NotImplementedError

    def deactivate(self):
        """Called when plugin is deactivated."""
        raise NotImplementedError

    def get_tools(self) -> dict:
        """Returns a dict mapping tool_name to async functions."""
        return {}
