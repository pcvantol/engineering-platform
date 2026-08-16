# Engineering Platform Prompt Template

EP Prompt Authoring Contract: 1
Template Version: 1

Use this producer-neutral template for one bounded Engineering Platform
engineering instruction. Complete it yourself or provide it to any GPT. Verify
repository facts before asserting them; this template does not create runtime
or submission evidence.

# <Capability / Increment Name>

Execution Mode: <Managed | Genesis>

Target repository:

<absolute authorized repository path>

# Context

<Describe the relevant situation and constraints. Do not assume repository
facts that must first be verified.>

# Objective

<State one bounded engineering objective.>

# Architecture / Ownership Boundaries

<What owns what? What must remain unchanged?>

# Required Behavior

<Describe observable required behavior and acceptance outcomes.>

# Compatibility / Invariants

<List backward-compatibility, persistence, lifecycle or architectural
invariants.>

# Tests

<List focused tests required for this change.>

# Validation

<List evidence required before completion.>

# Explicitly Out of Scope

<List work that must not be performed.>

# Final Report

<Define the evidence and explicit acceptance questions that must be reported.>
