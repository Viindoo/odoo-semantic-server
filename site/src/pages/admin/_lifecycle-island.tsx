// SPDX-License-Identifier: AGPL-3.0-or-later
// Module lifecycle React island - admin-only, read-only (#380, ADR-0056 / ADR-0057).
// Shows what the index run decided from git evidence and what the next run does.
// There is deliberately no approve / retire control: nothing here needs approval.
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { submitJson } from '../../lib/apiClient';
import {
  REPO_HEALTH_TEXT,
  ageText,
  auditView,
  driveAudit,
  healthCounts,
  repoHealth,
  retireReasonText,
  shortSha,
  successorText,
  type AuditSnapshot,
  type AuditView,
  type LifecycleOverview,
  type LifecycleRepo,
  type LifecycleVersion,
  type RepoHealth,
} from '../../lib/lifecycle';

interface Props {
  initial: LifecycleOverview;
}

const POLL_MS = 4000;

const HEALTH_BADGE: Record<RepoHealth, string> = {
  unavailable: 'bg-red-100 text-red-700',
  attention: 'bg-amber-100 text-amber-800',
  lagging: 'bg-amber-100 text-amber-800',
  never_indexed: 'bg-gray-100 text-gray-600',
  ok: 'bg-green-100 text-green-700',
};

