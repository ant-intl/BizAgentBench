"""Explicit dispatch of business actions and ordered lifecycle hooks."""

from dataclasses import dataclass
from types import MappingProxyType, MethodType


@dataclass(frozen=True)
class Handler:
    provider: type
    function: object
    position: int


class HandlerRegistry:
    """Immutable registry; earlier providers take precedence for shared hooks.

    A handler can continue a shared hook with ``next_handler(Provider)``.
    Ordinary calls resolve from the start, so a shared helper still applies
    all product constraints. Providers have no inheritance or per-episode state.
    """

    def __init__(self, providers):
        self.providers = tuple(providers)
        if len(set(self.providers)) != len(self.providers):
            raise ValueError("A business provider may only be registered once")
        methods = {}
        for position, provider in enumerate(self.providers):
            if provider.__bases__ != (object,):
                raise TypeError("Business providers must not inherit behavior")
            for name, function in vars(provider).items():
                if callable(function):
                    methods.setdefault(name, []).append(Handler(provider, function, position))
        self.methods = MappingProxyType({k: tuple(v) for k, v in methods.items()})
        self.positions = MappingProxyType({p: i for i, p in enumerate(self.providers)})

    def bind(self, runtime, name, *, after=None):
        minimum = -1 if after is None else self.positions[after]
        for handler in self.methods.get(name, ()):
            if handler.position > minimum:
                return MethodType(handler.function, runtime)
        raise AttributeError(name)

    def providers_for(self, name):
        """Expose the exact dispatch order for diagnostics and regression tests."""
        return tuple(h.provider for h in self.methods.get(name, ()))

    def starting_at(self, provider):
        return HandlerRegistry(self.providers[self.positions[provider] :])


class Continuation:
    def __init__(self, runtime, provider):
        self.runtime = runtime
        self.provider = provider

    def __getattr__(self, name):
        return self.runtime.registry.bind(self.runtime, name, after=self.provider)

    def dynamic_attribute(self, name):
        resolver = self.runtime.registry.bind(self.runtime, "__getattr__", after=self.provider)
        return resolver(name)


class BusinessRuntime:
    """Episode state plus explicit business handlers, without a mixin hierarchy."""

    def __init__(self, world, registry):
        self.w = world
        self.registry = registry

    def __getattr__(self, name):
        try:
            return self.registry.bind(self, name)
        except AttributeError:
            # Some product catalogs provide named operations through a common
            # service instruction. Their fallback chain is also explicit.
            if name == "__getattr__":
                raise
            return self.registry.bind(self, "__getattr__")(name)

    def next_handler(self, provider):
        return Continuation(self, provider)

    def view_from(self, provider, world):
        """Use a product's base query handlers over a customer-scoped projection."""
        return BusinessRuntime(world, self.registry.starting_at(provider))
