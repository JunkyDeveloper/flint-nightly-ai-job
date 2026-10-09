The plan is approved. Reviewer notes: $notes

Now implement it, following the skill's implementation loop:
- Write each test as JSON under `./tests/`, in the folder that fits the existing layout, following the existing naming conventions.
- Run each test with flintcli against the server until it passes **and** matches the behavior in the decompiled source. Never shape a test just to pass.
- Also run `flint-validate <file.json>` on every test (CI runs it). It checks rules flintcli doesn't, e.g. the cleanup region must contain the origin [0,0,0]. Fix until it passes.
- At most 5 fix attempts per test. After that, delete that test file and move on.
- Write tests only for the representatives from the plan.

When all cases are done, end your turn with a short list: which test files you wrote, and which cases you gave up on and why.
