#!/usr/bin/env python3
"""Cursor SDK model resolution and LLM-step wiring."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import _path_setup  # noqa: F401

import agent
import cursor_llm
import main


class TestCursorModelResolution(unittest.TestCase):

    def test_bare_cursor_uses_sdk_default(self):
        config = agent.resolve_model_config('cursor')
        self.assertEqual(config['type'], 'cursor_sdk')
        self.assertEqual(config['cursor_model'], 'composer-2.5')

    def test_composer_id_is_a_first_class_model(self):
        config = agent.resolve_model_config('composer-2.5')
        self.assertEqual(config['type'], 'cursor_sdk')
        self.assertEqual(config['cursor_model'], 'composer-2.5')

    def test_cursor_prefix_selects_any_catalog_id(self):
        config = agent.resolve_model_config('cursor:grok-4.6')
        self.assertEqual(config['type'], 'cursor_sdk')
        self.assertEqual(config['cursor_model'], 'grok-4.6')

    def test_grok_name_is_a_first_class_cursor_model(self):
        config = agent.resolve_model_config('grok-4.6')
        self.assertEqual(config['type'], 'cursor_sdk')
        self.assertEqual(config['cursor_model'], 'grok-4.6')

    def test_grok_selector_can_carry_effort_and_fast(self):
        catalog, params = cursor_llm.sdk_model_params('cursor:grok-4.6:high:fast')
        self.assertEqual(catalog, 'grok-4.6')
        self.assertEqual(
            params,
            [
                {'id': 'effort', 'value': 'high'},
                {'id': 'fast', 'value': 'true'},
            ],
        )

    def test_date_env_supplies_effort_and_fast(self):
        with mock.patch.dict(os.environ, {
            'SVAPSHOT_CURSOR_EFFORT': 'high',
            'SVAPSHOT_CURSOR_FAST': 'true',
        }, clear=False):
            catalog, params = cursor_llm.sdk_model_params('cursor:grok-4.6')
        self.assertEqual(catalog, 'grok-4.6')
        self.assertEqual(
            params,
            [
                {'id': 'effort', 'value': 'high'},
                {'id': 'fast', 'value': 'true'},
            ],
        )

    def test_legacy_cli_name_stays_on_the_cli(self):
        config = agent.resolve_model_config('cursor-cli')
        self.assertEqual(config['type'], 'cursor_cli')

    def test_unknown_names_are_not_forced_onto_cursor(self):
        self.assertIsNone(agent.resolve_model_config('some-local-model'))


class TestCursorInitialGeneration(unittest.TestCase):

    def test_initial_generation_calls_the_sdk(self):
        with tempfile.TemporaryDirectory() as work:
            rtl = os.path.join(work, 'unit.sv')
            with open(rtl, 'w', encoding='utf-8') as handle:
                handle.write(
                    'module unit(input logic req_i, output logic grant_o); '
                    'endmodule')
            with mock.patch.object(
                cursor_llm, 'generate',
                return_value='---\nid: p\nproperty: req_i |=> grant_o\n---\n',
            ) as generate, mock.patch.dict(
                os.environ, {'CURSOR_API_KEY': 'test-key'}, clear=False,
            ), mock.patch('os.getcwd', return_value=work):
                self.assertTrue(
                    main.run_initial_assertion_generation(rtl, 'composer-2.5'))
            generate.assert_called_once()
            self.assertEqual(generate.call_args.args[0], 'composer-2.5')
            with open(os.path.join(work, 'initial_assertions'), encoding='utf-8') as handle:
                self.assertIn('a_p: assert property', handle.read())

    def test_legacy_cli_is_still_rejected_for_initial_generation(self):
        with tempfile.TemporaryDirectory() as work:
            rtl = os.path.join(work, 'unit.sv')
            with open(rtl, 'w', encoding='utf-8') as handle:
                handle.write('module unit; endmodule')
            self.assertFalse(
                main.run_initial_assertion_generation(rtl, 'cursor-cli'))


class TestCursorAgentGenerate(unittest.TestCase):

    def test_agent_generate_uses_cursor_sdk(self):
        with tempfile.TemporaryDirectory() as work:
            rtl = os.path.join(work, 'unit.sv')
            with open(rtl, 'w', encoding='utf-8') as handle:
                handle.write('module unit; endmodule')
            assertions_dir = os.path.join(work, 'sva') + '/'
            os.makedirs(assertions_dir, exist_ok=True)
            with open(os.path.join(assertions_dir, 'unit_prop.sv'), 'w', encoding='utf-8') as handle:
                handle.write('// empty\n')
            coding = agent.CodingAgent(
                rtl_source=rtl,
                assertions_file='unit_prop.sv',
                assertions_dir=assertions_dir,
                llm='cursor:composer-2.5',
                prompting_strategy='zero-shot',
                module_type='combinational',
                verbosity=1,
                api_key='test-key',
            )
            with mock.patch.object(
                cursor_llm, 'generate', return_value='pong',
            ) as generate:
                text = coding._get_llm_completion(
                    [{'role': 'user', 'content': 'Reply pong'}])
            self.assertEqual(text, 'pong')
            generate.assert_called_once()
            self.assertEqual(generate.call_args.args[0], 'composer-2.5')
            self.assertIn('Reply pong', generate.call_args.args[1])


class TestCursorBaseline(unittest.TestCase):

    def test_one_shot_baseline_uses_cursor_sdk(self):
        from baselines import _single_completion

        with mock.patch.object(cursor_llm, 'generate', return_value='ok') as generate:
            text, usage = _single_completion(
                'composer-2.5',
                [{'role': 'user', 'content': 'hi'}],
                None,
            )
        self.assertEqual(text, 'ok')
        self.assertIsNone(usage)
        generate.assert_called_once_with('composer-2.5', 'hi')


if __name__ == '__main__':
    unittest.main()
