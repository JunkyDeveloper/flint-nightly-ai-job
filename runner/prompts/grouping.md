Below is a computed grouping of Minecraft block/item ids (without the `minecraft:` prefix) into test subjects. Each subject becomes one nightly job and one PR. Within a subject, the test writer tests one representative per distinct implementation, so a group may contain several implementations. What matters is that members are related, so one person reviewing one PR sees one topic.

Review it:
- Split a group whose members are unrelated (e.g. different mechanics that only share a word in their name).
- Merge single-id subjects into an existing group only when they clearly belong to it.
- Keep names short, lowercase, only `a-z 0-9 _ . -`. Keep existing names unless they are misleading.
- Every id must appear exactly once. Do not add or drop ids.

Reply with ONLY one JSON object mapping subject name → list of ids, the complete grouping.

Grouping:
$candidates
