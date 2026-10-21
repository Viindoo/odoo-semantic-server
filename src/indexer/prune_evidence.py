# SPDX-License-Identifier: AGPL-3.0-or-later
# src/indexer/prune_evidence.py
"""Source oracle for lifecycle deletions (ADR-0057).

Rule: **delete what git proves gone, keep what the source still defines.**

The intra-module entity prune (ADR-0056 B14/D9) used to delete every child of a
re-parsed module that the parse did not produce, behind a mass-drop threshold
that only a human (``--allow-mass-retire``) could release. The parse is not
evidence of a deletion: a parser gap or regression drops entities the source
still defines exactly like a commit that deletes them. This module asks the
source instead - the module's files at the trusted HEAD tree, read from the git
object database (``git ls-tree`` + ``git cat-file``), never through the OSM
parsers - whether each stale entity's identifier still occurs in the form that
can define it.

Verdicts (per stale node / relationship):

* ``gone``: the identifier no longer occurs in any defining form in the
  module's HEAD files - git proves the deletion; pruned, embeddings too.
* ``superseded``: still mentioned, but a live node of the same label and the
  same identity (its MERGE key minus module / file) was written by this run in
  the same module (a test class moved to another file), or the item is derived
  from a live parent the run re-derived (a lint violation of a re-validated
  view, a same-name INHERITS edge the version post-pass re-adds); pruned.
* ``present``: the source still defines it and nothing replaced it - the parse
  no longer produces something the code has (parser gap). Kept with its
  embeddings, recorded on the Module node, reported by ``lifecycle-audit`` as
  ``parser_gaps``; never an exit 3.

The oracle is ONE-DIRECTIONAL by construction: every pattern is broader than
what the parsers accept, so it may wrongly answer ``present`` (a ghost is kept
and reported) but must never wrongly answer ``gone``. Every doubt resolves to
``present``: an unknown label or relationship type, a path that cannot be
mapped into the module unless git proves the registered repo's HEAD has
nothing there (``ModuleSource.foreign_path_absent``: a file of another copy of
the module, e.g. the repo it moved from), a git error, a gitlink (submodule)
inside the module,
a module directory whose HEAD tree lacks its manifest, and a class whose
``_name`` / ``_inherit`` the oracle cannot evaluate statically (a constant
imported from another addon, a call, an f-string, an attribute): such a class
may define any model, so while one exists no Model node and no INHERITS /
DELEGATES_TO relationship of the module is proven gone. Other identifiers
built at runtime (``'sale.' + suffix``) or defined outside the module's own
files are the known limit (ADR-0057).

Fields and methods are class-scoped: one is present only when it is declared
in the body of a class whose models (its evaluated ``_name`` / ``_inherit``,
widened through local Python inheritance) include the node's model, or of a
class whose identity is unknown. A file Python 3 cannot parse (v8-v10
Python 2 source) has no class scope: there the text rule alone decides.

Imports only the standard library: it must stay independent of the parsers it
checks (``tests/test_prune_evidence.py`` runs the REAL parsers over fixtures and
asserts the oracle calls every entity they produce ``present``).
"""
from __future__ import annotations

import ast
import hashlib
import re
import subprocess
import threading
import warnings
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

VERDICT_GONE = "gone"
VERDICT_SUPERSEDED = "superseded"
VERDICT_PRESENT = "present"

# Verdicts the prune deletes.
DELETABLE_VERDICTS = frozenset({VERDICT_GONE, VERDICT_SUPERSEDED})

_MANIFEST_NAMES = ("__manifest__.py", "__openerp__.py")
# Translation catalogues are never read by a parser; they repeat every string
# of the module and would only keep ghosts.
_SKIP_SUFFIXES = (".po", ".pot")
# Binary formats no parser reads (images, fonts, media, archives, office
# documents, compiled code): never loaded. Their text could only keep ghosts.
# A blob of any other suffix is read and still skipped when it holds a NUL
# (the sniff below), so this list only saves the read.
_BINARY_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tif", ".tiff",
    ".psd", ".xcf", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4",
    ".m4a", ".ogg", ".oga", ".ogv", ".wav", ".webm", ".avi", ".mov", ".pdf",
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".jar", ".whl", ".egg",
    ".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".xls", ".xlsx", ".doc",
    ".docx", ".ppt", ".pptx", ".odt", ".ods", ".odp",
)
# A text blob larger than this makes the source unavailable (every verdict
# ``present``) instead of loading it: skipping its text could turn an entity
# it defines into a wrong ``gone``.
_MAX_BLOB_BYTES = 32 * 1024 * 1024
_GIT_TIMEOUT_S = 120
# Bytes inspected for a NUL to call a blob binary (git's own heuristic size).
_BINARY_SNIFF_BYTES = 8000

# Identifier runs. ``$`` is a JavaScript identifier character; ``\w`` is
# Unicode-aware, so non-ASCII identifiers tokenize the same on both sides.
_TOKEN_RE = re.compile(r"[\w$]+")
_B = r"(?<![\w$])"  # token start boundary
_E = r"(?![\w$])"  # token end boundary


# ---------------------------------------------------------------------------
# Code identity (re-evaluation trigger for kept sets)
# ---------------------------------------------------------------------------

# The code that decides what a re-parse produces and what the prune keeps: a
# change to any of it may turn a kept (parser-gap) entity into a produced or a
# gone one, so a recorded parser gap is re-evaluated once after such a deploy
# (``pipeline_repo._parser_gap_state``) - and never on an unchanged night.
#
# Everything that changes what the parse or the oracle outputs belongs here:
# also the version dispatch (``version_registry``), xmlid qualification
# (``_xmlid``), the curated framework bases, and the curated spec data the
# writers derive edges from (``tools_symbols_*.json`` decides which
# CoreSymbol is ``deprecated``, the only status ``USES_CORE_SYMBOL`` is
# written to; ``lint_rules_*.json``).
_PRUNE_CODE_SOURCES = (
    "prune_evidence.py", "pipeline_repo.py", "models.py", "resolver.py",
    "registry.py", "scanner.py", "parse_health.py", "version_registry.py",
    "_xmlid.py", "framework_bases.py", "writer_neo4j.py", "writer_neo4j_orm.py",
    "writer_neo4j_ui.py", "writer_neo4j_spec.py",
)
_PRUNE_CODE_GLOBS = (
    "parser_*.py", "spec_data/tools_symbols_*.json", "spec_data/lint_rules_*.json",
)
_CODE_TOKEN: str | None = None


def prune_code_token() -> str:
    """Digest of the parsers, writers, curated spec data and this oracle
    (``src-<16 hex>``)."""
    global _CODE_TOKEN
    if _CODE_TOKEN is None:
        here = Path(__file__).resolve().parent
        names = sorted(
            {p.relative_to(here).as_posix() for g in _PRUNE_CODE_GLOBS for p in here.glob(g)}
            | set(_PRUNE_CODE_SOURCES)
        )
        digest = hashlib.sha256()
        for name in names:
            digest.update(name.encode())
            try:
                digest.update((here / name).read_bytes())
            except OSError:
                digest.update(b"missing")
        _CODE_TOKEN = f"src-{digest.hexdigest()[:16]}"
    return _CODE_TOKEN


# ---------------------------------------------------------------------------
# Reading the HEAD tree
# ---------------------------------------------------------------------------

