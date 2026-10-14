class MockerError(Exception):
    """A configuration, serialization, or replay failure."""


class ReplayMiss(MockerError):
    pass


class Blocked(MockerError):
    pass
