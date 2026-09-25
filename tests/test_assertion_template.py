#!/usr/bin/env python3
"""Contract tests for structured LLM output and SVA template assembly.

The module under test is the boundary where untrusted, variably formatted model
text becomes SystemVerilog source.  These tests therefore cover both sides:

* the prompt tells the model to return data rather than source code,
* identifiers and display strings cannot break the generated syntax,
* malformed structured blocks are rejected rather than partially emitted,
* legacy assembled assertions are bounded without swallowing prose,
* assertions can be recovered exactly from the designer section.

No LLM, formal tool, RTL tree, or licence is required.
"""

from __future__ import annotations

import os
import re
import sys
import unittest

import _path_setup  # noqa: F401, E402

import assertion_template as template
from assertion_template import AssertionSpec


STRUCTURED_TWO = """\
---
id: req_implies_grnt
property: req_i |=> grnt_o
failure: Request asserted but grant not received next cycle
---
id: valid_stable
property: valid_i && !ready_i |=> valid_i && $stable(data_i)
failure: Valid dropped or data changed while waiting for ready
"""


def assembled(name: str = 'a_req', prop: str = 'req_i |=> grnt_o',
              failure: str = 'Request was not granted', indent: str = '') -> str:
    return (
        f'{indent}{name}: assert property (({prop}))\n'
        f'{indent}else begin\n'
        f'{indent}    $display("{failure}");\n'
        f'{indent}end'
    )


class TestPromptContract(unittest.TestCase):
    def test_the_prompt_requests_data_not_systemverilog(self):
        self.assertIn('Do NOT write SystemVerilog syntax', template.TEMPLATE_RULES)
        self.assertIn('Output ONLY assertion blocks', template.TEMPLATE_RULES)

    def test_the_three_fields_are_explicit(self):
        for field in ('id:', 'property:', 'failure:'):
            with self.subTest(field=field):
                self.assertIn(field, template.TEMPLATE_RULES)

    def test_the_module_placeholder_is_not_hard_coded(self):
        self.assertGreaterEqual(template.TEMPLATE_RULES.count('MODULE'), 3)

    def test_the_prompt_forbids_module_type_hierarchy_references(self):
        # A checker is bound below the DUT. ``MODULE.internal`` names a module
        # type rather than a checker input and VC Formal cannot resolve it while
        # computing assertion density.
        self.assertIn('Do NOT write MODULE.signal', template.TEMPLATE_RULES)
        self.assertRegex(
            template.TEMPLATE_RULES,
            r'explicitly declared as checker\s+inputs',
        )

    def test_the_example_obeys_the_documented_format(self):
        specs = template.parse_structured_assertions(
            template.STRUCTURED_OUTPUT_EXAMPLE)
        self.assertEqual(len(specs), 2)
        self.assertTrue(all(spec.id and spec.property and spec.failure
                            for spec in specs))

    def test_the_designer_marker_matches_the_generated_harness_contract(self):
        self.assertEqual(
            template.DESIGNER_MARKER,
            '//====DESIGNER-ADDED-SVA====//',
        )


class TestAssertionIdNormalisation(unittest.TestCase):
    def test_a_prefix_is_added(self):
        self.assertEqual(template.normalize_assertion_id('grant'), 'a_grant')

    def test_an_existing_prefix_is_not_duplicated(self):
        self.assertEqual(template.normalize_assertion_id('a_grant'), 'a_grant')

    def test_legacy_prefixes_are_replaced(self):
        for value in ('as__grant', '__grant'):
            with self.subTest(value=value):
                self.assertEqual(
                    template.normalize_assertion_id(value), 'a_grant')

    def test_surrounding_whitespace_is_ignored(self):
        self.assertEqual(
            template.normalize_assertion_id('  grant  '), 'a_grant')

    def test_spaces_and_punctuation_cannot_escape_the_label(self):
        self.assertEqual(
            template.normalize_assertion_id('Grant accepted!'),
            'a_grant_accepted',
        )

    def test_camel_case_is_converted_to_snake_case(self):
        self.assertEqual(
            template.normalize_assertion_id('ReqImpliesGrant'),
            'a_req_implies_grant',
        )

    def test_a_leading_digit_is_made_into_a_legal_identifier(self):
        self.assertEqual(
            template.normalize_assertion_id('3_cycle_response'),
            'a_assertion_3_cycle_response',
        )

    def test_an_empty_identifier_gets_a_deterministic_name(self):
        for value in ('', '   ', 'a_', 'as__'):
            with self.subTest(value=value):
                self.assertEqual(
                    template.normalize_assertion_id(value), 'a_assertion')

    def test_the_result_is_always_a_systemverilog_identifier(self):
        values = ('hello-world', '9 lives', 'a_MixedCase', '***', 'réponse')
        identifier = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
        for value in values:
            with self.subTest(value=value):
                self.assertRegex(
                    template.normalize_assertion_id(value), identifier)