def _git(cwd: Path, *args: str, stdin: bytes | None = None) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            input=stdin, capture_output=True, timeout=_GIT_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _norm_dir(rel: str) -> str:
    """``a/b`` -> ``a/b/`` (repo-relative POSIX directory prefix)."""
    rel = str(PurePosixPath(rel.replace("\\", "/"))).strip("/")
    return "" if rel in ("", ".") else rel + "/"


@dataclass
class _DirTree:
    """One module directory's HEAD tree, inside the git repo that tracks it."""

    toplevel: Path
    prefix_in_top: str  # module dir relative to ``toplevel`` (``/``-terminated)
    prefix_in_repo: str  # module dir relative to the registered repo root
    tree_id: str
    blobs: dict[str, str]  # path relative to ``toplevel`` -> blob oid


def _read_dir_tree(repo_path: Path, rel_dir: str) -> tuple[_DirTree | None, str | None]:
    """The HEAD tree of *rel_dir* (repo-relative), or ``(None, reason)``.

    The directory may live inside a git submodule: the tree is read from the
    repository that tracks it (``rev-parse --show-toplevel`` from inside the
    directory), so a vendored submodule addon is judged by its own HEAD.
    """
    prefix_in_repo = _norm_dir(rel_dir)
    abs_dir = (repo_path / prefix_in_repo).resolve() if prefix_in_repo else repo_path.resolve()
    if not abs_dir.is_dir():
        return None, f"module directory {prefix_in_repo or '.'} not on disk"
    out = _git(abs_dir, "rev-parse", "--show-toplevel")
    if not out or not out.strip():
        return None, f"git cannot resolve the repository of {prefix_in_repo or '.'}"
    toplevel = Path(out.decode("utf-8", "surrogateescape").strip()).resolve()
    try:
        prefix_in_top = _norm_dir(abs_dir.relative_to(toplevel).as_posix())
    except ValueError:
        return None, f"{prefix_in_repo} is outside its git toplevel"
    tree = _git(toplevel, "rev-parse", "--verify", "--quiet", f"HEAD:{prefix_in_top}")
    if not tree or not tree.strip():
        return None, f"HEAD has no tree at {prefix_in_repo or '.'}"
    # A module at the root of its repository has the empty prefix, which git
    # rejects as a pathspec ("empty string is not a valid pathspec"): the
    # whole tree is listed then.
    pathspec = ["--", prefix_in_top] if prefix_in_top else []
    listing = _git(toplevel, "ls-tree", "-r", "-z", "--full-tree", "HEAD", *pathspec)
    if listing is None:
        return None, f"git ls-tree failed for {prefix_in_repo or '.'}"
    blobs: dict[str, str] = {}
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        meta, _, path_b = entry.partition(b"\t")
        parts = meta.split()
        if len(parts) != 3:
            return None, f"unreadable ls-tree entry under {prefix_in_repo}"
        mode, kind, oid = (p.decode() for p in parts)
        path = path_b.decode("utf-8", "surrogateescape")
        if mode == "160000" or kind == "commit":
            # A gitlink: the module's files are partly in another repository
            # whose HEAD this listing does not show - nothing can be proven.
            return None, f"submodule {path} inside the module directory"
        if kind == "blob":
            blobs[path] = oid
    if not any(prefix_in_top + m in blobs for m in _MANIFEST_NAMES):
        return None, f"HEAD tree of {prefix_in_repo or '.'} has no manifest"
    return _DirTree(
        toplevel=toplevel, prefix_in_top=prefix_in_top, prefix_in_repo=prefix_in_repo,
        tree_id=tree.decode().strip(), blobs=blobs,
    ), None


