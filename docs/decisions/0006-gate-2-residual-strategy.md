---
status: confirmed
scope: decisions/0006
date: 2026-04-23
confirmed_date: 2026-04-23
deciders: [SonCrits]
reads-with:
  - ../../../project-docs/odoo-semantic-mcp/tasks/phase-01-plan.md
  - ../../../project-docs/odoo-semantic-mcp/tasks/phase-02-plan.md
  - ../../../project-docs/odoo-semantic-mcp/roadmap.md
---

# ADR-0006: Gate 2 residual strategy — defer vs. ship Docker first

## Context

Phase 1 exit criteria pass với biên rộng — correctness, token-reduction, performance, multi-tenancy đều vượt target (numbers in `reports/phase-01-accept.md`, evidence map in `reports/phase-01-exit-criteria.md`). Hai hạng mục còn pending trong Gate 2 (Ship ready) theo global lifecycle:

1. **WP-10 Docker Compose dev topology** — deliverable gồm `docker-compose.yml` multi-service (db + app + indexer + optional Tailscale sidecar), `Dockerfile.server`, `Dockerfile.indexer`. Placeholder files đã ở repo và YAML validates, nhưng chưa smoke `docker compose up -d` trên clean host vì dev host không có Docker.
2. **Code/security review bundle** — `code-reviewer` + `security-reviewer` agents chạy clean trên diff P1 cuối.

Trong khi đó Phase 2 đã kick-off: WP-13 (fixture corpus) + WP-14 (XML parser) ship, WP-15 (DOM view inheritance resolver) spec `status: confirmed` (2026-04-22) và phase-02-plan sẵn sàng cho code.

Tình huống hiện tại = Gate 2 chưa chính thức pass mà P2 đã chạy. Đây không phải drift vô tình — WP-10 và WP-15 độc lập technical (parallel OK), nhưng process integrity yêu cầu explicit decision chứ không ngầm gọi là "đang cả hai song song".

## Drivers

- **Momentum P2** — WP-15 spec confirmed, phase-02-plan chi tiết; pause 1-2 tuần chờ WP-10 = cold start overhead khi resume. Context switching cost non-trivial trên dự án solo dev.
- **Process integrity** — lifecycle framework quy định không skip gate. Nếu ship P2 mà Gate 2 chưa đóng → precedent xấu cho các phase sau; audit tương lai khó re-trace evidence.
- **Host tooling reality** — dev host hiện tại (mq-laptop) không có Docker; osm-dev có WSL2 Ubuntu (Docker install được nhưng chưa). Provisioning 1 host Docker có khả thi nhưng tốn setup + WSL Docker Desktop license/config.
- **Evidence leverage** — accept suite P1 (numbers + exit-criteria map) đã đủ coverage cho correctness/perf; Docker compose deliverable chủ yếu cho operational deployment (customer self-host), không ảnh hưởng correctness của P1 tools.
- **Reviewer agent run** — `code-reviewer` + `security-reviewer` scheduled but not blocked bởi Docker. Có thể chạy độc lập trên diff hiện tại.

## Considered options

### Option A — Defer Gate 2 formal closure đến sau WP-17

Ship WP-15 → WP-16 → WP-17 trước. WP-10 Docker và review agent bundle đóng cùng với Phase 2 Gate 2 (hoặc Gate riêng). Dashboard hiện nguyên trạng "P1 Gate 2 chưa pass" để transparent.

- **Pros**:
  - Maintain momentum WP-15 (spec fresh, plan vừa mới review).
  - P2 artifacts + P2 accept suite bổ sung evidence cho Gate 2 (nhiều hơn = đóng sạch hơn).
  - Không đốt thời gian provision Docker khi chưa cần (customer-deployment chưa kick-off).
  - Cho phép `code-reviewer` + `security-reviewer` chạy 1 lần trên superset P1+P2 diff, ít redundant.
- **Cons**:
  - Gate debt visible 2-3 tuần — bất kỳ outside observer (partner, OSS contributor, future-self) thấy dashboard "P1 95% Gate 2 chưa pass" mà P2 đang chạy → phải check ADR để hiểu.
  - Risk compound: nếu WP-10 gặp issue (WSL Docker sidecar, pgvector image, Tailscale), có thể delay cả Gate 2 lẫn P2 deliverable khi consolidate.
  - Framework drift precedent — lần sau cũng dễ "defer tiếp".

### Option B — Ship WP-10 Docker trước, đóng Gate 2 sạch

