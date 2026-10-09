import json
import tempfile
import unittest
from pathlib import Path

from runner import subjects as s

ROOT = Path(__file__).resolve().parent.parent


def write_tags(root: Path, kind: str, tags: dict[str, list]) -> None:
    for name, values in tags.items():
        f = root / kind / f"{name}.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"values": values}))


class TagTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        write_tags(self.tmp, "block", {
            "wooden_buttons": ["minecraft:oak_button", "minecraft:birch_button"],
            "buttons": ["#minecraft:wooden_buttons", "minecraft:stone_button"],
            "mineable/axe": ["#minecraft:wooden_buttons", "minecraft:oak_log"],
            "frog_prefer_jump_to": ["minecraft:lily_pad", "minecraft:big_dripleaf"],
            "loop_a": ["#minecraft:loop_b"], "loop_b": ["#minecraft:loop_a", "minecraft:x"],
        })
        write_tags(self.tmp, "item", {"buttons": [{"id": "minecraft:jungle_button", "required": False}]})
        self.tags = s.load_tags(self.tmp)

    def test_refs_resolved_and_block_item_merged(self):
        self.assertEqual(self.tags["buttons"], {"oak_button", "birch_button", "stone_button", "jungle_button"})

    def test_missing_tags_dir_fails_loudly(self):
        with self.assertRaises(FileNotFoundError):
            s.load_tags(self.tmp / "nope")

    def test_ref_cycle_terminates(self):
        self.assertEqual(self.tags["loop_a"], {"x"})

    def test_family_tags_drop_behavior_tags(self):
        ids = {"oak_button", "birch_button", "stone_button", "oak_log", "lily_pad", "big_dripleaf"}
        fams = s.family_tags(self.tags, ids)
        self.assertIn("buttons", fams)
        self.assertNotIn("mineable/axe", fams)
        self.assertNotIn("frog_prefer_jump_to", fams)

    def test_largest_family_wins(self):
        groups = s.candidate_groups(["oak_button", "birch_button", "stone_button"], self.tags)
        self.assertEqual(groups, {"buttons": ["oak_button", "birch_button", "stone_button"]})


class NameRuleTests(unittest.TestCase):
    def test_last_word_prefix_generic_single(self):
        ids = ["white_stained_glass_pane", "red_stained_glass_pane", "glass_pane",
               "grass_block", "bone_block", "dirt_block",
               "potted_oak_sapling", "potted_cactus",
               "waxed_copper_bulb", "copper_bulb", "exposed_copper_bulb", "bell"]
        g = s.candidate_groups(ids, {})
        self.assertEqual(g["pane"], ["white_stained_glass_pane", "red_stained_glass_pane", "glass_pane"])
        self.assertEqual(g["bulb"], ["waxed_copper_bulb", "copper_bulb", "exposed_copper_bulb"])
        self.assertEqual(g["potted"], ["potted_oak_sapling", "potted_cactus"])
        for single in ("grass_block", "bone_block", "dirt_block", "bell"):
            self.assertEqual(g[single], [single])


class FileTests(unittest.TestCase):
    def test_read_ids_filters_spawn_eggs_comments_dupes(self):
        f = Path(tempfile.mkdtemp()) / "ids.txt"
        f.write_text("minecraft:stone\n# c\nminecraft:zombie_spawn_egg\nminecraft:stone\n\nminecraft:bell  # x\n")
        self.assertEqual(s.read_ids(f), ["stone", "bell"])

    def test_priority_missing_file(self):
        self.assertEqual(s.read_priority(Path("/nonexistent/priority.txt")), [])


class OrderTests(unittest.TestCase):
    groups = {"buttons": ["oak_button", "stone_button"], "doors": ["oak_door"], "bell": ["bell"]}
    ids = ["bell", "oak_button", "oak_door", "stone_button"]

    def test_file_order_by_first_member(self):
        order, warnings = s.order_subjects(self.groups, self.ids, [])
        self.assertEqual(order, ["bell", "buttons", "doors"])
        self.assertEqual(warnings, [])

    def test_priority_by_id_and_name_and_unknown(self):
        order, warnings = s.order_subjects(self.groups, self.ids, ["minecraft:oak_door", "buttons", "typo"])
        self.assertEqual(order, ["doors", "buttons", "bell"])
        self.assertEqual(len(warnings), 1)


class GroupStateTests(unittest.TestCase):
    def test_validate_groups(self):
        ids = ["a", "b", "c"]
        self.assertTrue(s.validate_groups({"x": ["a", "minecraft:b"], "c": ["c"]}, ids))
        self.assertFalse(s.validate_groups({"x": ["a", "b"]}, ids))              # dropped c
        self.assertFalse(s.validate_groups({"x": ["a", "b", "c", "c"]}, ids))    # duplicate
        self.assertFalse(s.validate_groups({"Bad Name": ["a", "b", "c"]}, ids))
        self.assertFalse(s.validate_groups(None, ids))

    def test_frozen_groups_only_get_new_ids(self):
        groups = {"buttons": ["oak_button"], "bell": ["bell"]}
        new = s.add_new_ids(groups, ["oak_button", "bell", "chain"], {})
        self.assertEqual(new, ["chain"])
        self.assertEqual(groups, {"buttons": ["oak_button"], "bell": ["bell"], "chain": ["chain"]})

    def test_lifecycle(self):
        state = {}
        self.assertTrue(s.selectable("x", state))
        s.record(state, "x", "gave_up")
        self.assertTrue(s.selectable("x", state))
        s.record(state, "x", "gave_up")
        self.assertFalse(s.selectable("x", state))
        s.record(state, "y", "unsupported")
        self.assertFalse(s.selectable("y", state))


class RealConfigTests(unittest.TestCase):
    """Sanity check on the real config + 26.2 tags: every id lands in exactly one subject."""

    def test_real_config_groups_cover_all_ids(self):
        ids_file = ROOT / "config" / "implemented.txt"
        tags_dir = ROOT / "minecraft_26.2" / "data" / "minecraft" / "tags"
        if not ids_file.exists() or not tags_dir.is_dir():
            self.skipTest("no config/implemented.txt or minecraft_26.2/")
        ids = s.read_ids(ids_file)
        groups = s.candidate_groups(ids, s.load_tags(tags_dir))
        self.assertTrue(s.validate_groups(groups, ids))
        self.assertLess(len(groups), len(ids))


if __name__ == "__main__":
    unittest.main()