function formatTime(iso: string | null | undefined): string {
  if (!iso) return '-';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

async function fetchAudit(version: string, start: boolean, force = false) {
  const url = `/api/admin/lifecycle/audit/${encodeURIComponent(version)}${force ? '?force=true' : ''}`;
  const r = await submitJson<AuditSnapshot>(url, { method: start ? 'POST' : 'GET', stepUp: false });
  // 409 (another version runs) still carries the snapshot.
  if (r.ok || r.status === 409) return { snap: r.data, error: null as string | null };
  return { snap: null, error: r.error ?? `HTTP ${r.status}` };
}

function Explain({ children }: { children: ReactNode }) {
  return <p className="text-xs text-gray-500 mt-1 mb-2">{children}</p>;
}

function Section({ title, count, children }: { title: string; count?: number; children: ReactNode }) {
  return (
    <div className="border-t border-gray-100 pt-3 mt-3">
      <h4 className="text-sm font-semibold text-gray-800">
        {title}
        {count !== undefined && (
          <span className="ml-2 text-xs font-medium text-gray-500">({count})</span>
        )}
      </h4>
      {children}
    </div>
  );
}

function RepoCard({ repo }: { repo: LifecycleRepo }) {
  const health = repoHealth(repo);
  const c = repo.lifecycle_counts;
  return (
    <div data-testid={`lifecycle-repo-${repo.id}`} className="bg-white rounded-xl border border-gray-200 p-4 min-w-0">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="font-medium text-gray-900 break-all">{repo.basename ?? `repo ${repo.id}`}</div>
          <div className="text-xs text-gray-500 break-all">
            {repo.profile} - {repo.odoo_version} - branch {repo.branch}
          </div>
        </div>
        <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${HEALTH_BADGE[health]}`}>
          {REPO_HEALTH_TEXT[health]}
        </span>
      </div>
      <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs mt-3">
        <dt className="text-gray-500">Indexed HEAD</dt>
        <dd className="font-mono text-gray-800">{shortSha(repo.head_sha)}</dd>
        <dt className="text-gray-500">Ledger HEAD</dt>
        <dd className="font-mono text-gray-800">
          {shortSha(repo.presence_head_sha)}
          {repo.head_sha && !repo.synced && <span className="ml-1 text-amber-700 font-sans">(behind)</span>}
        </dd>
        <dt className="text-gray-500">Clone</dt>
        <dd className="text-gray-800">{repo.clone_status}{repo.checkout_on_disk ? '' : ', no checkout on disk'}</dd>
        <dt className="text-gray-500">Modules</dt>
        <dd className="text-gray-800">
          {c ? `${c.present} present, ${c.excluded} excluded, ${c.retired} retired` : 'ledger unreadable'}
          {c && c.retire_pending > 0 ? `, ${c.retire_pending} pending` : ''}
        </dd>
      </dl>
      {repo.clone_error_msg && (
        <p className="mt-2 text-xs text-red-700 break-words">Clone error: {repo.clone_error_msg}</p>
      )}
      {(repo.lifecycle_attention || repo.lifecycle_attention_at) && (
        <div className="mt-2 text-xs bg-amber-50 border border-amber-200 text-amber-900 rounded-lg px-3 py-2 break-words">
          <span className="font-semibold">Last run said</span>
          {repo.lifecycle_attention_at && <span> ({formatTime(repo.lifecycle_attention_at)})</span>}:{' '}
          {repo.lifecycle_attention ?? 'attention recorded'}
        </div>
      )}
    </div>
  );
}

function Retirements({ version }: { version: LifecycleVersion }) {
  if (version.recent_retirements.length === 0) {
    return (
      <Explain>
        No module has been retired at {version.odoo_version}. When a module's manifest is deleted in
        git, the index run retires it by itself and it is listed here with the removing commit.
      </Explain>
    );
  }
  return (
    <>
      <Explain>
        History only: the index run retired these because git shows them removed. Showing the latest{' '}
        {version.recent_retirements.length} of {version.retired_total}.
      </Explain>
      <ul className="divide-y divide-gray-100 text-xs">
        {version.recent_retirements.map((r) => {
          const succ = successorText(r.successor_names, r.successor_source);
          return (
            <li key={`${r.id ?? r.name}-${r.repo_basename}`} className="py-2 min-w-0">
              <div className="flex flex-wrap gap-x-2">
                <span className="font-mono font-medium text-gray-900">{r.name}</span>
                <span className="text-gray-500">{r.repo_basename ?? 'deleted repo'} ({r.profile_name})</span>
                <span className="text-gray-400">{formatTime(r.state_changed_at)}</span>
              </div>
              <div className="text-gray-600 break-words">
                {retireReasonText(r.retire_reason)}
                {r.removing_commit_sha
                  ? ` - removed by ${shortSha(r.removing_commit_sha)}${r.removing_commit_subject ? ` "${r.removing_commit_subject}"` : ''}`
                  : ''}
              </div>
              {succ && <div className="text-blue-700 font-medium">{succ}</div>}
            </li>
          );
        })}
      </ul>
    </>
  );
}

function List({ items }: { items: string[] }) {
  return (
    <ul className="text-xs text-gray-700 list-disc pl-5 space-y-0.5 break-words">
      {items.map((t, i) => <li key={i}>{t}</li>)}
    </ul>
  );
}

function AuditFindings({ view }: { view: AuditView }) {
  const r = view.nextRunRemovals;
  const removalCount = r.retire.length + r.orphanModules.length + r.childOrphans.length
    + r.embeddingGroups + r.sharedPrunes.length;
  return (
    <div>
      <Section title="Parser gaps" count={view.parserGaps.length}>
        {view.parserGaps.length === 0 ? (
          <Explain>None. Everything the source in git still defines is produced by the parser.</Explain>
        ) : (
          <>
            <Explain>
              The source in git still defines these, but the parser no longer produces them, so the
              index run kept them instead of deleting live code (ADR-0057). This is a parser defect to
              fix in OSM, not something to approve; they are re-checked automatically when the module
              or the indexer code changes.
            </Explain>
            <List items={view.parserGaps.map((g) =>
              `${g.name} in ${g.repo}: kept ${g.kept} of ${g.total} node(s), ${g.rels_kept} of ${g.rels_total} relationship(s)`
              + (g.reason ? ` (source unavailable: ${g.reason})` : '')
              + (g.sample.length ? ` - e.g. ${g.sample.slice(0, 3).join('; ')}` : ''))} />
          </>
        )}
      </Section>

      <Section title="Unproven drops" count={view.unprovenDrops.reduce((n, d) => n + d.names.length, 0)}>
        {view.unprovenDrops.length === 0 ? (
          <Explain>None. Every module the scan stopped indexing is really gone from git.</Explain>
        ) : (
          <>
            <Explain>
              The scan no longer indexes these, but their manifest is still in the HEAD tree, so the
              run keeps them (a scan gap, never a deletion). Nothing to approve.
            </Explain>
            <List items={view.unprovenDrops.map((d) => `${d.repo}: ${d.names.join(', ')}`)} />
          </>
        )}
        {view.unparseableKept.length > 0 && (
          <>
            <Explain>Kept because their manifest does not parse (fix the manifest upstream):</Explain>
            <List items={view.unparseableKept.map((d) => `${d.repo}: ${d.names.join(', ')}`)} />
          </>
        )}
      </Section>

      <Section
        title="Kept orphans"
        count={view.keptOrphans.length + view.childOrphansKept.length + view.embeddingOrphansKept.length}
      >
        {view.keptOrphans.length + view.childOrphansKept.length + view.embeddingOrphansKept.length === 0 ? (
          <Explain>None. No leftover index entry is waiting for evidence.</Explain>
        ) : (
          <>
            <Explain>
              Index entries no repository indexes any more, which git cannot prove gone yet (a checkout
              of the owning profile still ships the module, or a repository is not synced). They stay
              until the evidence settles; nothing to approve.
            </Explain>
            <List items={[
              ...view.keptOrphans.map((o) => `module ${o.name}: ${o.why}`),
              ...view.childOrphansKept.map((o) => `children of ${o.name}: ${o.why}`),
              ...view.embeddingOrphansKept.map((e) => `embeddings of ${e.module} / ${e.profile} (${e.rows} rows): a checkout of the profile still ships the module`),
            ]} />
          </>
        )}
      </Section>

      <Section title="Shared-module bootstrap backlog" count={view.backlog.reduce((n, b) => n + b.remaining, 0)}>
        {view.backlog.length === 0 ? (
          <Explain>Drained. Every module two or more repositories ship has a complete parse on record.</Explain>
        ) : (
          <>
            <Explain>
              Modules two or more repositories ship that still need one complete parse each before
              their shared prune can be judged. Not a problem: it drains by itself, a batch per
              repository per run.
            </Explain>
            <List items={view.backlog.map((b) => `${b.repo}: ${b.remaining} remaining, ${b.nextRun} in the next run`)} />
          </>
        )}
      </Section>

      <Section title="Removed by the next index run" count={removalCount}>
        {removalCount === 0 ? (
          <Explain>Nothing. The index matches git.</Explain>
        ) : (
          <>
            <Explain>Git proves these gone; the next index run removes them by itself.</Explain>
            <List items={[
              ...r.retire.map((x) => `retire ${x.name} (${x.repo}): ${x.detail}`),
              ...r.orphanModules.map((n) => `orphan module ${n}`),
              ...r.childOrphans.map((n) => `module-less children of ${n}`),
              ...(r.embeddingGroups ? [`${r.embeddingGroups} orphan embedding group(s)`] : []),
              ...r.sharedPrunes.map((s) => `shared module ${s.name}: ${s.outcome === 'partial' ? 'part of it (the rest is still defined in some copy)' : 'stale entities'}`),
            ]} />
          </>
        )}
      </Section>

      {(view.repoNotes.length > 0 || view.unsyncedRepos.length > 0) && (
        <Section title="Next run, per repository">
          <List items={[
            ...view.repoNotes.map((n) => `${n.repo}: next run ${n.nextRun ?? 'unknown'}${n.notes.length ? ` - ${n.notes.join(' / ')}` : ''}`),
            ...view.unsyncedRepos.map((u) => `${u.repo} stays unsynced: ${u.why}`),
          ]} />
        </Section>
      )}

      {view.errors.length > 0 && (
        <Section title="Audit errors" count={view.errors.length}>
          <List items={view.errors} />
        </Section>
      )}
    </div>
  );
}

function VersionPanel({ version, repos }: { version: LifecycleVersion; repos: LifecycleRepo[] }) {
  const [snap, setSnap] = useState<AuditSnapshot | null>(version.audit);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const runToken = useRef(0);

  useEffect(() => () => { runToken.current += 1; }, []);

  async function ensure(force: boolean) {
    const token = ++runToken.current;
    setLoading(true);
    setError(null);
    const failure = await driveAudit({
      force,
      fetch: (start, f) => fetchAudit(version.odoo_version, start, f),
      sleep: () => new Promise((r) => setTimeout(r, POLL_MS)),
      cancelled: () => token !== runToken.current,
      onSnapshot: setSnap,
    });
    if (token === runToken.current) {
      setError(failure);
      setLoading(false);
    }
  }

  const view = snap?.report ? auditView(snap.report, version.odoo_version) : null;
  const versionRepos = repos.filter((r) => version.repo_ids.includes(r.id));

  return (
    <details
      data-testid={`lifecycle-version-${version.odoo_version}`}
      className="bg-white rounded-xl border border-gray-200 p-4"
      onToggle={(e) => {
        if ((e.currentTarget as HTMLDetailsElement).open && !loading && !view) void ensure(false);
      }}
    >
      <summary className="cursor-pointer select-none flex flex-wrap items-center gap-2">
        <span className="text-base font-semibold text-gray-900">Odoo {version.odoo_version}</span>
        <span className="text-xs text-gray-500">
          {versionRepos.length} repo(s), {version.retired_total} retired module(s)
        </span>
      </summary>

      <Section title="Recent retirements" count={version.retired_total}>
        <Retirements version={version} />
      </Section>

      <div className="border-t border-gray-100 pt-3 mt-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h4 className="text-sm font-semibold text-gray-800">What the next index run finds (dry run)</h4>
          <button
            type="button"
            onClick={() => void ensure(true)}
            disabled={loading}
            className="px-3 py-1.5 text-xs rounded-lg border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50"
          >
            {loading ? 'Running...' : 'Run again'}
          </button>
        </div>
        <Explain>
          Computed by the same dry-run audit as the weekly check (lifecycle-audit, writes nothing).
          {snap?.finished_at ? ` Last computed ${ageText(snap.age_seconds) ?? formatTime(snap.finished_at)}.` : ''}
          {snap?.running_version && snap.running_version !== version.odoo_version
            ? ` Waiting for the audit of ${snap.running_version} to finish (one at a time).` : ''}
          {snap?.state === 'running' ? ' Running now; this can take a few minutes.' : ''}
        </Explain>
        {(error || snap?.error) && (
          <div className="mb-2 text-xs bg-red-50 border border-red-200 text-red-700 rounded-lg px-3 py-2 break-words">
            {error ?? snap?.error}
          </div>
        )}
        {view ? <AuditFindings view={view} /> : (
          !loading && <Explain>Open this panel or press "Run again" to compute it.</Explain>
        )}
      </div>
    </details>
  );
}

export default function LifecycleIsland({ initial }: Props) {
  const [onlyAttention, setOnlyAttention] = useState(false);
  const repos = initial.repos;
  const counts = healthCounts(repos);
  const shown = onlyAttention ? repos.filter((r) => repoHealth(r) !== 'ok') : repos;

  return (
    <div className="space-y-6">
      <div className="bg-blue-50 border border-blue-200 text-blue-900 rounded-xl px-4 py-3 text-sm">
        OSM keeps its index equal to the code in git. Every retirement and prune on this page was
        decided by the index run from git evidence; nothing waits for approval (ADR-0057). This page
        only shows what happened and what the next run will do.
      </div>

      <div className="flex flex-wrap gap-2 text-xs" data-testid="lifecycle-summary">
        <span className="px-3 py-1 rounded-full bg-gray-100 text-gray-700">{repos.length} repositories</span>
        <span className={`px-3 py-1 rounded-full ${HEALTH_BADGE.ok}`}>{counts.ok} in sync</span>
        <span className={`px-3 py-1 rounded-full ${HEALTH_BADGE.attention}`}>{counts.attention + counts.lagging} need a look</span>
        <span className={`px-3 py-1 rounded-full ${HEALTH_BADGE.unavailable}`}>{counts.unavailable} without a checkout</span>
        {counts.never_indexed > 0 && (
          <span className={`px-3 py-1 rounded-full ${HEALTH_BADGE.never_indexed}`}>{counts.never_indexed} not indexed yet</span>
        )}
      </div>

      <section>
        <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
          <h2 className="text-lg font-semibold text-gray-900">Repositories</h2>
          <label className="flex items-center gap-2 text-sm text-gray-600">
            <input
              type="checkbox"
              checked={onlyAttention}
              onChange={(e) => setOnlyAttention(e.target.checked)}
            />
            Only repositories that need a look
          </label>
        </div>
        {shown.length === 0 ? (
          <p className="text-sm text-gray-500">
            {repos.length === 0 ? 'No repository is registered.' : 'Every repository is in sync with git.'}
          </p>
        ) : (
          <div className="grid gap-3 grid-cols-1 lg:grid-cols-2">
            {shown.map((r) => <RepoCard key={r.id} repo={r} />)}
          </div>
        )}
      </section>

      <section>
        <h2 className="text-lg font-semibold text-gray-900 mb-1">Versions</h2>
        <p className="text-sm text-gray-500 mb-3">
          Open a version to see its recent retirements and what the next index run finds there.
        </p>
        {initial.retirements_error && (
          <div className="mb-3 text-xs bg-red-50 border border-red-200 text-red-700 rounded-lg px-3 py-2">
            {initial.retirements_error}
          </div>
        )}
        <div className="space-y-3">
          {initial.versions.map((v) => (
            <VersionPanel key={v.odoo_version} version={v} repos={repos} />
          ))}
        </div>
      </section>
    </div>
  );
}