def _blob_sizes(toplevel: Path, oids: list[str]) -> dict[str, int] | None:
    """Blob sizes by oid (one ``git cat-file --batch-check``), or None on error."""
    if not oids:
        return {}
    unique = sorted(set(oids))
    out = _git(toplevel, "cat-file", "--batch-check",
               stdin=("\n".join(unique) + "\n").encode())
    if out is None:
        return None
    sizes: dict[str, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 3 or parts[1] != b"blob":
            return None
        sizes[parts[0].decode()] = int(parts[2])
    return sizes if len(sizes) == len(unique) else None


def _cat_blobs(toplevel: Path, oids: list[str]) -> dict[str, bytes] | None:
    """Blob contents by oid (one ``git cat-file --batch``), or None on error."""
    if not oids:
        return {}
    unique = sorted(set(oids))
    out = _git(toplevel, "cat-file", "--batch", stdin=("\n".join(unique) + "\n").encode())
    if out is None:
        return None
    contents: dict[str, bytes] = {}
    pos = 0
    for _ in unique:
        nl = out.find(b"\n", pos)
        if nl < 0:
            return None
        header = out[pos:nl].split()
        if len(header) != 3 or header[1] != b"blob":
            return None
        size = int(header[2])
        start = nl + 1
        contents[header[0].decode()] = out[start:start + size]
        pos = start + size + 1  # content is followed by a newline
    return contents


def module_tree_fingerprint(repo_path: Path | str, module_dirs: Iterable[str]) -> str:
    """Identity of the module's HEAD tree(s): ``dir=treeid`` per directory.

    ``dir=?`` where git cannot resolve a tree, so an unavailable source has a
    stable fingerprint too (it is not re-evaluated every night).
    """
    repo = Path(repo_path)
    parts = []
    for rel in sorted({_norm_dir(d) for d in module_dirs}):
        prefix_abs = (repo / rel).resolve() if rel else repo.resolve()
        tree_id = "?"
        if prefix_abs.is_dir():
            top = _git(prefix_abs, "rev-parse", "--show-toplevel")
            if top and top.strip():
                toplevel = Path(top.decode("utf-8", "surrogateescape").strip()).resolve()
                try:
                    in_top = _norm_dir(prefix_abs.relative_to(toplevel).as_posix())
                    out = _git(toplevel, "rev-parse", "--verify", "--quiet", f"HEAD:{in_top}")
                    if out and out.strip():
                        tree_id = out.decode().strip()
                except ValueError:
                    pass
        parts.append(f"{rel or '.'}={tree_id}")
    return ";".join(parts)


@dataclass
class ModuleSource:
    """The text of one module at HEAD, indexed by identifier token.

    ``files``: repo-relative POSIX path -> decoded text of every non-binary
    tracked file under the module directories (translation catalogues left
    out). ``prefixes``: the module directories (repo-relative, ``/``-ended).
    ``available`` False (with ``reason``) means nothing can be proven: every
    verdict is ``present``.
    """

    module: str
    repo_path: Path
    prefixes: tuple[str, ...]
    available: bool
    reason: str | None = None
    fingerprint: str = ""
    files: dict[str, str] = field(default_factory=dict)
    # Previous module directories git proves HEAD does not have: a file
    # under one maps (so its absence can be concluded) but is never present.
    absent_prefixes: tuple[str, ...] = ()
    _index: dict[str, set[str]] = field(default_factory=dict, repr=False)
    _memo: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        index: dict[str, set[str]] = defaultdict(set)
        for path, text in self.files.items():
            for tok in set(_TOKEN_RE.findall(text)):
                index[tok].add(path)
        self._index = dict(index)

    # -- lookups -------------------------------------------------------------

    def rel_path(self, path: str | None) -> str | None:
        """*path* (absolute or repo-relative) as a repo-relative path inside
        the module, or None when it cannot be mapped there (then no verdict
        may rest on its absence from the module's files; only
        :meth:`foreign_path_absent` may prove it gone)."""
        if not path:
            return None
        p = path.replace("\\", "/")
        if p.startswith("/"):
            try:
                p = Path(p).resolve().relative_to(self.repo_path.resolve()).as_posix()
            except (ValueError, OSError):
                return None
        p = str(PurePosixPath(p))
        if any(p.startswith(prefix) for prefix in (*self.prefixes, *self.absent_prefixes)
               if prefix):
            return p
        if "" in self.prefixes:
            return p
        return None

    def foreign_path_absent(self, path: str | None) -> bool:
        """True when *path* maps into none of the module's directories
        (:meth:`rel_path` is None) and git proves the registered repo's HEAD
        has nothing at it: no blob, no tree, no submodule above it.

        Every parser reads a module's files from under its own directory
        (``static/``, ``tests/``, ``views/``, ...), so a file-keyed child whose
        path lies outside every directory of the module's copies was written
        from a copy this repo does not ship - the repo the module moved from
        (``addons/<m>`` in the old repo, ``<m>`` here), a layout it no longer
        has. Its file is provably not in the code the module is judged
        against. An absolute path outside the repo, or a path git cannot rule
        out (it exists here, a submodule could hold it, git fails), is False:
        no verdict may rest on it."""
        if not path or not self.available:
            return False
        p = path.replace("\\", "/")
        if p.startswith("/"):
            try:
                p = Path(p).resolve().relative_to(self.repo_path.resolve()).as_posix()
            except (ValueError, OSError):
                return False
        if self.rel_path(p) is not None:
            return False
        key = ("foreign_path_absent", p)
        if key not in self._memo:
            self._memo[key] = _provably_absent(self.repo_path, p)
        return self._memo[key]

    def has_file(self, rel: str) -> bool:
        return rel in self.files

    def candidates(self, ident: str, suffixes: tuple[str, ...] | None = None) -> list[str]:
        """Files containing every identifier run of *ident* (sorted); every
        file when *ident* has no identifier run (the index cannot narrow it,
        so nothing may be concluded from it)."""
        runs = _TOKEN_RE.findall(ident)
        if not runs:
            out = sorted(self.files)
            return [p for p in out if p.endswith(suffixes)] if suffixes is not None else out
        found: set[str] | None = None
        for run in runs:
            files = self._index.get(run)
            if not files:
                return []
            found = set(files) if found is None else found & files
            if not found:
                return []
        out = sorted(found or ())
        if suffixes is not None:
            out = [p for p in out if p.endswith(suffixes)]
        return out

    def search(
        self, pattern: re.Pattern, ident: str, *,
        suffixes: tuple[str, ...] | None = None, files: Iterable[str] | None = None,
    ) -> list[str]:
        """Files (among the candidates for *ident*) where *pattern* matches.
        Memoized: a module's stale set repeats the same identifiers."""
        scope = None if files is None else tuple(sorted(set(files)))
        key = (pattern.pattern, ident, suffixes, scope)
        if key in self._memo:
            return self._memo[key]
        pool = self.candidates(ident, suffixes)
        if scope is not None:
            wanted = set(scope)
            pool = [p for p in pool if p in wanted]
        found = [p for p in pool if pattern.search(self.files[p])]
        self._memo[key] = found
        return found

    def manifests(self) -> list[str]:
        return [
            prefix + name for prefix in self.prefixes for name in _MANIFEST_NAMES
            if prefix + name in self.files
        ]


def load_module_source(
    repo_path: Path | str, module: str, module_dir: str,
    previous_dirs: Iterable[str] = (),
) -> ModuleSource:
    """Read *module*'s HEAD files from git (never the working tree).

    *module_dir*: the module's repo-relative directory. *previous_dirs*: the
    directories it was indexed from before a path drift; one that still
    exists in HEAD is read too (its text can only add ``present`` verdicts),
    one that is gone is skipped. A current directory that cannot be read, a
    gitlink inside it, or a HEAD tree without the module's manifest makes the
    source unavailable (every verdict ``present``).
    """
    repo = Path(repo_path)
    current = _norm_dir(module_dir)
    others = sorted({_norm_dir(d) for d in previous_dirs if d is not None} - {current})
    dirs = [current, *others]
    fingerprint = module_tree_fingerprint(repo, dirs)
    files: dict[str, str] = {}
    trees: list[_DirTree] = []
    absent: list[str] = []
    for rel in dirs:
        tree, reason = _read_dir_tree(repo, rel)
        if tree is None:
            if rel == current:
                return ModuleSource(module, repo, tuple(dirs), False, reason, fingerprint)
            # A previous (pre-drift) directory adds no text. When git proves
            # HEAD has nothing there (e.g. an untracked copy the module was
            # once indexed from), files under it are provably not in HEAD.
            if _provably_absent(repo, rel):
                absent.append(rel)
            continue
        trees.append(tree)
    for tree in trees:
        wanted = {
            path: oid for path, oid in tree.blobs.items()
            if not path.lower().endswith(_SKIP_SUFFIXES + _BINARY_SUFFIXES)
        }
        sizes = _blob_sizes(tree.toplevel, list(wanted.values()))
        if sizes is None:
            return ModuleSource(module, repo, tuple(dirs), False,
                                f"git cat-file --batch-check failed under {tree.prefix_in_repo}",
                                fingerprint)
        too_big = sorted(p for p, oid in wanted.items() if sizes[oid] > _MAX_BLOB_BYTES)
        if too_big:
            return ModuleSource(module, repo, tuple(dirs), False,
                                f"{too_big[0]} is larger than "
                                f"{_MAX_BLOB_BYTES // (1024 * 1024)} MiB", fingerprint)
        contents = _cat_blobs(tree.toplevel, list(wanted.values()))
        if contents is None:
            return ModuleSource(module, repo, tuple(dirs), False,
                                f"git cat-file failed under {tree.prefix_in_repo}",
                                fingerprint)
        for path_in_top, oid in wanted.items():
            data = contents.get(oid)
            if data is None:
                return ModuleSource(module, repo, tuple(dirs), False,
                                    f"blob {oid} missing", fingerprint)
            if b"\0" in data[:_BINARY_SNIFF_BYTES]:
                continue
            rel = tree.prefix_in_repo + path_in_top[len(tree.prefix_in_top):]
            files[rel] = data.decode("utf-8", errors="replace")
    return ModuleSource(
        module, repo, tuple(t.prefix_in_repo for t in trees), True, None, fingerprint, files,
        absent_prefixes=tuple(absent),
    )


def _provably_absent(repo: Path, rel: str) -> bool:
    """True when the registered repo's HEAD has nothing at *rel* (a
    repo-relative directory or file) and no ancestor of *rel* is a gitlink
    (a submodule could hold it)."""
    rel = _norm_dir(rel).rstrip("/")
    if not rel or ".." in rel.split("/"):
        return False
    if _git(repo, "rev-parse", "--verify", "--quiet", "HEAD^{commit}") is None:
        return False
    if _git(repo, "rev-parse", "--verify", "--quiet", f"HEAD:{rel}"):
        return False
    parts = rel.split("/")
    ancestors = ["/".join(parts[:i]) for i in range(1, len(parts))]
    if ancestors:
        listing = _git(repo, "ls-tree", "-z", "--full-tree", "HEAD", "--", *ancestors)
        if listing is None:
            return False
        for entry in listing.split(b"\0"):
            if entry and entry.split()[0] == b"160000":
                return False
    return True


# ---------------------------------------------------------------------------
# Stale items (what the writer reads back) and their verdicts
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StaleNode:
    """A module child the run did not re-write (``Neo4jWriter.module_stale_children``).

    ``twin``: a live node of the same label and the same identity minus
    module / file exists in the module (written by this run).
    ``parent_live``: (LintViolation) its View was re-written this run.
    """

    id: str
    label: str
    name: str | None = None
    model: str | None = None
    xmlid: str | None = None
    file_path: str | None = None
    test_class: str | None = None
    target: str | None = None
    twin: bool = False
    parent_live: bool = False

    def describe(self) -> str:
        key = self.xmlid or self.name or self.file_path or self.target or "?"
        where = self.model or self.test_class
        return f"{self.label} {key}" + (f" ({where})" if where else "")


@dataclass(frozen=True)
class StaleRel:
    """A relationship a surviving child (or the Module) no longer re-stamped."""

    id: str
    type: str
    start_label: str
    start_name: str | None = None
    start_model: str | None = None
    start_file: str | None = None
    end_labels: tuple[str, ...] = ()
    end_name: str | None = None
    end_xmlid: str | None = None
    end_file: str | None = None
    end_qualified_name: str | None = None
    end_id: str | None = None
    # A record-form QWeb view is keyed by its website key; an extender's
    # ``inherit_id`` / a report may name its record xmlid instead.
    end_record_xmlid: str | None = None
    # ``twin``: this run wrote a relationship of the same type from the same
    # start node to ANOTHER node of the same identity (``REL_TWIN_TYPES``,
    # ``Neo4jWriter.module_stale_children``): the relationship was re-pointed.
    twin: bool = False
    # The end node's ``status`` (a CoreSymbol's: ``USES_CORE_SYMBOL`` exists
    # only towards a ``deprecated`` one).
    end_status: str | None = None

    def describe(self) -> str:
        end = self.end_xmlid or self.end_qualified_name or self.end_name or self.end_file
        return f"{self.start_label} {self.start_name or '?'} -[{self.type}]-> {end or '?'}"


def _model_pattern(name: str) -> re.Pattern:
    # A model name as a whole dotted name: 'sale.order' is not found inside
    # 'sale.order.line' nor 'x_sale.order', but anywhere else (literal,
    # constant, XML attribute, comment) it counts as present.
    return re.compile(rf"(?<![\w$.]){re.escape(name)}(?![\w$])(?!\.[\w$])")


def _dotted_pattern(name: str) -> re.Pattern:
    """A dotted name (xmlid, bundle) as a whole: not followed by more name chars."""
    return re.compile(rf"(?<![\w$.]){re.escape(name)}{_E}")


def _token_pattern(name: str) -> re.Pattern:
    return re.compile(rf"{_B}{re.escape(name)}{_E}")


def _field_pattern(name: str) -> re.Pattern:
    # Every form a parser reads a field from, widened: class-body assignment
    # (also parenthesized / annotated), era1 ``_columns`` dict key (quoted),
    # ``_columns.update({...})``, keyword ``name=``; any whitespace (incl.
    # newlines inside brackets, backslash continuations) before ``=`` / ``:``.
    # Anchored on the name itself (a leading ``(`` passes the look-behind), so
    # the scan does not restart at every whitespace run of a large file.
    n = re.escape(name)
    return re.compile(
        rf"(?<![\w$.]){n}[\s\\)]*(?:=(?!=)|:)"
        rf"|['\"]{n}['\"][\s\\]*:"
    )


def _def_pattern(name: str) -> re.Pattern:
    return re.compile(rf"{_B}def[\s\\]+{re.escape(name)}{_E}")


def _class_pattern(name: str) -> re.Pattern:
    return re.compile(rf"{_B}class[\s\\]+{re.escape(name)}{_E}")


_PATCH_CALL = re.compile(rf"{_B}(?:extend|include|patch)\s*\(")
_PY = (".py",)


def _local_part(xmlid: str) -> str:
    return xmlid.split(".", 1)[1] if "." in xmlid else xmlid


# ---------------------------------------------------------------------------
# Python class index (class-scoped Field / Method presence, model identity)
# ---------------------------------------------------------------------------

# ``warnings.catch_warnings`` swaps a process-global list: serialize it so
# concurrent prune workers do not interleave the save / restore.
_AST_LOCK = threading.Lock()

# Framework bases: never a local Python base of a model class.
_FRAMEWORK_BASES = frozenset({
    "Model", "TransientModel", "AbstractModel", "BaseModel",
    "osv", "osv_memory", "Model_memory", "AbstractModel_memory", "object",
})

# Bound on the constant chains followed (``A = B``, ``B = 'x'``, re-exports).
_MAX_EVAL_DEPTH = 16

# A ``_name`` / ``_inherit`` assignment in a file Python 3 cannot parse
# (v8-v10 Python 2): group 1 is the right-hand side up to the end of the line.
_META_ASSIGN_RE = re.compile(r"^[ \t]+_(?:name|inherit)[ \t]*=[ \t]*(.*)$", re.M)
_PY_STRING_RE = re.compile(
    r"""[rRuUbB]{0,2}(?:'''.*?'''|\"\"\".*?\"\"\"|'(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*")""",
    re.S,
)
_LITERAL_WORDS = frozenset({"None", "True", "False"})


def _parse_py(text: str) -> ast.Module | None:
    """``ast.parse`` of one file, or None when Python 3 cannot parse it."""
    with _AST_LOCK, warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        try:
            return ast.parse(text)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            return None


def _base_name(expr: ast.expr) -> str | None:
    """``Base`` / ``mod.Base`` -> ``Base`` (the name a local class has)."""
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return None


def _target_names(target: ast.expr) -> Iterable[str]:
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            yield from _target_names(elt)
    elif isinstance(target, ast.Starred):
        yield from _target_names(target.value)


def _scope_statements(body: list[ast.stmt]) -> Iterable[ast.stmt]:
    """The statements of one scope, compound blocks (``if`` / ``try`` /
    ``with`` / loops / ``match``) included, nested functions and classes not."""
    for stmt in body:
        yield stmt
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for attr in ("body", "orelse", "finalbody"):
            yield from _scope_statements(getattr(stmt, attr, None) or [])
        for handler in getattr(stmt, "handlers", None) or []:
            yield from _scope_statements(handler.body)
        for case in getattr(stmt, "cases", None) or []:
            yield from _scope_statements(case.body)


def _bindings(stmts: Iterable[ast.stmt]) -> dict[str, list[ast.expr | None]]:
    """``name -> [value expression]`` of every assignment in *stmts* (None:
    a binding whose value cannot be attributed, e.g. a tuple unpacked from a
    call, an augmented assignment, a loop variable)."""
    out: dict[str, list[ast.expr | None]] = defaultdict(list)
    for stmt in stmts:
        if isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    out[target.id].append(stmt.value)
                elif (isinstance(target, (ast.Tuple, ast.List))
                      and isinstance(stmt.value, (ast.Tuple, ast.List))
                      and len(target.elts) == len(stmt.value.elts)
                      and not any(isinstance(e, ast.Starred) for e in target.elts)):
                    for t, v in zip(target.elts, stmt.value.elts):
                        for name in _target_names(t):
                            out[name].append(v if isinstance(t, ast.Name) else None)
                else:
                    for name in _target_names(target):
                        out[name].append(None)
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if stmt.value is not None:
                out[stmt.target.id].append(stmt.value)
        elif isinstance(stmt, ast.AugAssign) and isinstance(stmt.target, ast.Name):
            out[stmt.target.id].append(None)
        elif isinstance(stmt, (ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)):
            targets = ([stmt.target] if isinstance(stmt, (ast.For, ast.AsyncFor))
                       else [i.optional_vars for i in stmt.items if i.optional_vars])
            for target in targets:
                for name in _target_names(target):
                    out[name].append(None)
    return dict(out)


def _columns_keys(stmt: ast.stmt) -> Iterable[str]:
    """String keys of ``_columns = {...}`` / ``_columns.update({...})``."""
    dicts: list[ast.Dict] = []
    if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Dict) and any(
        isinstance(t, ast.Name) and t.id == "_columns" for t in stmt.targets
    ):
        dicts.append(stmt.value)
    elif (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)
          and isinstance(stmt.value.func, ast.Attribute)
          and stmt.value.func.attr == "update"
          and isinstance(stmt.value.func.value, ast.Name)
          and stmt.value.func.value.id == "_columns"):
        dicts.extend(a for a in stmt.value.args if isinstance(a, ast.Dict))
    for d in dicts:
        for key in d.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                yield key.value


