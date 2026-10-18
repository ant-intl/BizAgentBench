"""Explicit composition of independent business acceptance checks."""


class VerificationPipeline:
    """Apply named verifier layers without patching module namespaces.

    Layers receive the prior verifier as a callback. They may add checks or
    select an applicable product rule; predicates remain independent of the
    business execution handlers.
    """

    def __init__(self, base, layers):
        self.base = base
        self.layers = tuple(layers)
        self.verify = base
        for layer in self.layers:
            self.verify = layer(self.verify)
            if not callable(self.verify):
                raise TypeError("A verifier layer must return a callable")

    def __call__(self, world, contract):
        return self.verify(world, contract)
