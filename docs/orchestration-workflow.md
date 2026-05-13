# Orchestrated Multi-Subagent Workflow

> Tách ra từ `CLAUDE.md` ngày 2026-05-13 (consolidation). Workflow này được dùng khi 1 milestone/wave có ≥4 work-items có dependencies. Cho task đơn lẻ, skip pattern này.

Khi 1 milestone/wave có ≥4 work-items (WIs) có dependencies, dùng pattern này thay vì làm tuần tự. Proven qua M5.5 closeout · M6 Wave 1 (8 WIs, depth-2 stack) · M6 Wave 2 (8 WIs, 3 chains, depth-5 deepest).

### Phase 1 — Investigation (parallel Sonnet)

Trước plan, spawn 1-4 Sonnet subagent song song (1 message với multiple `Agent` tool calls), mỗi cluster 1 agent. Prompt self-contained: file paths cụ thể + gap questions + output format `<600 words` + `DO NOT write code`. Findings inform plan.

### Phase 2 — Plan mode + AskUserQuestion

Trong plan mode: read findings, draft topology + WI list. Use `AskUserQuestion` cho scope decisions (in-scope / defer rules / worktree path) **trước** `ExitPlanMode`. Plan file structure: Context · Approach · Topology · Shared Context · per-WI specs · Integration Phase · Risk & Rollback · Decision Points.

### Phase 3 — Worktree topology

#### 3a. Two invariants

**Main repo isolation** — KHÔNG `git checkout`/`commit`/`rebase`/`cherry-pick` ở main repo. User có thể đang trên branch khác, working tree dirty; workflow tuyệt đối không đụng. Lệnh duy nhất chạy với main-repo cwd là initial `git worktree add` để mint trunk; sau đó mọi git op chạy qua trunk hoặc WI worktree.

**Session-scoped paths** — KHÔNG hardcode worktree dir cố định. Multi-session sẽ collision. Mint session tag (timestamp + random) → `$WAVE_DIR=/tmp/<project>-wt-<tag>/` riêng cho phiên hiện tại.

Layout sau setup:

```
$WAVE_DIR/
├── trunk/                       ← branch=master, never modified, used as base ref
├── m<N>-w<X>-<topic>/           ← per-WI worktree (1 commit, 1 branch)
├── m<N>-w<Y>-<topic>/
└── wave-<N>-integration/        ← cherry-pick consolidator
```

Trunk worktree đóng vai canonical "master" reference. Sau khi trunk tồn tại, mọi git op dispatch qua trunk; main repo path không cần được truy cập nữa cho phase implement.

Branch names clean (`feat/m<N>-w<X>-<topic>`). Branch collision detect tại push (`--force-with-lease`).

#### 3b. Topology patterns

Mỗi WI = 1 worktree under `$WAVE_DIR`, 1 branch, 1 commit. Diagrams show **commit graph** — `master` = ref pointed-to by trunk worktree.

**Pattern 1 — All independent (parallel off trunk):**

```
master ┬── WI1
       ├── WI2
       └── WI3
```

WIs độc lập (không share files, không share schema). Dispatch parallel trong 1 message với multiple `Agent` calls.

**Pattern 2 — Linear stack (each depends on prior):**

```
master ── WI1 ── WI2 ── WI3 ── WI4
```

Dùng khi WIs share files OR step depend trên previous output. Wave 2 Chain A = 5-deep linear (schema → field → scanner → writer → module → pipeline). Dispatch tuần tự (next WI sau khi parent landed).

**Pattern 3 — Mixed parallel + linear (Wave 2 actual):**

```
master ┬── WI1 ── WI2 ── WI3 ── WI4 ── WI5    (Chain A, depth-5)
       ├── WI6 ── WI7                          (Chain B, depth-2)
       └── WI8                                  (Chain C, depth-1)
```

Each chain dispatched independently. Inside chain: sequential.

**Pattern 4 — Diamond DAG (multi-parent dependency, "synthetic base"):**

```
master ┬── WI1 ────────────────────┐
       ├── WI2 ──────┐              │
       │             ▼              │
       │       feat/wi4-base ── WI4 ┬── WI5 ──────┐
       │       (= master + cherry-pick WI1+WI2)   │
       │                             │            ▼
       │                             └── WI6 ── feat/wi7-base ── WI7
       │                                          ▲   (= master + cherry-pick WI3+WI5+WI6)
       └── WI3 ───────────────────────────────────┘
```

Khi WI có ≥2 parent dependencies, orchestrator tạo synthetic `feat/<wi>-base` branch off master + cherry-pick TẤT CẢ deps lên đó, RỒI spawn WI worktree off base. Pattern generalize cho mọi DAG via topological sort.

WI4's commit = delta over `wi4-base` only (KHÔNG chứa WI1+WI2 changes — đã ở base). Khi cherry-pick WI4 lên integration đã có WI1+WI2 → no double-apply, no conflict.

Final integration: cherry-pick các WI commits theo **topological order** vào `wave-<N>-integration` worktree off master.

### Phase 4 — Subagent dispatch

**Sonnet** cho design judgment (logic mới, refactor, pipeline integration, multi-file). **Haiku** cho mechanical work (schema column add, single-file plumbing, pure data type plumbing).

Mỗi prompt MUST chứa: worktree path tuyệt đối + branch · stack history (parent commits + what they added, including synthetic base nếu có) · Goal + Files list with concrete edits · Hard rules block · Validation commands · Commit message format · Output format requirements.