def _meta_rhs(text: str, start: int) -> str:
    """The right-hand side of a meta assignment starting at *start*: up to the
    bracket that closes an opening ``[`` / ``(`` (strings skipped), else to the
    end of the line, backslash continuations followed."""
    if text[start:start + 1] in ("[", "("):
        depth, i = 0, start
        while i < len(text):
            m = _PY_STRING_RE.match(text, i)
            if m and text[i] in "'\"rRuUbB" and m.end() > i:
                i = m.end()
                continue
            ch = text[i]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
            elif ch == "#":
                nl = text.find("\n", i)
                i = len(text) if nl < 0 else nl
                continue
            i += 1
        return text[start:]
    end = start
    while True:
        nl = text.find("\n", end)
        if nl < 0:
            return text[start:]
        if text[nl - 1:nl] == "\\":
            end = nl + 1
            continue
        return text[start:nl]


def _text_meta_unresolvable(text: str) -> bool:
    """True when a file Python 3 cannot parse assigns ``_name`` / ``_inherit``
    from anything but string literals (a constant, a call, an attribute):
    the text fallback cannot know which model that class defines."""
    for m in _META_ASSIGN_RE.finditer(text):
        rhs = _meta_rhs(text, m.start(1))
        bare = _PY_STRING_RE.sub(" ", rhs)
        bare = re.sub(r"#[^\n]*", " ", bare)
        words = set(re.findall(r"[A-Za-z_]\w*", bare)) - _LITERAL_WORDS
        if words:
            return True
    return False


