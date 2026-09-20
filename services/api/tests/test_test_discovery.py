import importlib
import unittest


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


class ProtectionDiscoveryTests(unittest.TestCase):
    def test_protection_modules_only_collect_their_own_scenarios(self):
        for name in ("handoff", "adjustment", "review"):
            with self.subTest(module=name):
                module = importlib.import_module("tests.test_protection_" + name)
                suite = unittest.TestLoader().loadTestsFromModule(module)
                borrowed = [
                    case.id() for case in cases(suite)
                    if type(case).__module__ != module.__name__
                    or getattr(type(case), case._testMethodName).__module__ != module.__name__
                ]
                self.assertEqual(len(borrowed), 0, f"{name} collects borrowed scenarios: {borrowed[:1]}")
