/flint-tests

You are running unattended at night. No human is available. Your messages are read by an automated reviewer that answers questions and approves plans. Never wait for a human.

## Setup answers (do not ask for these)

1. flint-core spec: `$spec` (read it; it is the exact version flintcli understands)
2. flintcli binary: `$flintcli`, run tests with `$flintcli <file.json> -s $server`
3. Decompiled Minecraft 26.2 source: `$mc_src`
4. A vanilla 26.2 server is running at `$server` (offline mode, the bot is op).
5. Existing tests (examples and conventions): `./tests/` in the current directory. Read 3–5 of them before planning.
   Some existing tests are outdated (e.g. they use the action `use_item_on`, which no longer exists). `$spec` is authoritative: if an example disagrees with it, follow the spec.

## What to test (do not ask)

Subject: `$subject`
Members: $members

Only blocks and items are supported. If a member needs entities or anything else unsupported, say so for that member.

## Rule: test each implementation once

For every member, look up its implementing class and registration in `$mc_src`.
- Members with the same class **and** no member-specific behavior in the source share one implementation.
- Pick one representative per distinct implementation and test that representative thoroughly.
- Do not write tests for the other members of the same implementation. List them as covered by the representative.
- If existing tests in `./tests/` already cover an implementation, don't duplicate them.

## This step: plan only

Do the skill's setup and source exploration, then end your turn with:
1. the members grouped by implementation (class + reason), with the representative of each
2. the proposed test cases, one plain sentence each
3. any member you consider unsupported or already covered, with the reason

Do not write test files in this step.
