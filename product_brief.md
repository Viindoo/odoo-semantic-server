
# Odoo Semantic MCP - Product & Architecture Brief

## Product Vision

Build a **local-first Odoo Semantic Knowledge Engine** that enables AI coding tools to:

- Understand `_inherit`, `_inherits`, and `depends`
- Resolve final merged XML views (`inherit_id + xpath`)
- Track override chains
- Retrieve correct examples
- Reduce prompt size and hallucination
- Run locally via MCP with minimal setup

---

# High-Level Architecture

```text
Odoo Repositories
(Python, XML, JS, manifest)
        |
        v
+-------------------------+
|     Indexer Pipeline    |
|-------------------------|
| Python Parser           |
| XML View Parser         |
| Manifest Parser         |
| JS Parser               |
| Odoo Resolver Engine    |
+-------------------------+
        |
        +-----------------------------+
        |                             |
        v                             v

+-------------------+        +-------------------+
|   Graph Database  |        |    Vector Store   |
|-------------------|        |-------------------|
| modules           |        | method embeddings |
| models            |        | view embeddings   |
| fields            |        | snippet examples  |
| methods           |        | tests             |
| views             |        +-------------------+
+-------------------+
        |
        v

+-------------------------+
|      MCP Server         |
|-------------------------|
| resolve_model           |
| resolve_field           |
| resolve_method          |
| resolve_view            |
| find_examples           |
| impact_analysis         |
+-------------------------+
        |
        v

Claude Code / Codex / Continue / Cursor
(Local AI Coding Clients)

```

---

# Minimal Data Schema

## Core Tables

### modules
- id
- name
- manifest_path
- dependencies

### models
- id
- name
- module_id
- inherits_model
- delegates_model

### fields
- id
- model_id
- field_name
- field_type
- related_model

### methods
- id
- model_id
- method_name
- override_of

### views
- id
- xmlid
- model
- inherit_id
- xpath_targets

---

# MCP Tool Interface (Core)

## resolve_model(model_name)

Returns:

- inheritance chain
- delegated models
- defining module
- field summary

---

## resolve_field(model_name, field_name)

Returns:

- original field
- extension chain
- related/computed metadata

---

## resolve_method(model_name, method_name)

Returns:

- override chain
- module priority
- super chain

---

## resolve_view(xmlid)

Returns:

- inheritance chain
- xpath modifications
- final merged XML

---

## find_examples(query)

Returns:

- ranked Odoo code examples

---

## impact_analysis(entity)

Returns:

- affected models
- dependent modules
- impacted views

---

# Phase Roadmap

## Phase 1 - Python Model Graph

### Goal
Understand model inheritance.

### Scope

- Parse Python models
- Index `_inherit`
- Index `_inherits`
- Track method overrides

### Deliverables

- Graph DB
- resolve_model
- resolve_field
- resolve_method

### Usable Outcome

AI understands model structure and override logic.

---

## Phase 2 - XML View Resolver

### Goal
Resolve final view architecture.

### Scope

- Parse XML views
- Index `inherit_id`
- Process `xpath`
- Build final view resolver

### Deliverables

- resolve_view
- merged view cache

### Usable Outcome

AI understands real UI structure.

---

## Phase 3 - Hybrid Retrieval Engine

### Goal
Add semantic example search.

### Scope

- Build vector embeddings
- Hybrid graph + vector retrieval
- Reranking logic

### Deliverables

- semantic_search
- find_examples

### Usable Outcome

AI generates code with correct examples.

---

## Phase 4 - Full Stack Resolution

### Goal
Cover frontend and templates.

### Scope

- QWeb parsing
- JS patch detection
- Test indexing
- Deep impact analysis

### Deliverables

- JS/QWeb resolver
- test-aware analysis

### Usable Outcome

AI understands entire Odoo stack.

---

## Phase 5 - Public Distribution

### Goal
Ship global-ready tooling.

### Scope

- Dockerized installer
- CLI indexer
- MCP auto-start
- Version presets (Odoo 16-19)

### Deliverables

- Open distribution
- Documentation
- Community adoption

### Usable Outcome

Any developer can plug AI into Odoo knowledge instantly.

---

# Suggested Technology Stack

## Parsers

- Python AST
- libcst
- lxml
- tree-sitter
- ast-grep

## Storage

- PostgreSQL
- pgvector

## MCP

- Python FastAPI
- Async cache layer

## Vector

- Qdrant
OR
- pgvector

---

# Developer Workflow

```text
Clone Odoo Repo
        |
Run Indexer
        |
Start MCP Server
        |
Connect AI Client
        |
Start Coding
(with semantic awareness)
```

---

# Core Architectural Principle

**Graph ensures correctness**  
**Vector ensures speed**  
**Cache ensures cost efficiency**

This combination minimizes hallucination and token usage while maximizing precision.