@dataclass
class _PyClass:
    """One ``class`` statement of the module's Python source."""

    path: str
    name: str
    bases: tuple[str, ...]
    meta: list[ast.expr]  # the values assigned to ``_name`` / ``_inherit``
    has_meta: bool
    bindings: dict[str, list[ast.expr | None]]  # the class body's names
    field_names: frozenset[str]
    method_names: frozenset[str]


class _PyIndex:
    """The module's Python classes, parsed once, for class-scoped verdicts.

    A class's models are the names its ``_name`` / ``_inherit`` evaluate to
    statically: string literals, tuples / lists of them, and names bound to
    them in the class body, at module level of the same file, or in another
    file of the same addon (relative / ``odoo.addons.<module>`` imports,
    star imports). Every value it cannot evaluate - a constant imported from
    another addon, a call, an f-string, an attribute, a concatenation - makes
    the class UNRESOLVED: it may define any model, so nothing about its models
    can be proven. Like the rest of the oracle the evaluation is deliberately
    broader than the parser's (all bindings of a name count, wherever in the
    scope), never narrower.
    """

    def __init__(self, source: ModuleSource) -> None:
        self.module = source.module
        self.prefixes = source.prefixes
        self.trees: dict[str, ast.Module | None] = {}
        self.unparsed: set[str] = set()
        self.classes: list[_PyClass] = []
        self.file_bindings: dict[str, dict[str, list[ast.expr | None]]] = {}
        # name -> ("internal", original name) | ("external", None)
        self.file_imports: dict[str, dict[str, tuple[str, str | None]]] = {}
        self.star_internal: set[str] = set()
        self.star_external: set[str] = set()
        self.module_bindings: dict[str, list[tuple[str, ast.expr | None]]] = defaultdict(list)
        self._text_unresolved = False
        for path in sorted(p for p in source.files if p.endswith(_PY)):
            text = source.files[path]
            tree = _parse_py(text)
            self.trees[path] = tree
            if tree is None:
                self.unparsed.add(path)
                if _text_meta_unresolvable(text):
                    self._text_unresolved = True
                continue
            self._index_file(path, tree)
        self.by_name: dict[str, list[_PyClass]] = defaultdict(list)
        self.by_path: dict[str, list[_PyClass]] = defaultdict(list)
        self.children: dict[str, list[_PyClass]] = defaultdict(list)
        for cls in self.classes:
            self.by_name[cls.name].append(cls)
            self.by_path[cls.path].append(cls)
            for base in cls.bases:
                self.children[base].append(cls)
        self._own: dict[int, set[str] | None] = {}
        self._eff: dict[int, set[str] | None] = {}
        self._has_unresolved: bool | None = None

    # -- indexing -----------------------------------------------------------

    def _is_internal(self, stmt: ast.ImportFrom, path: str) -> bool:
        """True when *stmt* imports from a file of this addon: a relative
        import that stays inside the module directory, or an absolute
        ``odoo.addons.<module>`` / ``openerp.addons.<module>`` one."""
        if stmt.level:
            homes = [p for p in self.prefixes if path.startswith(p)]
            if not homes:
                return False
            prefix = max(homes, key=len)
            # models/ui.py -> 2 parts: level 1 is models/, level 2 the addon
            # root, level 3 already outside the addon.
            depth = len(PurePosixPath(path[len(prefix):]).parts)
            return stmt.level <= depth
        mod = stmt.module or ""
        return any(
            mod == f"{root}{self.module}" or mod.startswith(f"{root}{self.module}.")
            for root in ("odoo.addons.", "openerp.addons.")
        )

    def _index_file(self, path: str, tree: ast.Module) -> None:
        top = list(_scope_statements(tree.body))
        binds = _bindings(top)
        self.file_bindings[path] = binds
        for name, values in binds.items():
            self.module_bindings[name].extend((path, v) for v in values)
        imports: dict[str, tuple[str, str | None]] = {}
        for stmt in top:
            if isinstance(stmt, ast.ImportFrom):
                internal = self._is_internal(stmt, path)
                for alias in stmt.names:
                    if alias.name == "*":
                        (self.star_internal if internal else self.star_external).add(path)
                        continue
                    imports[alias.asname or alias.name] = (
                        ("internal", alias.name) if internal else ("external", None)
                    )
            elif isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    imports[alias.asname or alias.name.split(".")[0]] = ("external", None)
        self.file_imports[path] = imports
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                self.classes.append(self._class(path, node))

    @staticmethod
    def _class(path: str, node: ast.ClassDef) -> _PyClass:
        stmts = list(_scope_statements(node.body))
        binds = _bindings(stmts)
        meta: list[ast.expr] = []
        has_meta = False
        fields_: set[str] = set()
        methods: set[str] = set()
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                methods.add(stmt.name)
                continue
            if isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    names = list(_target_names(target))
                    fields_.update(names)
                    if isinstance(target, ast.Name) and target.id in ("_name", "_inherit"):
                        has_meta = True
                        meta.append(stmt.value)
                    elif {"_name", "_inherit"} & set(names):
                        # Unpacked into a meta attribute: not evaluable.
                        has_meta = True
                        meta.append(ast.Call(func=ast.Name(id="?"), args=[], keywords=[]))
            elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
                if isinstance(stmt.target, ast.Name):
                    fields_.add(stmt.target.id)
                    if stmt.target.id in ("_name", "_inherit"):
                        has_meta = True
                        meta.append(stmt.value if isinstance(stmt, ast.AnnAssign)
                                    and stmt.value is not None
                                    else ast.Call(func=ast.Name(id="?"), args=[], keywords=[]))
            fields_.update(_columns_keys(stmt))
        bases = tuple(
            b for b in (_base_name(e) for e in node.bases)
            if b and b not in _FRAMEWORK_BASES and b != node.name
        )
        return _PyClass(
            path=path, name=node.name, bases=bases, meta=meta, has_meta=has_meta,
            bindings=binds, field_names=frozenset(fields_), method_names=frozenset(methods),
        )

    # -- evaluation ---------------------------------------------------------

    def _eval(self, expr, cls: _PyClass | None, path: str, seen: frozenset) -> set[str] | None:
        """Model names *expr* evaluates to, or None when it cannot be evaluated."""
        if expr is None or len(seen) > _MAX_EVAL_DEPTH:
            return None
        if isinstance(expr, ast.Constant):
            return {expr.value} if isinstance(expr.value, str) else set()
        if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
            out: set[str] = set()
            for elt in expr.elts:
                val = self._eval(elt, cls, path, seen)
                if val is None:
                    return None
                out |= val
            return out
        if isinstance(expr, ast.Name):
            return self._eval_name(expr.id, cls, path, seen)
        return None

    def _eval_all(self, values, cls, path, seen) -> set[str] | None:
        out: set[str] = set()
        for value in values:
            val = self._eval(value, cls, path, seen)
            if val is None:
                return None
            out |= val
        return out

    def _eval_name(self, name: str, cls: _PyClass | None, path: str,
                   seen: frozenset) -> set[str] | None:
        key = (id(cls) if cls is not None else None, path, name)
        if key in seen:
            return None  # a cycle
        seen = seen | {key}
        if cls is not None and name in cls.bindings:
            mine = self._eval_all(cls.bindings[name], cls, path, seen)
            if mine is None:
                return None
            if (name in self.file_bindings.get(path, {})
                    or name in self.file_imports.get(path, {})
                    or path in self.star_internal or path in self.star_external):
                # Also bound outside the class: a class-body read before the
                # class-level binding sees the outer value (``_name = X`` then
                # ``X = 'a'``), so both count; an outer value the oracle cannot
                # evaluate makes the class unresolved.
                outer = self._eval_name(name, None, path, seen)
                return None if outer is None else mine | outer
            return mine
        imported = self.file_imports.get(path, {}).get(name)
        local = self.file_bindings.get(path, {}).get(name)
        if imported is not None and imported[0] == "external":
            return None
        if local is not None:
            if imported is not None:
                # Bound here AND imported: either may be the live value.
                other = self._eval_module_wide(imported[1] or name, path, seen)
                mine = self._eval_all(local, None, path, seen)
                return None if other is None or mine is None else other | mine
            return self._eval_all(local, None, path, seen)
        if imported is not None:
            return self._eval_module_wide(imported[1] or name, path, seen)
        if path in self.star_external or path not in self.star_internal:
            # Bound by a star import of another addon, a def / class, a
            # builtin: nothing here says which value it has.
            return None
        return self._eval_module_wide(name, path, seen)

    def _eval_module_wide(self, name: str, path: str, seen: frozenset) -> set[str] | None:
        """Every module-level binding of *name* in the module's files (other
        than *path*); None when there is none - the value lives elsewhere."""
        values = [(p, v) for p, v in self.module_bindings.get(name, ()) if p != path]
        if not values:
            return None
        out: set[str] = set()
        for p, value in values:
            val = self._eval(value, None, p, seen)
            if val is None:
                return None
            out |= val
        return out

    # -- models -------------------------------------------------------------

    def own_models(self, cls: _PyClass) -> set[str] | None:
        """Models *cls*'s own ``_name`` / ``_inherit`` name (None: unresolved)."""
        if id(cls) not in self._own:
            self._own[id(cls)] = (
                _meta_names(self, cls) if cls.has_meta else None
            )
        return self._own[id(cls)]

    def _up(self, cls: _PyClass, seen: set[int]) -> set[str] | None:
        """Own models, else (no meta) the models of its local Python bases -
        Odoo reads ``_name`` through the class MRO. None: unknown."""
        if cls.has_meta:
            return self.own_models(cls)
        if id(cls) in seen:
            return set()
        seen.add(id(cls))
        bases = [b for name in cls.bases for b in self.by_name.get(name, ())]
        if not bases:
            return None  # no model identity at all: anything is possible
        out: set[str] = set()
        for base in bases:
            val = self._up(base, seen)
            if val is None:
                return None
            out |= val
        return out

    def effective_models(self, cls: _PyClass) -> set[str] | None:
        """Every model a field / method of *cls*'s body can be defined on:
        its own (or inherited-through-MRO) models plus those of every local
        class deriving from it (Python inheritance carries the members).
        None: some class of that family is unresolved or has no identity."""
        if id(cls) in self._eff:
            return self._eff[id(cls)]
        out: set[str] | None = set()
        stack, seen = [cls], set()
        while stack:
            cur = stack.pop()
            if id(cur) in seen:
                continue
            seen.add(id(cur))
            val = self._up(cur, set())
            if val is None:
                out = None
                break
            out |= val
            stack.extend(self.children.get(cur.name, ()))
        self._eff[id(cls)] = out
        return out

    def has_unresolved(self) -> bool:
        """True when some class's ``_name`` / ``_inherit`` cannot be
        evaluated: then no model of the module can be proven absent."""
        if self._has_unresolved is None:
            self._has_unresolved = self._text_unresolved or any(
                cls.has_meta and self.own_models(cls) is None for cls in self.classes
            )
        return self._has_unresolved

    def member_present(self, kind: str, name: str, model: str, files: Iterable[str]) -> bool:
        """True when *name* (a field or a method) is defined in the body of a
        class that can define it on *model*, in one of *files* (the text
        candidates). A file Python cannot parse falls back to the text rule."""
        for path in files:
            if path in self.unparsed or path not in self.trees:
                return True
            for cls in self.by_path.get(path, ()):
                names = cls.field_names if kind == "field" else cls.method_names
                if name not in names:
                    continue
                models = self.effective_models(cls)
                if models is None or model in models:
                    return True
        return False


