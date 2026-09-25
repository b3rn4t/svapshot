#!/usr/bin/env python3
"""Unit tests for the native harness-scaffold components.

These cover the small, importable pieces that replaced harness helpers:
assertion text assembly and the library-walk skip policy used when writing
``files.vc``. End-to-end ``ft_*`` contracts stay in ``test_harness.py``.
"""

from __future__ import annotations

import os
import unittest

import _path_setup  # noqa: F401

from scaffold.assertions import (
    assemble_assertion,
    escape_display_string,
    normalize_assertion_id,
)
from scaffold.tool_scripts import SKIP_LIB_SUBDIRS, should_skip_lib_subdir


class TestNormalizeAssertionId(unittest.TestCase):

    def test_bare_names_get_the_a_prefix(self):
        self.assertEqual(normalize_assertion_id('fairness'), 'a_fairness')

    def test_existing_a_prefix_is_kept(self):
        self.assertEqual(normalize_assertion_id('a_fairness'), 'a_fairness')

    def test_as_double_underscore_prefix_is_stripped(self):
        self.assertEqual(normalize_assertion_id('as__req_grant'), 'a_req_grant')

    def test_leading_double_underscore_is_stripped(self):
        self.assertEqual(normalize_assertion_id('__stable'), 'a_stable')


class TestAssembleAssertion(unittest.TestCase):

    def test_wraps_a_bare_expression_in_parentheses(self):
        text = assemble_assertion('req', 'req |-> ack', 'failed')
        self.assertIn('a_req: assert property (req |-> ack)', text)
        self.assertIn('$display("failed");', text)

    def test_keeps_parentheses_already_present(self):
        text = assemble_assertion('a_x', '(1)', '')
        self.assertIn('assert property (1)', text)
        self.assertNotIn('assert property ((1))', text)

    def test_default_failure_message_names_the_assertion(self):
        text = assemble_assertion('hold', '1', '')
        self.assertIn('Assertion a_hold failed', text)

    def test_escapes_quotes_in_the_failure_message(self):
        text = assemble_assertion('x', '1', 'say "hi"')
        self.assertIn('$display("say \\"hi\\"");', text)

    def test_indent_is_applied_to_every_line(self):
        text = assemble_assertion('x', '1', 'fail', indent='\t')
        for line in text.splitlines():
            self.assertTrue(line.startswith('\t'), line)


class TestEscapeDisplayString(unittest.TestCase):

    def test_escapes_backslashes_and_quotes(self):
        self.assertEqual(escape_display_string('a\\b"c'), 'a\\\\b\\"c')


class TestLibraryWalkPolicy(unittest.TestCase):

    def test_known_mutation_trees_are_skipped(self):
        for name in ('mutations', 'buggy_artifacts', 'figures', '__pycache__', '.git'):
            with self.subTest(name=name):
                self.assertTrue(should_skip_lib_subdir(name))
                self.assertIn(name, SKIP_LIB_SUBDIRS)

    def test_hidden_directories_are_skipped(self):
        self.assertTrue(should_skip_lib_subdir('.svn'))

    def test_rtl_directories_are_kept(self):
        self.assertFalse(should_skip_lib_subdir('rtl'))
        self.assertFalse(should_skip_lib_subdir('include'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
