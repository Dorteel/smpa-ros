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


# AGENTS.md: Narrative-First Literate Programming

## Guiding principle

**Write the narrative before writing the implementation.** Every function must
begin with a docstring that explains, in simple and deliberately explicit
language, what the function is supposed to do. The code that follows must
implement that narrative faithfully.

Optimize for human understanding rather than brevity. A reader should be able
to understand the intended behavior from the narrative without first reading
the implementation. Apply these rules to all new or modified code in this
repository, while respecting the language's conventions and existing contracts.

## Mandatory workflow for every function

1. **Define the purpose.** Before implementing or changing the function, write
   its docstring. State what the operation accomplishes and why it exists.
2. **Identify the participants.** Explain what each important input represents,
   what relevant state the function reads or changes, and what the result means.
3. **Tell the story in logical order.** Describe the operation step by step in
   straightforward sentences. Name important decisions, conditions, branches,
   and transformations in the order they occur.
4. **Describe outcomes.** Explain what is returned or changed, what happens
   when a condition is not met, and how errors or cleanup are handled when
   relevant. State important assumptions and invariants.
5. **Implement the narrative.** Only after writing the explanation, write or
   modify the code. Make meaningful operations correspond clearly to the
   described steps. If the implementation reveals a missing case, revise the
   narrative and then the code until both agree.
6. **Review the correspondence.** Read the docstring and code side by side.
   Ensure that every important narrated action occurs in the implementation,
   and that every important implementation decision is explained.

**This applies to every function**, including constructors, callbacks, tests,
helpers, and short methods. Even when behavior is simple, give a genuine
narrative rather than omitting the docstring or writing a generic one-liner.
Do not add documentation that is false merely to make it longer.

## Style of the narrative

- **Use plain language.** Prefer simple, complete sentences over jargon,
  compressed bullet fragments, or implementation shorthand.
- **Be intentionally descriptive.** It is acceptable for a docstring to feel
  unusually detailed if that helps a new reader form an accurate mental model.
- **Explain meaning, not syntax.** Explain why a condition matters and what
  changes conceptually; do not merely paraphrase `if`, `for`, or assignments.
- **Use a consistent vocabulary.** The terms in the docstring should match
  the concepts expressed by variable, function, and class names.
- **Separate intent from mechanism.** Describe the conceptual operation first;
  mention implementation-specific constraints only when they matter to
  understanding or correctness.
- **Make uncertainty explicit.** If the purpose or behavior cannot be inferred
  reliably from the code and context, do not invent an explanation. State the
  uncertainty in the handoff and make only justified changes.

## Semantic naming and concept definitions

- Name entities for what they represent, operations for what they accomplish,
  and states for the conditions they describe.
- Prefer precise domain names over vague placeholders such as `data`, `obj`,
  or `handler` when a clearer concept is known. Do not lengthen names without
  adding meaning.
- Maintain consistent terminology across the narrative, code, and tests.
- When a module introduces non-obvious domain concepts, include a `Concepts`
  glossary in the module docstring defining the terms once. For smaller scopes,
  define important concepts directly in each function's narrative.

## Example

```python
def calculate_total_price(unit_price: float, item_count: int) -> float:
    """Calculate the total price of a number of identical items.

    The unit price is the price of one item. The item count is the number
    of items being purchased.

    First, multiply the price of one item by the number of items. This
    produces the total price for all items, before any taxes or discounts.

    Return that total price to the caller. The operation does not change
    either input.
    """
    total_price = unit_price * item_count
    return total_price
```

## Preserve software contracts

- For naming or documentation tasks, make the smallest behavior-preserving
  changes possible. Do not modify algorithms or control flow simply to make
  them fit the narrative.
- Preserve public interfaces, ROS 2 topic and service names, message fields,
  launch parameters, serialized formats, and externally used APIs unless the
  user explicitly authorizes migration.
- When renaming an internal symbol, update its references and relevant tests.
  Do not rename third-party or generated fields.
- Keep narrative-first documentation compatible with idiomatic Python and
  framework requirements, including required callback signatures.

## Verification and handoff

- Verify both **behavior** and **narrative accuracy**. Add or update focused
  tests where behavior changes. For documentation-only or rename-only edits,
  verify that references and interfaces remain valid.
- Maintain a repository-root `test.sh` when relevant. For ROS 2 projects, it
  should build with `colcon`, source the environment, run relevant tests,
  exercise applicable nodes or services, and clean up started processes.
- In the final response, briefly identify what changed and give **exact,
  executable verification commands**. Clearly distinguish checks run from
  checks not run.

## Completion criterion

A person unfamiliar with the function should be able to read its docstring
first, predict its important behavior and decisions, and then recognize that
same conceptual sequence in the implementation.

**Avoid:** vague summaries, purely syntactic commentary, misleading claims,
needless abstraction, and unrelated refactors. Explicitness is desirable;
repetition without added understanding is not.