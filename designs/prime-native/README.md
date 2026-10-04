# Prime-native integration design

Read the [no-fork design](NO_FORK.md) for the active implementation and all original acceptance IDs. Prime stays a published, unmodified runtime. Omnigent uses its supported extension APIs. Optional Kit resources remain separate work.

The [adapter guide](../../docs/PRIME_NATIVE.md) describes `prime-native`, live controls, resource delivery, reattachment, and scoped cleanup. The adapter reuses Pi extension transport with a Prime root binding. Durable recovery, global control exclusivity, mixed-role completion, Kit, and whole-tree confinement remain unqualified.

## Verification

The disposable public-daemon probe completed against published Prime 0.9.6. Eight scenarios passed and three failed qualification. The replay-cursor contract and the tested worker-recovery sequence prevent a production daemon migration. The adapter retains the extension transport. Missing capabilities do not authorize a Prime fork.

Read [AGENTS.md](../../AGENTS.md) and [CONTRIBUTING.md](../../CONTRIBUTING.md) before changing the adapter. Use the [verification skill](../../.agents/skills/verify-prime-native/SKILL.md) for adapter scenarios and the separate daemon qualification gate. Each probe records literal outcomes, source hashes, and owned cleanup.

## Historical proposal

[DESIGN.md](DESIGN.md), [CONTRACT.md](CONTRACT.md), and [IMPLEMENTATION.md](IMPLEMENTATION.md) preserve the earlier design and acceptance rationale. Their Prime-side bridge, future Prime fork, cross-client controller guarantee, and P1-Prime sequence are superseded. Their checklists do not establish runtime qualification. Follow the active no-fork design instead of those old implementation instructions.