class TestDisplayStringEscaping(unittest.TestCase):
    def test_quotes_are_escaped(self):
        self.assertEqual(
            template.escape_display_string('expected "valid"'),
            r'expected \"valid\"',
        )

    def test_backslashes_are_escaped_before_quotes(self):
        self.assertEqual(
            template.escape_display_string(r'path C:\rtl'),
            r'path C:\\rtl',
        )

    def test_line_breaks_cannot_break_the_string_literal(self):
        self.assertEqual(
            template.escape_display_string('first\nsecond\rthird'),
            r'first\nsecond\rthird',
        )

    def test_tabs_are_kept_as_an_escape_sequence(self):
        self.assertEqual(
            template.escape_display_string('left\tright'),
            r'left\tright',
        )

    def test_generated_display_stays_on_one_source_line(self):
        result = template.assemble_assertion(
            'message', 'valid_i', 'first\n"second"')
        display_lines = [line for line in result.splitlines()
                         if '$display' in line]
        self.assertEqual(len(display_lines), 1)
        self.assertIn(r'first\n\"second\"', display_lines[0])


class TestAssertionAssembly(unittest.TestCase):
    def test_a_structured_spec_becomes_the_canonical_template(self):
        self.assertEqual(
            template.assemble_assertion(
                'req', 'req_i |=> grnt_o', 'Request was not granted'),
            assembled(),
        )

    def test_existing_outer_parentheses_are_not_added_again(self):
        result = template.assemble_assertion(
            'req', '(req_i |=> grnt_o)', 'failed')
        self.assertIn('assert property ((req_i |=> grnt_o))', result)
        self.assertNotIn('assert property (((req_i |=> grnt_o)))', result)

    def test_indentation_applies_to_every_generated_line(self):
        result = template.assemble_assertion(
            'req', 'req_i', 'failed', indent='    ')
        self.assertTrue(all(line.startswith('    ')
                            for line in result.splitlines()))

    def test_empty_failure_text_gets_a_useful_default(self):
        result = template.assemble_assertion('req', 'req_i', '  ')
        self.assertIn('$display("Assertion a_req failed");', result)

    def test_multiple_specs_keep_order(self):
        specs = [
            AssertionSpec('first', 'a_i', 'first failed'),
            AssertionSpec('second', 'b_i', 'second failed'),
        ]
        result = template.assemble_assertions(specs)
        self.assertEqual(
            [template.get_assertion_name(item) for item in result],
            ['a_first', 'a_second'],
        )

    def test_an_empty_property_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'property'):
            template.assemble_assertion('empty', '   ', 'failed')

    def test_an_assert_property_wrapper_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'expression only'):
            template.assemble_assertion(
                'wrapped', 'assert property (a_i)', 'failed')

    def test_a_trailing_semicolon_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'semicolon'):
            template.assemble_assertion('semicolon', 'a_i;', 'failed')

    def test_an_explicit_clock_event_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'clock'):
            template.assemble_assertion(
                'clocked', '@(posedge clk_i) a_i', 'failed')

    def test_combinational_assembly_drops_an_explicit_clock_event(self):
        result = template.assemble_assertion(
            'add', '@(posedge clk) opcode == 0 |-> result == sum',
            'failed', module_type='combinational')
        self.assertIn('assert property ((opcode == 0 |-> result == sum))', result)
        self.assertNotIn('@(', result)

    def test_combinational_extraction_strips_a_legacy_clock_event(self):
        source = (
            'a_x: assert property (@(posedge clk) a_i |-> b_i) '
            'else begin\n    $display("x");\nend')
        result = template.extract_assertions_from_response(
            source, module_type='combinational')
        self.assertEqual(len(result), 1)
        self.assertNotIn('@(', result[0])
        self.assertIn('a_i |-> b_i', result[0])


