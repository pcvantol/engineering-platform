# Agent delivery scheduling architecture

**Status:** proposed canonical target for P-QUEUE, Universal Installer and Phase S

**Scope:** EP Server/CENTRAL scheduling, generic Engineering Platform Agents, capability placement, delivery ownership, Agent installation/security contexts, provider connections, runtime/tool ownership, health telemetry, priority scheduling, Agent-local concurrency and telemetry-driven placement.

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
- platform scheduling and priority policy;
- durable evidence and telemetry used for placement decisions;
- read-only Console projection of Agent/provider/runtime health.

The Agent owns:

- physical execution after accepting delivery ownership;
- worktree and branch creation;
- local execution-slot and resource scheduling;
- safe parallelism versus serialization for Actions already assigned to it;
- provider/tool invocation;
- transient delivery retries;
- branch refresh/rebase and bounded conflict reconciliation;
- validation execution and delivery attempts;
- local credential use and provider/session state;
- reporting progress, evidence, telemetry, runtime inventory and final delivery outcome to CENTRAL.

The Universal Installer/runtime manager owns:

- installation of the Agent instance in its OS security scope;
- the EP-owned base toolchain and isolated runtime;
- provider CLI installation only when that provider is configured;
- tool/runtime update detection, controlled upgrade, verification and rollback.

A local Agent must never become a second canonical lifecycle authority. CENTRAL must never become the credential store for Agent-local provider or signing secrets.

## Generic Agent model

There is one generic `engineering-platform-agent` implementation. An Agent may serve multiple projects and repositories through explicit attachments. The logical project/repository topology exists independently of Agent attachment.

An Agent is an execution node, not a project-specific binary.

```text
Agent
  ├── durable Agent identity
  ├── machine identity
  ├── installation/security scope
  ├── health
  ├── provider and privileged connections
  ├── effective capabilities
  ├── resource capacities
  ├── EP-owned runtime inventory
  └── repository attachments
        ├── project A / repository A
        ├── project A / repository B
        └── project B / repository C
```

## Agent installation and OS security scopes

The Universal Installer must support both user-level and machine-level Agent installation.

On one machine the supported topology is:

```text
Machine
├── Machine Agent                  max 1
├── User Agent: Alice              max 1 for Alice
└── User Agent: Bob                max 1 for Bob
```

More generally, at most one Agent instance exists for a given `(machine, installation_scope, os_principal)` tuple.

The identities are deliberately separate:

- `agent_id` is the durable EP execution-node identity;
- `machine_id` identifies the physical machine;
- `installation_scope` is `MACHINE` or `USER`;
- `os_principal` is the local runtime/security principal.

A username change must not redefine `agent_id`.

### User-level Agent

A user-level Agent runs inside that OS user's security context. Provider authentication and credential material belong to that context.

Example:

```text
Alice Agent
├── Codex: Alice private account
└── GitHub: Alice corporate account

Bob Agent
├── Codex: Bob private account
└── GitHub: Bob private account
```

The Alice Agent must not inherit or read Bob's provider sessions, and the Bob Agent must not inherit or read Alice's.

### Machine-level Agent

A machine-level Agent runs in a machine/system security context and may hold privileged machine credentials that individual users do not need or receive.

For example:

```text
Machine Agent
├── Xcode toolchain
├── Apple signing certificate/private key
├── provisioning profiles
├── App Store Connect credential
└── effective capability: ios-release
```

A signing/release Action can therefore be placed only on the Machine Agent even when Alice and Bob are logged into the same physical computer.

Credentials are never inherited automatically between Agent instances on one machine.

## Provider and privileged connection model

During Agent setup the Universal Installer presents connection/setup actions for supported providers and privileged capabilities. Initial provider support is:

- GitHub;
- Codex.

Future providers may include Claude and GitLab without changing the Agent identity/scheduling model.

The setup surface may also support privileged connections such as:

- Apple code signing;
- App Store Connect;
- future Android signing, notarization or other release services.

Conceptually:

```text
AgentConnection
├── connection_id
├── agent_id
├── connection_type
│     ├── AI_PROVIDER
│     ├── SOURCE_CONTROL
│     ├── CODE_SIGNING
│     └── RELEASE_SERVICE
├── provider/capability identifier
├── display label
├── authentication/health state
├── non-secret capability metadata
└── last_verified_at
```