Hard rules block (copy-paste vào mỗi prompt):

```
- DO NOT push (orchestrator integrates)
- DO NOT switch branches
- DO NOT touch files outside list
- DO NOT add `from __future__ import annotations` (Python 3.12 enforce, Wave 1 H4)
- Use `str | None` not `Optional[str]` (ruff UP)
- Single commit per WI
- Commit format: `[TYPE] scope: description (M<N> W<X>)` (bracket style, NOT conventional)
```

Spawn parallel agents trong 1 message khi WIs độc lập. Stack chains dispatch tuần tự (next WI sau khi parent landed). Synthetic-base WIs: orchestrator cherry-pick deps + verify clean state TRƯỚC khi dispatch agent.

### Phase 5 — Integration cherry-pick

Tạo `feat/m<N>-wave-<X>` worktree off master. Cherry-pick từng WI commit theo topological order. Conflict resolve manual với Edit tool — thường keep-both cho additive changes, merge logic cho branching.

Cross-impact integration fixes (vd Wave 2 W2-4 ↔ W2-8 mocks): commit mới trên integration branch — KHÔNG quay lại sửa WI worktrees.

### Phase 6 — Local validation BEFORE push

Run `make lint` + `make test` (unit) + `make test-integration` trên integration worktree. Local pgvector skip baseline ~25-30 tests (CI có pgvector → ít skip hơn). Browser skip qua conftest hook nếu chromium missing.

### Phase 7 — PR + CI monitor

Push integration branch → `gh pr create`. Body structure: `## Summary` (1-3 bullets) · per-WI bullets · `## Test plan` checklist.

Monitor `gh pr checks <N>`. Re-poll qua `ScheduleWakeup(270s, "<<autonomous-loop-dynamic>>")` — under prompt-cache TTL (300s).

CI fail: pull failed-job logs, fix as new commit trên integration branch, squash multi-round fixes via `--fixup` + `--autosquash`, force-push với `--force-with-lease`.

### Phase 8 — Plan adherence review (Opus)

Post-CI-green, spawn 1 Opus subagent review PR. 4 passes: plan adherence per WI · test coverage adequacy · latent bug hunt (read implementation) · documentation drift. Output severity-tagged findings (HIGH/MED/LOW) với file:line refs + fix sketches.

User principle **"Boil the lake"**: fix tất cả findings trong cùng PR (brief Sonnet for fixes), không defer Wave sau.

### Phase 9 — Merge + cleanup

`gh pr merge <N> --rebase --delete-branch`. Local error `"master is already used by worktree"` là harmless (trunk có master checked out) — verify server-side success qua `gh pr view --json state,mergedAt,mergeCommit`.

Cleanup: sync trunk via `pull --ff-only origin master` (KHÔNG ở main repo), remove WI + integration worktrees, remove trunk last, delete branches, xóa `$WAVE_DIR`.

### Concurrency hazards (multi-session)

- **Worktree path**: dùng session tag (Phase 3a). KHÔNG hardcode shared path cố định.
- **Branch namespace**: branch name conflict trên remote chỉ phát hiện tại push. 2 sessions cùng làm same Wave # → cần coordinate qua human channel hoặc đặt suffix.
- **`TEST_VERSION='99.0'`**: integration tests dùng "99.0" như carve-out — testcontainers spawn ephemeral containers per session (default behavior) nên KHÔNG share local Postgres/Neo4j giữa sessions là an toàn. Nếu phải dùng shared local DB → carve riêng `99.{session-tag-short}` hoặc serialize sessions.
- **Plan file**: Claude session có own plan file path — không collide.
- **`docker compose up -d` local**: nếu 2 sessions cùng start local stack → port collision (7687, 5432). Pattern: dùng testcontainers (per-session ephemeral) thay vì local docker compose cho phase implement.
- **gh CLI auth**: shared user-level token. Sequential GH operations atomic on remote.

### Hard-won lessons (chronological)

- **CI service container env explicit:** workflow YAML phải set DB passwords explicit — `.env.example` không readable parse-time bởi GitHub Actions.
- **PG DSN IPv4:** `127.0.0.1` không `localhost` (resolves to `::1` trên ubuntu-latest, port mapping IPv4-only → conn refused).
- **Playwright CI:** `playwright install --with-deps chromium`. Conftest hook skip cleanly nếu chromium missing locally (Wave 1 fix).
- **Test data leak:** một số fixture (vd `clean_pg`) không wipe pgvector tables — earlier tests có thể leak rows. Use pre/post count **delta** thay vì absolute `count == 0` (Wave 2 PR #39 round-3 lesson).
- **Linear stack > parallel branches off master** khi WIs share files. Wave 1 P3-off-P1, Wave 2 Chain A 5-deep linear.
- **Synthetic base for multi-parent**: WI depends on ≥2 prior WIs → `feat/<wi>-base` branch with cherry-picks before WI dispatch. Each WI commit remains delta-only.
- **`-rs` pytest flag** chỉ enable khi CI fail với silent skips — exposes root cause với cost noisy logs.

### When to skip this pattern

- 1-3 WI changes: fix trực tiếp trên 1 feature branch.
- Pure docs change: single commit + PR.
- Bug hotfix without investigation: same.

---

Quay về [`CLAUDE.md`](../CLAUDE.md) cho project rules tổng quát.
