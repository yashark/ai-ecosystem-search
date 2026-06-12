"""
Region configuration loader.

Reads REGION_CONFIG env var to determine which configuration to use.
Default: 'search_parameter' (Turkey + AI).
"""
import os
import importlib

_active_config = None


def get_active_config():
    """Return the active RegionConfig singleton."""
    global _active_config
    if _active_config is not None:
        return _active_config

    config_name = os.environ.get("REGION_CONFIG", "search_parameter")

    try:
        module = importlib.import_module(f"region_config.{config_name}")
    except ModuleNotFoundError:
        raise ImportError(
            f"Region config '{config_name}' not found. "
            f"Expected module: region_config/{config_name}.py"
        )

    # Find the first RegionConfig subclass in the module
    from region_config.base import RegionConfig
    for attr_name in dir(module):
        attr = getattr(module, attr_name)
        if (isinstance(attr, type)
                and issubclass(attr, RegionConfig)
                and attr is not RegionConfig):
            _active_config = attr()
            return _active_config

    raise ImportError(
        f"No RegionConfig subclass found in region_config/{config_name}.py"
    )
