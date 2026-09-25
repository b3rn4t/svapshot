#!/usr/bin/env python3
"""Run unittest with an in-place per-module progress spinner.

Progress updates the same line (.: / :. alternate). Tool/test stdout is not muted.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import unittest
from typing import List, Optional, TextIO


def _module_label(test) -> str:
    if isinstance(test, unittest.TestSuite):
        return 'suite'
    module = getattr(test, '__module__', None) or type(test).__module__
    if module.endswith('.py'):
        module = module[:-3]
    return module.split('.')[0] if module else type(test).__name__


class VisualTestResult(unittest.TextTestResult):
    def __init__(self, stream: TextIO, descriptions: bool, verbosity: int):
        super().__init__(stream, descriptions, verbosity)
        self._current_module: Optional[str] = None
        self._tick = 0
        self._progress_width = 0

    def _clear_progress(self) -> None:
        if self._progress_width:
            self.stream.write('\r' + ' ' * self._progress_width + '\r')
            self.stream.flush()
            self._progress_width = 0

    def _draw_progress(self, label: str) -> None:
        prefix = '.:' if self._tick % 2 else ':.'
        text = f'{prefix} running {label}'
        # Pad to erase a longer previous label when the module name shortens.
        pad = max(0, self._progress_width - len(text))
        self.stream.write('\r' + text + (' ' * pad))
        self.stream.flush()
        self._progress_width = len(text)

    def startTest(self, test):
        label = _module_label(test)
        if self._current_module is not None and label != self._current_module:
            self._clear_progress()
            self.stream.write(f'✅ finished {self._current_module}\n')
            self.stream.flush()
        self._current_module = label
        self._tick += 1
        self._draw_progress(label)
        super().startTest(test)

    def addSkip(self, test, reason):
        self._clear_progress()
        super().addSkip(test, reason)
        self.stream.write(f'⏭️  skipped {test.id()}: {reason}\n')
        self.stream.flush()
        if self._current_module is not None:
            self._draw_progress(self._current_module)

    def addError(self, test, err):
        self._clear_progress()
        super().addError(test, err)
        self.stream.write(f'❌ ERROR {test.id()}\n')
        self.stream.flush()

    def addFailure(self, test, err):
        self._clear_progress()
        super().addFailure(test, err)
        self.stream.write(f'❌ FAIL {test.id()}\n')
        self.stream.flush()

    def stopTestRun(self):
        if self._current_module is not None:
            self._clear_progress()
            self.stream.write(f'✅ finished {self._current_module}\n')
            self.stream.flush()
            self._current_module = None
        super().stopTestRun()


class VisualTestRunner(unittest.TextTestRunner):
    resultclass = VisualTestResult

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('verbosity', 0)
        kwargs.setdefault('buffer', False)
        super().__init__(*args, **kwargs)

    def run(self, test):
        result = self._makeResult()
        unittest.registerResult(result)
        result.failfast = self.failfast
        result.buffer = self.buffer
        result.tb_locals = getattr(self, 'tb_locals', False)
        start = time.perf_counter()
        startTestRun = getattr(result, 'startTestRun', None)
        if startTestRun is not None:
            startTestRun()
        try:
            test(result)
        finally:
            stopTestRun = getattr(result, 'stopTestRun', None)
            if stopTestRun is not None:
                stopTestRun()
        stop = time.perf_counter()
        result.printErrors()
        ran = result.testsRun
        failed = len(result.failures) + len(result.errors)
        skipped = len(result.skipped)
        self.stream.write(
            f'\nRan {ran} tests in {stop - start:.3f}s'
            + (f', skipped={skipped}' if skipped else '')
            + (f', failures={failed}' if failed else '')
            + '\n'
        )
        self.stream.flush()
        return result


def _load_tests(args: argparse.Namespace) -> unittest.TestSuite:
    loader = unittest.TestLoader()
    if args.discover:
        # Match `python3 -m unittest discover -s tests`: relative start_dir,
        # no top_level_dir. Passing the repo as top_level_dir requires
        # tests/__init__.py and fails with "Start directory is not importable".
        kwargs = {
            'start_dir': args.discover,
            'pattern': args.pattern,
        }
        if args.top_level_dir:
            kwargs['top_level_dir'] = args.top_level_dir
        return loader.discover(**kwargs)
    if not args.tests:
        raise SystemExit('error: pass --discover DIR or one or more test names')
    suite = unittest.TestSuite()
    for name in args.tests:
        suite.addTests(loader.loadTestsFromName(name))
    return suite


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--discover',
        metavar='DIR',
        help='unittest discover start directory (e.g. tests)',
    )
    parser.add_argument(
        '--pattern',
        default='test*.py',
        help='discover pattern (default: test*.py)',
    )
    parser.add_argument(
        '--top-level-dir',
        default=None,
        help='optional discover top-level directory (usually omit)',
    )
    parser.add_argument(
        'tests',
        nargs='*',
        help='unittest loadTestsFromName targets (e.g. test_main)',
    )
    args = parser.parse_args(argv)

    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    tests_dir = os.path.join(repo, 'tests')
    for path in (tests_dir, repo):
        if path not in sys.path:
            sys.path.insert(0, path)
    os.chdir(repo)

    suite = _load_tests(args)
    # Progress on stderr so it stays visible alongside test stdout.
    runner = VisualTestRunner(stream=sys.stderr)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
