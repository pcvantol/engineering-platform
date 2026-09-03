# Agent delivery scheduling architecture

**Status:** proposed canonical target for P-QUEUE and Phase S

**Scope:** EP Server/CENTRAL scheduling, generic Engineering Platform Agents, capability placement, delivery ownership, agent-local concurrency and telemetry-driven placement.

## Purpose

EP is modeled as a sorting centre. Engineering Actions arrive through multiple transports and are normalized into CENTRAL. CENTRAL determines when an Action is eligible and which Agent is best able to deliver it. Once an Agent accepts an Action, the Agent owns delivery until it succeeds or exhausts its bounded recovery policy and reports the Action as undeliverable.

This target supersedes the idea that project membership itself requires one globally serialized FIFO execution lane. FIFO may remain a fairness policy and Agents may use local per-project or per-repository FIFO queues when needed for execution safety, but independent Actions are not globally serialized merely because they belong to the same project or repository.

## Authority boundary

The EP Server/CENTRAL owns:

- canonical Action identity and lifecycle;
- admission and durable dependency relationships;
- dependency eligibility;
- Agent identity, attachments, capabilities and health;
- placement and delivery ownership;
- durable leases and terminal delivery state;
- platform scheduling policy;
- durable evidence and telemetry used for placement decisions.

The Agent owns:

- physical execution after accepting delivery ownership;
- worktree and branch creation;
- local execution-slot and resource scheduling;
- safe parallelism versus serialization for Actions already assigned to it;
- provider/tool invocation;
- transient delivery retries;
- branch refresh/rebase and bounded conflict reconciliation;
- validation execution and delivery attempts;
- reporting progress, evidence, telemetry and final delivery outcome to CENTRAL.

A local Agent must never become a second canonical lifecycle authority.

## Generic Agent model

There is one generic `engineering-platform-agent` implementation. An Agent may serve multiple projects and repositories through explicit attachments. The logical project/repository topology exists independently of Agent attachment.

An Agent is an execution node, not a project-specific binary.

```text
Agent
  ├── identity / machine identity
  ├── health
  ├── capabilities
  ├── resource capacities
  └── repository attachments
        ├── project A / repository A
        ├── project A / repository B
        └── project B / repository C
```

## Three scheduling layers

### 1. CENTRAL eligibility scheduler

CENTRAL answers: **may this Action be dispatched yet?**

Inputs include:

- admission state;
- explicit dependency DAG;
- predecessor success requirements;
- policy and authorization.

A successor with a hard dependency is not dispatchable until its required predecessor has succeeded.

Project membership alone is not a hard ordering dependency.

### 2. CENTRAL placement scheduler

CENTRAL answers: **which eligible Agent should own delivery?**

First apply hard eligibility filters:

- Agent healthy/available;
- repository attachment/reachability;
- required capabilities;
- authorization/policy.

Capability eligibility always precedes load balancing. If an Action requires `ios-signing` and only one Agent provides it, that Agent is the only placement candidate even when it has a longer queue.

For two or more eligible Agents, CENTRAL optimizes primarily for expected end-to-end delivery time rather than round-robin or raw queue length.

Conceptually:

```text
estimated_time_to_delivery
    = estimated_start_delay
    + estimated_execution_duration
    + estimated_delivery/finalization_overhead
```

The Agent with the lowest expected successful delivery time is preferred, subject to policy, fairness and confidence constraints.

### 3. Agent execution scheduler

After accepting an Action, the Agent answers: **how can I deliver my accepted work safely and quickly?**

The Agent may:

- run independent Actions concurrently in isolated worktrees;
- serialize Actions for the same project/repository when conflict risk or local resource constraints make that safer;
- maintain local per-project/per-repository FIFO ordering as an execution policy;
- revise local ordering as repository state and capacity change.

This local queue is not a second CENTRAL queue. The Actions in it have already been assigned and remain durably owned by that Agent.

## Concurrency invariants

The target invariants are:

```text
per Action:
    active delivery owners <= 1

per project:
    active independent Actions = 0..N

per repository:
    no mandatory global CENTRAL serialization

per dependency edge:
    successor dispatch only after required predecessor success

per Agent:
    accepted/running work <= Agent policy and resource capacity
```

Independent Actions for the same repository may execute concurrently when the Agent judges this safe. Each mutating Action uses an isolated worktree/branch.

## Repository concurrency and merge reconciliation

CENTRAL does not need to predict file-level merge conflicts before dispatch. An Agent has the physical repository context and owns execution safety.

For example, a dependency update and an unrelated UI change may run concurrently in separate worktrees. If one branch merges first and makes the second stale, the second Action is not immediately returned to CENTRAL. The Agent remains responsible for delivery and may:

1. fetch the new target-branch baseline;
2. rebase or update its branch;
3. resolve bounded conflicts, including another provider invocation where policy allows;
4. rerun validation;
5. push and continue delivery.

Only after bounded autonomous recovery is exhausted does the Agent report the Action as undeliverable.

## Two retry levels

### Agent-local delivery retry

Transient and locally recoverable failures remain the Agent's responsibility, for example:

