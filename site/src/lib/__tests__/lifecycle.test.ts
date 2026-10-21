// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Tests for the admin module lifecycle page view logic (#380).
 *
 * Business rules under protection:
 *   - A git-proven N:1 merge reads "Merged into", any other recorded successor
 *     "Renamed to"; no successor reads as nothing (never a guess).
 *   - A repo's label is its worst state: no checkout on disk, then an open
 *     attention, then a ledger behind the indexed HEAD.
 *   - The island starts an audit only when the slot is free and no fresh
 *     report exists; it waits while one runs (one audit at a time).
 *   - The audit panel shows what the index run KEPT (parser gaps, unproven
 *     drops, kept orphans) apart from what it will REMOVE by itself, read
 *     from the osm.lifecycle-audit/5 report of the requested version only.
 */
import { describe, expect, it } from 'vitest';

import {
  auditView,
  driveAudit,
  healthCounts,
  nextAuditStep,
  repoHealth,
  retireReasonText,
  successorText,
  type AuditSnapshot,
  type LifecycleRepo,
} from '../lifecycle';

function repo(over: Partial<LifecycleRepo> = {}): LifecycleRepo {
  return {
    id: 1, profile: 'odoo_17', odoo_version: '17.0', url: null, branch: '17.0',
    basename: 'odoo', local_path: '/clones/odoo_17/odoo', status: 'done', error_msg: null,
    last_indexed_at: null, clone_status: 'done', clone_error_msg: null,
    checkout_on_disk: true, head_sha: 'a'.repeat(40), presence_head_sha: 'a'.repeat(40),
    synced: true, lifecycle_attention: null, lifecycle_attention_at: null,
    lifecycle_counts: null, ...over,
  };
}

function snap(over: Partial<AuditSnapshot> = {}): AuditSnapshot {
  return {
    version: '17.0', state: 'idle', running_version: null, fresh: false, ttl_seconds: 600,
    age_seconds: null, finished_at: null, error: null, error_at: null, ...over,
  };
}

describe('successorText', () => {
  it('reads a git merge as "Merged into" and a rename as "Renamed to"', () => {
    expect(successorText(['viin_ai'], 'git_merge')).toBe('Merged into viin_ai');
    expect(successorText(['test_viin_pylint'], 'git_rename')).toBe('Renamed to test_viin_pylint');
    expect(successorText(['loyalty'], 'old_technical_name')).toBe('Renamed to loyalty');
  });

  it('says nothing when the ledger recorded no successor', () => {
    expect(successorText(null, null)).toBeNull();
    expect(successorText([], 'git_rename')).toBeNull();
  });
});

describe('retireReasonText', () => {
  it('explains each ledger retire reason in plain words', () => {
    expect(retireReasonText('absent')).toBe('manifest deleted from the branch');
    expect(retireReasonText('repo_removed')).toBe('repository removed from its profile');
    expect(retireReasonText('orphan_sweep')).toBe('no repository ships it any more');
  });
});

describe('repoHealth', () => {
  it('ranks a missing checkout above an open attention', () => {
    expect(repoHealth(repo({ checkout_on_disk: false, lifecycle_attention: 'x' })))
      .toBe('unavailable');
  });

  it('flags an open attention even when the ledger is in sync', () => {
    expect(repoHealth(repo({ lifecycle_attention: 'gate tripped' }))).toBe('attention');
  });

  it('flags a ledger behind the indexed HEAD', () => {
    expect(repoHealth(repo({ synced: false, presence_head_sha: 'c'.repeat(40) })))
      .toBe('lagging');
  });

  it('tells a never-indexed repo apart from a lagging one', () => {
    expect(repoHealth(repo({ head_sha: null, presence_head_sha: null, synced: false })))
      .toBe('never_indexed');
    expect(repoHealth(repo())).toBe('ok');
  });

  it('counts every repo exactly once', () => {
    const counts = healthCounts([
      repo(), repo({ checkout_on_disk: false }), repo({ synced: false }),
    ]);
    expect(counts).toEqual({ unavailable: 1, attention: 0, lagging: 1, never_indexed: 0, ok: 1 });
  });
});