class TestStructuredOutputParsing(unittest.TestCase):
    def test_two_delimited_blocks_are_parsed_in_order(self):
        specs = template.parse_structured_assertions(STRUCTURED_TWO)
        self.assertEqual(
            [spec.id for spec in specs],
            ['req_implies_grnt', 'valid_stable'],
        )

    def test_field_names_are_case_insensitive(self):
        specs = template.parse_structured_assertions(
            '---\nID: upper\nPROPERTY: a_i\nFAILURE: failed\n')
        self.assertEqual(specs, [AssertionSpec('upper', 'a_i', 'failed')])

    def test_indented_delimiters_are_accepted(self):
        specs = template.parse_structured_assertions(
            '  ---  \n id: one\n property: a_i\n failure: failed\n')
        self.assertEqual(len(specs), 1)

    def test_crlf_output_is_accepted(self):
        specs = template.parse_structured_assertions(
            '---\r\nid: one\r\nproperty: a_i\r\nfailure: failed\r\n')
        self.assertEqual(len(specs), 1)

    def test_a_missing_failure_gets_a_default(self):
        specs = template.parse_structured_assertions(
            '---\nid: one\nproperty: a_i\n')
        self.assertEqual(specs[0].failure, 'Assertion one failed')

    def test_a_multiline_property_is_joined_without_losing_tokens(self):
        specs = template.parse_structured_assertions("""\
---
id: held_until_ready
property: valid_i && !ready_i
  |=> valid_i && $stable(data_i)
failure: output changed
""")
        self.assertEqual(
            specs[0].property,
            'valid_i && !ready_i |=> valid_i && $stable(data_i)',
        )

    def test_a_multiline_failure_is_joined_as_plain_text(self):
        specs = template.parse_structured_assertions("""\
---
id: held
property: valid_i
failure: Valid dropped
  while ready was low
""")
        self.assertEqual(
            specs[0].failure, 'Valid dropped while ready was low')

    def test_markdown_fences_around_the_whole_response_are_ignored(self):
        specs = template.parse_structured_assertions(
            '```text\n---\nid: one\nproperty: a_i\nfailure: failed\n```\n')
        self.assertEqual(specs, [AssertionSpec('one', 'a_i', 'failed')])

    def test_commentary_before_a_delimiter_is_not_treated_as_a_block(self):
        specs = template.parse_structured_assertions("""\
I chose this property:
id: not_an_assertion
property: prose
---
id: real
property: a_i
failure: failed
""")
        self.assertEqual([spec.id for spec in specs], ['real'])

    def test_a_clean_single_block_can_omit_the_delimiter_for_compatibility(self):
        specs = template.parse_structured_assertions(
            'id: one\nproperty: a_i\nfailure: failed\n')
        self.assertEqual(specs, [AssertionSpec('one', 'a_i', 'failed')])

    def test_prose_makes_an_undelimited_block_ambiguous_and_it_is_rejected(self):
        specs = template.parse_structured_assertions(
            'Here is the result:\nid: one\nproperty: a_i\nfailure: failed\n')
        self.assertEqual(specs, [])

    def test_a_duplicate_field_rejects_the_block(self):
        specs = template.parse_structured_assertions(
            '---\nid: one\nid: two\nproperty: a_i\nfailure: failed\n')
        self.assertEqual(specs, [])

    def test_an_unknown_field_rejects_the_block(self):
        specs = template.parse_structured_assertions(
            '---\nid: one\nproperty: a_i\nseverity: fatal\nfailure: failed\n')
        self.assertEqual(specs, [])

    def test_empty_required_fields_reject_the_block(self):
        for text in (
            '---\nid:\nproperty: a_i\nfailure: failed\n',
            '---\nid: one\nproperty:\nfailure: failed\n',
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    template.parse_structured_assertions(text), [])

    def test_one_bad_block_does_not_discard_the_valid_one(self):
        specs = template.parse_structured_assertions("""\
---
id:
property: bad_i
---
id: valid
property: valid_i
failure: failed
""")
        self.assertEqual([spec.id for spec in specs], ['valid'])

    def test_an_expression_only_violation_is_filtered_before_source_emission(self):
        result = template.extract_assertions_from_response("""\
---
id: bad
property: assert property (a_i)
failure: failed
---
id: good
property: b_i
failure: failed
""")
        self.assertEqual(
            [template.get_assertion_name(item) for item in result],
            ['a_good'],
        )


