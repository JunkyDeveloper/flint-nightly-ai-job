---
name: flint-tests
description: Write Flint tests in JSON format for Minecraft using the Flint testing framework. Use this skill whenever the user wants to create, write, or generate Flint test cases, or asks about testing Minecraft behavior with Flint. Trigger on any mention of "flint test", "flintcli", "write a test for Minecraft", or anything involving the Flint testing framework, even if the user doesn't explicitly say "skill".
---

# Flint Test Writer

This skill helps Claude write Flint tests in JSON format for Minecraft.

Before writing any tests, you must complete the **Setup Phase** below in full. Do not skip or reorder steps.

---

## Phase 1: Setup

Ask all questions upfront in a single message before doing anything else. Then execute the automated checks in order.

### Step 1: Ask All Questions At Once

Open with a single message collecting everything needed:

> "Before I get started, I need a few things:
>
> 1. **flint-core spec** — Do you have flint-core available locally at the latest version? If yes, what is the path to `test_spec.rs`? (e.g. `/path/to/flint-core/src/test_spec.rs`). If no, I'll fetch it from GitHub.
> 2. **flintcli binary** — Where is the flintcli binary located? (e.g. `./flintcli` or `/usr/local/bin/flintcli`)
> 3. **Decompiled Minecraft source** — Where is the decompiled Minecraft source located? (e.g. `/path/to/minecraft-decompiled/`)"

Wait for the user to answer all three before continuing.

---

### Step 2: Load the Flint Test Specification

**If the user has flint-core locally:**
Read `test_spec.rs` from the path they provided using the `view` tool.

**If not:**
Fetch it from GitHub using `web_fetch`:
```
https://raw.githubusercontent.com/FlintTestMC/flint-core/refs/heads/main/src/test_spec.rs
```

Read it carefully either way — it defines the JSON structure and all valid fields for Flint tests.

---

### Step 3: Check for a Running Minecraft Server

Check whether a Minecraft server is listening on port 25565:

```bash
nc -z localhost 25565 2>/dev/null && echo "running" || echo "not running"
```

**If running:** Continue to the next step.

**If not running:** Abort immediately and do not continue under any circumstances:
> "🚫 No Minecraft server is running on port 25565. This skill cannot function without a running server — it is required for executing Flint tests. Start your Minecraft server and restart this skill once it's up."

Do not offer workarounds. Do not proceed. The skill is non-functional without the server.

---

### Step 4: Read Example Flint Tests

Read several `.json` test files from the current working directory to understand the project's conventions and patterns:

```bash
ls *.json 2>/dev/null | head -20
```

Read at least 3–5 example files using the `view` tool before writing anything. Pay attention to:
- Naming conventions
- Test structure and field usage
- Any patterns that repeat across tests

---

## Setup Summary

Once all steps pass, confirm to the user that setup is complete and summarize what was found:

```
✅ Flint test spec loaded (from [source])
✅ flintcli binary: [path]
✅ Minecraft server running on port 25565
✅ Decompiled Minecraft source: [path]
✅ [N] example test files read from current directory
```

---

## Phase 2: Writing Tests

### Step 1: Ask What to Test

Ask the user:
> "What block or item would you like to write Flint tests for?"

---

### Step 2: Validate the Request

Check whether the requested subject is currently supported. At this time, **only blocks and items are supported**. If the user requests anything else (entities, world gen, redstone logic, etc.), respond:
> "⚠️ Flint currently only supports tests for blocks and items. Testing for [requested subject] isn't available yet but may be added in the future. Please choose a block or item to test instead."

Do not proceed until the user provides a supported subject.

---

### Step 3: Explore the Minecraft Source Code

Navigate the decompiled Minecraft source (path collected during setup) to understand how the requested block or item is implemented. Look for:
- The block/item class and its superclass chain
- All overridden methods and what they do (placement, interactions, ticks, drops, states, etc.)
- Block states and their possible values

**Normal behaviour** — what the block/item does under expected, everyday conditions:
- How it is placed and what state it starts in
- How it responds to player interaction
- What it drops when broken with the correct tool
- How it behaves over time (ticks, growth, decay, etc.)

**Edge cases** — situations where behaviour might differ or break:
- Breaking with the wrong tool or no tool
- Placement in invalid or unusual locations (underwater, mid-air, on non-solid blocks, etc.)
- Interaction in unexpected conditions (powered by redstone, waterlogged, in the Nether/End, etc.)
- Behaviour at world boundaries or in unusual game modes
- Any null checks, guard clauses, or special-cased conditions visible in the code

Build a complete picture of both the happy path and the failure modes before proceeding.

---

### Step 4: Identify Existing Tests

Search the current directory for any test files already covering this block or item:

```bash
ls *.json | xargs grep -l "<block_or_item_name>" 2>/dev/null
```

Read each matching file to understand what is already covered.

---

### Step 5: Propose Test Cases

Based on your source code exploration and the existing tests, compile a list of test cases that should exist. Present them to the user as plain factual descriptions — not code, not JSON — one per line. For example:

> "Here are the test cases I identified for [block/item]:
> 1. Placing the block in a valid position registers it as the correct block type
> 2. Breaking the block with the correct tool drops the expected item
> 3. Breaking without the correct tool yields no drop
> 4. [etc.]
>
> Already covered: [list any from existing files, or "none"]
>
> Shall I implement all of these, or would you like to adjust the list first?"

Wait for the user to confirm or modify the list before writing anything.

---

### Step 6: Implementation Loop

For each confirmed test case, one at a time:

#### 6a. Write the Test

Write the test as a JSON file in the current directory, following:
- The structure defined in `test_spec.rs`
- The naming conventions from the example files read during setup

#### 6b. Run the Test

Execute the test using flintcli:

```bash
<FLINTCLI_BINARY> <test_file.json> -s localhost:25565
```

Where `<FLINTCLI_BINARY>` is the path to the flintcli binary provided during setup.

#### 6c. Evaluate the Result

**If the test passes:** Confirm to the user, then move on to the next test case.

**If the test fails:**
1. Read the failure output carefully
2. Go back to the decompiled Minecraft source and re-examine the relevant behavior
3. Correct the test JSON based on what the code actually does
4. Re-run and repeat from 6b until the test passes

Do not move to the next test case until the current one passes **and** correctly reflects the actual Minecraft behavior (not just shaped to pass).

---

### Step 7: Completion

Once all test cases have passed, summarize the session:

> "All [N] tests for [block/item] are written and passing:
> - [test file name] — [one-line description]
> - ...
>
> Would you like to write tests for another block or item?"