describe('nextAuditStep', () => {
  it('starts when nothing was run yet, or the last run failed', () => {
    expect(nextAuditStep(null)).toBe('start');
    expect(nextAuditStep(snap())).toBe('start');
    expect(nextAuditStep(snap({ state: 'error', error: 'exit 1' }))).toBe('start');
  });

  it('waits while this version runs or another version holds the slot', () => {
    expect(nextAuditStep(snap({ state: 'running', running_version: '17.0' }))).toBe('wait');
    expect(nextAuditStep(snap({ running_version: '16.0' }))).toBe('wait');
    expect(nextAuditStep(snap({ state: 'ready', fresh: false, running_version: '16.0' })))
      .toBe('wait');
  });

  it('shows a fresh report and re-runs a stale one', () => {
    expect(nextAuditStep(snap({ state: 'ready', fresh: true }))).toBe('show');
    expect(nextAuditStep(snap({ state: 'ready', fresh: false }))).toBe('start');
  });
});

describe('driveAudit', () => {
  type Call = { start: boolean; force: boolean };

  function server(answers: Array<Partial<AuditSnapshot> | { error: string }>) {
    const calls: Call[] = [];
    const shown: AuditSnapshot[] = [];
    let i = 0;
    const opts = {
      force: false,
      fetch: async (start: boolean, force = false) => {
        calls.push({ start, force });
        const a = answers[Math.min(i++, answers.length - 1)];
        if ('error' in a && typeof a.error === 'string' && !('state' in a)) {
          return { snap: null, error: a.error };
        }
        return { snap: snap(a as Partial<AuditSnapshot>), error: null };
      },
      sleep: async () => {},
      cancelled: () => calls.length > 20,
      onSnapshot: (s: AuditSnapshot) => { shown.push(s); },
    };
    return { calls, shown, opts };
  }

  it('shows a fresh report without starting anything', async () => {
    const s = server([{ state: 'ready', fresh: true }]);
    expect(await driveAudit(s.opts)).toBeNull();
    expect(s.calls).toEqual([{ start: false, force: false }]);
  });

  it('starts when nothing exists, polls while it runs, then shows it', async () => {
    const s = server([
      { state: 'idle' },
      { state: 'running', running_version: '17.0' },
      { state: 'running', running_version: '17.0' },
      { state: 'ready', fresh: true },
    ]);
    expect(await driveAudit(s.opts)).toBeNull();
    expect(s.calls.map((c) => c.start)).toEqual([false, true, false, false]);
    expect(s.shown.at(-1)?.state).toBe('ready');
  });

  it('a failed run it started is shown, not restarted', async () => {
    const s = server([
      { state: 'idle' },
      { state: 'running', running_version: '17.0' },
      { state: 'error', error: 'the audit exited with code 1: Neo4j unreachable' },
      { state: 'running', running_version: '17.0' },
    ]);
    expect(await driveAudit(s.opts)).toBeNull();
    expect(s.calls.filter((c) => c.start)).toHaveLength(1);
    expect(s.shown.at(-1)?.error).toContain('Neo4j unreachable');
  });

  it('waits while another version holds the slot, then starts its own run', async () => {
    const s = server([
      { state: 'idle', running_version: '16.0' },
      { state: 'idle', running_version: '16.0' },
      { state: 'idle' },
      { state: 'running', running_version: '17.0' },
      { state: 'ready', fresh: true },
    ]);
    expect(await driveAudit(s.opts)).toBeNull();
    expect(s.calls.map((c) => c.start)).toEqual([false, false, false, true, false]);
  });

  it('"Run again" forces a new run even over a fresh report', async () => {
    const s = server([
      { state: 'running', running_version: '17.0' },
      { state: 'ready', fresh: true },
    ]);
    s.opts.force = true;
    expect(await driveAudit(s.opts)).toBeNull();
    expect(s.calls[0]).toEqual({ start: true, force: true });
  });

  it('returns the request error to display', async () => {
    const s = server([{ error: 'HTTP 403' }]);
    expect(await driveAudit(s.opts)).toBe('HTTP 403');
  });
});

