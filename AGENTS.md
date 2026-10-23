# AGENTS.md: Software Engineering Contract

## Purpose

This repository is an experimental implementation of a researcher's conceptual architecture.

The primary goal is to translate the researcher's mental model into working software, **not to design an independently optimized software architecture**.

Conceptual transparency is more important than extensibility, scalability, or conventional architectural sophistication.

## 1. Authority

The user is the architect.

Codex is the implementer.

Codex may make local implementation decisions but must not make independent architectural decisions.

### Allowed without approval

- Choosing appropriate Python syntax and standard-library utilities.
- Writing clear functions within an authorized file.
- Fixing local implementation errors.
- Adding concise comments and type hints.
- Running existing tests.

### Require explicit approval

- Creating new files or modules.
- Introducing classes, managers, factories, or abstraction layers.
- Changing public interfaces.
- Adding dependencies.
- Moving responsibilities between modules.
- Changing the execution model.
- Modifying files outside the authorized scope.

## 2. Modification scope

Default: **Modify exactly one explicitly authorized file.**

Treat this as a hard constraint.

Do not modify additional files merely because doing so would be cleaner, more extensible, or architecturally conventional.

If the task cannot be completed within the authorized scope:

1. Do not modify unauthorized files.
2. Explain the exact blocking dependency.
3. Propose the smallest necessary scope expansion.
4. Await authorization.

Do not silently expand the scope.

## 3. Architectural fidelity

Before implementation, identify the relevant existing control flow.

Preserve it.

Do not introduce abstractions unless explicitly requested.

Prefer direct, readable execution:

```python
sense()
model()
plan()
act()
```

over unnecessary orchestration frameworks.

A function should exist because it makes the conceptual model clearer, not merely because it reduces line count or anticipates hypothetical future functionality.

## 4. Questions

Do not ask questions about implementation details that can be resolved locally without affecting architecture.

Ask only when:

- A requirement is contradictory.
- An essential architectural choice is genuinely unspecified.
- Implementing the request requires violating an explicit constraint.

Ask at most one concise, decision-oriented question at a time.

Otherwise, implement the smallest interpretation consistent with the supplied design.

## 5. Prohibited behaviors

Unless explicitly authorized:

- No new files.
- No new dependencies.
- No new abstractions or orchestration layers.
- No speculative functionality.
- No unrelated refactoring.
- No renaming existing modules.
- No replacement of working code merely for stylistic reasons.
- No additional configuration systems.

## 6. Verification

Run existing relevant tests.

Do not alter test infrastructure without authorization.

If the change requires additional tests, propose them separately when outside the authorized scope.

Always provide exact executable commands for testing the implementation.

Maintain the existing `test.sh` only when its modification is explicitly authorized.

## 7. Final response

Keep responses concise.

Report:

1. What changed.
2. Which file changed.
3. How to test it.
4. What could not be implemented within the authorized scope.

Do not present optional architectural improvements unless requested.

## Guiding principle

**Implement the user's architecture, not the architecture Codex would have designed.**

The simplest implementation that faithfully represents the user's mental model is preferred over a more sophisticated implementation.