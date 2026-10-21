// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Admin module lifecycle page (/admin/lifecycle, #380) - pure view logic.
 *
 * The data is the ledger (GET /api/admin/lifecycle) and the report of
 * `lifecycle-audit` itself (GET/POST /api/admin/lifecycle/audit/{version},
 * schema osm.lifecycle-audit/5). Nothing here decides anything: the index run
 * retires and prunes from git evidence alone (ADR-0057). This module only
 * turns those payloads into what the page shows, and tells the island when to
 * start or wait for an audit.
 */

export interface LifecycleCounts {
  present: number;
  excluded: number;
  retired: number;
  retire_pending: number;
  needs_rewrite: number;
}

export interface LifecycleRepo {
  id: number;
  profile: string | null;
  odoo_version: string | null;
  url: string | null;
  branch: string | null;
  basename: string | null;
  local_path: string | null;
  status: string | null;
  error_msg: string | null;
  last_indexed_at: string | null;
  clone_status: string;
  clone_error_msg: string | null;
  checkout_on_disk: boolean;
  head_sha: string | null;
  presence_head_sha: string | null;
  synced: boolean;
  lifecycle_attention: string | null;
  lifecycle_attention_at: string | null;
  lifecycle_counts: LifecycleCounts | null;
}

export interface Retirement {
  id?: number;
  repo_id: number | null;
  repo_basename: string | null;
  profile_name: string | null;
  odoo_version: string;
  name: string;
  retire_reason: string | null;
  state_changed_sha: string | null;
  state_changed_at: string | null;
  removing_commit_sha: string | null;
  removing_commit_date: string | null;
  removing_commit_subject: string | null;
  successor_names: string[] | null;
  successor_source: string | null;
}

export interface AuditSnapshot {
  version: string;
  state: 'idle' | 'running' | 'ready' | 'error';
  running_version: string | null;
  fresh: boolean;
  ttl_seconds: number;
  age_seconds: number | null;
  finished_at: string | null;
  error: string | null;
  error_at: string | null;
  report?: AuditReport | null;
  busy?: string;
}

export interface LifecycleVersion {
  odoo_version: string;
  repo_ids: number[];
  retired_total: number;
  recent_retirements: Retirement[];
  audit: AuditSnapshot | null;
}

export interface LifecycleOverview {
  generated_at: string;
  repos: LifecycleRepo[];
  versions: LifecycleVersion[];
  retirements_error: string | null;
  audit: { ttl_seconds: number; timeout_seconds: number; running_version: string | null };
}

// The audit report is read defensively: keys are optional so a report of a
// newer schema that dropped one renders what it still has.
export type AuditReport = {
  schema?: string;
  generated_at?: string;
  duration_s?: number;
  repos?: Array<Record<string, any>>;
  versions?: Array<Record<string, any>>;
  findings?: Record<string, number>;
};

export function shortSha(sha: string | null | undefined): string {
  return sha ? sha.slice(0, 10) : '-';
}

/**
 * "Merged into X" for a git-proven N:1 merge (successor source `git_merge`),
 * "Renamed to X" for every other recorded successor (git rename, manifest
 * old_technical_name), null when the ledger recorded none.
 */
export function successorText(
  names: string[] | null | undefined,
  source: string | null | undefined,
): string | null {
  const list = (names ?? []).filter(Boolean);
  if (list.length === 0) return null;
  const verb = source === 'git_merge' ? 'Merged into' : 'Renamed to';
  return `${verb} ${list.join(', ')}`;
}

const RETIRE_REASON_TEXT: Record<string, string> = {
  absent: 'manifest deleted from the branch',
  repo_removed: 'repository removed from its profile',
  orphan_sweep: 'no repository ships it any more',
};

export function retireReasonText(reason: string | null | undefined): string {
  if (!reason) return 'retired';
  return RETIRE_REASON_TEXT[reason] ?? reason;
}

export type RepoHealth = 'unavailable' | 'attention' | 'lagging' | 'never_indexed' | 'ok';

/**
 * One label per repo, worst first: no checkout on disk (the index run cannot
 * read it), an open lifecycle attention, a ledger behind the indexed HEAD,
 * never indexed, else ok.
 */
export function repoHealth(repo: LifecycleRepo): RepoHealth {
  if (!repo.checkout_on_disk) return 'unavailable';
  if (repo.lifecycle_attention || repo.lifecycle_attention_at) return 'attention';
  if (!repo.head_sha) return 'never_indexed';
  if (!repo.synced) return 'lagging';
  return 'ok';
}

