"""Shared formal harness for evaluating snapshots and baselines.

Every comparison SVApshot makes — a snapshot against a mutant, one generation
method against another — is only meaningful if both sides run under identical
elaboration settings, assumptions, clock and reset configuration, and tool
version.  This module is that single point of control: it drives the existing
``ft_<module>/`` testbench in place, swapping only the one artifact under test
(the RTL, or the property file) and restoring it afterwards.

Nothing here knows about LLMs.  A baseline that produces properties by any
means can be measured with the same code path as SVApshot itself.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

import proof_status
from proof_status import FormalRunResult, Qualification

REPO_ROOT = Path(__file__).resolve().parents[2]
FPV_SCRIPTS_DIR = REPO_ROOT / 'fpv_app_scripts'


@dataclass
class HarnessPaths:
    """Where the formal testbench for one module lives."""

    module: str
    rtl_source: str
    ft_dir: str
    property_file: str
    bind_file: str
    project_dir: str
    log_file: str
    report_dir: str

    @classmethod
    def for_module(cls, rtl_source: str, formal_tool: str = 'vcformal') -> 'HarnessPaths':
        module = os.path.splitext(os.path.basename(rtl_source))[0]
        ft_dir = f'ft_{module}'
        if formal_tool == 'vcformal':
            project_dir = os.path.join('vcf_projs', module)
            log_name = 'vcf.log'
        else:
            project_dir = os.path.join('projs', module)
            log_name = 'jg.log'
        return cls(
            module=module,
            rtl_source=rtl_source,
            ft_dir=ft_dir,
            property_file=os.path.join(ft_dir, 'sva', f'{module}_prop.sv'),
            bind_file=os.path.join(ft_dir, 'sva', f'{module}_bind.svh'),
            project_dir=project_dir,
            log_file=os.path.join(project_dir, log_name),
            report_dir=os.path.join(project_dir, 'reports'),
        )


@dataclass
class HarnessRun:
    """One invocation of the formal tool through the harness."""

    result: FormalRunResult
    seconds: float = 0.0
    label: str = ''
    #: True when the run was killed before the tool finished. Its results are
    #: partial and must not be read as verdicts.
    timed_out: bool = False

    def failing(self) -> List[str]:
        """Properties that produced a counterexample in this run."""
        return sorted(
            name for name, record in self.result.records.items()
            if record.qualification in (
                Qualification.FAILING_PROPERTY_MISMATCH,
                Qualification.FAILING_MISSING_ASSUMPTION,
            )
        )

    def proved_non_vacuously(self) -> List[str]:
        return self.result.names_with(Qualification.PROVED_NON_VACUOUS)


class FormalHarness:
    """Run one module's formal testbench with controlled substitutions."""

    def __init__(
        self,
        rtl_source: str,
        formal_tool: str = 'vcformal',
        log: Optional[Callable[[str], None]] = None,
        timeout_s: Optional[float] = None,
    ):
        self.rtl_source = rtl_source
        self.formal_tool = formal_tool
        self.paths = HarnessPaths.for_module(rtl_source, formal_tool)
        self.log = log or (lambda message: print(message, flush=True))
        self.timeout_s = timeout_s
        self.runs: List[HarnessRun] = []
        self.total_formal_seconds = 0.0

    # -- substitutions -----------------------------------------------------

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    @classmethod
    def _claim_substitution(cls, path: str) -> tuple[str, str]:
        """Recover a dead writer, then atomically claim an in-place rewrite."""
        lock = path + '.svapshot.lock'
        backup = path + '.svapshot.backup'
        if os.path.isfile(lock):
            try:
                with open(lock, encoding='utf-8') as handle:
                    owner = int(handle.read().strip() or 0)
            except (OSError, ValueError):
                owner = 0
            if cls._pid_alive(owner):
                raise RuntimeError(
                    f'{path} is already substituted by live pid {owner}')
            if not os.path.isfile(backup):
                raise RuntimeError(
                    f'{path} has a stale substitution lock but no backup')
            os.replace(backup, path)
            os.unlink(lock)
        elif os.path.isfile(backup):
            # A crash between restoring the file and deleting the lock is safe;
            # a backup without a lock is still treated as the authoritative
            # original rather than trusting possibly-mutated RTL.
            os.replace(backup, path)

        try:
            descriptor = os.open(
                lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as error:
            raise RuntimeError(
                f'could not claim substitution lock for {path}') from error
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(str(os.getpid()))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            shutil.copy2(path, backup)
        except Exception:
            try:
                os.unlink(lock)
            except OSError:
                pass
            raise
        return lock, backup

    @contextmanager
    def substituted(self, path: str, new_text: str):
        """Temporarily replace a file's contents, restoring it on exit.

        The original bytes are persisted beside the source under an atomic
        lock. A normal exception restores in ``finally``; a later process also
        recovers a writer killed by SIGKILL before it can install another
        mutant. Concurrent writers fail instead of racing over shared RTL.
        """
        lock, backup = self._claim_substitution(path)
        try:
            with open(path, 'w') as handle:
                handle.write(new_text)
            yield path
        finally:
            if os.path.isfile(backup):
                os.replace(backup, path)
            try:
                os.unlink(lock)
            except OSError:
                pass

    @contextmanager
    def rtl(self, rtl_text: str):
        """Run against a substituted RTL body (used for mutants and candidates)."""
        with self.substituted(self.rtl_source, rtl_text):
            yield

    @contextmanager
    def properties(self, property_text: str):
        """Run against a substituted property file (used for baselines)."""
        with self.substituted(self.paths.property_file, property_text):
            yield

    # -- execution ---------------------------------------------------------

    def _script(self) -> List[str]:
        if self.formal_tool == 'vcformal':
            return [str(FPV_SCRIPTS_DIR / 'run_vcf_batch.sh'), self.paths.module]
        if self.formal_tool == 'jaspergold':
            return [str(FPV_SCRIPTS_DIR / 'run_jg_batch.sh'), self.paths.module]
        raise ValueError(f'Unsupported formal tool: {self.formal_tool}')

    def run(self, label: str = '') -> HarnessRun:
        """Run the formal tool from a clean project directory and qualify it.

        Returns:
            HarnessRun: The qualified result and its wall time.
        """
        if self.formal_tool == 'vcformal':
            from vcf_session import VcfSessionBusy, prepare_vcf_session
            try:
                prepare_vcf_session(os.getcwd(), self.paths.module)
            except VcfSessionBusy as exc:
                raise RuntimeError(str(exc)) from exc
        shutil.rmtree(self.paths.project_dir, ignore_errors=True)

        start = time.time()
        timed_out = False
        # The batch scripts fork further tool processes, so the run gets its own
        # session and the whole group is signalled on timeout. Killing only the
        # script would leave the tool holding its licence and stall every later
        # run — including other users' on a shared machine. The session also
        # keeps the tools away from a controlling terminal, and /dev/null on
        # stdin leaves them nothing to read from it.
        process = subprocess.Popen(
            self._script(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            process.communicate(timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            self.log(f'  [{label}] formal run exceeded {self.timeout_s}s and was killed')
            self._terminate(process)
        seconds = time.time() - start
        self.total_formal_seconds += seconds

        # JasperGold detaches; wait for the log rather than assuming it exists.
        deadline = time.time() + 120
        while not os.path.isfile(self.paths.log_file) and time.time() < deadline:
            time.sleep(1)

        result = FormalRunResult()
        if os.path.isfile(self.paths.log_file):
            with open(self.paths.log_file, 'r', errors='replace') as handle:
                log_text = handle.read()
            result = proof_status.parse_formal_log(
                log_text, self.formal_tool, self.paths.module)
            if self.formal_tool == 'vcformal' and os.path.isdir(self.paths.report_dir):
                result = proof_status.merge_reports_into_result(
                    result, proof_status.load_vcformal_reports(self.paths.report_dir))
        else:
            self.log(f'  [{label}] no formal log produced at {self.paths.log_file}')

        run = HarnessRun(
            result=result, seconds=seconds, label=label, timed_out=timed_out)
        self.runs.append(run)
        return run

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        """Signal the run's whole process group, then insist."""
        import signal

        try:
            group = os.getpgid(process.pid)
        except OSError:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(group, sig)
            except OSError:
                return
            try:
                process.wait(timeout=15)
                return
            except subprocess.TimeoutExpired:
                continue

    def run_with_rtl(self, rtl_text: str, label: str = '') -> HarnessRun:
        with self.rtl(rtl_text):
            return self.run(label)

    def run_with_properties(self, property_text: str, label: str = '') -> HarnessRun:
        with self.properties(property_text):
            return self.run(label)

    # -- convenience -------------------------------------------------------

    def read_rtl(self) -> str:
        with open(self.rtl_source, 'r', errors='replace') as handle:
            return handle.read()

    def read_properties(self) -> str:
        with open(self.paths.property_file, 'r', errors='replace') as handle:
            return handle.read()

    def is_ready(self) -> bool:
        """True when the testbench needed for a run already exists."""
        required = [self.rtl_source, self.paths.property_file]
        if self.formal_tool == 'vcformal':
            required.append(os.path.join(self.paths.ft_dir, 'FPV_vcf.tcl'))
        else:
            required.append(os.path.join(self.paths.ft_dir, 'FPV.tcl'))
        missing = [path for path in required if not os.path.exists(path)]
        if missing:
            self.log(
                'Formal testbench is incomplete; run main.py for this module first.\n'
                + '\n'.join(f'  missing: {path}' for path in missing))
            return False
        return True


_ASSERT_HEADER_RE = re.compile(
    r'^\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*assert\s+property\b')


def _assertion_spans(lines: List[str]):
    """Yield ``(name, start, end)`` line spans of every assertion block.

    Blocks follow the generator template — a header line, an optional
    ``else begin`` action block, and a terminating ``end`` or ``;`` — so the
    span ends at the first balanced terminator after the header.
    """
    index = 0
    while index < len(lines):
        header = _ASSERT_HEADER_RE.match(lines[index])
        if not header:
            index += 1
            continue

        name = header.group('name')
        depth = 0
        saw_else = False
        end = index
        for offset in range(index, len(lines)):
            line = lines[offset]
            depth += line.count('(') - line.count(')')
            if re.search(r'\belse\b', line):
                saw_else = True
            end = offset
            if depth > 0:
                continue
            stripped = line.strip()
            if saw_else:
                if stripped == 'end' or stripped.startswith('end '):
                    break
            elif stripped.endswith(';'):
                break
        yield name, index, end
        index = end + 1


def contract_property_file(property_text: str, keep: List[str]) -> str:
    """Comment out every assertion that is not part of the snapshot contract.

    Applying a snapshot means applying exactly the properties that were proved
    non-vacuously on the reference design.  A property that already failed on
    the reference would otherwise be reported as a regression on every mutant
    and inflate the detection rate.  Assumptions and covers are left untouched:
    the mutant must run under the same environment as the reference.

    Args:
        property_text: Full text of the property file.
        keep: Names of assertions to leave enabled.

    Returns:
        str: The property file with non-contract assertions commented out.
    """
    keep_set = set(keep)
    lines = property_text.splitlines()
    disabled = set()
    for name, start, end in _assertion_spans(lines):
        if name not in keep_set:
            disabled.update(range(start, end + 1))

    output = [
        ('// [not in contract] ' + line) if number in disabled else line
        for number, line in enumerate(lines)
    ]
    return '\n'.join(output) + '\n'


def contract_names(property_text: str) -> List[str]:
    """Names of the assertions currently enabled in a property file."""
    return [name for name, _, _ in _assertion_spans(property_text.splitlines())]
