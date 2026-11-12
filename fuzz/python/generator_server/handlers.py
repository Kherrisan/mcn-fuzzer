"""Declarations and case dispatch for synthesized step handlers."""

import inspect
from types import MappingProxyType

from .context import InteractionError


def step_handler(step_name):
    if not isinstance(step_name, str) or not step_name:
        raise ValueError("step name must be a non-empty string")

    def decorate(function):
        function._mcn_step_name = step_name
        return function
    return decorate


def global_init(function):
    function._mcn_global_init = True
    return function


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, bytearray):
        return bytes(value)
    return value


def build_dispatch(module):
    handlers = {}
    initializer = None
    for function in vars(module).values():
        name = getattr(function, "_mcn_step_name", None)
        init = getattr(function, "_mcn_global_init", False)
        if name is None and not init:
            continue
        if not inspect.isfunction(function) or inspect.iscoroutinefunction(function) or inspect.isgeneratorfunction(function):
            raise ValueError("handlers must be synchronous functions")
        try:
            inspect.signature(function).bind(object(), object())
        except TypeError as exc:
            raise ValueError("handlers must accept (ctx, params)") from exc
        if name is not None:
            if name in handlers:
                raise ValueError(f"duplicate step handler {name!r}")
            handlers[name] = function
        if init:
            if initializer is not None:
                raise ValueError("multiple global initializers")
            initializer = function
    if not handlers:
        return None

    def generate(ctx, step_trace):
        params = _freeze(ctx.generator_params)
        try:
            if initializer is not None:
                initializer(ctx, params)
            for instance in step_trace:
                handler = handlers.get(instance.get("step"))
                if handler is None:
                    raise InteractionError("generator", f"unsupported step {instance.get('step')!r}")
                ctx.step_instance = _freeze(instance)
                handler(ctx, params)
        finally:
            ctx.step_instance = None
            ctx.close_all()
    return generate
