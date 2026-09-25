#!/usr/bin/env python3
"""Large SVA → CNF conversion regression gate (CI stage 6).

Walks every assertion in ``tests/collateral/assertion_corpus.sv`` through
``SVAParser.convert_to_cnf`` (which calls ``flatten_to_cnf``) and fails when:

* the converter raises
* refusals rise above the recorded bound
* ``_cnf_imperfect`` flags more accepted properties than the recorded bound

This is a second opinion on the conversion pipeline, not a substitute for
``tests/test_svaparser_corpus.py``. Stage 1 still runs that suite on the
full corpus.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = REPO_ROOT / 'tests'
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(REPO_ROOT / 'src' / 'core'))
sys.path.insert(0, str(REPO_ROOT / 'src' / 'analysis'))
sys.path.insert(0, str(REPO_ROOT))

import _path_setup  # noqa: F401, E402
import agent as agent_module  # noqa: E402
from agent import _cnf_imperfect  # noqa: E402
from svaparser import SVAParser  # noqa: E402
from test_svaparser_corpus import (  # noqa: E402
    CORPUS,
    KNOWN_REFUSALS,
    load_corpus,
    parser_input,
)

# Accepted properties must not trip the agent quarantine gate.
KNOWN_IMPERFECT = 0
KNOWN_CRASHES = 0


def convert_one(parser: SVAParser, assertion: str):
    """Tokenize → convert_to_cnf / flatten_to_cnf, matching parse_sva's body path."""
    sva = parser_input(assertion)
    _name, _delay, body, _consequent_delay = parser._extract_structure(sva)
    tokens = parser.tokenize(body)
    return parser.convert_to_cnf(tokens)


def run(output_dir: Path | None = None) -> int:
    corpus = load_corpus()
    if not corpus:
        print(f'error: assertion corpus missing or empty: {CORPUS}', file=sys.stderr)
        return 1

    parser = SVAParser()
    crashes: list[tuple[str, str]] = []
    refused: list[str] = []
    imperfect: list[str] = []
    accepted = 0
    started = time.perf_counter()

    for assertion in corpus:
        try:
            clauses, postcondition = convert_one(parser, assertion)
        except Exception as exc:  # noqa: BLE001 - counted as a crash
            crashes.append((assertion, f'{type(exc).__name__}: {exc}'))
            continue
        if clauses is None:
            refused.append(assertion)
            continue
        accepted += 1
        if _cnf_imperfect(clauses, postcondition):
            imperfect.append(assertion)

    elapsed = time.perf_counter() - started
    summary = {
        'corpus': str(CORPUS),
        'assertions': len(corpus),
        'accepted': accepted,
        'refusals': len(refused),
        'imperfect': len(imperfect),
        'crashes': len(crashes),
        'bounds': {
            'max_refusals': KNOWN_REFUSALS,
            'max_imperfect': KNOWN_IMPERFECT,
            'max_crashes': KNOWN_CRASHES,
        },
        'elapsed_s': round(elapsed, 3),
        'pipeline': 'tokenize → SVAParser.convert_to_cnf → flatten_to_cnf',
    }

    print('SVA → CNF conversion (full assertion corpus)')
    print(f'  assertions: {len(corpus)}')
    print(f'  accepted:   {accepted}')
    print(f'  refusals:   {len(refused)} (bound {KNOWN_REFUSALS})')
    print(f'  imperfect:  {len(imperfect)} (bound {KNOWN_IMPERFECT})')
    print(f'  crashes:    {len(crashes)} (bound {KNOWN_CRASHES})')
    print(f'  elapsed:    {elapsed:.1f}s')

    regressions: list[str] = []
    if len(crashes) > KNOWN_CRASHES:
        regressions.append(
            f'converter crashed on {len(crashes)} assertions (bound {KNOWN_CRASHES})')
        for assertion, err in crashes[:10]:
            print(f'  crash: {err}: {assertion[:80]}', file=sys.stderr)
    if len(refused) > KNOWN_REFUSALS:
        regressions.append(
            f'refusals rose to {len(refused)} (bound {KNOWN_REFUSALS})')
        for assertion in refused[:10]:
            print(f'  refused: {assertion[:80]}', file=sys.stderr)
    if len(imperfect) > KNOWN_IMPERFECT:
        regressions.append(
            f'_cnf_imperfect flagged {len(imperfect)} (bound {KNOWN_IMPERFECT})')
        for assertion in imperfect[:10]:
            print(f'  imperfect: {assertion[:80]}', file=sys.stderr)

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        payload = dict(summary)
        payload['ok'] = not regressions
        payload['regressions'] = regressions
        payload['crash_samples'] = [
            {'assertion': assertion, 'error': err} for assertion, err in crashes[:20]
        ]
        payload['refusal_samples'] = refused[:20]
        payload['imperfect_samples'] = imperfect[:20]
        (output_dir / 'summary.json').write_text(
            json.dumps(payload, indent=2) + '\n', encoding='utf-8')

    if regressions:
        print('FAIL: ' + '; '.join(regressions), file=sys.stderr)
        return 1
    print('OK: SVA → CNF conversion within recorded bounds')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--output-dir',
        default=os.environ.get('SVAPSHOT_CNF_OUTPUT_DIR', ''),
        help='Write summary.json here (default: ci_artifacts/sva_cnf_conversion)',
    )
    args = parser.parse_args(argv)
    output = Path(args.output_dir) if args.output_dir else (
        REPO_ROOT / 'ci_artifacts' / 'sva_cnf_conversion')
    return run(output)


if __name__ == '__main__':
    sys.exit(main())