def _meta_names(index: _PyIndex, cls: _PyClass) -> set[str] | None:
    """Evaluate a class's ``_name`` / ``_inherit`` values (None: unresolved).

    The one place the "an unevaluable identity proves nothing" rule lives:
    ``tests/test_prune_evidence.py`` replaces it to prove the tests depend on it.
    """
    return index._eval_all(cls.meta, cls, cls.path, frozenset())


class _Judge:
    """Verdicts for one module's stale items against its HEAD source."""

    def __init__(self, source: ModuleSource) -> None:
        self.src = source
        self.module_names = {source.module}
        self._regions: dict[str, list[str]] = {}
        self._trees: dict[str, dict[str, list] | None] = {}
        self._pyindex: _PyIndex | None = None

    def py(self) -> _PyIndex:
        """The module's parsed Python classes (built on first use)."""
        if self._pyindex is None:
            self._pyindex = _PyIndex(self.src)
        return self._pyindex

    # -- nodes ---------------------------------------------------------------

    def node(self, n: StaleNode) -> str:
        if not self.src.available:
            return VERDICT_PRESENT
        present = self._node_present(n)
        if present is None:
            return VERDICT_PRESENT
        if not present:
            return VERDICT_GONE
        if n.twin:
            # A same-name test class (method) was written this run in another
            # file of the module. A module may define two classes of one name
            # in two files (sale v17 has two TestSaleCommon), so the twin
            # replaces this node only when its own file no longer defines it.
            if n.label in ("TestClass", "TestMethod") and self._test_in_own_file(n):
                return VERDICT_PRESENT
            return VERDICT_SUPERSEDED
        if n.label == "LintViolation" and n.parent_live:
            # Derived data: the violation's view was re-validated this run and
            # no longer raises it (fixed, or moved to another line).
            return VERDICT_SUPERSEDED
        return VERDICT_PRESENT

    def _test_in_own_file(self, n: StaleNode) -> bool:
        """True when the node's own file still defines its test class (and,
        for a TestMethod, the method); True as well when the file cannot be
        mapped into the module (nothing can be concluded)."""
        src = self.src
        rel = src.rel_path(n.file_path)
        if rel is None:
            # A file of another copy of the module (the repo it moved from):
            # its own file is provably not shipped here.
            return not src.foreign_path_absent(n.file_path)
        if not src.has_file(rel):
            return False
        cls = n.name if n.label == "TestClass" else n.test_class
        if not cls or not n.name:
            return True
        hits = src.search(_class_pattern(cls), cls, suffixes=_PY, files=[rel])
        if n.label == "TestMethod" and hits:
            hits = src.search(_def_pattern(n.name), n.name, files=[rel])
        return bool(hits)

    def _node_present(self, n: StaleNode) -> bool | None:
        """True / False = still defined / provably gone; None = undecidable."""
        src = self.src
        label = n.label
        if label == "Model":
            if not n.name:
                return None
            if src.search(_model_pattern(n.name), n.name):
                return True
            # A class whose identity cannot be evaluated (a constant of
            # another addon, a call, an f-string) may define this model.
            return self.py().has_unresolved()
        if label in ("Field", "Method"):
            if not n.name:
                return None
            pattern = _field_pattern(n.name) if label == "Field" else _def_pattern(n.name)
            files = src.search(pattern, n.name, suffixes=_PY)
            if not files or not n.model:
                return bool(files)
            # Class-scoped: defined in the body of a class that can define it
            # on the node's model (a file Python cannot parse: text rule).
            kind = "field" if label == "Field" else "method"
            return self.py().member_present(kind, n.name, n.model, files)
        if label in ("TestClass", "TestHelper"):
            if not n.name:
                return None
            return bool(src.search(_class_pattern(n.name), n.name, suffixes=_PY))
        if label == "TestMethod":
            if not n.name or not n.test_class:
                return None
            homes = src.search(_class_pattern(n.test_class), n.test_class, suffixes=_PY)
            if not homes:
                return False
            return bool(src.search(_def_pattern(n.name), n.name, files=homes))
        if label in ("View", "QWebTmpl", "Report"):
            if not n.xmlid:
                return None
            local = _local_part(n.xmlid)
            return bool(src.search(_token_pattern(local), local))
        if label == "OWLComp":
            if not n.name:
                return None
            return bool(src.search(_class_pattern(n.name), n.name))
        if label == "JSPatch":
            rel = src.rel_path(n.file_path)
            if rel is None:
                return self._foreign_file_gone(n)
            if not src.has_file(rel):
                return False
            text = src.files[rel]
            target_ok = (
                not n.target or n.target == PurePosixPath(rel).stem
                or bool(_token_pattern(n.target).search(text))
            )
            return target_ok and bool(_PATCH_CALL.search(text))
        if label in ("JsTestSuite", "Stylesheet"):
            rel = src.rel_path(n.file_path)
            if rel is None:
                return self._foreign_file_gone(n)
            return src.has_file(rel)
        if label == "LintViolation":
            # Derived from its View: gone with its file or with its view's
            # record; re-derived (superseded, see node()) when the view was
            # re-validated this run; otherwise judged by the view's xmlid.
            rel = src.rel_path(n.file_path)
            if rel is None:
                return self._foreign_file_gone(n)
            if not src.has_file(rel):
                return False
            if n.parent_live:
                return True
            if not n.xmlid:
                return None
            local = _local_part(n.xmlid)
            return bool(src.search(_token_pattern(local), local))
        return None  # unknown label: never provably gone

    def _foreign_file_gone(self, n: StaleNode) -> bool | None:
        """A file-keyed node whose path maps into no module directory: False
        (provably gone) when the repo's HEAD has nothing at the path
        (``ModuleSource.foreign_path_absent``), None (undecidable) otherwise."""
        return False if self.src.foreign_path_absent(n.file_path) else None

    # -- relationships ------------------------------------------------------

    def rel(self, r: StaleRel) -> str:
        if not self.src.available:
            return VERDICT_PRESENT
        if r.type == "INHERITS" and r.end_name and r.end_name == r.start_name:
            # Same-name extension edge: derived from the graph by the version
            # post-pass (reconcile_same_name_inherits), which re-adds every
            # one still valid after the prune; no source text declares it.
            return VERDICT_SUPERSEDED
        if r.twin:
            # This run wrote a relationship of the same type from the same
            # start node to another node of the same identity: the writer
            # re-pointed it (it picks one of the same-name nodes by ranking,
            # and the winner changed). The declaration is reflected by the
            # fresh relationship; this one is the old target.
            return VERDICT_SUPERSEDED
        if (r.type == "USES_CORE_SYMBOL" and r.end_status is not None
                and r.end_status != "deprecated"):
            # Derived from graph state: the writer links a method only to a
            # ``deprecated`` CoreSymbol; one no longer deprecated (a curated
            # status change) can never be re-linked, whatever the text says.
            return VERDICT_SUPERSEDED
        present = self._rel_present(r)
        if present is None or present:
            return VERDICT_PRESENT
        return VERDICT_GONE

    def _end_ident_present(self, r: StaleRel, *, files=None, suffixes=None) -> bool | None:
        src = self.src
        labels = set(r.end_labels)
        if "Model" in labels and r.end_name:
            return bool(src.search(_model_pattern(r.end_name), r.end_name,
                                   files=files, suffixes=suffixes))
        dotted = r.end_xmlid or (r.end_name if labels & {"AssetBundle"} else None)
        if dotted:
            # Every identity the source may name the end node by: its xmlid
            # and, for a record-form QWeb view, its record xmlid.
            for ident in dict.fromkeys(i for i in (dotted, r.end_record_xmlid) if i):
                if src.search(_dotted_pattern(ident), ident, files=files, suffixes=suffixes):
                    return True
                ns = ident.split(".", 1)[0] if "." in ident else None
                if ns is None or ns in self.module_names:
                    local = _local_part(ident)
                    if src.search(_token_pattern(local), local,
                                  files=files, suffixes=suffixes):
                        return True
            return False
        if "Stylesheet" in labels and r.end_file:
            # ``@import "vars"`` / ``"./_vars.scss"`` / ``(reference) "vars.less"``:
            # the target's stem, with or without the partial's underscore.
            stem = PurePosixPath(r.end_file).stem.lstrip("_")
            if not stem:
                return None
            pat = re.compile(rf"{_B}_*{re.escape(stem)}{_E}")
            pool = files if files is not None else list(src.files)
            return any(pat.search(src.files[p]) for p in pool if p in src.files)
        ident = r.end_qualified_name or r.end_name
        if not ident:
            return None
        last = ident.rsplit(".", 1)[-1]
        if not last:
            return None
        return bool(src.search(_token_pattern(last), last, files=files, suffixes=suffixes))

    def _rel_present(self, r: StaleRel) -> bool | None:
        src = self.src
        t = r.type
        if t == "DEPENDS_ON":
            if not r.end_name:
                return None
            manifests = src.manifests()
            if not manifests:
                return None
            return bool(src.search(_token_pattern(r.end_name), r.end_name, files=manifests))
        if t == "DELEGATES_TO" and not src.candidates("_inherits", _PY):
            return False
        if t in ("USES_FIELD", "DEPENDS_ON_FIELD", "USES_CORE_SYMBOL"):
            if not r.start_name:
                return None
            regions = self._method_regions(r.start_name)
            if regions is None:
                return None
            ident = r.end_qualified_name or r.end_name
            if not ident:
                return None
            last = ident.rsplit(".", 1)[-1]
            pat = _token_pattern(last)
            return any(pat.search(text) for text in regions)
        if t == "IMPORTS":
            rel = src.rel_path(r.start_file)
            files = [rel] if rel and src.has_file(rel) else None
            return self._end_ident_present(r, files=files)
        if t in ("INHERITS", "DELEGATES_TO"):
            present = self._end_ident_present(r, suffixes=_PY)
            if present is None or present:
                return present
            # The parent may be named by a class identity the oracle cannot
            # evaluate (``_inherit = OTHER_ADDON_CONSTANT``).
            return self.py().has_unresolved()
        return self._end_ident_present(r)

    def _method_regions(self, name: str) -> list[str] | None:
        """Source text of every ``def <name>`` in the module's .py files,
        decorators included; a file Python cannot parse contributes its whole
        text (a superset). Empty when no definition exists. Cached per name
        (and the parsed files per path) for the whole module."""
        if name in self._regions:
            return self._regions[name]
        files = self.src.search(_def_pattern(name), name, suffixes=_PY)
        regions: list[str] = []
        for path in files:
            text = self.src.files[path]
            spans = self._function_spans(path, text)
            if spans is None or name not in spans:
                # Unparseable file, or a ``def`` Python does not see as one
                # (in a string): the whole file, a superset.
                regions.append(text)
                continue
            lines = text.splitlines()
            regions.extend("\n".join(lines[first - 1:last]) for first, last in spans[name])
        self._regions[name] = regions
        return regions

    def _function_spans(self, path: str, text: str) -> dict[str, list] | None:
        """``{function name: [(first line incl. decorators, last line)]}`` of
        one file, parsed once; None when Python cannot parse it."""
        if path not in self._trees:
            tree = self.py().trees.get(path) if path.endswith(_PY) else _parse_py(text)
            if tree is None:
                self._trees[path] = None
            else:
                spans: dict[str, list] = defaultdict(list)
                n_lines = text.count("\n") + 1
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                        last = getattr(node, "end_lineno", None) or n_lines
                        spans[node.name].append((first, last))
                self._trees[path] = dict(spans)
        return self._trees[path]


