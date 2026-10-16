---
status: proposed
---

# Vitrine stays its own repository and package

Vitrine and Lincei are developed together, and features such as the card page touch both. We
considered merging them into one repository and decided to keep them apart. Lincei is T3 Code plus
a thin overlay: since October 2026 it shares git history with upstream T3 Code and merges
`t3code/main` directly, at upstream's pace of over a thousand commits a month. Its main branch
carries 5,362 commits and about 2 GB of file history. Vitrine is a 550 KB Python library that
people install with pip. Lincei already installs Vitrine into its managed Python environment and
can point at a local Vitrine checkout through `LINCEI_CLINICAL_REQUIREMENTS`, so working on both
at once does not require a release in between.

## Boundary rules

- Vitrine never imports, names, or special-cases Lincei. What Lincei knows and Vitrine needs
  reaches Vitrine through host-neutral interfaces: `VITRINE_*` environment variables and the
  agent-session records in ADR-0002.
- Vitrine serves its own UI. Lincei embeds that UI and does not rebuild cards in its own frontend.
- Vitrine works without a host: in a plain Python process, in Claude Code, and in Codex. A feature
  that only works inside Lincei belongs in Lincei.
- Lincei's main branch depends only on released Vitrine versions. Changes that span both
  repositories are developed together through the local-checkout override, released in Vitrine
  first, and picked up in Lincei by raising its version range.

## Considered options

- **Merge Lincei into the Vitrine repository**, which would keep Vitrine's GitHub stars. Rejected:
  the Vitrine repository would become a T3 Code fork. Every clone, including
  `pip install git+…`, would pull T3 Code's history, T3 Code's contributors would appear as
  Vitrine's, and every upstream merge would land in Vitrine's history.
- **Move Vitrine into the Lincei repository as a new top-level directory.** This fits Lincei's
  overlay rule, because it only adds files, and it would let one change cover both sides.
  Rejected for now because it hides a pip library inside a desktop app's fork and gives up
  Vitrine's own home. Revisit if most features end up needing coordinated changes in both
  repositories.