- provider interruption;
- temporary Git/GitHub failure;
- push failure;
- stale branch;
- bounded merge conflict;
- validation race;
- temporary workspace/tool failure.

CENTRAL continues to see the same Action and same delivery owner. Agent attempts are telemetry/evidence, not new global Actions.

### CENTRAL redelivery/recovery

Only when the Agent reports `UNDELIVERABLE`, disappears beyond lease/recovery policy, or explicitly rejects delivery before ownership is established does CENTRAL reconsider placement or operator resolution.

CENTRAL may then choose, according to policy:

- retry on the same Agent;
- redeliver to another capable Agent;
- require operator resolution;
- return failure to the producer.

No delivery may be silently abandoned.

## Capability model

Capabilities are explicit facts and constraints, not project names. They may include:

- tools: `git`, `github`, `managed-codex`, `dotnet`, `node`, `xcode`;
- platform: `macos`, `linux`, `windows`, architecture;
- privileged capabilities: `ios-signing`, notarization, App Store Connect;
- runtime capabilities: Docker, browser, iOS simulator, local AI runtime;
- physical/network capabilities where required;
- bounded resource characteristics.

An Action declares required capabilities. Placement candidates are the intersection of healthy authorized Agents that can reach the repository and satisfy all hard requirements.

## Resource capacity

Capacity is multi-dimensional rather than one scalar slot count. An Agent may advertise, for example:

```text
general_execution_slots = 6
provider_slots = 4
xcode_slots = 1
ios_signing_slots = 1
local_llm_slots = 1
```

The Agent's local scheduler decides how these resources constrain accepted work.

## Agent Delivery Estimate

Each eligible Agent contributes an **Agent Delivery Estimate**. This is the placement contract between CENTRAL's global scheduler and the Agent's local scheduler.

An estimate may include:

- estimated start delay for the Action's requirements;
- estimated execution duration;
- estimated finalization/delivery overhead;
- expected completion timestamp;
- confidence/sample quality;
- current relevant resource pressure.

The Agent has better knowledge of its local queue, repository serialization decisions and resource slots than CENTRAL. CENTRAL should therefore not estimate queue delay solely from `queue_length × average_duration`.

## Telemetry-driven placement

CENTRAL combines Agent-reported live estimates with durable historical telemetry. Useful dimensions include:

- `agent_id`;
- project/repository;
- Action class;
- required capability set;
- provider/model;
- validation profile;
- historical execution and delivery duration;
- provider latency;
- validation/build duration;
- retry/failure rate.

Historical prediction should fall back from specific to general when sample counts are insufficient, for example:

```text
same Agent + repository + Action class
→ same Agent + Action class
→ same Agent + capability/profile
→ Agent-wide history
→ platform default
```

This naturally accounts for differences such as a fast machine, a slower machine, managed cloud AI versus a slower local model, or an Agent that happens to perform a particular class of work more efficiently.

A currently idle but slow Agent is not automatically preferable to a faster Agent with a short backlog. CENTRAL compares expected completion, not merely current queue length.

## Placement stability

Once an Agent accepts delivery, CENTRAL does not continuously move the Action because another Agent's estimate later becomes slightly better. Delivery ownership remains stable until a defined recovery boundary such as:

- Agent loss beyond policy;
- explicit rejection before accepted ownership;
- capability loss that makes delivery impossible;
- `UNDELIVERABLE` terminal delivery result.

This prevents scheduler thrashing and duplicate execution.

## Queue terminology

The architecture distinguishes:

1. **CENTRAL pending/ready pool** — admitted Actions not yet assigned;
2. **CENTRAL dependency wait** — admitted Actions whose hard predecessors are incomplete;
3. **Agent delivery queue** — accepted Actions already owned by an Agent but waiting locally for execution safety/capacity.

FIFO is allowed as a fairness or local safety policy. It is not a platform-wide rule that serializes every Action in a project.

## Delivery contract

The strong delivery guarantee is:

> An Agent that accepts an Action assumes durable delivery responsibility until it reports successful delivery or explicitly reports that delivery is impossible under the bounded retry/recovery policy.

The Server may assume accepted work is owned, but it must retain durable lease/heartbeat/recovery semantics for machine loss and ambiguous ownership.

## P-QUEUE and Phase S implications

P-QUEUE must not harden the current transitional `one active execution per project` behavior into the target architecture. Its target is a dependency-aware, capability-constrained, telemetry-informed scheduler with exactly one active delivery owner per Action.

Phase S introduces the generic Agent execution plane and Agent Delivery Estimate contract. Server-local execution used during Phase P is a transitional execution binding, not the long-term concurrency model.

Required future qualification includes:

- independent Actions from one project executing concurrently;
- hard dependency edges preventing successor dispatch;
- capability-exclusive placement (for example iOS signing);
- two eligible Agents choosing the lower estimated delivery time rather than raw queue length;
- Agent-local serialization for risky same-repository work;
- safe same-repository parallel work in isolated worktrees;
- autonomous stale-branch/rebase/conflict recovery;
- bounded local retry before `UNDELIVERABLE`;
- exactly one delivery owner per Action;
- Agent loss/reconnect without duplicate delivery;
- no local Agent state becoming canonical lifecycle authority.
