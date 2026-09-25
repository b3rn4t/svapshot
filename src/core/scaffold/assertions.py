"""Helpers that turn assertion ids and expressions into SystemVerilog text.

Shared by the harness scaffolder when it emits annotation-derived properties
and by unit tests that pin the spelling contract the formal tools expect.
"""

from __future__ import annotations


def normalize_assertion_id(assertion_id: str) -> str:
    name = assertion_id.strip()
    if name.startswith('as__'):
        name = name[4:]
    elif name.startswith('a_'):
        return name
    elif name.startswith('__'):
        name = name[2:]
    return 'a_' + name


def escape_display_string(text: str) -> str:
    return text.replace('\\', '\\\\').replace('"', '\\"')


def assemble_assertion(
    assertion_id: str,
    property_expr: str,
    failure_text: str,
    indent: str = '',
) -> str:
    name = normalize_assertion_id(assertion_id)
    prop = property_expr.strip()
    if prop.startswith('(') and prop.endswith(')'):
        inner = prop
    else:
        inner = '(' + prop + ')'
    if failure_text and failure_text.strip():
        failure = escape_display_string(failure_text.strip())
    else:
        failure = escape_display_string('Assertion ' + name + ' failed')
    return (
        indent + name + ': assert property ' + inner + '\n'
        + indent + 'else begin\n'
        + indent + '    $display("' + failure + '");\n'
        + indent + 'end'
    )


def write_assert(prop, name, expr, failure=None, indent=""):
    if failure is None:
        failure = 'Assertion ' + name + ' failed'
    block = assemble_assertion(name, expr, failure, indent=indent)
    prop.write(block)
    prop.write("\n")


def write_assume(prop, name, expr, indent=""):
    if name.startswith('m_'):
        label = name
    else:
        label = 'm_' + name
    prop.write(indent + label + ': assume property (' + expr + ');\n')
