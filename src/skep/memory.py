from __future__ import annotations

import asyncio
import logging
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

MEMORY_DIRNAME = ".agent-memory"

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def memory_dir(repo_path: Path) -> Path:
    """The memory store for a repo. Always the PARENT repo, never a worktree."""
    return repo_path / MEMORY_DIRNAME


def slugify(text: str) -> str:
    """Reduce free text to `[a-z0-9-]+`. Raises ValueError on an empty result.

    `title` is chosen by a language model and becomes a filename, so this is
    the first of three defences in spec §5.1. It is NOT the last: see
    `resolve_memory_file`.
    """
    slug = _SLUG_STRIP.sub("-", text.lower()).strip("-")
    if not slug:
        raise ValueError(f"slugifies to empty: {text!r}")
    return slug


def resolve_memory_file(repo_path: Path, slug: str) -> Path:
    """Resolve `<repo>/.agent-memory/<slug>.md`, asserting containment.

    Step 3 of spec §5.1, and NOT redundant with `slugify`: it is the assertion
    that still holds if the slugifier is later "improved" to allow a character
    it should not, and it catches a symlink whose target escapes the store.
    """
    if slug != slugify(slug):
        raise ValueError(f"not a clean slug: {slug!r}")
    root = memory_dir(repo_path).resolve()
    candidate = (root / f"{slug}.md").resolve()
    if candidate.parent != root:
        raise ValueError(f"path escapes memory dir: {slug!r}")
    return candidate


KINDS = frozenset({"gotcha", "constraint", "decision", "convention", "incident"})

_FM_DELIM = "---"


@dataclass(frozen=True)
class MemoryFact:
    slug: str
    title: str
    kind: str
    created: str
    superseded_by: str | None
    body: str


def _quote(value: str) -> str:
    """Serialize a string as a quoted scalar. `title` is agent-supplied."""
    if "\n" in value or "\r" in value:
        raise ValueError("newline in frontmatter value")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _unquote(raw: str) -> str | None:
    """Inverse of `_quote`. Returns None if `raw` is not a quoted scalar."""
    raw = raw.strip()
    if len(raw) < 2 or raw[0] != '"' or raw[-1] != '"':
        return None
    out: list[str] = []
    i = 1
    while i < len(raw) - 1:
        ch = raw[i]
        if ch == "\\" and i + 1 < len(raw) - 1:
            out.append(raw[i + 1])
            i += 2
            continue
        if ch == '"':
            return None  # unescaped quote before the end
        out.append(ch)
        i += 1
    return "".join(out)


def serialize_fact(fact: MemoryFact) -> str:
    """Render a fact to Markdown. Every frontmatter value is machine-generated
    or escaped here -- §6's skip-on-malformed rule guards hand-edits and merge
    artifacts, not routine agent input.
    """
    if fact.kind not in KINDS:
        raise ValueError(f"unknown kind: {fact.kind!r}")
    superseded = _quote(fact.superseded_by) if fact.superseded_by else "null"
    return (
        f"{_FM_DELIM}\n"
        f"title: {_quote(fact.title)}\n"
        f"kind: {fact.kind}\n"
        f"created: {fact.created}\n"
        f"superseded_by: {superseded}\n"
        f"{_FM_DELIM}\n"
        f"\n{fact.body.strip()}\n"
    )


DEFAULT_MAX_BYTES = 8192

_WRITE_INSTRUCTIONS = (
    "Record a memory when you learn something that would save the next agent\n"
    "time and that the repo itself does not already record -- an operational\n"
    "fact, a constraint you discovered the hard way, or a decision and its\n"
    "reasoning:\n"
    "    the `remember` tool (title, body, kind, supersedes)\n"
    "\n"
    "If a fact above is now wrong, supersede it rather than adding a\n"
    "contradiction. Pass the slug in brackets, exactly as shown above:\n"
    '    remember(title=..., body=..., supersedes="stack-takes-90s-to-come-up")\n'
    "\n"
    "Do NOT record what the repo already records -- code structure, git\n"
    "history, CLAUDE.md. Scratch notes for this task alone: write a file in\n"
    "your worktree.\n"
)


