# Domain docs

This repository uses a single-context layout:

- Root `GLOSSARY.md`: domain vocabulary.
- Root `docs/adr/`: architecture decision records.

Create domain documents lazily when domain modeling resolves terms or decisions.
If they do not exist, proceed silently without creating empty placeholders or
requesting that they be created just to complete setup.

## Before exploring

1. Read root `GLOSSARY.md` if it exists and use its vocabulary.
2. Read ADRs under root `docs/adr/` that affect the current task.
3. For changes to the new architecture or cross-module interfaces, also follow
   root [AGENTS.md](../../AGENTS.md): read
   [architecture.md](../architecture.md) and
   [contracts.md](../contracts.md).

`deprecated/` is a historical archive, not another active bounded context.
Read it when investigating history or explicitly evaluating reuse. Its old
constraints do not automatically apply to the new implementation.

## Vocabulary

Use glossary terms consistently in issues, specs, tests, and code.
If a needed concept is missing, distinguish a real domain gap from an invented
synonym; surface real gaps for `domain-modeling`.

## Decision conflicts

When a proposal contradicts an ADR, name the ADR and explain the conflict and
reason for reopening it rather than silently overriding it.

Surface conflicts among architecture docs, contracts, and ADRs. Unconfirmed
suggestions must not be presented as settled decisions.
