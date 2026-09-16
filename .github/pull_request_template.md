<!-- Security issues: do not use this template. Report privately — see SECURITY.md. -->

## What

<!-- What changes, and why. Link the issue if there is one: Closes #123 -->

## How

<!-- The approach, and anything a reviewer would otherwise have to reverse-engineer from the diff. -->

## Testing

<!-- Which lane covers this, and what it asserts. `make check` runs all of them. -->

- [ ] `make check` passes locally
- [ ] Unit coverage for the new behaviour (`tests/unit/`)
- [ ] Integration lane updated, if this touches a migration, an RPC, or a money invariant
- [ ] Coverage floor raised for any module this lifts (`coverage.floors`)

## Notes

<!--
Anything the reviewer should know: a coverage floor that moved and why, a deliberate shortcut,
a follow-up that is out of scope. Delete the section if there is nothing.
-->

---

<!-- The PR title must be a Conventional Commit — it becomes the squash commit on main.
     e.g. fix(credits): claw back a refunded purchase even when the balance is short -->
