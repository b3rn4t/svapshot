"""Subprocess wrapper for running SVALint on SVA property files."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOCAL_SVALINT_ROOT = _REPO_ROOT / 'SVALint'
_SVALINT_ENTRY = Path('bin') / 'svalint.py'
_VERIBLE_NAME = 'verible-verilog-syntax'
# Kept for callers that still import the name. Resolution happens at call time
# via resolve_svalint_root(); this is never a host-specific home directory.
DEFAULT_SVALINT_ROOT = _LOCAL_SVALINT_ROOT


def is_svalint_checkout(root: Optional[Path | str]) -> bool:
    """True when *root* contains the SVALint entry point."""
    if not root:
        return False
    try:
        return (Path(root) / _SVALINT_ENTRY).is_file()
    except OSError:
        return False


def _cwd_svalint_candidates() -> List[Path]:
    try:
        here = Path.cwd().resolve()
    except OSError:
        return []
    candidates = []
    for parent in (here, *here.parents):
        candidates.append(parent / 'SVALint')
        if len(candidates) >= 8:
            break
    return candidates


def resolve_svalint_root(svalint_root: Optional[Path] = None) -> Path:
    """Pick a usable SVALint checkout independently of cwd and design tree.

    An explicit argument is returned as given so a missing test checkout still
    fails closed. Otherwise the vendored tree next to this module wins over
    ``SVALINT_ROOT`` and over ``$PWD/SVALint``, including a stale overlay path
    left by ``source setup_svapshot.sh`` from the wrong directory.
    """
    if svalint_root is not None:
        return Path(svalint_root)
    if is_svalint_checkout(_LOCAL_SVALINT_ROOT):
        return _LOCAL_SVALINT_ROOT.resolve()
    env = os.environ.get('SVALINT_ROOT')
    if is_svalint_checkout(env):
        return Path(env).resolve()
    for candidate in _cwd_svalint_candidates():
        if is_svalint_checkout(candidate):
            return candidate.resolve()
    return _LOCAL_SVALINT_ROOT


def resolve_verible_executable() -> Optional[Path]:
    """Find ``verible-verilog-syntax`` even when PATH omits ``~/.local/bin``."""
    found = shutil.which(_VERIBLE_NAME)
    if found:
        return Path(found)
    extras = (
        Path.home() / '.local' / 'bin' / _VERIBLE_NAME,
        Path('/usr/local/bin') / _VERIBLE_NAME,
        Path('/usr/bin') / _VERIBLE_NAME,
        Path.home() / 'bin' / _VERIBLE_NAME,
        _REPO_ROOT / 'tools' / _VERIBLE_NAME,
        _LOCAL_SVALINT_ROOT / 'bin' / _VERIBLE_NAME,
    )
    for path in extras:
        try:
            if path.is_file() and os.access(path, os.X_OK):
                return path
        except OSError:
            continue
    return None


def _svalint_env(config_file: Optional[Path] = None) -> dict:
    env = os.environ.copy()
    # An unrelated parent shell must not silently choose the rule set. Callers
    # that need a non-default configuration pass it explicitly.
    env.pop('SVALINT_CONFIG', None)
    if config_file is not None:
        env['SVALINT_CONFIG'] = str(config_file.resolve())
    verible = resolve_verible_executable()
    if verible is not None:
        bindir = str(verible.parent)
        path = env.get('PATH', '')
        parts = [part for part in path.split(os.pathsep) if part] if path else []
        if bindir not in parts:
            env['PATH'] = os.pathsep.join([bindir, *parts])
    return env


@dataclass
class SVALintViolation:
    rule_id: str
    message: str
    assertion_text: str = ''


@dataclass
class SVALintResult:
    returncode: int
    stdout: str
    stderr: str
    error_count: int
    warning_count: int
    violations: List[SVALintViolation] = field(default_factory=list)
    passed: bool = False
    execution_error: str = ''
    timed_out: bool = False


DEFAULT_TIMEOUT_S = 120.0
_ANSI_ESCAPE_RE = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


def _combine_output(stdout: str, stderr: str) -> str:
    """Join process streams without merging their boundary lines."""
    if not stdout:
        return stderr
    if not stderr:
        return stdout
    return stdout + ('' if stdout.endswith('\n') else '\n') + stderr


def _execution_failure(
    message: str,
    *,
    returncode: int,
    stdout: str = '',
    stderr: str = '',
    timed_out: bool = False,
) -> SVALintResult:
    return SVALintResult(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        error_count=0,
        warning_count=0,
        violations=[],
        passed=False,
        execution_error=message,
        timed_out=timed_out,
    )


def run_svalint(
    sv_file: Path,
    svalint_root: Optional[Path] = None,
    config_file: Optional[Path] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> SVALintResult:
    """Run SVALint on a property file, returning failures instead of raising.

    The wrapper is a gate. Missing inputs, a broken checkout, timeout, and
    process-launch failures must all keep it closed rather than being confused
    with a run that reported zero lint errors.
    """
    root = resolve_svalint_root(svalint_root)
    target = Path(sv_file).resolve()
    script = root / _SVALINT_ENTRY

    if not target.is_file():
        return _execution_failure(
            f'SVALint property file does not exist: {target}', returncode=2)
    if not root.is_dir():
        return _execution_failure(
            f'SVALint root does not exist: {root}', returncode=2)
    if not script.is_file():
        return _execution_failure(
            f'SVALint entry point does not exist: {script} '
            '(expected bin/svalint.py)',
            returncode=2,
        )

    # Fake checkouts used in unit tests have no Verible bridge. A real
    # checkout must see the binary even when the parent PATH is stripped.
    if (root / 'bin' / 'verible_verilog_syntax.py').is_file():
        if resolve_verible_executable() is None:
            return _execution_failure(
                'verible-verilog-syntax is not on PATH or in ~/.local/bin',
                returncode=2,
            )

    cmd = [sys.executable, 'bin/svalint.py', '-t', str(target)]
    env = _svalint_env(config_file)

    try:
        proc = subprocess.run(
            cmd,
            cwd=root,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ''
        stderr = exc.stderr or ''
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors='replace')
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors='replace')
        return _execution_failure(
            f'SVALint timed out after {timeout_s:g} seconds',
            returncode=124,
            stdout=stdout,
            stderr=stderr,
            timed_out=True,
        )
    except OSError as exc:
        return _execution_failure(
            f'Could not execute SVALint: {exc}',
            returncode=126,
            stderr=str(exc),
        )

    output = _combine_output(proc.stdout, proc.stderr)
    return _parse_svalint_output(
        proc.returncode, proc.stdout, proc.stderr, output)


def _parse_svalint_output(
    returncode: int,
    stdout: str,
    stderr: str,
    combined: str,
) -> SVALintResult:
    cleaned = _ANSI_ESCAPE_RE.sub('', combined).replace('\r\n', '\n').replace('\r', '\n')
    error_totals = re.findall(
        r'\bERROR\s*:\s*(\d+)', cleaned, re.IGNORECASE)
    warning_totals = re.findall(
        r'\bWARNING\s*:\s*(\d+)', cleaned, re.IGNORECASE)
    error_count = int(error_totals[-1]) if error_totals else 0
    warning_count = int(warning_totals[-1]) if warning_totals else 0

    violations: List[SVALintViolation] = []
    pattern = re.compile(
        r'AsFigo\s*:\s*Violation\s*:\s*\[(?P<rule_id>[^\]]+)\]\s*:\s*\n'
        r'(?P<message>.*?)'
        r'(?=\n\s*AsFigo\s*:|\n\s*(?:ERROR|WARNING)\s*:|\Z)',
        re.DOTALL | re.IGNORECASE,
    )
    seen = set()
    for match in pattern.finditer(cleaned):
        message = match.group('message').strip()
        rule_id = match.group('rule_id').strip()
        key = (rule_id, message)
        if key in seen:
            continue
        seen.add(key)
        assertion_text = _extract_assertion_from_message(message)
        violations.append(SVALintViolation(
            rule_id=rule_id,
            message=message,
            assertion_text=assertion_text,
        ))

    # A parsed violation is lint evidence even when a truncated or malformed
    # summary claims zero. Never let that combination open the gate.
    error_count = max(error_count, len(violations))
    execution_error = ''
    if returncode != 0 and error_count == 0:
        execution_error = f'SVALint exited with status {returncode}'

    return SVALintResult(
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        error_count=error_count,
        warning_count=warning_count,
        violations=violations,
        passed=error_count == 0 and returncode == 0 and not execution_error,
        execution_error=execution_error,
    )


def _extract_assertion_from_message(message: str) -> str:
    lines = [line for line in message.splitlines() if line.strip()]
    labelled = re.compile(
        r'^\s*[A-Za-z_]\w*\s*:\s*assert\s+property\b', re.IGNORECASE)
    for line in lines:
        if labelled.search(line):
            return line.strip()
    for line in lines:
        if re.search(r'^\s*assert\s+property\b', line, re.IGNORECASE):
            return line.strip()
    return ''


def format_violations_for_llm(result: SVALintResult) -> str:
    """Format SVALint violations for inclusion in an LLM correction prompt."""
    if result.execution_error:
        diagnostics = _combine_output(result.stdout, result.stderr).strip()
        text = f'SVALint could not complete: {result.execution_error}.'
        if diagnostics:
            text += f'\nTool diagnostics:\n{diagnostics}'
        return text

    if not result.violations:
        if result.error_count:
            raw = _combine_output(result.stdout, result.stderr)
            return (
                f'SVALint reported {result.error_count} error(s) and '
                f'{result.warning_count} warning(s) but no detailed violations were parsed.\n'
                f'Raw output:\n{raw}'
            )
        return ''

    parts = [
        f'SVALint reported {result.error_count} error(s) and {result.warning_count} warning(s).',
        'Fix ALL of the following violations:',
        '',
    ]
    for idx, violation in enumerate(result.violations, 1):
        parts.append(f'{idx}. [{violation.rule_id}]')
        parts.append(violation.message)
        if violation.assertion_text:
            parts.append(f'   Assertion: {violation.assertion_text}')
        parts.append('')
    return '\n'.join(parts)


def format_violation_summary(result: SVALintResult) -> str:
    if result.execution_error:
        return f'SVALint: execution failed — {result.execution_error}'
    if result.passed:
        if result.warning_count:
            return f'SVALint: no errors, {result.warning_count} warning(s)'
        return 'SVALint: no errors'
    return f'SVALint: {result.error_count} error(s), {result.warning_count} warning(s)'
