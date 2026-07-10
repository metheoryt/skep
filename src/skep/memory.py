from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
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


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _next_free(root: Path, slug: str) -> tuple[str, Path]:
    """A new fact never overwrites another agent's memory: `-2`, `-3`, ..."""
    candidate = slug
    n = 1
    while True:
        path = root / f"{candidate}.md"
        if not path.exists():
            return candidate, path
        n += 1
        candidate = f"{slug}-{n}"


def write_memory(
    repo_path: Path,
    title: str,
    body: str,
    kind: str = "gotcha",
    supersedes: str | None = None,
    now: Callable[[], datetime] | None = None,
) -> Path:
    """Write one fact. Returns the written path.

    Both `title` and `supersedes` are attacker-controlled input to a filesystem
    path (spec §5.1) and both run slugify -> resolve -> containment-assert.
    `supersedes` must additionally name a file that already exists.

    Validates everything BEFORE writing anything: a rejected `supersedes` must
    not leave a new memory behind.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown kind: {kind!r}")

    clock = now or _utcnow
    root = memory_dir(repo_path)

    old_path: Path | None = None
    if supersedes is not None:
        old_path = resolve_memory_file(repo_path, supersedes)
        if not old_path.is_file():
            raise ValueError(f"supersedes names no existing memory: {supersedes!r}")

    slug = slugify(title)
    resolve_memory_file(repo_path, slug)  # containment assert before mkdir
    created = clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    root.mkdir(parents=True, exist_ok=True)
    final_slug, path = _next_free(root, slug)
    # re-check the SUFFIXED slug, not just the base: `_next_free` can pick a
    # different (suffixed) name on collision, and a symlink planted there
    # would otherwise escape the containment check above.
    resolve_memory_file(repo_path, final_slug)
    fact = MemoryFact(
        slug=final_slug,
        title=title,
        kind=kind,
        created=created,
        superseded_by=None,
        body=body,
    )
    text = serialize_fact(fact)  # raises on a newline in title, before any write
    path.write_text(text)

    if old_path is not None:
        old = parse_fact(old_path.stem, old_path.read_text())
        if old is not None:
            marked = MemoryFact(
                slug=old.slug,
                title=old.title,
                kind=old.kind,
                created=old.created,
                superseded_by=final_slug,
                body=old.body,
            )
            old_path.write_text(serialize_fact(marked))
    return path


class MemoryProbe(Protocol):
    """What Supervisor needs from memory: an addendum, or None."""

    async def addendum_for(self, repo_path: Path) -> str | None: ...