@dataclass
class Classification:
    """Verdicts of one module's stale set."""

    nodes: dict[str, str]  # node id -> verdict
    rels: dict[str, str]  # rel id -> verdict
    kept_nodes: list[StaleNode]
    kept_rels: list[StaleRel]
    counts: dict[str, int]

    @property
    def delete_node_ids(self) -> list[str]:
        return sorted(i for i, v in self.nodes.items() if v in DELETABLE_VERDICTS)

    @property
    def delete_rel_ids(self) -> list[str]:
        return sorted(i for i, v in self.rels.items() if v in DELETABLE_VERDICTS)


def classify(
    source: ModuleSource,
    nodes: Iterable[StaleNode],
    rels: Iterable[StaleRel] = (),
) -> Classification:
    """Verdict for every stale node and relationship of one module."""
    judge = _Judge(source)
    node_verdicts: dict[str, str] = {}
    rel_verdicts: dict[str, str] = {}
    kept_nodes: list[StaleNode] = []
    kept_rels: list[StaleRel] = []
    counts = {f"nodes_{v}": 0 for v in (VERDICT_GONE, VERDICT_SUPERSEDED, VERDICT_PRESENT)}
    counts.update({f"rels_{v}": 0 for v in (VERDICT_GONE, VERDICT_SUPERSEDED, VERDICT_PRESENT)})
    for n in nodes:
        verdict = judge.node(n)
        node_verdicts[n.id] = verdict
        counts[f"nodes_{verdict}"] += 1
        if verdict == VERDICT_PRESENT:
            kept_nodes.append(n)
    deleted = {i for i, v in node_verdicts.items() if v in DELETABLE_VERDICTS}
    for r in rels:
        # A relationship into a node the prune deletes goes with that node
        # (DETACH DELETE); it is no kept parser gap.
        verdict = VERDICT_SUPERSEDED if r.end_id in deleted else judge.rel(r)
        rel_verdicts[r.id] = verdict
        counts[f"rels_{verdict}"] += 1
        if verdict == VERDICT_PRESENT:
            kept_rels.append(r)
    return Classification(node_verdicts, rel_verdicts, kept_nodes, kept_rels, counts)