### Credential boundary

CENTRAL stores only safe facts required for scheduling and Console projection, such as:

- connection type/provider;
- safe operator-defined display label;
- health/readiness;
- capability availability;
- capacity/quota projection where supported;
- non-secret expiry/validity facts where appropriate.

Secret material remains Agent-local. CENTRAL and the browser must never receive provider tokens, refresh tokens, session cookies, private signing keys, credential files, raw authorization headers or secret environment variables.

A read-only Console does not imply that secret material may be projected.

## Effective capabilities

Capabilities are explicit facts and constraints, not project names. They may include:

- tools: `git`, `github`, `managed-codex`, `dotnet`, `node`, `xcode`;
- platform: `macos`, `linux`, `windows`, architecture;
- privileged capabilities: `ios-signing`, notarization, App Store Connect;
- runtime capabilities: Docker, browser, iOS simulator, local AI runtime;
- physical/network capabilities where required;
- bounded resource characteristics.

An Action declares required capabilities. Placement candidates are the intersection of healthy authorized Agents that can reach the repository and satisfy all hard requirements.

Capabilities may be derived from multiple local facts. For example:

```text
xcode installed                     PASS
signing certificate valid           PASS
private key accessible               PASS
provisioning profile valid           PASS
App Store Connect connection         PASS
────────────────────────────────────────
ios-release                          READY
```

If one prerequisite expires or becomes unavailable, the Agent advertises the effective capability as unavailable and CENTRAL stops assigning new Actions that require it.

Capability availability belongs to the Agent security context, not merely to the physical machine. A user Agent can therefore provide `xcode` without providing `ios-signing`.

## Three scheduling layers

### 1. CENTRAL eligibility scheduler

CENTRAL answers: **may this Action be dispatched yet?**

Inputs include:

- admission state;
- explicit dependency DAG;
- predecessor success requirements;
- Action priority;
- policy and authorization.

A successor with a hard dependency is not dispatchable until its required predecessor has succeeded.

Project membership alone is not a hard ordering dependency.

### 2. CENTRAL placement scheduler

CENTRAL answers: **which eligible Agent should own delivery?**

First apply hard eligibility filters:

- Agent healthy/available;
- repository attachment/reachability;
- required capabilities;
- required provider/privileged connection health;
- hard resource thresholds;
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

The Agent with the lowest expected successful delivery time is preferred, subject to priority, policy, fairness, health/risk and confidence constraints.

### 3. Agent execution scheduler

After accepting an Action, the Agent answers: **how can I deliver my accepted work safely and quickly?**

The Agent may:

- run independent Actions concurrently in isolated worktrees;
- serialize Actions for the same project/repository when conflict risk or local resource constraints make that safer;
- maintain local per-project/per-repository FIFO ordering as an execution policy;
- promote higher-priority accepted Actions ahead of ordinary locally waiting work where safe;
- revise local ordering as repository state and capacity change.

This local queue is not a second CENTRAL queue. The Actions in it have already been assigned and remain durably owned by that Agent.

## Priority and expedited delivery

An Action may declare scheduling priority when it is submitted to EP. The initial conceptual model is:

```text
NORMAL
HIGH
URGENT
```

Priority influences how quickly eligible work is dispatched and how the Agent orders already-accepted waiting work. Priority never bypasses:

- hard dependencies;
- capability requirements;
- authorization;
- resource safety thresholds;
- repository/execution safety.

An URGENT Action must therefore still wait for a required predecessor or a uniquely capable Agent such as the only `ios-signing` Agent.

CENTRAL should prefer higher-priority eligible Actions before lower-priority eligible Actions, then optimize expected successful delivery time within the applicable priority class. Waiting-time aging/fairness may prevent starvation of ordinary work.

In the initial model priority does not forcibly interrupt arbitrary already-running provider or merge operations. Running work is allowed to reach a safe boundary; higher-priority waiting work receives the next safe execution opportunity. Safe-point pre-emption may be considered later as a separate design decision.

The Agent reports bounded reasons when a high-priority Action cannot start immediately, for example:

- `WAITING_DEPENDENCY`;
- `NO_ELIGIBLE_AGENT`;
- `AGENT_CAPACITY`;
- `REPOSITORY_SERIALIZATION`;
- `REQUIRED_CAPABILITY_BUSY`.

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

