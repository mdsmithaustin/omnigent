# Prime integration design

These documents describe a Prime Agent integration for Omnigent and Agent Profile Kit.
The [Omnigent adapter](../../docs/PRIME_NATIVE.md) implements the `prime-native`
terminal and message integration. Prime-side bridge and Kit work remain planned.
The full contract below requires separate qualification.

The following handoff records the original design-only branch and the broader
planned sequence. Use the adapter guide above for the implemented command.

## Open the implementation branch

Clone the fork into a directory you choose.

```sh
git clone --branch design/prime-native-integration https://github.com/mdsmithaustin/omnigent.git
```

For an existing checkout of this fork, fetch and switch to the branch.

```sh
git fetch origin design/prime-native-integration
git switch design/prime-native-integration
```

Confirm the branch before editing.

```sh
git branch --show-current
git status --short
```

The expected branch is `design/prime-native-integration`.
A fresh checkout has no changed files.

## Read the handoff

1. Read [AGENTS.md](../../AGENTS.md) and [CONTRIBUTING.md](../../CONTRIBUTING.md).
2. Read the [architecture and source evidence](DESIGN.md).
3. Read the [feature contract and acceptance criteria](CONTRACT.md).
4. Run the [implementation plan](IMPLEMENTATION.md) one verified unit at a time.

## Start the next session

Use this prompt in a session attached to the branch checkout.

```text
Implement the first native vertical slice from designs/prime-native/IMPLEMENTATION.md.
Start with P1-Prime, then P1-Omni, using separate repository branches.
Read AGENTS.md, CONTRIBUTING.md, designs/prime-native/DESIGN.md,
and designs/prime-native/CONTRACT.md first.
Deliver a real Omnigent prime-native native contribution and the thin
Prime-owned bridge needed to prove the native session contract.
Verify native registration, authenticated model and effort discovery,
identity and skills, terminal attachment, enforced denial, and detach/resume
against the same Prime root before extending the integration.
Use the source references as the reviewed baseline, then record any revision changes.
Keep Prime daemon adaptation on the Prime side.
Keep Omnigent declared roles distinct from Prime RLM descendants.
Open a separate Prime checkout or branch when its source must change.
Keep Agent Profile Kit changes on its own branch when P3 starts.
No implementation PR may merge before its verification and review gates pass.
```

The preparation task creates only this Omnigent fork and design branch.
Prime source changes and Kit implementation branches remain future work.

## Confirm the handoff

Check that `DESIGN.md`, `CONTRACT.md`, and `IMPLEMENTATION.md` exist beside this file.
Read the unproven items before treating any capability as supported.
The implementation checklist remains unchecked until the required evidence exists.