def stale_node_from_row(row: Mapping) -> StaleNode:
    """A :class:`StaleNode` from one ``module_stale_children`` node row."""
    return StaleNode(
        id=str(row["id"]), label=row["label"], name=row.get("name"),
        model=row.get("model"), xmlid=row.get("xmlid"), file_path=row.get("file_path"),
        test_class=row.get("test_class"), target=row.get("target"),
        twin=bool(row.get("twin")), parent_live=bool(row.get("parent_live")),
    )


def stale_rel_from_row(row: Mapping) -> StaleRel:
    """A :class:`StaleRel` from one ``module_stale_children`` relationship row."""
    return StaleRel(
        id=str(row["id"]), type=row["type"], start_label=row["start_label"],
        start_name=row.get("start_name"), start_model=row.get("start_model"),
        start_file=row.get("start_file"), end_labels=tuple(row.get("end_labels") or ()),
        end_name=row.get("end_name"), end_xmlid=row.get("end_xmlid"),
        end_file=row.get("end_file"), end_qualified_name=row.get("end_qualified_name"),
        end_id=str(row["end_id"]) if row.get("end_id") is not None else None,
        end_record_xmlid=row.get("end_record_xmlid"),
        twin=bool(row.get("twin")), end_status=row.get("end_status"),
    )
