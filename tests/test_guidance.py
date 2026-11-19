"""Bundled starter data, user-supplied collections and data provenance."""

import json
import tempfile
import unittest
from importlib.resources import files
from pathlib import Path

from xbloom_studio import (
    check_recipe,
    encode_coffee_recipe,
    recipe_tea_references,
    recipe_templates,
    validate_tea_recipe,
)
from xbloom_studio.coffee_guidance import (
    load_corpus,
    rank_examples,
    recipe_examples,
    tokens,
)
from xbloom_studio.recipes import compile_recipe
from xbloom_studio.tea_guidance import FAMILIES, load_tea_corpus

PROJECT_AUTHORED = {
    "kind": "project-authored",
    "license": "MIT",
    "derived_from": None,
}


def packaged_data():
    root = files("xbloom_studio").joinpath("data")
    pending = [root]
    while pending:
        node = pending.pop()
        for child in node.iterdir():
            if child.is_dir():
                pending.append(child)
            else:
                yield child


class ProvenanceTest(unittest.TestCase):
    def test_every_packaged_data_file_is_project_authored(self):
        names = []
        for resource in packaged_data():
            names.append(resource.name)
            self.assertTrue(resource.name.endswith(".json"), resource.name)
            data = json.loads(resource.read_text(encoding="utf-8"))
            provenance = data["provenance"]
            self.assertEqual(
                {key: provenance[key] for key in PROJECT_AUTHORED}, PROJECT_AUTHORED
            )
            text = json.dumps(data).casefold()
            for marker in ("http://", "https://", "source_url", "retrieved"):
                self.assertNotIn(marker, text, resource.name)
        self.assertEqual(
            sorted(names),
            [
                "coffee-examples.json",
                "starter-dark.json",
                "starter-light.json",
                "starter-medium.json",
                "tea-references.json",
            ],
        )


class TemplateTest(unittest.TestCase):
    def test_templates_are_valid_detached_and_encodable(self):
        templates = recipe_templates()
        self.assertEqual(
            [t["id"] for t in templates],
            ["starter-dark", "starter-light", "starter-medium"],
        )
        for template in templates:
            report = check_recipe(template["document"])
            self.assertTrue(report["valid"], report["errors"])
            encoded = encode_coffee_recipe(template["document"])
            self.assertEqual(encoded.cup_limits, (90, 110))
            self.assertEqual(
                compile_recipe(template["document"])["requires_untested"], True
            )
        templates[0]["document"]["name"] = "Changed"
        self.assertNotEqual(recipe_templates()[0]["document"]["name"], "Changed")


