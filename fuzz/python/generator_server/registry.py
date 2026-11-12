"""Load generator scripts once, keyed by their canonical absolute path."""

import importlib.util
import os
from types import ModuleType
from .handlers import build_dispatch


class Registry:
    def __init__(self) -> None:
        self._modules: dict[str, ModuleType] = {}
        self._generators = {}
        self.loads = 0

    def get(self, path: str):
        if not os.path.isabs(path):
            raise ValueError(f"generator path must be absolute: {path}")
        if not os.path.isfile(path):
            raise ValueError(f"generator script not found: {path}")
        key = os.path.realpath(path)
        cached = self._modules.get(key)
        if cached is not None:
            return self._generators[key]
        module_name = f"libaflstar_generator_{len(self._modules)}"
        spec = importlib.util.spec_from_file_location(module_name, key)
        if spec is None or spec.loader is None:
            raise ValueError(f"cannot import generator script: {key}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        generate = build_dispatch(module)
        if generate is None:
            raise ValueError(f"{key} has no step handlers")
        self._modules[key] = module
        self._generators[key] = generate
        self.loads += 1
        return generate