class TestLegacyAssembledAssertionExtraction(unittest.TestCase):
    def test_an_already_assembled_template_is_preserved(self):
        source = assembled()
        self.assertEqual(
            template.extract_assertions_from_response(source), [source])

    def test_prose_and_markdown_after_an_assertion_are_not_emitted(self):
        source = assembled() + '\n```\nThis assertion checks the request.'
        self.assertEqual(
            template.extract_assertions_from_response(source), [assembled()])

    def test_prose_between_two_assertions_belongs_to_neither(self):
        first = assembled('a_first', 'a_i', 'first failed')
        second = assembled('a_second', 'b_i', 'second failed')
        source = first + '\nExplanation between blocks.\n' + second
        self.assertEqual(
            template.extract_assertions_from_response(source),
            [first, second],
        )

    def test_a_one_line_assertion_without_an_else_is_templated(self):
        result = template.extract_assertions_from_response(
            'raw: assert property (req_i |=> grnt_o);')
        self.assertEqual(
            result,
            [assembled('a_raw', 'req_i |=> grnt_o',
                       'Assertion a_raw failed')],
        )

    def test_multiple_one_line_assertions_are_extracted(self):
        result = template.extract_assertions_from_response(
            'one: assert property (a_i);\ntwo: assert property (b_i);')
        self.assertEqual(
            [template.get_assertion_name(item) for item in result],
            ['a_one', 'a_two'],
        )

    def test_a_literal_backslash_n_inside_display_is_not_decoded(self):
        source = assembled(failure=r'first\nsecond')
        self.assertEqual(
            template.extract_assertions_from_response(source), [source])

    def test_an_api_response_with_escaped_line_breaks_is_decoded(self):
        source = assembled().replace('\n', r'\n')
        self.assertEqual(
            template.extract_assertions_from_response(source), [assembled()])

    def test_assert_property_in_prose_is_not_an_assertion(self):
        self.assertEqual(
            template.extract_assertions_from_response(
                'Use assert property to check this behavior.'),
            [],
        )

    def test_a_header_inside_a_line_comment_is_ignored(self):
        source = '// fake: assert property (bad_i);\n' + assembled()
        self.assertEqual(
            template.extract_assertions_from_response(source), [assembled()])

    def test_a_header_inside_a_block_comment_is_ignored(self):
        source = '/*\nfake: assert property (bad_i);\n*/\n' + assembled()
        self.assertEqual(
            template.extract_assertions_from_response(source), [assembled()])


class TestAssertionNamesAndRenaming(unittest.TestCase):
    def test_the_name_is_read_from_a_template(self):
        self.assertEqual(template.get_assertion_name(assembled()), 'a_req')

    def test_keyword_case_does_not_change_the_name(self):
        source = assembled().replace('assert property', 'ASSERT PROPERTY')
        self.assertEqual(template.get_assertion_name(source), 'a_req')

    def test_a_missing_label_has_no_name(self):
        self.assertIsNone(
            template.get_assertion_name('assert property (a_i);'))

    def test_rename_changes_only_the_label(self):
        source = assembled(failure='a_req also appears in this message')
        result = template.rename_assertion(source, 'replacement')
        self.assertEqual(template.get_assertion_name(result), 'a_replacement')
        self.assertIn('a_req also appears in this message', result)

    def test_rename_preserves_outer_whitespace_and_indentation(self):
        source = '\n    ' + assembled(indent='    ') + '\n\n'
        result = template.rename_assertion(source, 'new name')
        self.assertTrue(result.startswith('\n        a_new_name:'))
        self.assertTrue(result.endswith('\n\n'))

    def test_renaming_text_without_an_assertion_is_a_noop(self):
        source = 'logic a;\n'
        self.assertEqual(template.rename_assertion(source, 'new'), source)


class TestPropertyTextForTheParser(unittest.TestCase):
    """The assertion reduced to the property the database converts to clauses."""

    def test_the_action_block_is_removed(self):
        self.assertEqual(template.assertion_property_text(assembled()),
                         'a_req: assert property ((req_i |=> grnt_o));')

    def test_the_body_is_returned_verbatim(self):
        source = assembled(prop='@(posedge clk_i) disable iff (!rst_ni) '
                                '(a_i && b_i) |=> c_o')
        self.assertEqual(
            template.assertion_property_text(source),
            'a_req: assert property ((@(posedge clk_i) disable iff (!rst_ni) '
            '(a_i && b_i) |=> c_o));')

    def test_a_failure_message_is_not_mistaken_for_the_property(self):
        source = assembled(failure='use assert property (other_i |=> thing_o)')
        self.assertEqual(template.assertion_property_text(source),
                         'a_req: assert property ((req_i |=> grnt_o));')

    def test_text_without_an_assertion_has_no_property_text(self):
        self.assertIsNone(template.assertion_property_text('logic a;\n'))