class MemoryStore:
    """Reads `<repo>/.agent-memory/` and renders the spawn addendum.

    Implements the `MemoryProbe` protocol, so `Supervisor` needs no change to
    how it asks for an addendum. No daemon, no subprocess, no timeout -- just
    filesystem reality (spec §6).
    """

    def __init__(self, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self._max_bytes = max_bytes

    def _load(self, repo_path: Path) -> list[MemoryFact]:
        """Never raises. Returns [] when the store is absent or unreadable.

        `Path.glob()` swallows `OSError` while scanning a directory (an
        unreadable dir, or a `.agent-memory` that is a plain file, both yield
        an empty iterator) -- so it cannot tell "absent" from "unreadable"
        apart, and the latter must still be logged. `iterdir()` does not
        swallow: it raises `FileNotFoundError` for "absent" (silent -- no
        memory store is not an error) and any other `OSError`
        (`PermissionError`, `NotADirectoryError`, ...) for "unreadable"
        (logged once).
        """
        d = memory_dir(repo_path)
        try:
            entries = list(d.iterdir())
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning("agent memory unreadable at %s: %s", d, exc)
            return []

        paths = sorted(p for p in entries if p.suffix == ".md")

        facts: list[MemoryFact] = []
        skipped: list[str] = []
        for p in paths:
            try:
                text = p.read_text(errors="replace")
            except OSError:
                skipped.append(p.name)
                continue
            fact = parse_fact(p.stem, text)
            if fact is None:
                skipped.append(p.name)
                continue
            if fact.superseded_by is None:
                facts.append(fact)

        if skipped:
            # One line per _load(), not one per file: a spawn calls this on
            # every launch, so N corrupt files must not mean N log lines
            # forever. Bounded to 5 names so a fully-corrupt directory still
            # produces one short line.
            logger.warning(
                "skipping %d unusable memory file(s) in %s: %s",
                len(skipped),
                d,
                ", ".join(sorted(skipped)[:5]),
            )

        facts.sort(key=lambda f: f.created, reverse=True)
        return facts

    def render(self, facts: list[MemoryFact]) -> str:
        """Zero facts omits the recall section but keeps the write instructions:
        an agent with nothing to recall can still learn something.
        """
        if not facts:
            return f"## Memory\n\n{_WRITE_INSTRUCTIONS}"

        lines: list[str] = []
        used = 0
        shown = 0
        for f in facts:
            entry = f"- [{f.slug}] **{f.title}** ({f.kind}): {f.body}\n"
            if used + len(entry.encode()) > self._max_bytes and shown:
                break
            lines.append(entry)
            used += len(entry.encode())
            shown += 1

        omitted = len(facts) - shown
        body = "".join(lines)
        if omitted:
            # Explicit, not silent: an agent that believes it has seen all the
            # memory is worse off than one that knows it has not.
            body += f"\n... {omitted} older memories omitted\n"
        return (
            "## Memory\n\n"
            "Durable facts other agents learned about this repo:\n\n"
            f"{body}\n{_WRITE_INSTRUCTIONS}"
        )

    async def addendum_for(self, repo_path: Path) -> str | None:
        return self.render(self._load(repo_path))


def parse_fact(slug: str, text: str) -> MemoryFact | None:
    """Parse a memory file. Returns None on anything malformed -- never raises."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != _FM_DELIM:
        return None
    try:
        end = lines.index(_FM_DELIM, 1)
    except ValueError:
        return None

    fields: dict[str, str] = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(":")
        if not sep:
            return None
        fields[key.strip()] = value.strip()

    title = _unquote(fields.get("title", ""))
    kind = fields.get("kind", "")
    created = fields.get("created", "")
    if title is None or kind not in KINDS or not created:
        return None

    raw_superseded = fields.get("superseded_by", "null")
    if raw_superseded == "null":
        superseded_by: str | None = None
    else:
        superseded_by = _unquote(raw_superseded)
        if superseded_by is None:
            return None

    return MemoryFact(
        slug=slug,
        title=title,
        kind=kind,
        created=created,
        superseded_by=superseded_by,
        body="\n".join(lines[end + 1 :]).strip(),
    )


PROBE_TIMEOUT = 10.0


def recall_command(repo_path: Path) -> list[str]:
    """The exact invocation the addendum recommends and the preflight probes."""
    return ["gortex", "memory", "recall", "--index", str(repo_path), "--limit", "10"]


def memory_addendum(repo_path: Path) -> str:
    """System-prompt addendum telling an agent it has durable repo memory.

    `repo_path` is the PARENT repo (repos_root/<repo>), never the agent's
    worktree: the gortex daemon tracks the parent, and `--index <parent>` is
    the only form that works from inside a worktree.

    Phrased prescriptively (when to write, not just how) -- prescriptive
    trigger conditions measurably lift correct tool use.
    """
    repo = str(repo_path)
    recall = shlex.join(recall_command(repo_path))
    return f"""## Memory

You have durable memory for this repo, shared with every agent that works on it.

Before starting, recall what is already known:
    {recall}

Store a memory when you learn something that would save the next agent time and
that the repo itself does not already record -- an operational fact (this stack
takes 90s to come up; this test flakes under load), a constraint you discovered
the hard way, or a decision and its reasoning:
    gortex memory store --index {repo} --kind gotcha \\
        --title "<short caption>" --body "<what + why>"

If a memory you find is now wrong, supersede it rather than adding a contradiction:
    gortex memory store --index {repo} --supersedes <id> --body "<corrected>"

Do NOT store what the repo already records -- code structure, git history, CLAUDE.md.
Where the repo holds the fact, reference it instead of copying it.

Scratch notes for this task alone: write a file in your worktree.
"""


async def probe_memory(repo_path: Path, timeout: float = PROBE_TIMEOUT) -> str | None:
    """Smoke-check the exact command the addendum recommends.

    Returns None when memory is available, else a short reason. Never raises:
    the memory dependency is soft and must not be able to fail a spawn. A
    wedged daemon is a real failure mode, hence the timeout.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *recall_command(repo_path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return "gortex not found on PATH"
    except OSError as exc:
        return f"could not run gortex: {exc}"

    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return f"gortex did not respond within {timeout:g}s"

    if proc.returncode != 0:
        lines = stderr.decode(errors="replace").strip().splitlines()
        return lines[-1].strip() if lines else f"exit {proc.returncode}"
    return None


Probe = Callable[[Path], Awaitable[str | None]]


class MemoryProbe(Protocol):
    """What Supervisor needs from memory: an addendum, or None."""

    async def addendum_for(self, repo_path: Path) -> str | None: ...


class MemoryPreflight:
    """Probes each repo once, caches the verdict, warns once on unavailability.

    Spec says "preflight once at worker startup"; tracked-ness is per-repo and
    a worker learns its repos dynamically, so once-per-repo-cached is the
    faithful reading. No per-spawn subprocess, no repeated warnings.
    """

    def __init__(self, probe: Probe = probe_memory) -> None:
        self._probe = probe
        self._reasons: dict[Path, str | None] = {}
        self._lock = asyncio.Lock()

    async def addendum_for(self, repo_path: Path) -> str | None:
        async with self._lock:
            if repo_path not in self._reasons:
                reason = await self._probe(repo_path)
                self._reasons[repo_path] = reason
                if reason is not None:
                    logger.warning(
                        "agent memory disabled for %s: %s", repo_path, reason
                    )
        if self._reasons[repo_path] is not None:
            return None
        return memory_addendum(repo_path)