## Agent health and machine metrics

Each Agent periodically reports an `AgentHealthSnapshot` to CENTRAL. At minimum the model should support:

- process/Agent uptime and heartbeat;
- CPU utilization/load;
- memory pressure and available memory;
- GPU utilization where available;
- GPU/VRAM pressure where meaningful;
- free disk space and disk pressure;
- thermal pressure/state;
- active execution/resource slots;
- local accepted/waiting delivery counts;
- relevant network/provider connectivity/latency signals;
- power/battery state where execution policy needs it.

The Agent may report both raw metrics and normalized states such as `NORMAL`, `WARN`, `HIGH`, `CRITICAL`.

Resource health participates in scheduling in two ways:

1. **hard ineligibility** — for example insufficient disk for the declared requirement, critical memory pressure, inaccessible required GPU/VRAM or a failed required provider connection;
2. **soft placement penalty** — for example elevated CPU/memory/thermal pressure that makes another otherwise-equivalent Agent more likely to deliver sooner and more reliably.

The scheduling objective is therefore expected **successful** delivery time, not raw processor speed or raw queue length.

Health telemetry is advisory for ordinary placement, while explicit critical resource thresholds may make an Agent temporarily ineligible for new deliveries.

## Agent Delivery Estimate

Each eligible Agent contributes an **Agent Delivery Estimate**. This is the placement contract between CENTRAL's global scheduler and the Agent's local scheduler.

An estimate may include:

- estimated start delay for the Action's requirements;
- estimated execution duration;
- estimated finalization/delivery overhead;
- expected completion timestamp;
- confidence/sample quality;
- current relevant resource pressure;
- local scheduling constraints such as repository serialization or exclusive resource slots.

The Agent has better knowledge of its local queue, repository serialization decisions and resource slots than CENTRAL. CENTRAL should therefore not estimate queue delay solely from `queue_length × average_duration`.

## Telemetry-driven placement

CENTRAL combines Agent-reported live estimates with durable historical telemetry. Useful dimensions include:

- `agent_id`;
- project/repository;
- Action class;
- required capability set;
- provider connection/model;
- validation profile;
- historical execution and delivery duration;
- provider latency;
- validation/build duration;
- retry/failure rate;
- relevant health/resource state.

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

## Provider capacity is Agent/connection scoped

Provider capacity belongs to an individual Agent connection/security context. Separate personal or corporate subscriptions must never be combined into one artificial capacity pool.

For example:

```text
Agent Alice / Codex Alice private      80% available
Agent Bob   / Codex Bob private        15% available
```

CENTRAL may report that two Codex connections are healthy, but it must not present `95%` as a single fungible capacity.

Provider-capacity telemetry is therefore keyed at least by:

- `agent_id`;
- `provider_connection_id`;
- provider;
- timestamp.

Scheduling uses the provider connection(s) actually available within the selected Agent's security context.

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

## EP-owned runtime and Universal Installer

All tooling owned by EP is installed and maintained locally by the Universal Installer/runtime manager. Machine-wide package managers and unrelated OS/application updaters are not the update authority for the EP execution stack.

A conceptual Agent runtime root is:

```text
EP runtime root
├── agent/
├── python/
├── venv/
├── git/
├── bash/
├── provider-tools/
│   ├── codex/
│   ├── gh/
│   ├── claude/      future
│   └── glab/        future
└── runtime-manifest.json
```

### Base tooling

The Universal Installer detects/bootstrap-installs and then owns the supported Agent execution toolchain, including at least:

- Python;
- EP-owned virtual environment/dependencies;
- Git;
- Bash/shell tooling where required;
- Engineering Platform Agent runtime.

The installed execution stack must not depend on whatever versions happen to be selected by Homebrew, system Python, a user's shell PATH or another machine updater.

OS components that are not reasonably bundled by EP, such as the operating system or Xcode itself, remain externally managed and are reported as external capabilities rather than silently upgraded by EP.

### Provider tooling

Provider CLIs are installed only when that provider is configured for the Agent.

For example:

```text
Add Codex connection
→ install/verify EP-managed Codex CLI
→ user authenticates in this Agent security context
→ connection READY

Add GitHub connection
→ install/verify EP-managed gh CLI
→ user authenticates in this Agent security context
→ connection READY
```

