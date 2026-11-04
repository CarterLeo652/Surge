import tempfile
import unittest
from pathlib import Path

from astrbot_plugin_keyword_reply.rules import MessageFacts, matches, validate_rule
from astrbot_plugin_keyword_reply.storage import RuleStore


def sample(**overrides):
    rule = {
        "group_id": "-100123", "pattern": "菜单", "mode": "contains",
        "targets": ["text"], "templates": [{"kind": "text", "text": "今日菜单"}],
    }
    rule.update(overrides)
    return validate_rule(rule)


class RuleTests(unittest.TestCase):
    def test_rule_name_is_saved_without_affecting_matching(self):
        with tempfile.TemporaryDirectory() as folder:
            store = RuleStore(Path(folder))
            saved = store.replace([sample(name="本群专属订阅")])
            self.assertEqual(saved[0]["name"], "本群专属订阅")
            self.assertEqual(RuleStore(Path(folder)).rules[0]["name"], "本群专属订阅")
            self.assertTrue(matches(saved[0], MessageFacts(text="菜单")))
            self.assertFalse(matches(saved[0], MessageFacts(text="本群专属订阅")))

    def test_match_modes_and_targets(self):
        self.assertTrue(matches(sample(), MessageFacts(text="看看菜单吧")))
        self.assertFalse(matches(sample(mode="exact"), MessageFacts(text="看看菜单吧")))
        self.assertTrue(matches(sample(mode="exact"), MessageFacts(text="菜单")))
        self.assertTrue(matches(sample(pattern=r"^账单\d+$", mode="regex"), MessageFacts(text="账单42")))
        self.assertTrue(matches(sample(pattern="report", targets=["filename"]), MessageFacts(filename="Report.pdf")))
        self.assertFalse(matches(sample(pattern="report", targets=["filename"]), MessageFacts(text="report")))

    def test_multiple_targets_are_independent(self):
        rule = sample(pattern="report", targets=["text", "filename", "sticker_emoji"])
        self.assertTrue(matches(rule, MessageFacts(text="report")))
        self.assertTrue(matches(rule, MessageFacts(filename="report.pdf")))
        self.assertTrue(matches(rule, MessageFacts(sticker_emoji="report")))
        self.assertFalse(matches(rule, MessageFacts(text="other", filename="other.pdf")))
        with self.assertRaises(ValueError):
            validate_rule({"group_id": "-100123", "pattern": "report", "targets": [],
                           "templates": [{"kind": "text", "text": "回复"}]})

    def test_forward_topic_and_disabled(self):
        self.assertFalse(matches(sample(ignore_forward=True), MessageFacts(text="菜单", forwarded=True)))
        self.assertFalse(matches(sample(topic_id="10"), MessageFacts(text="菜单", topic_id="11")))
        self.assertFalse(matches(sample(enabled=False), MessageFacts(text="菜单")))

    def test_bad_rule_rejected_without_dropping_saved_rules(self):
        with tempfile.TemporaryDirectory() as folder:
            store = RuleStore(Path(folder))
            good = store.replace([sample()])
            with self.assertRaises(ValueError):
                store.replace([sample(pattern="(", mode="regex")])
            self.assertEqual(store.rules, good)
            self.assertEqual(RuleStore(Path(folder)).rules, good)

    def test_unknown_media_rejected(self):
        with self.assertRaises(ValueError):
            validate_rule(
                {"group_id": "-100123", "pattern": "菜单", "templates": [{"kind": "photo", "media_id": "a" * 32}]},
                allowed_media=set(),
            )


if __name__ == "__main__":
    unittest.main()
