---
name: domain
description: Domain documentation layout and consumer rules
---

# Domain Documentation

## Layout: single-context

This project uses a single-context layout.

- **Global Context**: `CONTEXT.md` at the repository root.
- **Architectural Decisions**: `docs/adr/` at the repository root.

## Consumer Rules

When operating in this repository:
1. Always read `CONTEXT.md` first to understand the high-level domain and current goals.
2. Check `docs/adr/` for the rationale behind architectural choices before proposing significant changes.
3. Update `CONTEXT.md` when a major milestone is reached or the project's direction shifts.
