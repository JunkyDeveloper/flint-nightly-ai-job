Output ONLY one JSON object, no prose, describing the test files you wrote and kept in `./tests/`:

{"tests": [{"file": "tests/<path>.json", "representative": "minecraft:<id>", "implementation": "<ClassName>", "covers": ["minecraft:<other id>", "..."], "case": "<what this test checks, one sentence>"}],
 "not_tested": [{"id": "minecraft:<id>", "reason": "unsupported | covered | gave_up", "note": "<short reason>"}]}

`covers` lists the other members with the same implementation as the representative (no tests written for them). One entry in `tests` per test file.
