"""Run the installed base suite once and retain HTTP extension coverage counts."""

import argparse
import importlib.metadata
import json
import platform
import sys
import unittest
from collections import Counter
from pathlib import Path

import expose_api

_HTTP_MODULES = {'test_responses', 'test_requests', 'test_routing', 'test_routers', 'test_composition',
                 'test_route_access', 'test_route_docs', 'test_streaming', 'test_files', 'test_multipart',
                 'test_static', 'test_templates', 'test_http_extensions'}


class ReceiptResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.http_counts = Counter()

    def startTest(self, test):
        module = test.id().split('.', 1)[0]
        if module in _HTTP_MODULES:
            self.http_counts[module] += 1
        super().startTest(test)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tests', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    arguments = parser.parse_args()
    assert Path(expose_api.__file__).is_relative_to(Path(sys.prefix)), 'Run against an installed artifact.'
    suite = unittest.defaultTestLoader.discover(str(arguments.tests))
    result = unittest.TextTestRunner(verbosity=1, resultclass=ReceiptResult).run(suite)
    receipt = {'version': importlib.metadata.version('expose-api'), 'python': platform.python_version(),
               'platform': platform.system(), 'installed': True, 'tests_run': result.testsRun,
               'http_tests_run': sum(result.http_counts.values()), 'http_modules': dict(sorted(result.http_counts.items())),
               'failures': len(result.failures), 'errors': len(result.errors),
               'skipped': [{'test': test.id(), 'reason': reason} for test, reason in result.skipped],
               'jinja_imported': 'jinja2' in sys.modules, 'passed': result.wasSuccessful()}
    arguments.receipt.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    if set(result.http_counts) != _HTTP_MODULES or not result.wasSuccessful() or receipt['jinja_imported']:
        raise SystemExit(1)
    print(json.dumps(receipt, indent=2))


if __name__ == '__main__':
    main()
