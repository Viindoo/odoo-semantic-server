// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Tests for the indexing-state and job view logic (#403).
 *
 * Business rules under protection:
 *   - A repository shows "Indexing" only while a run that covers it is in
 *     progress, naming that run (job number, all-profiles job, or a run started
 *     outside the Web UI).
 *   - Otherwise the repository's own row decides: a failed index shows its
 *     error, a successful one its time, a never-indexed one says so; a finished
 *     job of the profile never decides it.
 *   - A run that does not cover the repository (added after the run started)
 *     is a note, not "Indexing".
 *   - A running job's current step is the last log line.
 */
import { describe, expect, it } from 'vitest';

import {
  isJobLive,
  jobDuration,
  jobWhat,
  lastLogLine,
  repoIndexState,
  runText,
  type IndexRun,
} from '../jobs';

const run = (over: Partial<IndexRun> = {}): IndexRun => ({
  job_id: 12, job_label: 'odoo_17', started_at: '2026-11-23 10:00:00+00:00',
  covers_repo: true, ...over,
});

describe('repoIndexState', () => {
  it('shows a covering run as Indexing with its job', () => {
    const s = repoIndexState({ status: 'indexed', last_indexed_at: '2026-11-22', index_run: run() });
    expect(s.kind).toBe('indexing');
    expect(s.jobId).toBe(12);
    expect(s.runText).toBe('job #12');
  });

  it('names a run started outside the Web UI', () => {
    const s = repoIndexState({ index_run: run({ job_id: null, job_label: null }) });
    expect(s.kind).toBe('indexing');
    expect(s.jobId).toBeNull();
    expect(s.runText).toContain('outside the Web UI');
  });

  it('shows a failed index with its error, whatever the last success', () => {
    const s = repoIndexState({
      status: 'error', error_msg: 'fatal: could not read', last_indexed_at: '2026-11-01',
    });
    expect(s.kind).toBe('failed');
    expect(s.error).toBe('fatal: could not read');
    expect(s.lastIndexedAt).toBe('2026-11-01');
  });

  it('shows an indexed repository with its time and a never-indexed one as such', () => {
    expect(repoIndexState({ status: 'indexed', last_indexed_at: '2026-11-22' }).kind).toBe('indexed');
    expect(repoIndexState({ status: 'pending', last_indexed_at: null }).kind).toBe('not_indexed');
  });

  it('turns a run that started before the repository was added into a note', () => {
    const s = repoIndexState({ status: 'pending', index_run: run({ covers_repo: false }) });
    expect(s.kind).toBe('not_indexed');
    expect(s.note).toContain('job #12');
    expect(s.note).toContain('Index again when it finishes');
  });
});

describe('runText', () => {
  it('distinguishes profile, all-profiles and hidden all-profiles jobs', () => {
    expect(runText(run({ job_label: 'all' }))).toBe('job #12 (all profiles)');
    expect(runText(run({ job_id: null, job_label: 'all' }))).toBe('an index run of all profiles');
  });
});

describe('job helpers', () => {
  it('says what a job does from its label', () => {
    expect(jobWhat('odoo_17')).toBe('Index profile odoo_17');
    expect(jobWhat('all')).toBe('Index all profiles');
    expect(jobWhat('core:17.0')).toBe('Index core 17.0');
    expect(jobWhat('patterns')).toBe('Seed patterns');
  });

  it('treats only queued and running jobs as live', () => {
    expect(isJobLive('queued')).toBe(true);
    expect(isJobLive('running')).toBe(true);
    expect(isJobLive('done')).toBe(false);
    expect(isJobLive('error')).toBe(false);
  });

  it('formats durations and ignores missing starts', () => {
    expect(jobDuration('2026-11-23T10:00:00Z', '2026-11-23T10:00:12Z')).toBe('12s');
    expect(jobDuration('2026-11-23T10:00:00Z', '2026-11-23T10:03:05Z')).toBe('3m 05s');
    expect(jobDuration('2026-11-23T10:00:00Z', '2026-11-23T11:02:00Z')).toBe('1h 02m');
    expect(jobDuration(null, null)).toBe('');
  });

  it('takes the last non-empty log line as the current step', () => {
    expect(lastLogLine('a\nb step 2\n\n')).toBe('b step 2');
    expect(lastLogLine('2026-11-23 11:41:10,620 INFO Repo x (id=1): indexing started\n'))
      .toBe('Repo x (id=1): indexing started');
    expect(lastLogLine('')).toBeNull();
    expect(lastLogLine(null)).toBeNull();
  });
});
