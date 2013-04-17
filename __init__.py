
__all__ = []


def load_tests(loader, tests, pattern):
    """Load test protocol
    """
    from unittest import TestSuite
    from . import tests, monad, core, stream, remoting

    suite = TestSuite()
    for test in (tests, monad, core, stream, remoting):
        suite.addTests(loader.loadTestsFromModule(test))

    return suite
