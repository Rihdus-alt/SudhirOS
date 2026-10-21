import json
import os
import re
import subprocess
import sys
import unittest

from _load import ROOT

SKILLS = ["india-copycat", "india-news", "india-budget", "india-insider", "india-analyst", "india-screener",
          "india-risk", "india-broker", "india-tax", "india-alerts", "india-rebalance", "india-costs",
          "india-fno-scan", "india-fno-plan", "india-fno-chain", "india-fno-backtest"]
TOOLS = {"india-copycat": "copycat", "india-news": "news", "india-budget": "size", "india-insider": "insider",
         "india-analyst": "results", "india-screener": "screen", "india-risk": "check", "india-broker": "ticket",
         "india-tax": "lots", "india-alerts": "alerts", "india-rebalance": "rebalance", "india-costs": "costs",
         "india-fno-scan": "scan", "india-fno-plan": "plan", "india-fno-chain": "chain", "india-fno-backtest": "backtest"}
ORDER_PATH = ["india-copycat", "india-budget", "india-insider", "india-risk", "india-broker", "india-tax",
              "india-alerts", "india-rebalance", "india-fno-scan", "india-fno-plan"]
REPO = os.path.dirname(os.path.dirname(ROOT))


def frontmatter(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return (m.group(1) if m else ""), text


def text_files():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for f in files:
            if f.endswith((".md", ".py", ".json", ".csv", ".txt", ".xml")) or f == "LICENSE":
                yield os.path.join(base, f)


class Repo(unittest.TestCase):
    def test_sixteen_skill_folders(self):
        found = sorted(d for d in os.listdir(ROOT) if os.path.isfile(os.path.join(ROOT, d, "SKILL.md")))
        self.assertEqual(found, sorted(SKILLS))
        root = sorted(d for d in os.listdir(ROOT) if not d.startswith("."))
        self.assertEqual(root, sorted(["LICENSE", "README.md", "tests"] + SKILLS))

    def test_frontmatter(self):
        for s in SKILLS:
            fm, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertIn("name: %s\n" % s, fm + "\n", s)
            self.assertRegex(fm, r"description: ", s)
            self.assertGreater(len(fm), 300, "%s description is too thin" % s)
            self.assertLess(len(fm), 1400, "%s description is too long" % s)
            self.assertGreater(len(text.splitlines()), 60, s)

    def test_skill_commands_name_real_tools(self):
        for s in SKILLS:
            _, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            for tool in re.findall(r'\$\{CLAUDE_SKILL_DIR\}/([\w.\-]+)', text):
                self.assertTrue(os.path.exists(os.path.join(ROOT, s, tool)), "%s mentions %s" % (s, tool))

    def test_order_path_skills_carry_the_safety_rule(self):
        for s in ORDER_PATH:
            _, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            low = " ".join(text.lower().split())
            self.assertIn("not financial advice", low, s)
            self.assertIn("explicit yes", low, s)
            self.assertIn("needs approval", low, s)
            self.assertIn("never ask for a password", low, s)

    def test_every_tool_has_help(self):
        for s, t in TOOLS.items():
            r = subprocess.run([sys.executable, os.path.join(ROOT, s, t + ".py"), "--help"], capture_output=True,
                               text=True, timeout=60)
            self.assertEqual(r.returncode, 0, "%s: %s" % (s, r.stderr))

    def test_plugin_manifest(self):
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json")) as fh:
            plugin = json.load(fh)
        self.assertEqual(plugin["name"], "trader-skill-india")
        self.assertEqual(plugin["skills"], "./")

    @unittest.skipUnless(os.path.exists(os.path.join(REPO, ".claude-plugin", "marketplace.json")),
                         "not inside the SudhirOS repository (an installed copy)")
    def test_sudhiros_marketplace(self):
        with open(os.path.join(REPO, ".claude-plugin", "marketplace.json")) as fh:
            market = json.load(fh)
        self.assertEqual(market["name"], "sudhiros")
        by = {p["name"]: p for p in market["plugins"]}
        self.assertEqual(by["trader-skill-india"]["source"], "./plugins/trader-skill-india")
        self.assertTrue(os.path.isdir(os.path.join(REPO, by["trader-skill-india"]["source"])))
        self.assertEqual(by["trader-skill"]["source"], {"source": "github", "repo": "Jakeschincariol/trader-skill"})
        with open(os.path.join(REPO, ".claude", "settings.json")) as fh:
            settings = json.load(fh)
        self.assertIn("trader-skill", settings["extraKnownMarketplaces"])
        self.assertIs(settings["enabledPlugins"]["trader-skill-india@sudhiros"], True)
        self.assertFalse(any("place_order" in a for a in settings.get("permissions", {}).get("allow", [])))
        # every tool that can create, change or cancel an order (GTTs are standing orders) asks first
        for tool in ("place_order", "modify_order", "cancel_order", "place_gtt_order", "modify_gtt_order",
                     "delete_gtt_order"):
            self.assertIn("mcp__kite__" + tool, settings["permissions"]["ask"])

    @unittest.skipUnless(os.path.exists(os.path.join(REPO, "scripts", "build_claude_ai_skill.py")),
                         "not inside the SudhirOS repository (an installed copy)")
    def test_claude_ai_bundle(self):
        import tempfile
        import zipfile
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "trader-india.zip")
            r = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "build_claude_ai_skill.py"),
                                "--out", out], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            with zipfile.ZipFile(out) as z:
                names = z.namelist()
                z.extractall(tmp)
            self.assertTrue(all(n.startswith("trader-india/") for n in names))
            self.assertFalse(any("/tests/" in n or n.endswith(".pyc") for n in names))
            fm, text = frontmatter(os.path.join(tmp, "trader-india", "SKILL.md"))
            self.assertIn("name: trader-india\n", fm + "\n")
            desc = " ".join(fm.split("description: >-", 1)[1].split())
            self.assertLessEqual(len(desc), 1024)
            for s in SKILLS:
                self.assertTrue(os.path.isfile(os.path.join(tmp, "trader-india", s, "SKILL.md")), s)
                self.assertIn("`%s/`" % s, text, s)
            r = subprocess.run([sys.executable, "-I", os.path.join(tmp, "trader-india", "india-fno-scan", "scan.py"),
                                "--help"], capture_output=True, text=True, timeout=60, cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_readme(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = " ".join(fh.read().split())
        self.assertTrue(readme.startswith("# The Trader skill, India edition"))
        for line in ("/plugin marketplace add rihdus-alt/SudhirOS", "/plugin install trader-skill-india@sudhiros",
                     "## Fine print", "Not financial advice", "Jakeschincariol/trader-skill"):
            self.assertIn(line, readme)
        for s in SKILLS:
            self.assertIn("/" + s, readme, s)

    def test_no_em_dashes_and_no_secrets(self):
        for path in text_files():
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            rel = os.path.relpath(path, ROOT)
            self.assertNotIn("\u2014", text, rel)
            self.assertNotRegex(text, r"(?i)(api_key|secret|access_token)\"\s*:\s*\"[A-Za-z0-9]{16,}", rel)

    def test_safe_defaults(self):
        with open(os.path.join(ROOT, "india-budget", "config.example.json")) as fh:
            cfg = json.load(fh)
        self.assertIs(cfg["paper"], True)
        self.assertEqual(cfg["broker"], "manual")
        self.assertEqual(cfg["product"], "CNC")
        for k in ("allow_fno", "allow_intraday", "allow_mtf", "allow_short"):
            self.assertIs(cfg[k], False, k)
        self.assertIs(cfg["block_surveillance"], True)

    def test_fixture_readme_labels_real_and_synthetic(self):
        with open(os.path.join(ROOT, "tests", "fixtures", "README.md")) as fh:
            text = fh.read()
        self.assertIn("**Real**", text)
        self.assertIn("**Synthetic", text)


if __name__ == "__main__":
    unittest.main()
