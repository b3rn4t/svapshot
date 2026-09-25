#!/usr/bin/env python3
"""Condensed SVApshot evaluation experiment runner.

Defines and executes the agreed evaluation matrix from
``experiments/svapshot_evaluation.yaml``:

  * main (controlled): 21 designs x {svapshot, one_shot} x 3 seeds = 126
  * ablation: 6 designs x 4 conditions x 3 seeds = 72
  * common-model total: 198
  * sensitivity (optional): 6 designs x 4 models x 3 seeds = 72

Style mirrors ``multi_llm_experiment.py`` (argparse, file+console logger,
numbered progress, end summary) while driving the resumable
``publication_eval.runner.PublicationRunner``.

Examples:
  python3 src/core/publication_evaluation_experiment.py --list
  python3 src/core/publication_evaluation_experiment.py --dry-run --track main
  python3 src/core/publication_evaluation_experiment.py --execute --track main --limit 2
  python3 src/core/publication_evaluation_experiment.py --execute --track ablation
  python3 src/core/publication_evaluation_experiment.py --execute --track sensitivity
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, Sequence

CORE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(CORE_DIR))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from publication_eval.config import load_experiment
from publication_eval.runner import PublicationRunner

DEFAULT_CONFIG = os.path.join(REPO_ROOT, 'experiments', 'svapshot_evaluation.yaml')

# User-facing track names -> runner track names
TRACK_ALIASES = {
    'main': 'controlled',
    'controlled': 'controlled',
    'ablation': 'ablation',
    'sensitivity': 'model_sensitivity',
    'model_sensitivity': 'model_sensitivity',
    'pilot': 'pilot',
}

COMMON_TRACKS = ('controlled', 'ablation')


class ExperimentLogger:
    """Logger with console and file output (multi_llm_experiment style)."""

    def __init__(self, log_dir: str, experiment_name: Optional[str] = None):
        os.makedirs(log_dir, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        label = experiment_name or 'svapshot_evaluation'
        self.log_file_path = os.path.join(
            log_dir, f'publication_evaluation_{label}_{timestamp}.log')
        try:
            with open(self.log_file_path, 'w', encoding='utf-8') as handle:
                handle.write(
                    f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                    f"SVApshot Evaluation Experiment Log Started\n")
                handle.write('=' * 80 + '\n')
        except OSError as error:
            print(f'Warning: Failed to initialize log file: {error}')
            self.log_file_path = None

    def log(self, message: str, level: str = 'INFO') -> None:
        print(message)
        if not self.log_file_path:
            return
        try:
            with open(self.log_file_path, 'a', encoding='utf-8') as handle:
                handle.write(f'{message}\n')
        except OSError as error:
            print(f'Warning: Failed to write to log file: {error}')

    def finalize(self) -> None:
        if not self.log_file_path:
            return
        try:
            stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            with open(self.log_file_path, 'a', encoding='utf-8') as handle:
                handle.write(
                    f'[{stamp}] [INFO] SVApshot evaluation experiment completed\n')
                handle.write('=' * 80 + '\n')
            print(f'Full experiment log saved to: {self.log_file_path}')
        except OSError as error:
            print(f'Warning: Failed to finalize log: {error}')


def resolve_tracks(names: Sequence[str]) -> List[str]:
    if not names:
        return list(COMMON_TRACKS)
    resolved: List[str] = []
    for name in names:
        key = name.strip().lower()
        if key not in TRACK_ALIASES:
            raise SystemExit(
                f'Unknown track {name!r}. Choose from: '
                + ', '.join(sorted(TRACK_ALIASES)))
        track = TRACK_ALIASES[key]
        if track not in resolved:
            resolved.append(track)
    return resolved


def parse_arguments(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    epilog = """
Examples:
  List the condensed matrix (198 common-model cells by default):
    python3 src/core/publication_evaluation_experiment.py --list

  Dry-run main track stage commands:
    python3 src/core/publication_evaluation_experiment.py --dry-run --track main

  Execute a small smoke slice:
    python3 src/core/publication_evaluation_experiment.py --execute --track main --limit 2

  Ablations (4 conditions; no_rag removed because SVApshot no longer uses RAG):
    python3 src/core/publication_evaluation_experiment.py --execute --track ablation

  Optional model sensitivity (not in the 198 default total):
    python3 src/core/publication_evaluation_experiment.py --execute --track sensitivity
