#!/usr/bin/env python3
"""One-task DATE mutant detection for an HPC job.

Each Slurm job owns a private scratch tree and copies either the golden
RTL or one frozen mutant onto the *same* relative path the filelist
already names (``design.sv``, not ``design_i.sv``). Task 0 proves the
reference and writes the contract; tasks 1..N prove one mutant and write
a shard. The watcher merges shards after every job has downloaded.

This module's ``--print-task`` mode uses only the stdlib so the Slurm
host can map ``TASK_ID`` before Singularity starts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from typing import Dict, List, Optional, Sequence, Tuple

ROLE_REFERENCE = 'reference'
ROLE_MUTANT = 'mutant'
ROLE_SKIP = 'skip'

CONTRACT_NAME = 'contract.json'
SHARD_DIRNAME = 'shards'
MERGED_MARKER = 'merged.ok'


def task_role(task_id: int, n_mutants: int) -> Tuple[str, Optional[int]]:
    """Map a Slurm array index onto reference / mutant / unused slot."""
    if task_id < 0:
        raise ValueError(f'task_id must be >= 0, got {task_id}')
    if task_id == 0:
        return ROLE_REFERENCE, None
    index = task_id - 1
    if index >= n_mutants:
        return ROLE_SKIP, None
    return ROLE_MUTANT, index


def load_manifest_ids(path: str) -> List[str]:
    with open(path, encoding='utf-8') as handle:
        values = json.load(handle)
    if not isinstance(values, list):
        raise ValueError(f'{path} is not a mutant-manifest list')
    return [str(item['mutant_id']) for item in values]


def print_task_label(task_id: int, manifest_path: str) -> str:
    """Stdout token consumed by ``eval_mutation.slurm.sh``: role or mutant id."""
    ids = load_manifest_ids(manifest_path)
    role, index = task_role(task_id, len(ids))
    if role == ROLE_REFERENCE:
        return ROLE_REFERENCE
    if role == ROLE_SKIP:
        return ROLE_SKIP
    return ids[index]


def mutant_source_path(source_dir: str, mutant_id: str) -> str:
    for extension in ('.sv', '.v'):
        candidate = os.path.join(source_dir, mutant_id + extension)
        if os.path.isfile(candidate):
            return candidate
    raise FileNotFoundError(
        f'no {mutant_id}.sv/.v under {source_dir}')


def install_rtl_copy(source_path: str, dest_path: str) -> str:
    """Place one RTL body at the filelist path. Destination basename is kept."""
    parent = os.path.dirname(dest_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    shutil.copy2(source_path, dest_path)
    return dest_path


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: str) -> str:
    with open(path, 'rb') as handle:
        return _sha256_bytes(handle.read())


def verify_installed_rtl(
    installed_path: str,
    expected_source: str,
    golden_path: str,
    *,
    role: str,
    mutant_id: str = '',
    expected_path: str = '',
) -> dict:
    """Fail closed unless the formal filelist path contains the expected RTL.

    Mutation workers deliberately keep the golden file name because generated
    filelists point at that path.  That makes a missed copy indistinguishable
    from a real surviving mutant unless the bytes are checked before formal.
    The returned record is written into the task shard for later harvesting.
    """
    if role not in {ROLE_REFERENCE, ROLE_MUTANT}:
        raise ValueError(f'cannot verify RTL for task role {role!r}')
    if not os.path.isfile(installed_path):
        raise FileNotFoundError(f'installed RTL is missing: {installed_path}')
    if not os.path.isfile(golden_path):
        raise FileNotFoundError(f'golden RTL is missing: {golden_path}')

    installed_sha256 = _sha256_file(installed_path)
    expected_sha256 = (
        _sha256_file(expected_path)
        if expected_path else
        _sha256_bytes(expected_source.encode())
    )
    golden_sha256 = _sha256_file(golden_path)
    matches_expected = installed_sha256 == expected_sha256
    differs_from_golden = installed_sha256 != golden_sha256

    if not matches_expected:
        label = mutant_id or role
        raise RuntimeError(
            f'RTL provenance mismatch for {label}: filelist path '
            f'{installed_path} has {installed_sha256}, expected '
            f'{expected_sha256}')
    if role == ROLE_MUTANT and not differs_from_golden:
        raise RuntimeError(
            f'mutant task {mutant_id or "unknown"} installed the golden RTL '
            f'at {installed_path}')
    if role == ROLE_REFERENCE and differs_from_golden:
        raise RuntimeError(
            f'reference task did not install the golden RTL at {installed_path}')

    return {
        'role': role,
        'mutant_id': mutant_id,
        'installed_path': os.path.abspath(installed_path),
        'golden_path': os.path.abspath(golden_path),
        'expected_path': (
            os.path.abspath(expected_path) if expected_path else ''),
        'installed_sha256': installed_sha256,
        'expected_sha256': expected_sha256,
        'golden_sha256': golden_sha256,
        'installed_matches_expected': matches_expected,
        'installed_differs_from_golden': differs_from_golden,
    }


def shard_path(out_dir: str, name: str) -> str:
    return os.path.join(out_dir, SHARD_DIRNAME, f'{name}.json')


def write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write('\n')
    os.replace(tmp, path)


def load_json(path: str) -> dict:
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


def qualifications_of(result) -> Dict[str, str]:
    return {
        name: record.qualification.value
        for name, record in result.records.items()
    }


def result_from_qualifications(
    qualifications: Dict[str, str],
    *,
    compile_failed: bool = False,
):
    from proof_status import (
        FormalRunResult,
        FormalRunSummary,
        ProofStatus,
        PropertyRecord,
        Qualification,
        VacuityStatus,
    )

    records = {}
    for name, raw in qualifications.items():
        qualification = Qualification(raw)
        if qualification is Qualification.PROVED_NON_VACUOUS:
            record = PropertyRecord(
                name=name,
                proof_status=ProofStatus.PROVEN,
                vacuity_status=VacuityStatus.NON_VACUOUS,
            )
        elif qualification is Qualification.PROVED_VACUOUS:
            record = PropertyRecord(
                name=name,
                proof_status=ProofStatus.PROVEN,
                vacuity_status=VacuityStatus.VACUOUS,
            )
        elif qualification is Qualification.FAILING_PROPERTY_MISMATCH:
            record = PropertyRecord(
                name=name, proof_status=ProofStatus.FALSIFIED)
        elif qualification is Qualification.FAILING_MISSING_ASSUMPTION:
            record = PropertyRecord(
                name=name,
                proof_status=ProofStatus.FALSIFIED,
                missing_assumptions=['restored'],
            )
        else:
            record = PropertyRecord(
                name=name, proof_status=ProofStatus.INCONCLUSIVE)
        records[name] = record
    result = FormalRunResult(records=records)
    result.summary = FormalRunSummary(compile_failed=compile_failed)
    return result


def contract_payload(evaluator, rtl_provenance: Optional[dict] = None) -> dict:
    reference = evaluator.reference
    return {
        'module': evaluator.module,
        'method': evaluator.config.method,
        'timeout_s': evaluator.config.timeout_s,
        'contract': list(evaluator.contract),
        'reference_qualifications': (
            qualifications_of(reference) if reference is not None else {}),
        'compile_failed': bool(
            reference is not None and reference.summary.compile_failed),
        'error': '',
        'valid': bool(evaluator.contract),
        'rtl_provenance': dict(rtl_provenance or {}),
    }


def contract_payload_usable(payload: dict) -> bool:
    """A mutation contract must be non-empty and come from the golden RTL."""
    if not isinstance(payload, dict):
        return False
    if payload.get('compile_failed') or payload.get('error'):
        return False
    if payload.get('valid') is False or not payload.get('contract'):
        return False
    provenance = payload.get('rtl_provenance') or {}
    return bool(
        provenance.get('role') == ROLE_REFERENCE
        and provenance.get('installed_matches_expected')
        and not provenance.get('installed_differs_from_golden')
    )


def attach_contract(evaluator, payload: dict) -> None:
    if not contract_payload_usable(payload):
        raise ValueError('reference contract is empty, failed, or lacks RTL provenance')
    evaluator.contract = list(payload.get('contract') or [])
    evaluator.reference = result_from_qualifications(
        payload.get('reference_qualifications') or {},
        compile_failed=bool(payload.get('compile_failed')),
    )


def outcome_shard(mutant_id: str, outcome, extra: Optional[dict] = None) -> dict:
    payload = {
        'role': ROLE_MUTANT,
        'mutant_id': mutant_id,
        'detected': bool(outcome.detected_by),
        'detected_by': list(outcome.detected_by),
        'weakened': list(outcome.weakened),
        'compile_failed': bool(outcome.compile_failed),
        'timed_out': bool(outcome.timed_out),
        'seconds': float(outcome.seconds),
        'escape_reason': outcome.escape_reason,
        'equivalence': '',
    }
    if extra:
        payload.update(extra)
    return payload


def apply_shard_to_mutant(mutant, shard: dict) -> bool:
    """Replay a shard onto a frozen Mutant. True when it stays in the score."""
    from mutation import EquivalenceVerdict

    mutant.detected_by = list(shard.get('detected_by') or [])
    mutant.escape_reason = shard.get('escape_reason') or ''
    timed_out = bool(shard.get('timed_out'))
    compile_failed = bool(shard.get('compile_failed'))
    if timed_out and not mutant.detected_by:
        mutant.equivalence = EquivalenceVerdict.UNKNOWN
        return False
    if compile_failed:
        mutant.equivalence = EquivalenceVerdict.UNKNOWN
        return False
    return True


def expected_shard_names(mutant_ids: Sequence[str]) -> List[str]:
    return [ROLE_REFERENCE] + list(mutant_ids)


def shard_payload_usable(payload: dict, expected_name: str) -> bool:
    """True only for the exact reference/mutant task and verified RTL bytes."""
    if not isinstance(payload, dict) or payload.get('error'):
        return False
    provenance = payload.get('rtl_provenance') or {}
    if not provenance.get('installed_matches_expected'):
        return False
    if expected_name == ROLE_REFERENCE:
        return bool(
            payload.get('role') == ROLE_REFERENCE
            and int(payload.get('contract_size') or 0) > 0
            and not payload.get('compile_failed')
            and provenance.get('role') == ROLE_REFERENCE
            and not provenance.get('installed_differs_from_golden')
        )
    return bool(
        payload.get('role') == ROLE_MUTANT
        and payload.get('mutant_id') == expected_name
        and provenance.get('role') == ROLE_MUTANT
        and provenance.get('mutant_id') == expected_name
        and provenance.get('installed_differs_from_golden')
    )


def shard_usable(path: str, expected_name: str) -> bool:
    if not os.path.isfile(path):
        return False
    try:
        payload = load_json(path)
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    return shard_payload_usable(payload, expected_name)


def shards_complete(out_dir: str, mutant_ids: Sequence[str]) -> bool:
    contract = os.path.join(out_dir, CONTRACT_NAME)
    try:
        payload = load_json(contract)
    except (OSError, json.JSONDecodeError, ValueError):
        return False
    if not contract_payload_usable(payload):
        return False
    return all(
        shard_usable(shard_path(out_dir, name), name)
        for name in expected_shard_names(mutant_ids)
    )


def try_merge(
    out_dir: str,
    mutant_ids: Sequence[str],
    *,
    rtl: str,
    formal_tool: str,
    method: str,
    mutant_manifest: str,
    mutant_source_dir: str,
    golden_rtl_path: str,
    log=print,
) -> bool:
    """Write the sensitivity report when every shard is present.

    Uses a directory lock so two finishing tasks cannot clobber the report.
    """
    marker = os.path.join(out_dir, MERGED_MARKER)
    if os.path.isfile(marker):
        return False
    if not shards_complete(out_dir, mutant_ids):
        return False

    lock = os.path.join(out_dir, '.merge.lock')
    try:
        os.mkdir(lock)
    except FileExistsError:
        return False

    try:
        if os.path.isfile(marker) or not shards_complete(out_dir, mutant_ids):
            return False
        _merge_locked(
            out_dir,
            mutant_ids,
            rtl=rtl,
            formal_tool=formal_tool,
            method=method,
            mutant_manifest=mutant_manifest,
            mutant_source_dir=mutant_source_dir,
            golden_rtl_path=golden_rtl_path,
            log=log,
        )
        write_json(marker, {'merged': True})
        return True
    finally:
        try:
            os.rmdir(lock)
        except OSError:
            pass


def _merge_locked(
    out_dir: str,
    mutant_ids: Sequence[str],
    *,
    rtl: str,
    formal_tool: str,
    method: str,
    mutant_manifest: str,
    mutant_source_dir: str,
    golden_rtl_path: str,
    log,
) -> None:
    from evaluate import EvaluationConfig, MutationEvaluator, MutantOutcome
    import mutation

    contract = load_json(os.path.join(out_dir, CONTRACT_NAME))
    mutants = mutation.load_mutants(mutant_manifest, mutant_source_dir)
    by_id = {item.mutant_id: item for item in mutants}
    missing = [name for name in mutant_ids if name not in by_id]
    if missing:
        raise ValueError(
            'frozen mutant source(s) missing for: ' + ', '.join(missing))
    ordered = [by_id[name] for name in mutant_ids if name in by_id]

    report_rtl = golden_rtl_path if os.path.isfile(golden_rtl_path) else rtl
    evaluator = MutationEvaluator(
        EvaluationConfig(
            rtl=report_rtl,
            formal_tool=formal_tool,
            timeout_s=contract.get('timeout_s'),
            output_dir=out_dir,
            keep_mutant_sources=False,
            method=method,
        ),
        log=log,
    )
    attach_contract(evaluator, contract)

    applied = []
    formal_seconds = 0.0
    for mutant in ordered:
        shard = load_json(shard_path(out_dir, mutant.mutant_id))
        formal_seconds += float(shard.get('seconds') or 0)
        outcome = MutantOutcome(
            mutant_id=mutant.mutant_id,
            detected_by=list(shard.get('detected_by') or []),
            weakened=list(shard.get('weakened') or []),
            compile_failed=bool(shard.get('compile_failed')),
            timed_out=bool(shard.get('timed_out')),
            seconds=float(shard.get('seconds') or 0),
            escape_reason=shard.get('escape_reason') or '',
        )
        evaluator.outcomes[mutant.mutant_id] = outcome
        if apply_shard_to_mutant(mutant, shard):
            applied.append(mutant)

    ref_shard = load_json(shard_path(out_dir, ROLE_REFERENCE))
    formal_seconds += float(ref_shard.get('seconds') or 0)
    evaluator.harness.total_formal_seconds = formal_seconds
    evaluator.write_reports(ordered, applied)
    log(f'Merged {len(ordered)} mutant shards into {out_dir}/')


def run_reference(
    evaluator,
    out_dir: str,
    *,
    rtl_provenance: Optional[dict] = None,
) -> dict:
    try:
        evaluator.establish_contract()
        if not evaluator.contract:
            raise RuntimeError(
                'reference established no proved, non-vacuous contract')
        payload = contract_payload(evaluator, rtl_provenance)
    except Exception as error:
        payload = {
            'module': evaluator.module,
            'method': evaluator.config.method,
            'timeout_s': evaluator.config.timeout_s,
            'contract': [],
            'reference_qualifications': {},
            'compile_failed': True,
            'error': str(error),
            'valid': False,
            'rtl_provenance': dict(rtl_provenance or {}),
        }
        write_json(os.path.join(out_dir, CONTRACT_NAME), payload)
        write_json(
            shard_path(out_dir, ROLE_REFERENCE),
            {
                'role': ROLE_REFERENCE,
                'mutant_id': ROLE_REFERENCE,
                'seconds': 0.0,
                'contract_size': 0,
                'compile_failed': True,
                'error': str(error),
                'rtl_provenance': dict(rtl_provenance or {}),
            },
        )
        return payload

    write_json(os.path.join(out_dir, CONTRACT_NAME), payload)
    seconds = 0.0
    if evaluator.harness.runs:
        seconds = evaluator.harness.runs[-1].seconds
    write_json(
        shard_path(out_dir, ROLE_REFERENCE),
        {
            'role': ROLE_REFERENCE,
            'mutant_id': ROLE_REFERENCE,
            'seconds': seconds,
            'contract_size': len(evaluator.contract),
            'compile_failed': False,
            'error': '',
            'rtl_provenance': dict(rtl_provenance or {}),
        },
    )
    return payload


def run_mutant(
    evaluator,
    mutant,
    out_dir: str,
    *,
    golden_rtl: str,
    rtl_provenance: Optional[dict] = None,
) -> dict:
    if not evaluator.contract:
        raise RuntimeError(
            f'cannot score {mutant.mutant_id}: reference contract is empty')

    evaluator.evaluate_one(
        mutant,
        rtl_already_installed=True,
        golden_rtl=golden_rtl,
    )
    outcome = evaluator.outcomes[mutant.mutant_id]
    shard = outcome_shard(
        mutant.mutant_id,
        outcome,
        extra={
            'equivalence': mutant.equivalence.value,
            'rtl_provenance': dict(rtl_provenance or {}),
        },
    )
    write_json(shard_path(out_dir, mutant.mutant_id), shard)
    return shard


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='One Slurm-array task of DATE mutant detection')
    parser.add_argument('--print-task', action='store_true')
    parser.add_argument('--task-id', type=int, default=0)
    parser.add_argument('--rtl', default='')
    parser.add_argument('--out', default='')
    parser.add_argument('--mutant-manifest', default='')
    parser.add_argument('--mutant-source-dir', default='')
    parser.add_argument('--golden-rtl', default='')
    parser.add_argument('--formal-tool', default='vcformal')
    parser.add_argument('--method', default='svapshot')
    parser.add_argument('--timeout', type=float, default=600.0)
    parser.add_argument('--merge-only', action='store_true')
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.print_task:
        if not args.mutant_manifest:
            print('error: --mutant-manifest is required with --print-task',
                  file=sys.stderr)
            return 2
        print(print_task_label(args.task_id, args.mutant_manifest))
        return 0

    if not args.out:
        print('error: --out is required', file=sys.stderr)
        return 2
    os.makedirs(args.out, exist_ok=True)
    os.makedirs(os.path.join(args.out, SHARD_DIRNAME), exist_ok=True)

    ids = load_manifest_ids(args.mutant_manifest) if args.mutant_manifest else []
    golden = args.golden_rtl or args.rtl

    if args.merge_only:
        if not ids:
            print('error: --mutant-manifest is required to merge', file=sys.stderr)
            return 2
        merged = try_merge(
            args.out,
            ids,
            rtl=args.rtl,
            formal_tool=args.formal_tool,
            method=args.method,
            mutant_manifest=args.mutant_manifest,
            mutant_source_dir=args.mutant_source_dir,
            golden_rtl_path=golden,
        )
        return 0 if merged or os.path.isfile(
            os.path.join(args.out, MERGED_MARKER)) else 1

    from evaluate import EvaluationConfig, MutationEvaluator
    import mutation

    role, index = task_role(args.task_id, len(ids))
    if role == ROLE_SKIP:
        print(f'skip unused array task {args.task_id} (mutants={len(ids)})')
        return 0
    if not args.rtl:
        print('error: --rtl is required', file=sys.stderr)
        return 2
    if not golden or not os.path.isfile(golden):
        print(f'error: golden RTL is missing: {golden}', file=sys.stderr)
        return 2

    with open(golden, encoding='utf-8', errors='replace') as handle:
        golden_text = handle.read()
    mutant = None
    if role == ROLE_REFERENCE:
        expected_source = golden_text
        mutant_id = ''
        expected_path = golden
    else:
        mutants = mutation.load_mutants(
            args.mutant_manifest, args.mutant_source_dir)
        mutant = mutants[index]
        mutant_id = ids[index]
        if mutant.mutant_id != mutant_id:
            print(
                f'error: manifest task {args.task_id} maps to {mutant_id}, '
                f'but loaded source maps to {mutant.mutant_id}',
                file=sys.stderr,
            )
            return 2
        expected_source = mutant.source
        try:
            expected_path = mutant_source_path(
                args.mutant_source_dir, mutant_id)
        except FileNotFoundError as error:
            print(f'error: {error}', file=sys.stderr)
            return 2
        try:
            if os.path.samefile(args.rtl, golden):
                print(
                    'error: mutant task RTL path aliases the golden RTL',
                    file=sys.stderr,
                )
                return 3
        except OSError:
            pass
        install_rtl_copy(expected_path, args.rtl)
    try:
        provenance = verify_installed_rtl(
            args.rtl,
            expected_source,
            golden,
            role=role,
            mutant_id=mutant_id,
            expected_path=expected_path,
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(f'error: {error}', file=sys.stderr)
        return 3

    evaluator = MutationEvaluator(
        EvaluationConfig(
            rtl=args.rtl,
            formal_tool=args.formal_tool,
            timeout_s=args.timeout,
            output_dir=args.out,
            keep_mutant_sources=False,
            method=args.method,
        ),
    )
    if not evaluator.harness.is_ready():
        return 1

    if role == ROLE_REFERENCE:
        payload = run_reference(
            evaluator, args.out, rtl_provenance=provenance)
        if not contract_payload_usable(payload):
            return 1
    else:
        contract_file = os.path.join(args.out, CONTRACT_NAME)
        if not os.path.isfile(contract_file):
            print(f'error: missing contract at {contract_file}', file=sys.stderr)
            return 1
        try:
            attach_contract(evaluator, load_json(contract_file))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            print(f'error: unusable reference contract: {error}', file=sys.stderr)
            return 1
        run_mutant(
            evaluator,
            mutant,
            args.out,
            golden_rtl=golden_text,
            rtl_provenance=provenance,
        )

    try_merge(
        args.out,
        ids,
        rtl=args.rtl,
        formal_tool=args.formal_tool,
        method=args.method,
        mutant_manifest=args.mutant_manifest,
        mutant_source_dir=args.mutant_source_dir,
        golden_rtl_path=golden,
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
