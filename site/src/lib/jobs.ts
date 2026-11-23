// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * View logic for indexing state and indexer jobs on the admin Repositories and
 * Operations pages (#403).
 *
 * A repository's indexing state is its own row (status / error_msg /
 * last_indexed_at, written by every index run: Web UI job, CLI or scheduled
 * reindex) plus `index_run`, the run indexing its profile right now. A
 * finished job is history (Operations -> Jobs), never a repository's state.
 */

/** The run indexing a repository's profile now (GET /api/repos/profiles). */
export interface IndexRun {
  /** null: started outside the Web UI, or an all-profiles job hidden from a non-admin. */
  job_id: number | null;
  /** The job's profile name, 'all', or null (no job). */
  job_label: string | null;
  started_at: string | null;
  /** False: the run read the repository list before this repository was added. */
  covers_repo: boolean;
}

export interface RepoIndexFields {
  status?: string | null;
  error_msg?: string | null;
  last_indexed_at?: string | null;
  index_run?: IndexRun | null;
}

export type IndexStateKind = 'indexing' | 'indexed' | 'failed' | 'not_indexed';

export interface RepoIndexState {
  kind: IndexStateKind;
  /** Badge text. */
  label: string;
  /** The job to link and poll (indexing only). */
  jobId: number | null;
  /** Which run indexes it ('job #12', 'a CLI or scheduled run', ...). */
  runText: string | null;
  /** Full error text (failed only): raw for admins, a category for others. */
  error: string | null;
  /** When the repository was last indexed successfully, if ever. */
  lastIndexedAt: string | null;
  /** A profile run is in progress but does not include this repository. */
  note: string | null;
}

/** Human name of the run behind an IndexRun. */
export function runText(run: IndexRun): string {
  if (run.job_id === null) {
    return run.job_label === 'all'
      ? 'an index run of all profiles'
      : 'a run started outside the Web UI (CLI or scheduled reindex)';
  }
  return run.job_label === 'all' ? `job #${run.job_id} (all profiles)` : `job #${run.job_id}`;
}

/** The state shown in a repository row's Index status cell. */
export function repoIndexState(repo: RepoIndexFields): RepoIndexState {
  const run = repo.index_run ?? null;
  const lastIndexedAt = repo.last_indexed_at ?? null;
  if (run && run.covers_repo) {
    return {
      kind: 'indexing', label: 'Indexing', jobId: run.job_id, runText: runText(run),
      error: null, lastIndexedAt, note: null,
    };
  }
  const note = run
    ? `The profile is being indexed by ${runText(run)}, which started before this ` +
      'repository was added. Index again when it finishes.'
    : null;
  if (repo.status === 'error') {
    return {
      kind: 'failed', label: 'Failed', jobId: null, runText: null,
      error: repo.error_msg || 'No error text was recorded.', lastIndexedAt, note,
    };
  }
  if (lastIndexedAt) {
    return {
      kind: 'indexed', label: 'Indexed', jobId: null, runText: null, error: null,
      lastIndexedAt, note,
    };
  }
  return {
    kind: 'not_indexed', label: 'Not indexed', jobId: null, runText: null, error: null,
    lastIndexedAt: null, note,
  };
}

/** One row of GET /api/jobs. */
export interface JobRow {
  id: number;
  profile_name: string;
  status: string;
  pid?: number | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  error_msg: string | null;
}

/** What a job does, from its label (indexer_jobs.profile_name). */
export function jobWhat(label: string): string {
  if (label === 'all') return 'Index all profiles';
  if (label === 'patterns') return 'Seed patterns';
  if (label.startsWith('core:')) return `Index core ${label.slice('core:'.length)}`;
  return `Index profile ${label}`;
}

export function isJobLive(status: string | null | undefined): boolean {
  return status === 'queued' || status === 'running';
}

/** "1h 02m", "3m 05s", "12s" between two timestamps (now when `end` is null). */
export function jobDuration(
  start: string | null, end: string | null, now: Date = new Date(),
): string {
  if (!start) return '';
  const from = new Date(start).getTime();
  const to = end ? new Date(end).getTime() : now.getTime();
  if (Number.isNaN(from) || Number.isNaN(to) || to < from) return '';
  const total = Math.floor((to - from) / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, '0');
  if (h > 0) return `${h}h ${pad(m)}m`;
  if (m > 0) return `${m}m ${pad(s)}s`;
  return `${s}s`;
}

// "2026-11-23 11:40:01,120 INFO " - the text log format of src/logging_config.py.
const LOG_PREFIX = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ [A-Z]+ /;

/** The last non-empty line of a log tail, without its timestamp and level:
 * what a running job is doing now. */
export function lastLogLine(tail: string | null | undefined): string | null {
  if (!tail) return null;
  const lines = tail.split('\n').map((l) => l.trimEnd()).filter((l) => l.length > 0);
  return lines.length > 0 ? lines[lines.length - 1].replace(LOG_PREFIX, '') : null;
}

/** "YYYY-MM-DD HH:mm" in the viewer's time zone, or "-" when unknown. */
export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return '-';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '-';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
