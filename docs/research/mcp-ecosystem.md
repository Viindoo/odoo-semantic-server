---
status: placeholder
scope: research/mcp-ecosystem
date: 2026-04-21
implications_for:
  - ../architecture/mcp-server.md
  - ../specs/
---

# MCP ecosystem survey

**Status**: placeholder. Fill before shipping P1 so tool shapes match what real clients consume.

## Goal

Validate that our tool interfaces are usable by all target clients (Claude Code, Codex, Cursor, Continue) without per-client shims.

## Questions

- What MCP transports does each client support (stdio only, HTTP, SSE)?
- Any client-specific quirks in tool schema (JSON Schema draft version, required vs nullable)?
- Response size ceilings per client? (We return arrays of chain entries — could blow up)
- Authentication patterns — do any clients pass user identity for audit log correlation?
- Error display — do clients surface MCP errors usefully, or do we need to embed `warnings` in `result`?

## Method

- Read each client's MCP docs
- Build a minimal `hello_world` MCP server and connect from each
- Note where responses render poorly, where errors are swallowed

## Output

- Matrix: client × transport × quirk × mitigation
- Recommendation on response envelope changes (if any) for `architecture/mcp-server.md`

## Implications

- Shape of response envelope in `architecture/mcp-server.md`
- Input/output schemas across `specs/*.md`
- Packaging decisions in P5 (do we ship per-client config stubs?)