class CoffeeExampleTest(unittest.TestCase):
    def test_bundled_examples_rank_by_overlap(self):
        result = recipe_examples({"origin": "Ethiopia", "process": "Washed"}, limit=3)
        self.assertEqual(result["source"], "bundled")
        self.assertEqual(result["corpus_version"], 1)
        examples = result["examples"]
        self.assertEqual(examples[0]["example"]["id"], "starter-washed-highland")
        self.assertEqual(
            examples[0]["matched"], {"origin": ["ethiopia"], "process": ["washed"]}
        )
        self.assertEqual(len(examples), 3)
        scores = [e["score"] for e in examples]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(recipe_examples({"roast": "Light"})["examples"], [])
        self.assertEqual(
            recipe_examples({"variety": "SL-28"}, water_ml=240)["examples"][0][
                "example"
            ]["id"],
            "starter-sl-kenya",
        )
        self.assertEqual(
            recipe_examples({"origin": "Kenya"}, water_ml=999)["examples"], []
        )
        for record in load_corpus()["records"]:
            self.assertEqual(
                sum(p["volume_ml"] for p in record["brew"]["pours"]),
                record["brew"]["water_ml"],
            )

    def test_tokens_normalize_processes_and_varieties(self):
        self.assertEqual(
            tokens("Anaerobic natural with yeast inoculation", "process"),
            {"anaerobic", "natural", "yeast"},
        )
        self.assertEqual(
            tokens("Fully washed, Moonshine", "process"), {"washed", "moonshine"}
        )
        self.assertEqual(
            tokens(["Gesha", "SL 28", "Ethiopian landrace"], "variety"),
            {"geisha", "sl28", "heirloom/landrace"},
        )
        self.assertEqual(tokens("Unknown", "origin"), set())
        self.assertEqual(tokens("Café, N/A", "origin"), {"cafe"})

    def test_ranking_scores_conflicts_and_arguments(self):
        records = [
            {
                "id": 2,
                "coffee": {"origin": "Colombia", "process": "Washed"},
                "brew": {},
            },
            {
                "id": "b",
                "coffee": {"origin": "Colombia", "process": "Natural"},
                "brew": {},
            },
            {
                "id": 1,
                "coffee": {"origin": "Colombia", "process": "Washed"},
                "brew": {},
            },
            {"id": 3, "coffee": {"origin": "", "variety": "Castillo"}, "brew": {}},
        ]
        ranked = rank_examples(
            records, {"origin": "Colombia", "process": "washed", "variety": "Caturra"}
        )
        self.assertEqual([r["example"]["id"] for r in ranked], [1, 2, "b"])
        self.assertEqual(
            ranked[2]["conflicting"]["process"],
            {"requested_only": ["washed"], "example_only": ["natural"]},
        )
        self.assertEqual(ranked[0]["unknown"], ["variety"])
        mixed = rank_examples(records, {"origin": "Colombia"})
        self.assertEqual([r["example"]["id"] for r in mixed], [1, 2, "b"])
        for args in (
            ([], {"bad": "x"}),
            ([], {"origin": 1}),
            ([], {"origin": ["x", 1]}),
            ([], {"origin": "x"}, None, 0),
            ([], {"origin": "x"}, None, True),
            ([], {"origin": "x"}, float("nan")),
            ([], {"origin": "x"}, True),
            ([], {"origin": "x"}, 0),
            ([], None),
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                rank_examples(*args)

    def test_user_supplied_collections(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "examples.json"
            corpus = {
                "version": 7,
                "scope": "Mine",
                "records": [
                    {"id": 1, "coffee": {"origin": "Peru"}, "brew": {"water_ml": 250}},
                ],
            }
            path.write_text(json.dumps(corpus))
            result = recipe_examples({"origin": "peru"}, path=path)
            self.assertEqual(
                (result["source"], result["corpus_version"]), ("user_supplied", 7)
            )
            self.assertEqual(result["examples"][0]["example"]["id"], 1)
            for bad in (
                [],
                {"records": {}},
                {"records": [1]},
                {"records": [{"id": True, "coffee": {}, "brew": {}}]},
                {"records": [{"id": 1, "coffee": {"a": 1}, "brew": {}}]},
                {"records": [{"id": 1, "coffee": {}}]},
            ):
                path.write_text(json.dumps(bad))
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    recipe_examples({"origin": "peru"}, path=path)
            with self.assertRaisesRegex(ValueError, "bounded"):
                load_corpus(Path(tmp) / "missing.json")


class TeaReferenceTest(unittest.TestCase):
    def test_bundled_profiles_are_valid_two_steep_drafts(self):
        corpus = load_tea_corpus()
        self.assertEqual(corpus["records"], [])
        self.assertEqual(
            sorted({p["family"] for p in corpus["profiles"]}), sorted(FAMILIES)
        )
        for profile in corpus["profiles"]:
            report = validate_tea_recipe(profile["document"], 240)
            self.assertTrue(report["valid"], (profile["id"], report["errors"]))
            self.assertEqual(
                compile_recipe(profile["document"])["execution"][0]["kind"], "tea"
            )
        result = recipe_tea_references()
        self.assertEqual(result["available_families"], list(FAMILIES))
        self.assertEqual([f["family"] for f in result["families"]], list(FAMILIES))
        self.assertFalse(result["yield_verified"])
        self.assertEqual(result["source"], "bundled")
        green = recipe_tea_references("green")["families"][0]
        self.assertEqual(green["source_record_count"], 0)
        self.assertEqual(green["published_ranges"]["ranges"], {})
        self.assertEqual(
            green["adapted_draft_ranges"]["ranges"]["temperature_c"],
            {"minimum": 80, "maximum": 80},
        )
        herbal = recipe_tea_references("herbal")["families"][0]
        self.assertNotIn("temperature_c", herbal["adapted_draft_ranges"]["ranges"])
        green["profiles"][0]["document"]["tea"]["dose_g"] = 999
        self.assertNotEqual(
            recipe_tea_references("green")["families"][0]["profiles"][0]["document"][
                "tea"
            ]["dose_g"],
            999,
        )
        for invalid in ("Green", "unknown", "", [], {}, False, 1):
            with self.assertRaisesRegex(ValueError, "green, black"):
                recipe_tea_references(invalid)

    def test_user_supplied_references_with_published_records(self):
        profile = load_tea_corpus()["profiles"][0]
        record = {
            "family": "green",
            "source_id": "mine-1",
            "published": {
                "dose_g": 4,
                "soak_volumes_ml": [80, 90],
                "temperatures_c_as_published": [75, 79.5],
                "steep_seconds": [40, 50],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tea.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "records": [record],
                        "profiles": [profile],
                        "limitations": ["Mine"],
                    }
                )
            )
            result = recipe_tea_references("green", path=path)
            family = result["families"][0]
            self.assertEqual(
                (result["version"], result["source"]), (2, "user_supplied")
            )
            self.assertEqual(family["source_ids"], ["mine-1"])
            self.assertEqual(
                family["published_ranges"]["ranges"]["temperature_c"],
                {"minimum": 75, "maximum": 79.5},
            )
            self.assertEqual(result["limitations"], ["Mine"])
            for bad in (
                [],
                {"profiles": {}},
                {"records": {}, "profiles": []},
                {"profiles": [{"family": "matcha"}]},
                {"profiles": [{"family": "green", "document": {}}]},
                {
                    "profiles": [
                        {"family": "green", "document": {"tea": {"steeps": {}}}}
                    ]
                },
                {"records": [1], "profiles": []},
            ):
                path.write_text(json.dumps(bad))
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    recipe_tea_references(path=path)
