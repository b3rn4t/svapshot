"""Provenance-preserving snapshot manifests.

A snapshot is a regression contract, so it is only useful if a later run can
establish what it was proved against.  The manifest records everything needed to
reproduce a proof:

* the RTL revision (git commit, dirty flag, and per-file content hashes),
* the assumptions in force, as named artifacts with their screening verdicts,
* the elaboration settings (top, clock, reset, parameters, Tcl hash),
* the generating model, prompt template, and rule text hashes,
* the formal tool and its exact version,
* every property with its qualification, cone of influence, and dependencies.

Applying a snapshot to a candidate RTL revision produces a
:class:`RegressionReport` that separates preserved behaviour from behaviour that
changed, which is the distinction the methodology exists to make.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

SCHEMA_VERSION = '1.0'


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8', errors='replace')).hexdigest()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _git(args: Sequence[str], cwd: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ['git', *args], cwd=cwd, capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


@dataclass
class SourceFile:
    """One RTL file with the content hash it had at snapshot time."""

    path: str
    sha256: str = ''
    lines: int = 0

    @classmethod
    def from_path(cls, path: str) -> 'SourceFile':
        try:
            with open(path, 'r', errors='replace') as handle:
                line_count = sum(1 for _ in handle)
            return cls(path=os.path.abspath(path), sha256=sha256_file(path), lines=line_count)
        except OSError:
            return cls(path=path)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DesignRevision:
    """Identity of the RTL revision a snapshot was proved against."""

    module: str = ''
    git_commit: str = ''
    git_branch: str = ''
    git_dirty: bool = False
    git_remote: str = ''
    files: List[SourceFile] = field(default_factory=list)

    @classmethod
    def capture(cls, module: str, rtl_paths: Sequence[str], repo_dir: str = '.') -> 'DesignRevision':
        """Record the current revision of the design under ``repo_dir``.

        A dirty working tree is recorded rather than rejected, but the flag makes
        it explicit that the commit hash alone does not identify the sources.
        """
        commit = _git(['rev-parse', 'HEAD'], repo_dir) or ''
        branch = _git(['rev-parse', '--abbrev-ref', 'HEAD'], repo_dir) or ''
        remote = _git(['config', '--get', 'remote.origin.url'], repo_dir) or ''
        status = _git(['status', '--porcelain'], repo_dir)

        return cls(
            module=module,
            git_commit=commit,
            git_branch=branch,
            git_dirty=bool(status),
            git_remote=remote,
            files=[SourceFile.from_path(path) for path in rtl_paths if path],
        )

    def to_dict(self) -> dict:
        data = asdict(self)
        data['files'] = [f.to_dict() for f in self.files]
        return data

    def matches(self, other: 'DesignRevision') -> bool:
        """True when both revisions have identical file content."""
        left = {f.path.rsplit('/', 1)[-1]: f.sha256 for f in self.files}
        right = {f.path.rsplit('/', 1)[-1]: f.sha256 for f in other.files}
        return left == right


@dataclass
class ElaborationSettings:
    """Everything that changes what the formal tool actually proved."""

    top: str = ''
    formal_tool: str = ''
    formal_tool_version: str = ''
    clock: str = ''
    reset: str = ''
    reset_sense: str = ''
    module_type: str = ''
    parameters: Dict[str, str] = field(default_factory=dict)
    tcl_path: str = ''
    tcl_sha256: str = ''
    file_list_sha256: str = ''
    vacuity_checking: bool = False
    coverage_mode: str = ''

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class GenerationSettings:
    """Which model, prompt, and rules produced the properties."""

    model: str = ''
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_completion_tokens: Optional[int] = None
    prompting_strategy: str = ''
    rules_sha256: str = ''
    template_version: str = ''
    prompt_sha256_by_stage: Dict[str, str] = field(default_factory=dict)
    svalint_root: str = ''
    svapshot_commit: str = ''

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AssumptionRecord:
    """A named environment constraint and the screens it passed."""

    name: str
    expression: str
    rationale: str = ''
    source: str = 'llm'
    #: Property names this assumption was introduced to unblock.
    targets: List[str] = field(default_factory=list)
    screening: Dict[str, object] = field(default_factory=dict)
    accepted: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PropertyEntry:
    """One property as stored in the regression contract."""

    name: str
    expression: str = ''
    assertion_text: str = ''
    qualification: str = ''
    proof_status: str = ''
    vacuity_status: str = ''
    engine: str = ''
    proof_time: str = ''
    cone_size: int = 0
    cone: List[str] = field(default_factory=list)
    referenced_signals: List[str] = field(default_factory=list)
    assumption_dependence: List[str] = field(default_factory=list)
    repair_attempts: int = 0
    note: str = ''

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SnapshotManifest:
    """Complete, reproducible description of one behavioural snapshot."""

    schema_version: str = SCHEMA_VERSION
    snapshot_id: str = ''
    created_utc: str = ''
    design: DesignRevision = field(default_factory=DesignRevision)
    elaboration: ElaborationSettings = field(default_factory=ElaborationSettings)
    generation: GenerationSettings = field(default_factory=GenerationSettings)
    assumptions: List[AssumptionRecord] = field(default_factory=list)
    properties: List[PropertyEntry] = field(default_factory=list)
    metrics: Dict[str, object] = field(default_factory=dict)
    cost: Dict[str, object] = field(default_factory=dict)
    diversity: Dict[str, object] = field(default_factory=dict)
    mutation: Dict[str, object] = field(default_factory=dict)
    #: Path of the property file that holds the contract itself.
    contract_property_file: str = ''
    assumption_file: str = ''

    def __post_init__(self):
        if not self.created_utc:
            self.created_utc = datetime.now(timezone.utc).isoformat(timespec='seconds')

    def finalise_id(self) -> str:
        """Derive a stable id from the content that defines the snapshot.

        Two runs that prove the same properties on the same revision with the
        same assumptions get the same id, which makes snapshots comparable
        across machines.
        """
        material = json.dumps(
            {
                'design': self.design.to_dict(),
                'elaboration': self.elaboration.to_dict(),
                'generation': self.generation.to_dict(),
                'assumptions': [a.to_dict() for a in self.assumptions],
                'properties': sorted(
                    (p.name, p.expression, p.qualification) for p in self.properties
                ),
            },
            sort_keys=True,
            default=str,
        )
        self.snapshot_id = sha256_text(material)[:16]
        return self.snapshot_id

    @property
    def contract_properties(self) -> List[PropertyEntry]:
        """Properties that carry regression value (non-vacuous proofs only)."""
        return [p for p in self.properties if p.qualification == 'proved_non_vacuous']

    def to_dict(self) -> dict:
        return {
            'schema_version': self.schema_version,
            'snapshot_id': self.snapshot_id,
            'created_utc': self.created_utc,
            'design': self.design.to_dict(),
            'elaboration': self.elaboration.to_dict(),
            'generation': self.generation.to_dict(),
            'assumptions': [a.to_dict() for a in self.assumptions],
            'properties': [p.to_dict() for p in self.properties],
            'contract_property_file': self.contract_property_file,
            'assumption_file': self.assumption_file,
            'metrics': self.metrics,
            'cost': self.cost,
            'diversity': self.diversity,
            'mutation': self.mutation,
        }

    def save(self, path: str) -> str:
        if not self.snapshot_id:
            self.finalise_id()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, 'w') as handle:
            json.dump(self.to_dict(), handle, indent=2, sort_keys=False)
        return path

    @classmethod
    def load(cls, path: str) -> 'SnapshotManifest':
        with open(path) as handle:
            data = json.load(handle)

        manifest = cls(
            schema_version=data.get('schema_version', SCHEMA_VERSION),
            snapshot_id=data.get('snapshot_id', ''),
            created_utc=data.get('created_utc', ''),
            contract_property_file=data.get('contract_property_file', ''),
            assumption_file=data.get('assumption_file', ''),
            metrics=data.get('metrics', {}),
            cost=data.get('cost', {}),
            diversity=data.get('diversity', {}),
            mutation=data.get('mutation', {}),
        )

        design = data.get('design', {})
        manifest.design = DesignRevision(
            module=design.get('module', ''),
            git_commit=design.get('git_commit', ''),
            git_branch=design.get('git_branch', ''),
            git_dirty=design.get('git_dirty', False),
            git_remote=design.get('git_remote', ''),
            files=[SourceFile(**f) for f in design.get('files', [])],
        )
        manifest.elaboration = ElaborationSettings(**data.get('elaboration', {}))
        manifest.generation = GenerationSettings(**data.get('generation', {}))
        manifest.assumptions = [AssumptionRecord(**a) for a in data.get('assumptions', [])]
        manifest.properties = [PropertyEntry(**p) for p in data.get('properties', [])]
        return manifest

    def format_summary(self) -> str:
        counts: Dict[str, int] = {}
        for entry in self.properties:
            counts[entry.qualification] = counts.get(entry.qualification, 0) + 1

        lines = [
            f'Snapshot {self.snapshot_id} — {self.design.module}',
            '=' * 72,
            f'  created            {self.created_utc}',
            f'  RTL revision       {self.design.git_commit[:12] or "unversioned"}'
            f'{" (dirty)" if self.design.git_dirty else ""}',
            f'  formal tool        {self.elaboration.formal_tool} '
            f'{self.elaboration.formal_tool_version}',
            f'  vacuity checking   {"on" if self.elaboration.vacuity_checking else "off"}',
            f'  model              {self.generation.model}',
            f'  assumptions        {len(self.assumptions)} '
            f'({sum(1 for a in self.assumptions if a.accepted)} accepted)',
            f'  properties         {len(self.properties)} '
            f'({len(self.contract_properties)} in contract)',
            '',
            '  Qualification breakdown:',
        ]
        for key in (
            'proved_non_vacuous', 'proved_vacuous', 'failing_property_mismatch',
            'failing_missing_assumption', 'inconclusive',
        ):
            lines.append(f'    {key:<32}{counts.get(key, 0)}')
        return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Regression application
# ---------------------------------------------------------------------------


@dataclass
class RegressionReport:
    """Outcome of replaying a snapshot against a candidate RTL revision."""

    snapshot_id: str = ''
    module: str = ''
    candidate_revision: Optional[DesignRevision] = None
    rtl_unchanged: bool = False
    preserved: List[str] = field(default_factory=list)
    regressed: List[str] = field(default_factory=list)
    inapplicable: List[str] = field(default_factory=list)
    newly_inconclusive: List[str] = field(default_factory=list)
    #: Properties the user declared as intentionally changed behaviour.
    intentionally_changed: List[str] = field(default_factory=list)

    @property
    def checked(self) -> int:
        return len(self.preserved) + len(self.regressed) + len(self.newly_inconclusive)

    @property
    def preservation_rate(self) -> float:
        return len(self.preserved) / self.checked if self.checked else 0.0

    @property
    def regression_detected(self) -> bool:
        return bool(self.regressed)

    def to_dict(self) -> dict:
        data = asdict(self)
        data['candidate_revision'] = (
            self.candidate_revision.to_dict() if self.candidate_revision else None
        )
        data['checked'] = self.checked
        data['preservation_rate'] = self.preservation_rate
        data['regression_detected'] = self.regression_detected
        return data

    def format_summary(self) -> str:
        lines = [
            f'Regression against snapshot {self.snapshot_id} — {self.module}',
            '=' * 72,
            f'  contract properties checked   {self.checked}',
            f'  preserved                     {len(self.preserved)}',
            f'  REGRESSED                     {len(self.regressed)}',
            f'  newly inconclusive            {len(self.newly_inconclusive)}',
            f'  inapplicable (signal removed) {len(self.inapplicable)}',
            f'  intentionally changed         {len(self.intentionally_changed)}',
            f'  preservation rate             {self.preservation_rate:.1%}',
        ]
        if self.regressed:
            lines.append('')
            lines.append('  Behaviour that changed unintentionally:')
            for name in self.regressed:
                lines.append(f'    - {name}')
        if self.inapplicable:
            lines.append('')
            lines.append('  Not applicable to the candidate (referenced signals gone):')
            for name in self.inapplicable:
                lines.append(f'    - {name}')
        return '\n'.join(lines)


def applicable_properties(
    manifest: SnapshotManifest,
    candidate_signals: Sequence[str],
) -> Dict[str, List[PropertyEntry]]:
    """Split contract properties by whether they still bind to the candidate.

    A property that references an internal signal the candidate no longer
    declares cannot be evaluated; reporting it as a regression would be wrong,
    so it is separated out as inapplicable.
    """
    available = set(candidate_signals)
    applicable: List[PropertyEntry] = []
    inapplicable: List[PropertyEntry] = []

    for entry in manifest.contract_properties:
        missing = [s for s in entry.referenced_signals if s not in available]
        if missing:
            entry.note = f'references signals absent from candidate: {", ".join(sorted(missing))}'
            inapplicable.append(entry)
        else:
            applicable.append(entry)

    return {'applicable': applicable, 'inapplicable': inapplicable}


def build_regression_report(
    manifest: SnapshotManifest,
    candidate_results: Dict[str, str],
    candidate_revision: Optional[DesignRevision] = None,
    inapplicable: Sequence[PropertyEntry] = (),
    intentionally_changed: Sequence[str] = (),
) -> RegressionReport:
    """Classify each contract property against a candidate's proof results.

    ``candidate_results`` maps property name to a
    :class:`proof_status.Qualification` value obtained by re-running the
    contract on the candidate RTL.
    """
    changed = set(intentionally_changed)
    report = RegressionReport(
        snapshot_id=manifest.snapshot_id,
        module=manifest.design.module,
        candidate_revision=candidate_revision,
        inapplicable=[e.name for e in inapplicable],
        intentionally_changed=sorted(changed),
    )

    if candidate_revision is not None:
        report.rtl_unchanged = manifest.design.matches(candidate_revision)

    inapplicable_names = set(report.inapplicable)
    for entry in manifest.contract_properties:
        if entry.name in inapplicable_names or entry.name in changed:
            continue

        outcome = candidate_results.get(entry.name)
        if outcome == 'proved_non_vacuous':
            report.preserved.append(entry.name)
        elif outcome in ('failing_property_mismatch', 'failing_missing_assumption',
                         'proved_vacuous'):
            # A contract property that becomes vacuous on the candidate is also a
            # regression: the behaviour it used to check is no longer reachable.
            report.regressed.append(entry.name)
        else:
            report.newly_inconclusive.append(entry.name)

    return report