export const REPO_HEALTH_TEXT: Record<RepoHealth, string> = {
  unavailable: 'No checkout on disk',
  attention: 'Needs a look',
  lagging: 'Ledger behind HEAD',
  never_indexed: 'Not indexed yet',
  ok: 'In sync',
};

export function healthCounts(repos: LifecycleRepo[]): Record<RepoHealth, number> {
  const out: Record<RepoHealth, number> = {
    unavailable: 0, attention: 0, lagging: 0, never_indexed: 0, ok: 0,
  };
  for (const r of repos) out[repoHealth(r)] += 1;
  return out;
}

/** What the island does for a version panel the admin opened. */
export type AuditStep = 'start' | 'wait' | 'show';

/**
 * - running (this version) -> wait and poll;
 * - another version holds the single audit slot -> wait, then start;
 * - a fresh report -> show it;
 * - a stale report, an error or nothing yet -> start a run (a stale report
 *   stays on screen while the new one runs).
 */
export function nextAuditStep(snap: AuditSnapshot | null): AuditStep {
  if (!snap) return 'start';
  if (snap.state === 'running') return 'wait';
  if (snap.running_version && snap.running_version !== snap.version) return 'wait';
  if (snap.state === 'ready' && snap.fresh) return 'show';
  return 'start';
}

export interface AuditFetchResult {
  snap: AuditSnapshot | null;
  error: string | null;
}

export interface DriveAuditOpts {
  /** "Run again": start a new run even when a fresh report exists. */
  force: boolean;
  /** GET (start=false) or POST (start=true, force=f) the version's audit. */
  fetch: (start: boolean, force?: boolean) => Promise<AuditFetchResult>;
  sleep: () => Promise<void>;
  cancelled: () => boolean;
  onSnapshot: (snap: AuditSnapshot) => void;
}

/**
 * Bring one version panel to a shown report: read the state, start a run
 * when {@link nextAuditStep} says so, poll while it (or another version's
 * run) is in progress. A run this loop started that ends without a fresh
 * report (it failed) is shown, never restarted in a loop. Returns the
 * request error to display, or null.
 */
export async function driveAudit(opts: DriveAuditOpts): Promise<string | null> {
  let started = false;
  let res = await opts.fetch(opts.force, opts.force);
  if (opts.force && res.snap?.state === 'running') started = true;
  while (!opts.cancelled()) {
    if (res.error || !res.snap) return res.error ?? 'no answer from the server';
    opts.onSnapshot(res.snap);
    const step = nextAuditStep(res.snap);
    if (step === 'show' || (step === 'start' && started)) return null;
    if (step === 'start') {
      res = await opts.fetch(true, false);
      if (res.snap?.state === 'running') started = true;
      continue;
    }
    await opts.sleep();
    if (opts.cancelled()) break;
    res = await opts.fetch(false);
  }
  return null;
}

export interface ParserGapRow {
  repo: string;
  name: string;
  kept: number;
  total: number;
  rels_kept: number;
  rels_total: number;
  sample: string[];
  reason: string | null;
  reevaluated_next_run: boolean;
}

export interface AuditView {
  generatedAt: string | null;
  durationS: number | null;
  findings: Record<string, number>;
  /** ADR-0057: kept because the HEAD source still defines them; a parser defect. */
  parserGaps: ParserGapRow[];
  /** No longer in the scan, but the manifest is still in the HEAD tree: kept. */
  unprovenDrops: Array<{ repo: string; names: string[] }>;
  unparseableKept: Array<{ repo: string; names: string[] }>;
  keptOrphans: Array<{ name: string; why: string }>;
  childOrphansKept: Array<{ name: string; why: string }>;
  embeddingOrphansKept: Array<{ module: string; profile: string; rows: number }>;
  backlog: Array<{ repo: string; remaining: number; nextRun: number }>;
  /** What the next index run removes by itself (git-proven). */
  nextRunRemovals: {
    retire: Array<{ name: string; repo: string; detail: string }>;
    orphanModules: string[];
    childOrphans: string[];
    embeddingGroups: number;
    sharedPrunes: Array<{ name: string; outcome: string }>;
  };
  repoNotes: Array<{ repo: string; nextRun: string | null; notes: string[]; error: string | null }>;
  unsyncedRepos: Array<{ repo: string; why: string }>;
  errors: string[];
}

function repoLabel(entry: Record<string, any>): string {
  const base = entry.basename || `repo ${entry.repo_id ?? '?'}`;
  return entry.profile ? `${base} (${entry.profile})` : base;
}

function commitText(commit: Record<string, any> | null | undefined): string {
  if (!commit || !commit.sha) return 'removing commit unknown';
  const subject = commit.subject ? ` "${commit.subject}"` : '';
  return `removed by ${shortSha(commit.sha)}${subject}`;
}