class TestDesignerSectionSplitting(unittest.TestCase):
    def test_no_marker_means_no_designer_assertions(self):
        self.assertEqual(
            template.split_designer_assertion_blocks(assembled()), [])

    def test_assertions_before_the_marker_are_ignored(self):
        after = assembled('a_after')
        contents = (
            assembled('a_before') + '\n' + template.DESIGNER_MARKER
            + '\n' + after + '\nendmodule\n'
        )
        self.assertEqual(
            template.split_designer_assertion_blocks(contents), [after])

    def test_two_blocks_are_returned_individually_and_in_order(self):
        first = assembled('a_first', 'a_i', 'first failed')
        second = assembled('a_second', 'b_i', 'second failed')
        contents = (
            template.DESIGNER_MARKER + '\n' + first + '\n\n' + second
            + '\nendmodule\n'
        )
        self.assertEqual(
            template.split_designer_assertion_blocks(contents),
            [first, second],
        )

    def test_comments_between_blocks_belong_to_neither(self):
        first = assembled('a_first', 'a_i', 'first failed')
        second = assembled('a_second', 'b_i', 'second failed')
        contents = (
            template.DESIGNER_MARKER + '\n' + first
            + '\n// explanation for the next property\n' + second
            + '\nendmodule\n'
        )
        self.assertEqual(
            template.split_designer_assertion_blocks(contents),
            [first, second],
        )

    def test_an_endmodule_word_in_a_failure_message_does_not_truncate(self):
        block = assembled(failure='endmodule must remain low')
        contents = (
            template.DESIGNER_MARKER + '\n' + block + '\nendmodule\n')
        self.assertEqual(
            template.split_designer_assertion_blocks(contents), [block])

    def test_a_commented_out_assertion_is_not_loaded_as_active(self):
        fake = '\n'.join('// ' + line for line in assembled('a_fake').splitlines())
        real = assembled('a_real')
        contents = (
            template.DESIGNER_MARKER + '\n' + fake + '\n' + real
            + '\nendmodule\n'
        )
        self.assertEqual(
            template.split_designer_assertion_blocks(contents), [real])

    def test_a_block_commented_assertion_is_not_loaded_as_active(self):
        fake = '/*\n' + assembled('a_fake') + '\n*/'
        real = assembled('a_real')
        contents = (
            template.DESIGNER_MARKER + '\n' + fake + '\n' + real
            + '\nendmodule\n'
        )
        self.assertEqual(
            template.split_designer_assertion_blocks(contents), [real])

    def test_content_after_endmodule_is_ignored(self):
        real = assembled('a_real')
        later = assembled('a_other')
        contents = (
            template.DESIGNER_MARKER + '\n' + real + '\nendmodule\n' + later)
        self.assertEqual(
            template.split_designer_assertion_blocks(contents), [real])


class TestEndToEndConversion(unittest.TestCase):
    def test_structured_text_becomes_two_separated_sva_blocks(self):
        result = template.process_llm_output_to_sv(STRUCTURED_TWO)
        self.assertEqual(result.count(': assert property'), 2)
        self.assertIn('\n\n', result)
        self.assertEqual(
            [template.get_assertion_name(block)
             for block in template.extract_assertions_from_response(result)],
            ['a_req_implies_grnt', 'a_valid_stable'],
        )

    def test_empty_input_produces_empty_output(self):
        self.assertEqual(template.process_llm_output_to_sv(' \n '), '')

    def test_malformed_input_never_becomes_source_code(self):
        self.assertEqual(
            template.process_llm_output_to_sv(
                'Here are some ideas, but no assertion blocks.'), '')

    def test_conversion_is_idempotent_for_canonical_output(self):
        once = template.process_llm_output_to_sv(STRUCTURED_TWO)
        twice = template.process_llm_output_to_sv(once)
        self.assertEqual(twice, once)

    def test_every_emitted_block_has_a_legal_prefixed_unique_name(self):
        source = """\
---
id: First Result
property: a_i
failure: first
---
id: 2nd-result
property: b_i
failure: second
"""
        blocks = template.extract_assertions_from_response(source)
        names = [template.get_assertion_name(block) for block in blocks]
        self.assertEqual(names, ['a_first_result', 'a_assertion_2nd_result'])
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(re.match(r'^a_[A-Za-z0-9_]+$', name)
                            for name in names))


if __name__ == '__main__':
    unittest.main(verbosity=2)
