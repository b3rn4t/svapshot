#!/usr/bin/env python3
"""Rewrite assertion_corpus.sv from the property files in the tree.

Every ``assert property`` in every ``*_prop.sv`` file is collected.  Assertions
that differ only in signal names or literal values have the same shape, and one
of each shape is kept, which holds the variety the parser has to handle without
storing thousands of near-copies.  Run this after a batch of SVApshot runs has
added property files worth covering, then check whether
tests/test_svaparser_corpus.py still holds and update KNOWN_REFUSALS if the
recorded bound moved.

    python3 tests/collateral/build_assertion_corpus.py [output.sv]
"""
import os
import re
import sys
import collections

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'assertion_corpus.sv')


def assertions():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in ('.git', '__pycache__', 'sargantana', 'SVALint')]
        for name in sorted(filenames):
            if not name.endswith('_prop.sv'):
                continue
            path = os.path.join(dirpath, name)
            try:
                text = open(path, errors='replace').read()
            except OSError:
                continue
            for match in re.finditer(r'\bassert\s+property', text, re.IGNORECASE):
                start = text.rfind('\n', 0, match.start()) + 1
                depth, end, i = 0, None, match.start()
                while i < len(text):
                    ch = text[i]
                    if ch == '(':
                        depth += 1
                    elif ch == ')':
                        depth -= 1
                    elif ch == ';' and depth == 0:
                        end = i + 1
                        break
                    i += 1
                if end is None:
                    continue
                yield os.path.relpath(path, ROOT), text[start:end]


def shape(text):
    """The assertion with every name and number replaced, so only the form is left."""
    text = re.sub(r"[0-9]+'[bhdoBHDO][0-9a-fA-FxzXZ_?]+", 'L', text)
    text = re.sub(r"'[0-9a-fA-FxzXZ]+", 'L', text)
    text = re.sub(r'\b[A-Za-z_][A-Za-z0-9_.:$]*\b', 'N', text)
    text = re.sub(r'\bN+\b', 'N', text)
    return re.sub(r'\s+', '', text)


distinct = {}
shapes = collections.Counter()
total = 0
commented = 0

for path, text in assertions():
    total += 1
    flat = ' '.join(text.split())
    if flat.startswith('//'):
        commented += 1
        continue
    if flat in distinct:
        continue
    distinct[flat] = path
    shapes[shape(flat)] += 1

print(f'assertions found      : {total}')
print(f'  commented out       : {commented}')
print(f'  distinct text       : {len(distinct)}')
print(f'  distinct shapes     : {len(shapes)}')

# One representative per shape, in file order, keeps the variety and the size down.
representatives = {}
for flat, path in distinct.items():
    key = shape(flat)
    representatives.setdefault(key, (flat, path))

with open(OUT, 'w') as handle:
    handle.write(
        '// Test collateral: assertions from every *_prop.sv file checked into\n'
        '// SVApshot, written by earlier runs over Sargantana, the PTW, the TLB,\n'
        '// the FPU blocks and the I2C master.  One assertion per line, whitespace\n'
        f'// collapsed.  {total} were found, {commented} of them commented out and\n'
        f'// dropped, {len(distinct)} distinct in text.  Assertions differing only\n'
        '// in signal names or literal values share a shape, and one of each shape\n'
        f'// is kept here: {len(representatives)} lines.\n'
        '//\n'
        '// Used by tests/test_svaparser_corpus.py.  Nothing else in the suite\n'
        '// depends on it: every other test input is a literal in its own file.\n')
    for flat, path in representatives.values():
        handle.write(f'{flat}\n')

print(f'  written             : {len(representatives)} -> {OUT}')
print(f'  size                : {os.path.getsize(OUT)} bytes')
