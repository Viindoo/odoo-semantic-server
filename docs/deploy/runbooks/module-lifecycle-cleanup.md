# Module lifecycle rollout (0.19.0, ADR-0056) - ghosts leave on the first normal run

> Deploys the module lifecycle ledger (issues #373, #378) and lets the normal index run
> remove the ghost modules the old `--gc` design left behind. There is NO manual cleanup
> script (owner decision): the per-version reconcile cleans them under its safety gates.
> Every step that touches production needs an explicit go from the owner.

## What changes in production

- New Postgres table `module_presence` (the ledger) and three `repos` columns
  (`presence_head_sha`, `lifecycle_attention`, `lifecycle_attention_at`) - migration `0003`.
- Every `index-repo` run now retires modules no repo ships any more (renamed, deleted, moved,
  merged), with their subtree and embeddings, and prunes the fields/methods/views/relations a
  live module no longer defines. No flag. `--gc` is a no-op, `--full` is not needed.
- The first run after the deploy takes the "sync path" for every repo (the ledger is empty):
  it scans, writes the ledger rows, re-writes modules whose node drifted (wrong path, lost
  profile), and the version reconcile sweeps the pre-ledger ghosts.
- `index-repo` exits **3** when the lifecycle needs an operator; `OnFailure=` of the reindex
  unit fires. Nothing is deleted in that case.
- New dry run `python -m src.indexer lifecycle-audit` and a weekly unit for it.
- Neo4j gains new indexes (created by `setup_indexes()` at the start of the first index run).

Known ghosts when the issue was surveyed (2026-09): at 17.0 `test_pylint`, `viin_ai_rag`,
`viin_ai_agent`, `viin_ai_skill`, `viin_ai_search`, `viin_ai_pulse_memory`; at 18.0 and 19.0
`test_pylint`. Treat the list as a probe set, not as the expected output: step 3 prints what the
run will actually do.

## Placeholders

| Placeholder | Meaning | Default |
|---|---|---|
| `PY` | app venv python | `/home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python` |
| `$PG_DSN` | owner DSN from the app `.env` | - |
| `$NEO4J_PASSWORD` | from the compose `.env` | - |

Run every indexer / audit command through `sudo osm-fernet-run $PY -m src.indexer ...`
(`docs/deploy/osm-fernet-run`): it runs as `odoo-semantic`, loads `.env`, and delivers
FERNET_KEY for the pre-scan `git fetch` of SSH repos.

## Step 0 - Pre-deploy, read-only checks on the running 0.18.x (no code change yet)

**0a. Module nodes that will read "Indexed: No" after the deploy (F24).** 0.19.0 answers
"Yes" only for a Module with a non-empty `profile` list. Count the nodes with a real path and
an empty list:

```bash
docker compose exec -T neo4j cypher-shell -u neo4j -p "$NEO4J_PASSWORD" "
MATCH (m:Module)
WHERE size(coalesce(m.profile, [])) = 0
  AND m.path IS NOT NULL AND trim(toString(m.path)) <> ''
  AND NOT m.name IN ['@framework', '__unresolved__']
RETURN m.odoo_version AS v, count(*) AS n,
       count(m.repo_id) AS with_repo_id, collect(m.name)[..20] AS sample
ORDER BY v;"
```

Expected: 0 rows. If not 0, record the output. What happens to them after the deploy:
- the name is shipped by a registered repo -> the first normal run re-writes the node from that
  repo (self-heal `no_node`) and it reads "Yes" again;
- not shipped by any registered repo but `repo_id` set -> an orphan, removed by the sweep;
- not shipped and no `repo_id` (pre-ADR-0037 legacy) -> stays, reads "No" (correct for a
  module nobody ships). List these names in the rollout record.

**0b. Untracked manifests in the production clones (F7).** 0.19.0 indexes only git-tracked
manifests; modules indexed from untracked copies (a local `.odoo-ai/` folder, a stray copy)
become orphans or get their path re-written. Count them per clone:

```bash
# comm needs both lists sorted in the same collation; under en_US.UTF-8 sort and
# comm disagree on '_' / '/' and comm aborts with "file is not in sorted order".
export LC_ALL=C
psql "$PG_DSN" -Atc "SELECT local_path FROM repos WHERE local_path IS NOT NULL ORDER BY 1" |
while read -r p; do
  [ -d "$p/.git" ] || { echo "no checkout: $p"; continue; }
  n=$(comm -13 \
      <(sudo -u odoo-semantic git -C "$p" ls-files --recurse-submodules \
          | grep -E '(^|/)__(manifest|openerp)__\.py$' | sort) \
      <(cd "$p" && find . -path ./.git -prune -o -path '*/node_modules' -prune -o \
          \( -name __manifest__.py -o -name __openerp__.py \) -print | sed 's|^\./||' | sort) \
      | wc -l)
  echo "$n untracked manifest(s): $p"
done
```

Expected: 0 everywhere. A large number in one clone means many orphan modules at that version.
Since ADR-0057 phase 2 the sweep removes each one git proves gone (no checkout of its profile
tracks its manifest at HEAD), whatever the count, with no approval; one whose manifest a
checkout still has in its HEAD tree is kept and listed by `lifecycle-audit` (`orphan_modules`
with `kept_because`).

**0c. Registered repos without a checkout (blocks steps 4, 6 and 7).** A registered repo whose
`local_path` is not a directory is cloned by the index run itself when its clone never succeeded
(`clone_status` `manual` / `pending` / `error`, issue #383 item 2: same lock, SSH key and pinned
host keys as the cloner). If that clone fails (branch missing on the remote, wrong key) - or the
row says `cloned` but the directory is gone (never re-cloned automatically) - the run logs one
WARNING line, lists it under `unavailable_repos`, writes `lifecycle_attention` and exits **3**
(no traceback). The run still indexes and reconciles every other repo, but that repo never
syncs, so at its version the sweep keeps every orphan
Module of its profile and the children of every module-less name (attention on that repo, audit
findings `orphan_modules` / `child_orphans`). Step 6 "findings 0" and step 7 "exit=0" cannot pass
until it is fixed. List them:

```bash
psql "$PG_DSN" -Atc "SELECT r.id, p.name, r.url, r.branch, r.local_path, r.clone_status
                     FROM repos r JOIN profiles p ON p.id = r.profile_id ORDER BY r.id" |
while IFS='|' read -r id prof url branch path cs; do
  sudo -u odoo-semantic test -d "$path" || echo "MISSING checkout: repo id=$id profile=$prof $url@$branch path=$path clone_status=$cs"
done
```

Expected: no line. Fix every listed repo BEFORE step 1, one of:
- still wanted: clone it again, `sudo osm-fernet-run $PY -m src.cloner --repo-id <id>` (clones
  into the default clone dir and writes `local_path`; works whatever `clone_status` says, while the
  Web UI "clone all" (`POST /profiles/{id}/clone-all`) only picks repos in `manual` / `pending` /
  `error` and skips one still marked `cloned`);
- no longer wanted: delete the repo registration in the Web UI (the delete reconciles its
  modules through the ledger).

Re-run the check until it prints nothing, and record what was done in the rollout record.

**0d. Baseline probes (for the before/after record).** With an MCP key, capture
`check_module_exists` for the known ghosts above and one live module per version, and
`describe_module(name='viin_ai_rag', odoo_version='17.0')`.

## Step 1 - Backup (ADR-0018)

```bash
sudo systemctl start odoo-semantic-backup.service
sudo journalctl -u odoo-semantic-backup.service -n 50 --no-pager   # bundle path + OK
```

Keep the bundle path in the rollout record. Pause the nightly reindex for the deploy window so
no run starts on half-deployed code:

```bash
sudo systemctl stop odoo-semantic-reindex.timer
systemctl is-active odoo-semantic-reindex.service   # must be "inactive" before continuing
```

## Step 2 - Deploy 0.19.0, migrate 0003, regrant

Deploy the code as for any release (`post-pr-ops.md`), then:

```bash
sudo -u odoo-semantic $PY -m src.db.migrate          # applies 0003_module_presence; idempotent
bash ops/regrant_osm_reader_after_migration.sh       # osm_reader gets SELECT on module_presence
```

Verify (match the migration id exactly: ids are text, so `ORDER BY migration_id` sorts
`m9_*` after `m13_*` and both after every `0NNN_*`, and never names the latest one):

```bash
psql "$PG_DSN" -c "SELECT migration_id, applied_at_utc FROM _yoyo_migration
                   WHERE migration_id = '0003_module_presence';"               # exactly 1 row
psql "$PG_DSN" -c "\d module_presence" | head -5
psql "$PG_DSN" -c "SELECT count(*) FROM module_presence;"                      # 0 before the first run
psql "$PG_DSN" -c "SELECT privilege_type FROM information_schema.role_table_grants
                   WHERE grantee='osm_reader' AND table_name='module_presence';"  # SELECT
```

Restart the long-running services so they load 0.19.0 (MCP, Web UI, Astro), and check
`/health` + `/ready`. Run the systemd drift check FIRST, while the installed units are still
the ones the operator runs (issue #144: a check after a `cp` cannot see the edits the `cp`
just erased); move any reported body drift to a drop-in before going on:

```bash
make check-systemd-overrides        # the two lifecycle-audit units show "not installed"; no body drift elsewhere
```

`odoo-semantic-reindex.service` changes only in comments in 0.19.0 (the check ignores
comments), so it is NOT copied. Install only the two new units, and do NOT enable the audit
timer yet (step 7):

```bash
sudo cp docs/deploy/odoo-semantic-lifecycle-audit.service \
        docs/deploy/odoo-semantic-lifecycle-audit.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

## Step 3 - Preview the first run (dry run, writes nothing)

The report names repos, paths and commit subjects of every tenant: keep it in a private
directory, not in world-readable `/tmp`.

```bash
install -d -m 700 ~/osm-rollout
sudo osm-fernet-run $PY -m src.indexer lifecycle-audit --all > ~/osm-rollout/lifecycle-preview.txt
sudo osm-fernet-run $PY -m src.indexer lifecycle-audit --all --json > ~/osm-rollout/lifecycle-preview.json
```

Review, per version. Every finding key of the report (`findings`, the categories are
`FINDING_KEYS` in `src/indexer/lifecycle_audit.py`) is covered below; a key you cannot explain
is a reason to stop before step 4:

```bash
jq .findings ~/osm-rollout/lifecycle-preview.json
```

- `would_retire` / `orphan_modules` - the ghosts that will go. **Abort (and investigate) if a
  listed name has a tracked manifest in ANY registered repo at that version** (`git -C <clone> ls-files
  | grep -E '(^|/)<name>/__(manifest|openerp)__\.py$'`): that would be a scan or version-rule problem, not a ghost.
- `would_drop_owner` / `would_drop_excluded_owner` - a module two repos ship loses one owner (the
  node stays, `kept_by` names the survivor). Abort if the dropping repo still tracks the manifest
  (same `git ls-files` check).
- `child_orphans` - Module-less names whose leftover children (models, fields, views, ...) the
  sweep deletes; `child_orphans_deferred` - the same, held because a repo at the version is not
  synced (step 0c). Abort if a listed name has a tracked manifest in a registered repo at that
  version.
- `embedding_orphans` - embedding rows (`module` / `profile` / `rows`) that no live Module node
  and no present ledger row account for; the first run deletes every listed group.
  `embedding_orphans_held` + a `gates_tripped` entry `embedding_sweep:<gate>` - the mass gate G-B
  holds the version's embedding sweep (more than half, and at least 20, of the version's
  `(module, profile)` embedding groups, or any while nothing is present at the version): nothing is deleted, the run exits 3, and
  step 5 decides. Totals per version, for the rollout record:

  ```bash
  # groups/rows: deleted by the first run; held_*: kept by gate G-B (step 5 decides)
  jq -r '.versions[] | "\(.odoo_version) groups=\(.embedding_orphans | length) rows=\([.embedding_orphans[].rows] | add // 0) held_groups=\((.embedding_orphans_held // []) | length) held_rows=\([(.embedding_orphans_held // [])[].rows] | add // 0)"' \
     ~/osm-rollout/lifecycle-preview.json
  # the GUC lifts the tenant RLS policy (FORCEd after ops/rls_cutover.sh) for this session
  psql "$PG_DSN" -At -c "SET app.allowed_profiles = '*'" \
       -c "SELECT odoo_version, count(*) FROM embeddings
           WHERE profile_name <> '__global__' GROUP BY 1 ORDER BY 1;"
  ```

  **Abort (and investigate) if a listed `module` has a tracked manifest in a registered repo of
  the listed `profile` at that version** (same `git ls-files` check): that is a scan, profile or
  ledger problem, not ghost residue.
- `blocked` / `gates_tripped` and the repo entries' `gates` - a gate that will hold the first
  run (see step 5). On the first run the per-repo mass gate G-B compares each repo's scan with
  the Module nodes the GRAPH attributes to it (`gates.baseline = "graph"`: the ledger is still
  empty): a repo whose scan no longer covers more than half (and at least 20) of those nodes -
  untracked or gitignored manifests (step 0b), a checkout problem, modules re-keyed to the
  branch version - trips `mass_retire`, stays unsynced, and the sweep keeps every orphan of its
  profile (exit 3 + `lifecycle_attention`). Normal ghost cleanup of the other repos proceeds.
- `undecidable`, `unsynced_repos` - repos that must sync before a name can be decided; a
  registered repo with no checkout (`not_cloned`, unsynced `why` = `no checkout`) keeps its
  profile's orphans and, version-wide, the module-less children (`child_orphans_deferred`):
  go back to step 0c.
- `modules_without_profile` - the F24 list from step 0a.
- `would_rewrite` - nodes the first run re-writes (posbox stub path, `.odoo-ai` copies, lost
  profile). Expected on a pre-ledger graph.
- repo entries `wrong_paths` / `unapplied_changes` - drift a repo's next run does not fix because
  it is the unchanged skip; `index-repo --profile <p> --full` applies them (step 6).
- repo entries `parser_gaps` (schema `/4`, formerly `held_prunes`) - entities an entity prune
  KEPT because the module's git HEAD source still defines them but the parser no longer
  produces them (ADR-0057). The run exits 0 for them; nobody approves anything: fix the parser
  (the module is re-evaluated automatically after the deploy that changes the indexer code).
  Each entry carries a `sample`; an entry whose sample names something git really deleted is a
  ghost the oracle keeps on purpose: a model name still mentioned in a comment, a field /
  method still in the text of a Python 2 file, or any Model / inheritance edge of a module
  with a class whose `_name` / `_inherit` it cannot evaluate (a constant of another addon, a
  call, an f-string).
- version `shared_prunes` - the shared-module prune (`would_prune`, or `partial` when some
  owner's HEAD source still defines part of it, which is kept); schema `/5` has no `held`
  outcome and the prune never exits 3.
- repo entries `unparseable_kept` - an indexed module whose tracked manifest does not parse is
  kept as it is (exit 3); fix the manifest upstream.
- `errors` (version and repo entries) - the preview could not decide something; read the text and
  fix it before step 4.
- repo entries `shared_parse_backlog` (informational) - modules two or more repos ship, still to
  be re-parsed once each (at most `OSM_SHARED_PARSE_BOOTSTRAP_PER_RUN`, default 60, per repo per
  run; about 11 daily runs for a CE clone). Budget this extra parse time per run.

## Step 4 - The first normal index run (no `--full`, no `--gc`, no script)

Off-peak, either re-enable the timer and wait for 03:30, or run it once by hand:

```bash
sudo osm-fernet-run $PY -m src.indexer index-repo --all --profile-workers 2
echo "exit=$?"
```

Before it, repeat the step 0c check (no line).

- `exit=0` - every decision was taken.
- `exit=1` - a repo or profile failed to index (the traceback ends with `N repo(s) failed: id=...`
  or `N profile(s) failed: ...`); every other repo was still indexed and reconciled. The
  `Lifecycle needs attention (exit 1):` lines, if any, are the lifecycle outcome of the healthy
  repos, act on them as for exit 3. The repo error is in `repos.error_msg`. Fix it and run
  again: step 6 cannot pass while a repo fails.
- `exit=3` - the index was written, something OSM cannot fix by itself was held. Read the stderr
  tail (`Lifecycle needs attention (exit 3):` + `gates_tripped:` / `undecidable:` / `errors:` /
  `unavailable_repos:` lines, plus `embedding_orphans_held: <v>: ...` when a graph missing Module
  nodes held the embedding sweep) and `repos.lifecycle_attention`, then act per the
  exit-code-3 table in `docs/deploy.md` s3.6. A missing checkout is fixed per step 0c. A hold
  keeps the data; nothing is lost by waiting.
- Mass deletions are NOT held any more (ADR-0057 phase 2): a commit that deletes 22 of 25
  modules, an orphan sweep of hundreds of ghosts or a mass orphan embedding delete all run on
  this first pass when git proves them gone. What git cannot prove is kept and reported by
  `lifecycle-audit` (`unproven_drops`, kept orphans / embedding groups, `parser_gaps`).

**Shared GPU embedder (M1):** the shared-module bootstrap re-parses up to
`OSM_SHARED_PARSE_BOOTSTRAP_PER_RUN` modules (default 60) per repo per run, and each re-parsed
module is written and embedded again. On a host whose GPU embedder shares its card with another
model or workload, add `OSM_SHARED_PARSE_BOOTSTRAP_PER_RUN=20` to the app `.env`
(`/home/odoo-semantic/odoo-semantic-mcp/.env`) before step 4 and keep it for the first week, so
the nightly run's extra embed load stays small while the backlog drains (about three times as
many runs). That one file reaches every index run: `odoo-semantic-reindex.service` loads it
(`EnvironmentFile=`), `osm-fernet-run` passes it to its transient unit (`-p EnvironmentFile=`;
the transient unit does not inherit your shell's environment, so an `export` never reaches
the run), and a Web
UI-started run inherits `odoo-semantic-webui.service`'s environment, which loads the same file
(restart the Web UI to pick up the change). The value is read on every run. Remove the line
once `shared_parse_backlog` in `lifecycle-audit` is 0. The code default is unchanged.

## Step 5 - Only if the run exited 3 on a hold

Nobody approves a deletion (ADR-0057 and its phase 2): the entity prune, the shared-module prune,
repo-level retirement and the orphan / embedding sweeps all delete what git proves gone, whatever
its size, and keep what git cannot prove. What still exits 3 is a state OSM cannot repair by
itself; fix the cause, then let the next normal run decide:

- `total_wipe` - every module of a repo vanished and the wipe is not a commit of the branch
  (force-push, branch swap, wrong clone) or the HEAD tree still has manifests (broken scan /
  checkout). Check the checkout (`git -C <path> log --oneline -3`,
  `git merge-base --is-ancestor <presence_head_sha> HEAD`, the registered url / branch) and fix
  it.
- `embedding_sweep:graph_unhealthy` - the graph lost Module nodes of present modules. Re-write
  them with `sudo osm-fernet-run $PY -m src.indexer index-repo --profile <p> --full`, `<p>` the
  profile the attention / stderr line names.
- `undecidable` - index the profile of the repo named in the message (or unregister a repo that
  is no longer used); the next reconcile decides the name.
- `unavailable_repos` - step 0c.

`--allow-mass-retire` is an emergency bypass only (it ALSO deletes what git cannot prove gone and
lifts the holds above): use it once, scoped (`--profile <p>`), only when the git evidence itself
is known to be wrong. Never put it in the timer or a drop-in.

## Step 6 - Verify

```bash
# Every repo synced, no attention left:
psql "$PG_DSN" -c "SELECT id, url, branch, head_sha = presence_head_sha AS synced,
                   lifecycle_attention FROM repos ORDER BY id;"
# Ledger shape (retired rows carry the history of what left):
psql "$PG_DSN" -c "SELECT odoo_version, state, coalesce(retire_reason, exclusion_reason) AS why,
                   count(*) FROM module_presence GROUP BY 1,2,3 ORDER BY 1,2,3;"
psql "$PG_DSN" -c "SELECT odoo_version, name, retire_reason, removing_commit_sha,
                   removing_commit_subject, successor_names FROM module_presence
                   WHERE name IN ('test_pylint','viin_ai_rag','viin_ai_agent','viin_ai_skill',
                                  'viin_ai_search','viin_ai_pulse_memory')
                   ORDER BY odoo_version, name;"
```

MCP probes (compare with step 0d):
- `check_module_exists(name='test_pylint', odoo_version='17.0')` -> `Indexed: No`, a lifecycle
  block with the removing commit subject and `Renamed to: test_viin_pylint`, and a
  `Next: check_module_exists(name='test_viin_pylint', ...)` line.
- `check_module_exists(name='test_viin_pylint', odoo_version='17.0')` -> Yes, with a
  `Last seen: HEAD <sha7> on <date>` line.
- `impact_analysis` on `viin.ai.embedding` at 17.0 lists no `viin_ai_rag` methods.
- `test_class_inspect(name='SavepointCase', odoo_version='16.0')` shows no `[?]` row.

Audit (`findings` in the JSON report; the categories are `FINDING_KEYS` in
`src/indexer/lifecycle_audit.py`):

```bash
sudo osm-fernet-run $PY -m src.indexer lifecycle-audit --all --json | jq .findings
```

| Finding | After the first run |
|---|---|
| `would_retire`, `would_drop_owner`, `undecidable`, `blocked`, `orphan_modules`, `child_orphans`, `embedding_orphans`, `errors` | 0 (anything left: act per step 5 / deploy.md s3.6) |
| `modules_without_profile` | only the legacy names recorded in step 0a |
| `wrong_paths`, `unapplied_changes` | 0 (a repo the next run skips at an unchanged HEAD; `index-repo --profile <p> --full` applies them) |
| `unparseable_kept` | 0 (an indexed module whose tracked manifest does not parse is kept as it is; fix the manifest upstream, the next run re-indexes it) |
| `would_rewrite`, `shared_prunes` | may stay non-zero while the shared-module bootstrap drains (about 11 nightly runs for a CE clone, see step 3); each normal run resolves what the previous audit listed |
| `parser_gaps`, `unproven_drops` | informational (`informational_findings`): kept data, not an operator task and never an exit 4 - parser defects or scan regressions (or ghosts the oracle keeps on purpose), fixed in code; the next run after such a deploy re-evaluates them (ADR-0057) |

Re-enable the nightly reindex now (if it was stopped in step 1), so the bootstrap drains:

```bash
sudo systemctl enable --now odoo-semantic-reindex.timer
```

## Step 7 - Enable the weekly drift detector (only after the drain)

The weekly unit runs `lifecycle-audit --all --json --fail-on-findings` and fails (exit 4,
`osm-alert@`) on ANY finding except the informational `parser_gaps` / `unproven_drops`, the
bootstrap backlog included. Enabling it before the drain would
alert every week on a healthy rollout. Enable it only once the audit of step 6 reports every
finding at 0 (repeat that audit after the nightly runs; `shared_parse_backlog.remaining` of
every repo entry reaches 0 at the same time):

```bash
sudo osm-fernet-run $PY -m src.indexer lifecycle-audit --all --fail-on-findings > /dev/null; echo "exit=$?"   # exit=0 required
sudo systemctl enable --now odoo-semantic-lifecycle-audit.timer
systemctl list-timers 'odoo-semantic-*' --no-pager
```

Exit 4 from the unit = the audit found drift (alert via `osm-alert@`); without
`--fail-on-findings` the audit always exits 0 on findings. The JSON report is in
`/var/log/odoo-semantic/odoo-semantic-lifecycle-audit.log`.

## Development databases only: migration 0003 was edited before release

`0003_module_presence` was changed several times on the development branch before 0.19.0 was
cut (the last edit added `last_full_parse_*`). yoyo applies a migration once by id, so a dev or
staging database migrated from an earlier branch commit keeps the old shape and fails with
`UndefinedColumn`. Production never ran an unreleased 0003 and is unaffected. Fix a dev
database with the rollback, then migrate again (drops its ledger history):

```bash
psql "$PG_DSN" -f migrations/0003_module_presence.rollback.sql
psql "$PG_DSN" -c "DELETE FROM _yoyo_migration WHERE migration_id = '0003_module_presence';"
sudo -u odoo-semantic $PY -m src.db.migrate
```

## Rollback

- **Code:** redeploy 0.18.x and restart the services. 0.18 code neither reads nor writes the
  ledger table, but it is NOT inert for tenants: 0.18's Web UI returns repo rows as
  `SELECT r.*` unredacted, so the new `repos.lifecycle_attention` (which can name other
  tenants' repos) and `presence_head_sha` reach non-admin viewers. Rolling back also re-opens
  the tenant leaks this release fixes (`test_class_inspect` H6, Web UI F30-F35). Before
  redeploying 0.18, clear the text: `UPDATE repos SET lifecycle_attention = NULL;` (the ledger
  table keeps the history for a later re-deploy).
  `migrations/0003_module_presence.rollback.sql` drops the table, the columns and the history -
  do not run it unless 0.19 is abandoned.
- **A module retired by mistake:** it comes back from its owner's source with
  `index-repo --profile <owner profile> --full` (nodes, subtree, embeddings). If the source no
  longer ships it, it was correctly retired; restore from the step 1 bundle only if the data
  itself is needed (ADR-0018 restore, `docs/deploy/disaster-recovery.md`).
- **Incident while the cause is investigated:** stop deletions without stopping indexing with a
  temporary drop-in, removed again after the incident:

  ```bash
  sudo systemctl edit odoo-semantic-reindex.service
  # [Service]
  # ExecStart=
  # ExecStart=/home/odoo-semantic/.venv/odoo-semantic-mcp/bin/python -m src.indexer index-repo --all --profile-workers 2 --no-retire
  ```

  Names that should go stay `retire_pending`; the first run without `--no-retire` retires them.