"""
    parser = argparse.ArgumentParser(
        description=(
            'SVApshot condensed evaluation experiment runner: expand and '
            'execute the main / ablation / sensitivity matrix from '
            'experiments/svapshot_evaluation.yaml'),
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        '--config', default=DEFAULT_CONFIG,
        help='Path to JSON-compatible evaluation protocol YAML')
    parser.add_argument(
        '--track', action='append', default=[],
        help=(
            'Track to include (repeatable). Aliases: main|controlled, '
            'ablation, sensitivity|model_sensitivity, pilot. '
            'Default for --list/--execute: main+ablation (198 cells).'))
    parser.add_argument('--design', action='append', default=[],
                        help='Filter to design id(s)')
    parser.add_argument('--method', action='append', default=[],
                        help='Filter to method name(s)')
    parser.add_argument('--model', action='append', default=[],
                        help='Filter to model id(s)')
    parser.add_argument('--seed', action='append', type=int, default=[],
                        help='Filter to generation seed(s), e.g. 1729')
    parser.add_argument('--list', action='store_true',
                        help='Print per-track cell counts and exit')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print cell ids and planned stage commands')
    parser.add_argument('--execute', action='store_true',
                        help='Execute selected cells via PublicationRunner')
    parser.add_argument('--limit', type=int, default=0,
                        help='Cap number of cells (0 = no limit)')
    parser.add_argument('--retry-failed', action='store_true',
                        help='Retry failed/timed_out cells')
    parser.add_argument('--force', action='store_true',
                        help='Rerun stages even if previously completed')
    parser.add_argument(
        '--block-reason', default='',
        help='Record selected unfinished cells as blocked without execution')
    parser.add_argument(
        '--experiment-name', '-en', default='',
        help='Optional label included in the log file name')
    parser.add_argument(
        '--common-model', default='',
        help='Override models.common.model_id for this invocation only')
    return parser.parse_args(argv)


def apply_common_model_override(runner: PublicationRunner, model_id: str) -> None:
    if not model_id:
        return
    runner.experiment.raw['models']['common']['model_id'] = model_id
    runner.experiment.raw['models']['common']['resolved_snapshot'] = model_id


def filter_cells(cells, designs, methods, models, seeds=None):
    selected = list(cells)
    if designs:
        allowed = set(designs)
        selected = [c for c in selected if c.design_id in allowed]
    if methods:
        allowed = set(methods)
        selected = [c for c in selected if c.method in allowed]
    if models:
        allowed = set(models)
        selected = [c for c in selected if c.model in allowed]
    if seeds:
        allowed = {int(seed) for seed in seeds}
        selected = [c for c in selected if int(c.seed) in allowed]
    return selected


def print_matrix_summary(
    logger: ExperimentLogger,
    runner: PublicationRunner,
    tracks: Sequence[str],
) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    logger.log('=' * 80)
    logger.log('SVApshot condensed evaluation matrix')
    logger.log('=' * 80)
    logger.log(f"Config: {runner.experiment.path}")
    logger.log(f"Study:  {runner.experiment.study_id}")
    logger.log(
        f"Status: {runner.experiment.raw.get('protocol_status')}")
    logger.log(
        f"Common model: "
        f"{runner.experiment.raw['models']['common']['model_id']}")
    logger.log('')

    broad = runner.experiment.selected(broad=True)
    ablation = runner.experiment.selected(ablation=True)
    logger.log(f'Main (broad) designs: {len(broad)}')
    for design in broad:
        logger.log(f'  - {design.id:32s} suite={design.suite:12s} '
                   f'stratum={design.stratum}')
    logger.log(f'Ablation designs: {len(ablation)}')
    for design in ablation:
        logger.log(f'  - {design.id}')
    logger.log(
        'Ablation conditions: '
        + ', '.join(runner.experiment.raw['methods']['ablations']))
    logger.log('')

    for track in tracks:
        cells = runner.matrix([track])
        counts[track] = len(cells)
        logger.log(f'Track {track:18s}: {len(cells):4d} cells')

    # Always report the full common-model budget, even when filtering tracks.
    full_common = (
        len(runner.matrix(['controlled'])) + len(runner.matrix(['ablation'])))
    logger.log('')
    logger.log(f'Common-model total (main+ablation): {full_common}')
    sensitivity_n = len(runner.matrix(['model_sensitivity']))
    logger.log(f'Sensitivity (extra, not in {full_common}): {sensitivity_n}')
    declared = runner.experiment.raw.get('counts', {})
    if declared:
        logger.log(
            f"Declared common_model_total: "
            f"{declared.get('common_model_total')}")
    return counts


def dry_run_cells(
    logger: ExperimentLogger,
    runner: PublicationRunner,
    cells,
) -> None:
    logger.log('=' * 80)
    logger.log(f'Dry-run: {len(cells)} cell(s)')
    logger.log('=' * 80)
    for index, cell in enumerate(cells, 1):
        stages = runner.stages(cell)
        logger.log(
            f'\n[{index}/{len(cells)}] {cell.id}\n'
            f'  track={cell.track} design={cell.design_id} '
            f'method={cell.method} model={cell.model} '
            f'seed={cell.seed} rep={cell.repetition}')
        if not stages:
            logger.log('  stages: (none — would be blocked / not runnable)')
            continue
        for stage in stages:
            cmd = ' '.join(stage.command)
            logger.log(f'  stage {stage.name}: {cmd}')
            logger.log(f'    timeout_s={stage.timeout_s}')


def execute_cells(
    logger: ExperimentLogger,
    runner: PublicationRunner,
    tracks: Sequence[str],
    args: argparse.Namespace,
) -> dict:
    logger.log('=' * 80)
    logger.log('Executing via PublicationRunner')
    logger.log('=' * 80)
    started = time.time()
    summary = runner.run(
        tracks=list(tracks),
        designs=args.design,
        methods=args.method,
        models=args.model,
        seeds=args.seed,
        execute=True,
        limit=args.limit,
        retry_failed=args.retry_failed,
        force=args.force,
        block_reason=args.block_reason,
    )
    elapsed = time.time() - started
    logger.log(json.dumps(summary, indent=2))
    logger.log(f'Wall time: {elapsed:.1f}s ({elapsed / 60.0:.1f} min)')
    return summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_arguments(argv)
    if not (args.list or args.dry_run or args.execute):
        print('Specify at least one of --list, --dry-run, or --execute.')
        return 2

    os.chdir(REPO_ROOT)
    config_path = args.config
    if not os.path.isabs(config_path):
        config_path = os.path.join(REPO_ROOT, config_path)
    if not os.path.isfile(config_path):
        print(f'Config not found: {config_path}')
        return 1

    experiment = load_experiment(config_path)
    warnings = experiment.validate(frozen=False)
    runner = PublicationRunner(experiment)
    apply_common_model_override(runner, args.common_model)

    log_dir = os.path.join(runner.root, 'evaluation_logs')
    logger = ExperimentLogger(log_dir, args.experiment_name or None)
    for warning in warnings:
        logger.log(f'WARNING: {warning}', level='WARNING')

    tracks = resolve_tracks(args.track)
    print_matrix_summary(logger, runner, tracks)

    cells = filter_cells(
        runner.matrix(tracks), args.design, args.method, args.model,
        args.seed)
    if args.limit and args.limit > 0:
        cells = cells[: args.limit]

    logger.log(f'\nSelected after filters: {len(cells)} cell(s)')
    if args.design:
        logger.log(f'  designs: {args.design}')
    if args.method:
        logger.log(f'  methods: {args.method}')
    if args.model:
        logger.log(f'  models: {args.model}')
    if args.seed:
        logger.log(f'  seeds: {args.seed}')
    if args.limit:
        logger.log(f'  limit: {args.limit}')

    if args.list and not args.dry_run and not args.execute:
        logger.finalize()
        return 0

    if args.dry_run:
        dry_run_cells(logger, runner, cells)

    summary = None
    if args.execute:
        summary = execute_cells(logger, runner, tracks, args)
        logger.log('\n' + '=' * 80)
        logger.log('EXPERIMENT SUMMARY')
        logger.log('=' * 80)
        if summary:
            for key in (
                'planned', 'completed', 'failed', 'timed_out',
                'blocked', 'skipped', 'running',
            ):
                if key in summary:
                    logger.log(f'  {key:12s}: {summary[key]}')
            for key, value in sorted(summary.items()):
                if key not in {
                    'planned', 'completed', 'failed', 'timed_out',
                    'blocked', 'skipped', 'running',
                }:
                    logger.log(f'  {key}: {value}')

    logger.finalize()

    if summary:
        failed = int(summary.get('failed', 0) or 0)
        timed_out = int(summary.get('timed_out', 0) or 0)
        if failed or timed_out:
            return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