Provision host có Docker (osm-dev install Docker, hoặc provision cloud VM, hoặc dev trên Windows/Mac Docker Desktop), ship WP-10 + run review agents, đóng Gate 2 chính thức. Sau đó mới vào WP-15.

- **Pros**:
  - Process clean — khi vào WP-15, P1 Gate 2 officially passed, dashboard không drift.
  - Flush out Docker/Tailscale issues sớm — nếu có bug config sẽ ảnh hưởng customer self-host (P5), tốt hơn fix trước khi cumulative debt.
  - Review agent runs trên P1 diff cô lập, dễ attribute findings.
- **Cons**:
  - Delay WP-15 tối thiểu 3-5 dev-day (provision + docker compose debug + review agent cycle). WSL Docker Desktop setup có thể kéo dài hơn.
  - WP-15 cold start khi resume — spec confirmed từ 2026-04-22 sẽ "nguội" sau pause, cần reload context.
  - Over-engineering rủi ro — Docker compose deliverable chủ yếu cho P5 customer distribution, không phải dependency của P2 tools. Ship Docker để "đóng gate" mà không có consumer thật = waste.

## Decision

**Chọn Option A — defer Gate 2 formal closure đến sau WP-17.**

Rationale (reference drivers):

1. **Evidence leverage driver thắng** — WP-10 Docker serve customer self-host (P5), không phải dependency của P2 tools (`resolve_view` chạy trên local Postgres bình thường, Docker chỉ đóng gói deployment). Ship WP-10 bây giờ = over-engineering không có consumer thực.
2. **Momentum P2 driver** — WP-15 spec `status: confirmed` (2026-04-22), phase-02-plan fresh. Pause 3-5 dev-day để provision Docker + debug WSL Docker Desktop → cold start khi resume tốn context reload hơn giá trị process clean gain.
3. **Reviewer agent driver** — `code-reviewer` + `security-reviewer` không dependency WP-10. Run 1 pass trên P1+P2 superset tiết kiệm cycle hơn 2 passes; findings cross-phase vẫn attribute được qua diff scope.
4. **Process integrity mitigation** — defer không phải skip. Dashboard link ADR này, kill criteria explicit (2026-05-07 hoặc post-WP-17 revisit, 2026-06-01 escalate). Transparency thay auto-close.

Trade-off chấp nhận: gate debt visible 2-3 tuần, requires dashboard reader check ADR để context. Trade-off này < cost của Option B (3-5+ dev-day delay + WSL Docker risk).

## Consequences

### Nếu chọn A

- **Positive**: WP-15 start ngay; P2 momentum giữ; 1 review pass cuối superset.
- **Negative**: dashboard phải có link tới ADR này để reader hiểu defer; cần explicit "Gate 2 closure target" = cuối P2 (WP-17 done).
- **Follow-ups**: revisit khi WP-17 xong hoặc +14 ngày từ hôm nay (2026-05-07), chọn sớm hơn.

### Nếu chọn B

- **Positive**: Gate 2 sạch; Docker/Tailscale validated; customer self-host viable sớm.
- **Negative**: WP-15 delay 3-5+ dev-day; provisioning overhead thực tế khó ước lượng trên WSL2 first-time.
- **Follow-ups**: post-WP-10 update phase-01-plan status `DONE`, Gate 2 mark pass.

## Kill criteria

- **Nếu chọn A**: revisit trigger = WP-15 hoàn thành HOẶC date 2026-05-07 (14 ngày). Nếu tại trigger WP-10 vẫn blocked trên host → ADR mới re-assess (có thể option C: provision cloud host).
- **Nếu chọn B**: abort trigger = Docker provisioning > 2 dev-day liên tục không up được clean, OR Tailscale sidecar config vượt ADR-0005 scope → switch sang Option A và ship Docker sau P2.
- **Chung**: nếu dashboard vẫn show "P1 Gate 2 chưa pass" sau 2026-06-01 (5 tuần kể từ ADR) → escalate, mời `ceo-advisor` + `release-manager` review process health.

## References

- ADR-0005: Tailscale sidecar tenancy model (driver cho Docker sidecar design)
- Phase 1 plan: `project-docs/odoo-semantic-mcp/tasks/phase-01-plan.md` §WP-10, §Exit criteria
- Phase 2 plan: `project-docs/odoo-semantic-mcp/tasks/phase-02-plan.md` §WP-15
- Accept evidence: `reports/phase-01-accept.md`, `reports/phase-01-exit-criteria.md`
- Global lifecycle gates: `~/.claude/CLAUDE.md` §Project Lifecycle Gates