describe('auditView', () => {
  const report = {
    schema: 'osm.lifecycle-audit/5',
    generated_at: '2026-10-20T08:00:00+00:00',
    duration_s: 42.5,
    findings: { parser_gaps: 1, unproven_drops: 1 },
    repos: [
      {
        repo_id: 7, basename: 'tvtmaaddons', profile: 'viindoo_17', next_run: 'incremental',
        parser_gaps: [{
          name: 'viin_ai', odoo_version: '17.0', kept: 1, total: 2214, rels_kept: 0,
          rels_total: 10, sample: ['TestMethod test_x'], reason: null,
          reevaluated_next_run: false,
        }],
        unproven_drops: ['hw_posbox_homepage'],
        unparseable_kept: [],
        shared_parse_backlog: { remaining: 120, next_run: ['a', 'b'] },
        would_retire: [{
          name: 'viin_ai_rag', removing_commit: { sha: '676cf82abcdef1234', subject: 'merge' },
          successor: { names: ['viin_ai'], source: 'git_merge' },
        }],
        scan_attention: [], attention: ['2 module(s) are re-parsed'], error: null,
      },
    ],
    versions: [
      {
        odoo_version: '16.0', orphan_modules: [{ name: 'other_version_ghost' }],
      },
      {
        odoo_version: '17.0',
        orphan_modules: [
          { name: 'ghost_mod', deferred_for: [], kept_because: null },
          { name: 'still_shipped', deferred_for: [], kept_because: 'a checkout still ships it' },
          { name: 'waiting_mod', deferred_for: ['viindoo_17'], kept_because: null },
        ],
        child_orphans: ['gone_parent'],
        child_orphans_kept: { kept_parent: 'a checkout still ships it' },
        child_orphans_deferred: [],
        embedding_orphans: [{ module: 'm', profile: 'p', rows: 3 }],
        embedding_orphans_kept: [{ module: 'k', profile: 'p', rows: 9 }],
        shared_prunes: [{ name: 'base_shared', outcome: 'partial' }],
        unsynced_repos: [{ repo: 'enterprise', profile: 'viindoo_17', why: 'no checkout' }],
        errors: {},
      },
    ],
  };

  it('lists what the run kept apart from what it removes by itself', () => {
    const view = auditView(report, '17.0');
    expect(view.parserGaps.map((g) => [g.repo, g.name, g.kept, g.total])).toEqual([
      ['tvtmaaddons (viindoo_17)', 'viin_ai', 1, 2214],
    ]);
    expect(view.unprovenDrops).toEqual([
      { repo: 'tvtmaaddons (viindoo_17)', names: ['hw_posbox_homepage'] },
    ]);
    expect(view.keptOrphans).toEqual([
      { name: 'still_shipped', why: 'a checkout still ships it' },
      { name: 'waiting_mod', why: 'waits until profile(s) viindoo_17 sync' },
    ]);
    expect(view.childOrphansKept).toEqual([
      { name: 'kept_parent', why: 'a checkout still ships it' },
    ]);
    expect(view.embeddingOrphansKept).toEqual([{ module: 'k', profile: 'p', rows: 9 }]);

    expect(view.nextRunRemovals.orphanModules).toEqual(['ghost_mod']);
    expect(view.nextRunRemovals.childOrphans).toEqual(['gone_parent']);
    expect(view.nextRunRemovals.embeddingGroups).toBe(1);
    expect(view.nextRunRemovals.sharedPrunes).toEqual([{ name: 'base_shared', outcome: 'partial' }]);
    expect(view.nextRunRemovals.retire).toEqual([{
      name: 'viin_ai_rag', repo: 'tvtmaaddons (viindoo_17)',
      detail: 'removed by 676cf82abc "merge"; Merged into viin_ai',
    }]);
  });

  it('reads only the requested version of the report', () => {
    const view = auditView(report, '17.0');
    expect(view.nextRunRemovals.orphanModules).not.toContain('other_version_ghost');
  });

  it('reports the shared-module bootstrap backlog and unsynced repos', () => {
    const view = auditView(report, '17.0');
    expect(view.backlog).toEqual([
      { repo: 'tvtmaaddons (viindoo_17)', remaining: 120, nextRun: 2 },
    ]);
    expect(view.unsyncedRepos).toEqual([{ repo: 'enterprise (viindoo_17)', why: 'no checkout' }]);
  });

  it('renders an empty or partial report without throwing', () => {
    const view = auditView({ schema: 'osm.lifecycle-audit/5' }, '17.0');
    expect(view.parserGaps).toEqual([]);
    expect(view.nextRunRemovals.retire).toEqual([]);
    expect(auditView(null, '17.0').findings).toEqual({});
  });

  it('surfaces audit errors of a repo and of the version', () => {
    const view = auditView({
      repos: [{ repo_id: 3, basename: 'x', error: 'OSError: gone' }],
      versions: [{ odoo_version: '17.0', errors: { sweep: 'timeout' } }],
    }, '17.0');
    expect(view.errors).toEqual(['x: OSError: gone', 'sweep: timeout']);
  });
});
