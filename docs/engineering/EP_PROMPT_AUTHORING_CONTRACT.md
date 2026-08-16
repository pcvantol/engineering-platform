# Engineering Platform Prompt Authoring Contract

**Contract ID:** `EP_PROMPT_AUTHORING_CONTRACT`
**Prompt Authoring Contract Version:** 1
**Status:** Canonical Repository Truth for Engineering Platform prompt authoring

## Purpose

This contract defines how any producer writes one bounded Engineering Platform
engineering instruction. It is producer-neutral: a human, generic GPT/LLM,
automation, Forge, or a future producer may use it.

It is distinct from the technical [Engineering Inbox Protocol](../../tools/engineering/ENGINEERING_INBOX_PROTOCOL.md):

```text
Human / GPT / Forge
        ↓
EP Prompt Authoring Contract
        ↓
Engineering Instruction
        ↓
Producer Submission Contract
        ↓
Engineering Platform
        ↓
Execution Host
```

The Prompt Authoring Contract describes how the instruction is written. The
Producer Submission Contract describes how an instruction and its metadata are
transported into Engineering Platform. The Execution Host Contract describes
how Engineering Platform admits and executes the work. Forge is optional and
is never required to create a valid Engineering Platform prompt.

## Authoring Rules

A compliant prompt MUST:

1. explicitly identify the authorized target repository or workspace;
2. specify the Execution Mode where applicable;
3. state one bounded engineering objective;
4. provide relevant context, constraints, architecture and ownership
   boundaries;
5. describe observable required behavior, important compatibility and
   invariant requirements, required tests and required validation;
6. state explicitly out-of-scope work and expected final report or acceptance
   criteria;
7. be self-contained enough for an execution agent that has no conversation
   history;
8. require Repository Truth inspection before implementation and
   evidence-backed completion; and
9. preserve fail-closed behavior whenever required evidence is unavailable.

Prompts MUST use `MUST`, `SHOULD`, and `MUST NOT` precisely when expressing
normative constraints. They MUST NOT invent commits, branches, pull-request
numbers, Mission IDs, runtime state, implementation status, or test results.
They MUST NOT request hidden chain-of-thought, fabricated evidence, validation
bypass, governance or authorization bypass, or Forge-specific semantics unless
the bounded target work genuinely concerns Forge.

### Repository Truth

Prompt context is an instruction and hypothesis. Repository Truth is
authoritative for current implementation state. The execution agent MUST inspect
the target repository before making changes and MUST NOT blindly assume the
prompt is current. If Repository Truth already satisfies a requirement, the
agent MUST NOT create unnecessary changes solely to satisfy prompt wording.

### Implementation Prescription

Prompts SHOULD describe desired behavior, constraints, invariants and
acceptance evidence rather than internal implementation mechanics.
Implementation prescriptions are appropriate only when they are genuine
architecture constraints or explicit owner decisions.

## Ownership Boundary

Engineering Platform owns execution admission, execution lifecycle, liveness,
execution evidence, execution receipts, retry/resume/dismiss execution
handling, Execution Host behavior, and provider execution boundaries.

The target product owns product and domain semantics, target architecture,
business rules, and product-specific governance. Forge, when present, owns its
own Mission, planning and governance semantics. Prompt authors MUST NOT move
product authority into Engineering Platform accidentally.

## Execution Modes

Use only the current canonical modes defined by the
[Engineering Inbox Protocol](../../tools/engineering/ENGINEERING_INBOX_PROTOCOL.md).

- **Managed** is normally used for repositories operating through the normal
  managed Engineering Platform workflow, including its configured workspace,
  upstream and pull-request requirements.
- **Genesis** is normally used for an authorized local, bootstrap or
  new-project repository where canonical Engineering Platform Genesis
  semantics apply.

Authors MUST NOT assume unsupported mode behavior. The actual mode contract and
admission evidence remain authoritative.

## Recommended Prompt Structure

Use the following sections when they apply. Optional useful sections may be
added; empty irrelevant sections are not required merely for formatting.

```text
# <Capability / Increment Name>

Execution Mode: <mode>

Target repository:
<target>

# Context

# Objective

# Architecture / Ownership Boundaries

# Required Behavior

# Compatibility / Invariants

# Tests

# Validation

# Explicitly Out of Scope

# Final Report
```

## Validation and Final Report Guidance

Validation MUST be proportional to the work and sufficient to support the
claimed completion evidence. Authors SHOULD distinguish relevant focused tests,
regression suites, static analysis, security checks, browser or E2E tests,
`git diff --check`, and repository-specific qualification. Prompts MUST NOT
blindly require every possible validation tool when it does not apply.

A final report SHOULD let a reviewer determine what changed, what did not
change, resulting repository state, validation result, unresolved limitations,
and capability status. The report is acceptance evidence, not a verbose echo of
the prompt.

## Versioning and Compatibility

Contract Version 1 defines the normative authoring rules in this document.
Increment the contract version when those normative requirements change. A
template version identifies the starter-template shape; increment it when that
shape or its guidance changes. Pure typo or editorial fixes need not change
either version unless they change meaning.

The canonical starter artifact is
[EP_PROMPT_TEMPLATE.md](EP_PROMPT_TEMPLATE.md). It is Repository Truth and the
single reusable English Markdown source. Existing manually authored or
plain-text prompts remain valid: this contract is authoring guidance, not a
retroactive ingress rule. Engineering Platform does not parse headings,
enforce a prompt schema, lint prompt semantics, or reject legacy submissions as
part of this contract.

## New-Project Bootstrap

The no-Forge path is: prepare a target repository; authorize/configure it under
the existing Engineering Platform bootstrap rules; obtain the canonical
template from Repository Truth; complete it manually or with any GPT; submit it
through a supported producer submission mechanism; and let Engineering Platform
perform normal admission, preflight and execution. Refer to the existing
bootstrap and submission contracts for their details rather than duplicating
them here.

The Operations Console is an execution-centric operational surface and is not
a prompt-authoring entry point. Future prompt-authoring and project-development
experiences, including Project Workspace, Architect Chat and Prompt Workbench,
may reuse this contract and template without changing their normative content.
Those future capabilities are intentionally outside this contract increment.

## Arbitrary GPT Authoring Instruction

Copy this compact instruction into a new GPT conversation together with the
target project context and desired capability:

```text
You are authoring an Engineering Platform execution prompt. Follow the
canonical Engineering Platform Prompt Authoring Contract. Inspect Repository
Truth before assuming current implementation state. Write one bounded
objective. Preserve architecture and ownership boundaries. Specify behavior,
invariants, tests, validation, out-of-scope work and final evidence. Do not
invent repository facts or execution evidence. Return only the finished EP
prompt.
```

This use case requires no implicit DJConnect or Forge conversation history.
Forge may continue to generate Engineering Actions and runtime prompts, but it
need not literally render the starter Markdown template; it SHOULD conform to
equivalent canonical authoring requirements where applicable.