/** The parts of an osm.lifecycle-audit/5 report the version panel shows. */
export function auditView(report: AuditReport | null | undefined, version: string): AuditView {
  const repos = report?.repos ?? [];
  const v = (report?.versions ?? []).find((x) => x.odoo_version === version) ?? {};
  const view: AuditView = {
    generatedAt: report?.generated_at ?? null,
    durationS: report?.duration_s ?? null,
    findings: report?.findings ?? {},
    parserGaps: [],
    unprovenDrops: [],
    unparseableKept: [],
    keptOrphans: [],
    childOrphansKept: [],
    embeddingOrphansKept: [],
    backlog: [],
    nextRunRemovals: {
      retire: [], orphanModules: [], childOrphans: [], embeddingGroups: 0, sharedPrunes: [],
    },
    repoNotes: [],
    unsyncedRepos: [],
    errors: [],
  };
  for (const r of repos) {
    const label = repoLabel(r);
    for (const g of r.parser_gaps ?? []) {
      view.parserGaps.push({
        repo: label,
        name: g.name,
        kept: g.kept ?? 0,
        total: g.total ?? 0,
        rels_kept: g.rels_kept ?? 0,
        rels_total: g.rels_total ?? 0,
        sample: g.sample ?? [],
        reason: g.reason ?? null,
        reevaluated_next_run: Boolean(g.reevaluated_next_run),
      });
    }
    if ((r.unproven_drops ?? []).length) {
      view.unprovenDrops.push({ repo: label, names: [...r.unproven_drops] });
    }
    if ((r.unparseable_kept ?? []).length) {
      view.unparseableKept.push({ repo: label, names: [...r.unparseable_kept] });
    }
    const backlog = r.shared_parse_backlog ?? {};
    if ((backlog.remaining ?? 0) > 0) {
      view.backlog.push({
        repo: label, remaining: backlog.remaining, nextRun: (backlog.next_run ?? []).length,
      });
    }
    for (const item of r.would_retire ?? []) {
      const succ = successorText(item.successor?.names, item.successor?.source);
      view.nextRunRemovals.retire.push({
        name: item.name,
        repo: label,
        detail: [commitText(item.removing_commit), succ].filter(Boolean).join('; '),
      });
    }
    const notes = [...(r.scan_attention ?? []), ...(r.attention ?? [])];
    if (notes.length || r.error) {
      view.repoNotes.push({ repo: label, nextRun: r.next_run ?? null, notes, error: r.error ?? null });
    }
    if (r.error) view.errors.push(`${label}: ${r.error}`);
  }
  for (const o of v.orphan_modules ?? []) {
    if (o.kept_because) {
      view.keptOrphans.push({ name: o.name, why: o.kept_because });
    } else if ((o.deferred_for ?? []).length) {
      view.keptOrphans.push({
        name: o.name, why: `waits until profile(s) ${o.deferred_for.join(', ')} sync`,
      });
    } else {
      view.nextRunRemovals.orphanModules.push(o.name);
    }
  }
  for (const [name, why] of Object.entries(v.child_orphans_kept ?? {})) {
    view.childOrphansKept.push({ name, why: String(why) });
  }
  for (const name of v.child_orphans_deferred ?? []) {
    view.childOrphansKept.push({
      name, why: 'waits until every repository at this version is synced',
    });
  }
  view.nextRunRemovals.childOrphans = [...(v.child_orphans ?? [])];
  view.embeddingOrphansKept = (v.embedding_orphans_kept ?? []).map((e: any) => ({
    module: e.module, profile: e.profile, rows: e.rows,
  }));
  view.nextRunRemovals.embeddingGroups = (v.embedding_orphans ?? []).length;
  view.nextRunRemovals.sharedPrunes = (v.shared_prunes ?? []).map((s: any) => ({
    name: s.name, outcome: s.outcome,
  }));
  view.unsyncedRepos = (v.unsynced_repos ?? []).map((u: any) => ({
    repo: u.profile ? `${u.repo} (${u.profile})` : String(u.repo), why: u.why,
  }));
  if (v.error) view.errors.push(`version ${version}: ${v.error}`);
  for (const [key, text] of Object.entries(v.errors ?? {})) {
    view.errors.push(`${key}: ${text}`);
  }
  return view;
}

/** "3 min ago" style age for the audit report; null when unknown. */
export function ageText(seconds: number | null | undefined): string | null {
  if (seconds === null || seconds === undefined) return null;
  if (seconds < 60) return 'less than a minute ago';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  return `${hours} h ago`;
}