Future provider adapters follow the same lifecycle.

### Runtime inventory and updates

Each Agent reports a non-secret runtime inventory to CENTRAL, for example:

```text
Agent          2.x       CURRENT
Python         3.x       CURRENT
Git            2.x       CURRENT
Codex CLI      0.x       UPDATE_AVAILABLE
gh             2.x       CURRENT
```

A runtime manifest records sufficient provenance for safe update/rollback, including tool identity, installed version, artifact digest/source, installation time, update state and rollback candidate where supported.

Update discovery propagates to the Agent Console view. While the Console remains installation-wide unauthenticated/read-only, update actions must remain disabled or unavailable. Once EP has suitable authentication/authorization, an authorized operator may invoke Agent-scoped `Update now` or `Update all` actions.

`Update all` is Agent-scoped: it updates only EP-owned runtime components for that Agent instance. It must not update the OS, Xcode, Homebrew globally, unrelated applications, or another user's/machine Agent instance.

Update execution is controlled by the Agent runtime manager:

```text
update requested
→ stop accepting new work affected by the tool
→ allow active deliveries to finish/reach a safe boundary
→ QUIESCED
→ verify trusted artifact/version
→ atomic upgrade
→ health/preflight
→ READY
→ resume scheduling eligibility
```

Rollback is required where the update contract supports it. Arbitrary `brew upgrade`, global Python mutation or replacement of unrelated machine tooling is not an acceptable Agent update mechanism.

## Console Agent views

The Operations Console must eventually provide Agent-aware views alongside project views.

The project view answers: **what is happening to this project's engineering work?**

The Agent view answers: **what is this execution node doing and how healthy is it?**

A read-only Agent view should include, where available:

- Agent identity/display label;
- machine and installation scope (`MACHINE` / `USER`);
- online/heartbeat state and uptime;
- Agent/runtime version;
- repository attachments;
- provider/privileged connections and health;
- non-secret provider capacity per Agent/connection;
- effective capabilities;
- machine health (CPU, memory, GPU, disk, thermal);
- running/accepted/locally waiting deliveries;
- recent telemetry, for example the last hour;
- historical delivery/ETA performance;
- runtime/tool update availability.

The Console currently has no user authentication/roles. Until such authorization exists, Agent and project management/actions remain read-only, even though installation-wide non-secret operational facts may be visible to all Console viewers.

The explicit transitional policy is:

> Console visibility is installation-wide read-only; credential material remains Agent-local and is never part of CENTRAL projection.

## P-QUEUE, Universal Installer and Phase S implications

P-QUEUE must not harden the current transitional `one active execution per project` behavior into the target architecture. Its target is a dependency-aware, priority-aware, capability-constrained, telemetry-informed scheduler with exactly one active delivery owner per Action.

The Universal Installer must establish isolated user-level and machine-level Agent instances, EP-owned runtimes, provider/privileged connections and runtime inventory/update management without turning CENTRAL into a secret store.

Phase S introduces the generic Agent execution plane, provider/effective-capability reporting, Agent health telemetry and Agent Delivery Estimate contract. Server-local execution used during Phase P is a transitional execution binding, not the long-term concurrency model.

Required future qualification includes:

- independent Actions from one project executing concurrently;
- hard dependency edges preventing successor dispatch;
- URGENT work overtaking ordinary eligible/waiting work without bypassing hard safety/dependency constraints;
- capability-exclusive placement (for example iOS signing on a Machine Agent);
- separate user-level Agents on one machine using distinct provider identities;
- machine-level privileged credentials not inherited by user Agents;
- two eligible Agents choosing the lower expected successful delivery time rather than raw queue length;
- resource pressure changing placement eligibility/ETA;
- provider capacity remaining separate per Agent/provider connection;
- Agent-local serialization for risky same-repository work;
- safe same-repository parallel work in isolated worktrees;
- autonomous stale-branch/rebase/conflict recovery;
- bounded local retry before `UNDELIVERABLE`;
- exactly one delivery owner per Action;
- Agent loss/reconnect without duplicate delivery;
- EP-owned base/provider tooling isolated from machine package-manager updates;
- controlled Agent runtime update/quiesce/health/rollback behavior;
- no local Agent state becoming canonical lifecycle authority;
- no Agent/provider/signing secret material projected into CENTRAL or the browser.
