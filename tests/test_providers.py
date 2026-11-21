"""User-directed provider imports with stubbed HTTP and synthetic sources.

Every source payload here is synthetic, shaped like the supported formats;
no response captured from a service is used.
"""

import io
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

import tests  # noqa: F401 - installs the test guards
from xbloom_studio import encode_coffee_recipe, encode_tea_recipe
from xbloom_studio.provider_exports import MAX_EXPORT, load_export, read_export
from xbloom_studio.provider_native import native_document
from xbloom_studio.providers import (
    MAX_RESPONSE,
    SHARE_OMITTED,
    HubProviders,
    NoRedirects,
    ProviderError,
    RecipeImports,
)
from xbloom_studio.storage import ConflictError, Store

HOST_RAW = {
    "recipeName": "Synthetic public recipe",
    "model": "Studio",
    "dose": 15,
    "grinderSize": 42,
    "rpm": 100,
    "pourList": [{"volume": 50, "temperature": 95, "pausing": 30, "pattern": 3}],
}


def native_pour(volume, temperature, pausing, pattern=1, flow=3.0, before=2, after=2):
    return {
        "flowRate": flow,
        "isEnableVibrationAfter": after,
        "isEnableVibrationBefore": before,
        "pattern": pattern,
        "pausing": pausing,
        "temperature": temperature,
        "theName": "Pour",
        "volume": volume,
    }


def native_coffee(**changes):
    recipe = {
        "adaptedModel": 1,
        "cupType": 2,
        "dose": 15,
        "grandWater": 15,
        "grinderSize": 55,
        "rpm": 90,
        "isSetGrinderSize": 1,
        "isEnableBypassWater": 1,
        "bypassTemp": 96,
        "bypassVolume": 20,
        "theName": "Synthetic native coffee",
        "podsVo": {"origin": "Synthetic"},
        "pourCount": 2,
        "totalBrewWater": 225,
        "pourList": [
            native_pour(45, 98, 30, pattern=3, before=1),
            native_pour(180, 92, 0, pattern=2, flow=3.5, after=1),
        ],
    }
    recipe.update(changes)
    return {"recipeVo": recipe}


def single(pours):
    return native_coffee(pourList=pours, pourCount=1, totalBrewWater=None)


def native_tea(**changes):
    recipe = {
        "adaptedModel": 1,
        "cupType": 4,
        "dose": 3,
        "grandWater": 42.5,
        "grinderSize": 50,
        "rpm": 120,
        "isSetGrinderSize": 2,
        "isEnableBypassWater": 2,
        "theName": "Synthetic native tea",
        "pourList": [native_pour(90, 39, 20), native_pour(80, 99, 75, flow=3.5)],
    }
    recipe.update(changes)
    return {"recipeVo": recipe}


def firestore(value):
    if isinstance(value, dict):
        return {"mapValue": {"fields": {k: firestore(v) for k, v in value.items()}}}
    if isinstance(value, list):
        return {"arrayValue": {"values": [firestore(v) for v in value]}}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if value is None:
        return {"nullValue": None}
    return {"stringValue": value}


def xrecipe_document(name="recipes_master/synthetic-1"):
    data = deepcopy(native_coffee()["recipeVo"])
    return {
        "name": "projects/xbloom-recipe-app/databases/(default)/documents/" + name,
        "fields": firestore({"name": "Synthetic export", "xBloomData": data})[
            "mapValue"
        ]["fields"],
    }


class Stub:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, url, data=None):
        self.calls.append((url, data))
        for key, value in self.responses.items():
            if key in url:
                return deepcopy(value)
        raise AssertionError(url)


class ProviderTest(unittest.TestCase):
    def setUp(self):
        self.stub = Stub(
            {
                "recipe/detail": {"code": 200, "data": HOST_RAW},
                "index/page": {"code": 200, "data": {"list": [{"id": 1}], "total": 1}},
                "firestore": xrecipe_document(),
            }
        )
        self.hubs = HubProviders(self.stub)

    def test_collective_read_search_and_host_adaptation(self):
        source = self.hubs.read("https://collective.xbloom.com/recipe/12?type=2")
        self.assertEqual(self.stub.calls[-1][1], {"id": 12, "type": 2})
        self.assertTrue(self.stub.calls[-1][0].startswith("https://collective-api."))
        document = self.hubs.normalize(source, flow_ml_s=3)
        self.assertEqual(document["steps"][0]["kind"], "user")
        self.assertEqual(document["steps"][1]["pattern"], "ring")
        self.assertEqual(document["steps"][2], {"kind": "wait", "duration_s": 30})
        self.assertEqual(document["source"]["raw"], HOST_RAW)
        self.assertEqual(document["source"]["provider_id"], "12")
        found = self.hubs.search("geisha", page=2)
        self.assertEqual(found["results"], [{"id": 1}])
        self.assertEqual(self.stub.calls[-1][1]["pageIndex"], 2)
        for bad in (0, True, 1.5):
            with self.assertRaises(ValueError):
                self.hubs.search("x", page=bad)
        with self.assertRaises(ProviderError):
            self.hubs.search("x" * 257)
        for response in ([], {"code": 500}, {"code": 200}, {"code": 200, "data": {}}):
            with self.subTest(response=response), self.assertRaises(ProviderError):
                HubProviders(lambda *a, r=response: r).search("x")
        for response in ([], {"code": 404}):
            with self.assertRaises(ProviderError):
                HubProviders(lambda *a, r=response: r).read(
                    "https://collective.xbloom.com/recipe/1"
                )
        for url in (
            "https://collective.xbloom.com/recipe/1?type=3",
            "https://collective.xbloom.com/recipes/1",
        ):
            with self.assertRaises(ProviderError):
                self.hubs.read(url)

    def test_host_adaptation_refuses_unknown_or_lossy_sources(self):
        source = {"raw": HOST_RAW, "provider": "test", "url": None}
        with self.assertRaises(ValueError):
            self.hubs.normalize(source)
        for extra in (
            {"model": "Other machine"},
            {"cupTypeInt": 4},
            {"cupTypeName": "TEA"},
            {"bypassVolume": 20},
            {"preVibration": 99},
            {"rpm": "fast"},
            {"xBloomData": []},
            {"pourList": []},
            {"pourList": [{"volume": 50, "temperature": 95, "pattern": 1, "x": 1}]},
            {"pourList": [{"volume": 50, "temperature": 95, "pattern": True}]},
            {
                "pourList": [
                    {"volume": 50, "temperature": 95, "pattern": 1, "pausing": -1}
                ]
            },
        ):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.hubs.normalize({**source, "raw": {**HOST_RAW, **extra}}, 3)
        with self.assertRaises(ProviderError):
            self.hubs.normalize({**source, "raw": []}, 3)
        with self.assertRaises(ProviderError):
            self.hubs.normalize({**source, "decode_error": "bad"}, 3)
        with self.assertRaises(ProviderError):
            self.hubs.normalize(source, 3, target="native")
        nested = self.hubs.normalize(
            {
                **source,
                "raw": {"xBloomData": {"name": "N", "pours": HOST_RAW["pourList"]}},
            },
            3,
        )
        self.assertEqual(nested["name"], "N")
        self.assertEqual(nested["steps"][0]["kind"], "water")
        with mock.patch("xbloom_studio.providers.SOURCE_PATTERNS", {1: "center"}):
            with self.assertRaisesRegex(ProviderError, "pattern"):
                self.hubs.normalize(source, 3)

    def test_xrecipe_reads_public_documents_only(self):
        source = self.hubs.read("https://xbloom-recipe-app.web.app/recipe/public-1/")
        self.assertIn("/documents/recipes_master/public-1", self.stub.calls[-1][0])
        self.assertEqual(source["raw"]["xBloomData"]["grinderSize"], 55)
        document = self.hubs.normalize(source, target="coffee")
        self.assertEqual(document["coffee_recipe"]["cup_type"], "xdripper")
        for url in (
            "https://xbloom-app.vercel.app/recipe/x?rev=2",
            "https://xbloom-app.vercel.app/recipe/scan:private",
        ):
            with self.assertRaises(ProviderError):
                self.hubs.read(url)
        for response in ({}, []):
            with self.assertRaises(ProviderError):
                HubProviders(lambda *a, r=response: r).read(
                    "https://xbloom-recipe-app.firebaseapp.com/recipe/x"
                )
        bad = HubProviders(lambda *a: {"fields": {"x": {"referenceValue": "y"}}})
        source = bad.read("https://xbloom-app.vercel.app/recipe/x")
        self.assertIsNone(source["raw"])
        with self.assertRaises(ProviderError):
            bad.normalize(source, 3)
        odd = HubProviders(lambda *a: {"fields": {"x": {"integerValue": "nan"}}})
        self.assertIn(
            "Unsupported",
            odd.read("https://xbloom-app.vercel.app/recipe/x")["decode_error"],
        )

    def test_origins_redirects_share_links_and_writes_are_refused(self):
        for url in (
            None,
            "http://collective.xbloom.com/recipe/1",
            "https://collective.xbloom.com.example.com/recipe/1",
            # Userinfo, built from parts so it is not read as an email address.
            "https://" + "@".join(("user", "collective.xbloom.com/recipe/1")),
            "https://127.0.0.1/recipe/1",
            "https://collective.xbloom.com:444/recipe/1",
            "https://collective.xbloom.com:x/recipe/1",
            "https://collective.xbloom.com/" + "a" * 2048,
            "https://collective-api.xbloom.com/communityRecipe/recipe/detail",
        ):
            with self.subTest(url=url), self.assertRaises(ProviderError):
                self.hubs.read(url)
        with self.assertRaisesRegex(ProviderError, "share links are not supported"):
            self.hubs.read("https://share-h5.xbloom.com/?id=abcdefgh")
        self.assertEqual(self.stub.calls, [])
        with self.assertRaises(ProviderError):
            NoRedirects().redirect_request(
                None, None, 302, "", {}, "https://example.com"
            )
        with self.assertRaises(ProviderError):
            self.hubs.online_write()
        capabilities = self.hubs.capabilities()
        self.assertFalse(capabilities["collective"]["native_settings"])
        self.assertEqual(capabilities["omitted"]["xbloom_share"], SHARE_OMITTED)

    def test_native_targets_on_collective_are_blocked_with_reason(self):
        source = self.hubs.read("https://collective.xbloom.com/recipe/12", native=True)
        self.assertEqual(source["native_decode_error"], SHARE_OMITTED)
        with self.assertRaisesRegex(ProviderError, "share links"):
            self.hubs.normalize(source, target="coffee")

    def test_firestore_values(self):
        value = {
            "arrayValue": {
                "values": [
                    {"integerValue": "5"},
                    {"doubleValue": 2.5},
                    {"nullValue": None},
                    {"booleanValue": True},
                    {"timestampValue": "2026-01-01T00:00:00Z"},
                    {"mapValue": {}},
                    {"arrayValue": {}},
                ]
            }
        }
        self.assertEqual(
            self.hubs._firestore(value),
            [5, 2.5, None, True, "2026-01-01T00:00:00Z", {}, []],
        )
        with self.assertRaises(ProviderError):
            self.hubs._firestore({"referenceValue": "other"})


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class HttpTest(unittest.TestCase):
    def opener(self, result):
        opener = mock.Mock()
        if isinstance(result, BaseException):
            opener.open.side_effect = result
        else:
            opener.open.return_value = FakeResponse(result)
        return mock.patch("xbloom_studio.providers.build_opener", return_value=opener)

    def test_deep_and_nonfinite_http_json_is_rejected(self):
        url = "https://collective-api.xbloom.com/communityRecipe/recipe/detail"
        for body in (b'{"x": NaN}', b'{"x": 1e900}', b"[" * 90 + b"0" + b"]" * 90):
            with self.subTest(body=body):
                with self.opener(body), self.assertRaisesRegex(ProviderError, "JSON"):
                    HubProviders()._json(url)

    def test_duplicate_json_keys_do_not_hide_invalid_values(self):
        url = "https://collective-api.xbloom.com/communityRecipe/recipe/detail"
        for first in (b"1", b"NaN", b"1e900", b"[" * 90 + b"0" + b"]" * 90):
            with (
                self.subTest(first=first),
                self.opener(b'{"nested":{"x":' + first + b',"x":0}}'),
                self.assertRaisesRegex(ProviderError, "JSON"),
            ):
                HubProviders()._json(url)

    def test_http_limit_applies_to_received_bytes(self):
        rows = [{"id": i, "description": "example"} for i in range(20)]
        response = {"code": 200, "data": {"list": rows, "total": len(rows)}}

        def encode():
            return json.dumps(response, separators=(",", ":")).encode()

        rows[-1]["description"] += "x" * (MAX_RESPONSE - len(encode()))
        body = encode()
        self.assertEqual(len(body), MAX_RESPONSE)
        with self.opener(body):
            self.assertEqual(len(HubProviders().search("sample")["results"]), 20)
        with self.opener(body + b" "), self.assertRaisesRegex(ProviderError, "2 MiB"):
            HubProviders().search("sample")

    def test_bounded_json_requests(self):
        hubs = HubProviders()
        url = "https://collective-api.xbloom.com/communityRecipe/recipe/detail"
        with self.opener(b'{"code": 200}') as build:
            self.assertEqual(hubs._json(url, {"id": 1}), {"code": 200})
        request = build.return_value.open.call_args.args[0]
        self.assertEqual(request.data, b'{"id": 1}')
        self.assertEqual(request.get_header("User-agent"), "xbloom-studio")
        self.assertIsNone(request.get_header("Referer"))
        self.assertIsInstance(build.call_args.args[0], NoRedirects)
        self.assertEqual(build.return_value.open.call_args.kwargs, {"timeout": 15})
        with self.opener(b"{}"):
            hubs._json(url)
        for body, message in ((b"x" * (2 * 1024 * 1024 + 1), "2 MiB"), (b"{", "JSON")):
            with self.opener(body), self.assertRaisesRegex(ProviderError, message):
                hubs._json(url)
        for code, message in (
            (401, "denied"),
            (403, "denied"),
            (404, "not found"),
            (500, "500"),
        ):
            error = HTTPError(url, code, "x", {}, io.BytesIO(b"secret body"))
            with self.opener(error), self.assertRaisesRegex(ProviderError, message):
                hubs._json(url)
        with (
            self.opener(URLError("down")),
            self.assertRaisesRegex(ProviderError, "connection"),
        ):
            hubs._json(url)
        with self.assertRaises(ProviderError):
            hubs._json("https://example.com/")


class NativeConversionTest(unittest.TestCase):
    def test_native_coffee_and_tea_map_to_exact_encodings(self):
        source = {"provider": "test", "url": None, "raw": native_coffee()}
        document = native_document(source, "coffee", ProviderError)
        content = document["coffee_recipe"]
        self.assertEqual(content["bypass"], {"volume_ml": 20, "temperature": "BP"})
        self.assertEqual(content["pours"][0]["temperature"], "BP")
        self.assertEqual(content["pours"][0]["pattern"], "ring")
        self.assertTrue(content["pours"][0]["vibration_before"])
        self.assertTrue(content["grinder_enabled"])
        self.assertEqual(document["source"]["conversion_target"], "coffee")
        self.assertEqual(
            encode_coffee_recipe(document).payload.hex(),
            # 45 ml BP(98) ring before; pause 30, RPM 90, flow 30; 127 + 53 ml
            # at 92 spiral after; no pause, flow 35; grind 55, ratio 150.
            "142d620101e2005a1e7f5c0202355c0202000000233796",
        )
        tea = native_document({"raw": native_tea()}, "tea", ProviderError)
        steeps = tea["tea"]["steeps"]
        self.assertEqual([s["temperature"] for s in steeps], ["RT", 99])
        self.assertEqual(tea["tea"]["ratio_metadata"], 42.5)
        self.assertEqual(
            encode_tea_recipe(tea).payload.hex(),
            # RT(39) 90 ml 20 s; 80 ml at 99, 1 min 15 s, flow 35; ratio 425 & 255
            "105a270000ec00781e50630000f120002332a9",
        )

    def test_native_whole_bypass_and_null_optional_fields(self):
        expected = native_document({"raw": native_coffee()}, "coffee", ProviderError)
        raw = native_coffee(
            bypassVolume=20.0, totalBrewWater=None, ratio=None, pourCount=None
        )
        result = native_document({"raw": raw}, "coffee", ProviderError)
        self.assertEqual(
            encode_coffee_recipe(result).payload, encode_coffee_recipe(expected).payload
        )
        raw = single([native_pour(45, 98, 30)])
        raw["recipeVo"]["pourCount"] = True
        with self.assertRaisesRegex(ProviderError, "pourCount"):
            native_document({"raw": raw}, "coffee", ProviderError)

    def test_native_sources_refuse_inexact_conversions(self):
        cases = [
            ([], "coffee", "object"),
            ({"recipeVo": []}, "coffee", "object"),
            (native_coffee(extra=1), "coffee", "Unknown native recipe fields"),
            (native_coffee(adaptedModel=2), "coffee", "Studio"),
            (native_coffee(cupType=9), "coffee", "cup type"),
            (native_coffee(), "tea", "does not match"),
            (native_coffee(pourList=[]), "coffee", "no pours"),
            (native_coffee(pourCount=3), "coffee", "pourCount"),
            (native_coffee(isSetGrinderSize=3), "coffee", "enum"),
            (native_coffee(ratio=16), "coffee", "ratio"),
            (single([1]), "coffee", "object"),
            (single([{"x": 1}]), "coffee", "Unknown native pour"),
            (single([{"volume": 1}]), "coffee", "missing"),
            (single([native_pour(1, 90, 0, pattern=7)]), "coffee", "pattern"),
            (single([native_pour(1.5, 90, 0)]), "coffee", "whole"),
            (native_coffee(totalBrewWater=1), "coffee", "totalBrewWater"),
            (native_coffee(theName=" "), "coffee", "name"),
            (native_coffee(dose=None), "coffee", "whole"),
            (native_tea(isSetGrinderSize=1), "tea", "grinder"),
        ]
        missing = native_coffee()
        del missing["recipeVo"]["isSetGrinderSize"]
        cases.append((missing, "coffee", "missing isSetGrinderSize"))
        for raw, target, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(ProviderError, message),
            ):
                native_document({"raw": raw}, target, ProviderError)
        with self.assertRaises(ValueError):
            native_document({"raw": native_tea(dose=9)}, "tea", ProviderError)


class ExportTest(unittest.TestCase):
    def test_offline_export_redacts_credentials(self):
        document = xrecipe_document("users/synthetic-owner/coffees/synthetic-coffee")
        document["authorization"] = "never-retain-secret"
        fields = document["fields"]
        fields["session"] = {"stringValue": "never-retain-secret"}
        extra = {"refreshToken": "never-retain-secret", "safe": "kept"}
        for key in ("authToken", "api_token", "jwt", "authentication", "bearer"):
            extra[key] = "never-retain-secret"
        fields["extra"] = firestore([extra])
        fields["image"] = {
            "stringValue": "https://example.com/i?token=never-retain-secret"
        }
        fields["login"] = {
            "stringValue": "HTTPS://user:pw@example.com/never-retain-secret"
        }
        fields["fragment"] = {
            "stringValue": "HTTPS://example.com/callback#access_token=never-retain-secret"
        }
        fields["routed_fragment"] = {
            "stringValue": "HTTPS://example.com/#/callback?auth=never-retain-secret"
        }
        fields["plain"] = {"stringValue": "https://example.com/#/browse?tab=coffee"}
        fields["broken"] = {"stringValue": "http://[::1"}
        source = read_export(document, HubProviders()._firestore)
        self.assertNotIn("never-retain-secret", json.dumps(source))
        self.assertEqual(source["redacted_fields"], 13)
        self.assertEqual(source["raw"]["extra"], [{"safe": "kept"}])
        self.assertEqual(
            source["raw"]["plain"], "https://example.com/#/browse?tab=coffee"
        )
        self.assertIsNone(source["url"])
        self.assertEqual(source["provider_id"], "synthetic-coffee")
        self.assertEqual(source["raw"]["xBloomData"]["grinderSize"], 55)

    def test_invalid_exports_and_bounds(self):
        decode = HubProviders()._firestore
        cyclic = {}
        cyclic["self"] = cyclic
        deep = current = {}
        for _ in range(81):
            current["x"] = current = {}
        valid_name = xrecipe_document()["name"]
        for document in (
            [],
            {},
            {"name": "projects/other/documents/1", "fields": {}},
            {"name": valid_name, "fields": []},
            {"secret": float("nan")},
            {"a": object()},
            {1: "value"},
            {"a": "x" * (MAX_EXPORT + 1)},
            {"a": ["x" * 1024] * 2048},
            cyclic,
            deep,
            "text",
        ):
            with (
                self.subTest(kind=type(document).__name__),
                self.assertRaises(ValueError),
            ):
                read_export(document, decode)
        broken = {"name": valid_name, "fields": {"x": {"referenceValue": "y"}}}
        source = read_export(broken, decode)
        self.assertIsNone(source["raw"])
        self.assertIn("unsupported", source["decode_error"])

    def test_load_export_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "export.json"
            path.write_text(json.dumps(xrecipe_document()))
            self.assertEqual(load_export(path)["name"], xrecipe_document()["name"])
            path.write_bytes(b"x" * (MAX_EXPORT + 1))
            with self.assertRaisesRegex(ValueError, "bounded"):
                load_export(path)
            path.write_text("{")
            with self.assertRaisesRegex(ValueError, "bounded"):
                load_export(path)
            with self.assertRaisesRegex(ValueError, "bounded"):
                load_export(Path(tmp) / "missing.json")


class ImportServiceTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "db.sqlite3")
        self.addCleanup(self.store.close)
        self.stub = Stub({"recipe/detail": {"code": 200, "data": HOST_RAW}})
        self.imports = RecipeImports(self.store, HubProviders(self.stub))

    def test_blocked_import_retains_source_and_refresh_proposes_only(self):
        url = "https://collective.xbloom.com/recipe/12"
        blocked = self.imports.import_recipe(url)
        self.assertIsNotNone(blocked["error"])
        self.assertEqual(
            self.store.get_import(blocked["id"])["source"]["raw"], HOST_RAW
        )
        imported = self.imports.import_recipe(url, flow_ml_s=3)
        ident = imported["recipe"]["id"]
        local = dict(imported["recipe"]["document"])
        local["name"] = "My variation"
        self.store.update_recipe(ident, local, 1)
        proposal = self.imports.refresh_recipe(ident, flow_ml_s=3)
        self.assertTrue(proposal["requires_explicit_update"])
        self.assertTrue(proposal["changed"])
        self.assertEqual(proposal["expected_revision"], 2)
        self.assertEqual(
            self.store.get_recipe(ident)["document"]["name"], "My variation"
        )
        self.stub.responses["recipe/detail"] = {
            "code": 200,
            "data": {**HOST_RAW, "new": 1},
        }
        refused = self.imports.refresh_recipe(ident, flow_ml_s=3)
        self.assertEqual(refused["blocked_import"]["source"]["raw"]["new"], 1)
        native = self.imports.import_recipe(url, target="coffee")
        self.assertIn("share links", native["error"])

    def test_name_conflict_rolls_back_source_and_event(self):
        local = self.store.create_recipe({"name": "Local", "steps": [{"kind": "tare"}]})
        self.store.alias(local["id"], HOST_RAW["recipeName"])
        events = self.store.events()
        with self.assertRaises(ConflictError):
            self.imports.import_recipe(
                "https://collective.xbloom.com/recipe/12", flow_ml_s=3
            )
        self.assertEqual(self.store.events(), events)
        self.assertEqual(
            self.store.db.execute("SELECT count(*) FROM imports").fetchone()[0], 0
        )

    def test_provenance_keeps_only_lookup_fields(self):
        imported = self.imports.import_recipe(
            "https://collective.xbloom.com:443/recipe/12/?type=2&access_token=synthetic"
            "#private-fragment",
            flow_ml_s=3,
        )
        url = "https://collective.xbloom.com/recipe/12?type=2"
        self.assertEqual(imported["source"]["url"], url)
        self.assertEqual(imported["recipe"]["document"]["source"]["url"], url)
        self.assertEqual(self.store.events()[-1]["data"]["source_url"], url)
        writer = HubProviders(Stub({"firestore": xrecipe_document()}))
        self.assertEqual(
            writer.read("https://xbloom-app.vercel.app/recipe/synthetic-1/#private")[
                "url"
            ],
            "https://xbloom-app.vercel.app/recipe/synthetic-1",
        )

    def test_signed_export_url_is_removed_before_persistence(self):
        export = xrecipe_document()
        export["fields"]["image"] = {
            "stringValue": "https://example.com/blob?sp=r&sv=2023-01-03&sig=synthetic"
        }
        imported = self.imports.import_export(export, target="coffee")
        retained = self.store.get_import(imported["id"])
        recipe = self.store.get_recipe(imported["recipe"]["id"])
        self.assertNotIn("sig=synthetic", json.dumps([retained, recipe]))

    def test_nonfinite_typed_values_are_retained_as_blocked(self):
        url = "https://xbloom-app.vercel.app/recipe/synthetic-1"
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                export = xrecipe_document()
                export["fields"]["optional"] = {"doubleValue": value}
                imports = RecipeImports(
                    self.store, HubProviders(Stub({"firestore": export}))
                )
                for result in (
                    imports.import_export(export, target="coffee"),
                    imports.import_recipe(url, target="coffee"),
                ):
                    self.assertIsNotNone(result["error"])
                    self.assertIsNone(result["source"]["raw"])
                    raw = result["source"]["raw_document"]
                    self.assertEqual(raw["fields"]["optional"], {"doubleValue": value})

    def test_oversized_integer_bypass_is_retained_as_blocked(self):
        export = xrecipe_document()
        volume = {"integerValue": str(10**309)}
        fields = export["fields"]["xBloomData"]["mapValue"]["fields"]
        fields["bypassVolume"] = volume
        blocked = self.imports.import_export(export, target="coffee")
        self.assertIn("/coffee_recipe/bypass/volume_ml", blocked["error"])
        retained = self.store.get_import(blocked["id"])
        self.assertEqual(
            retained["source"]["raw"]["xBloomData"]["bypassVolume"], 10**309
        )

    def test_oversized_host_numbers_are_retained_as_blocked(self):
        for field in ("dose", "grinderSize", "rpm"):
            with self.subTest(field=field):
                source = {**HOST_RAW, field: 10**309}
                self.stub.responses["recipe/detail"] = {"code": 200, "data": source}
                blocked = self.imports.import_recipe(
                    "https://collective.xbloom.com/recipe/12", flow_ml_s=3
                )
                self.assertIn(field, blocked["error"])
                retained = self.store.get_import(blocked["id"])
                self.assertEqual(retained["source"]["raw"][field], 10**309)

    def test_tea_cup_type_prevents_host_adaptation(self):
        export = xrecipe_document()
        export["fields"]["xBloomData"] = firestore({**HOST_RAW, "cupType": 4})
        blocked = self.imports.import_export(export, flow_ml_s=3)
        self.assertIn("Tea requires", blocked["error"])
        self.assertNotIn("recipe", blocked)

    def test_offline_export_import_and_refresh_without_upstream(self):
        imported = self.imports.import_export(xrecipe_document(), target="coffee")
        recipe = imported["recipe"]
        self.assertEqual(recipe["document"]["coffee_recipe"]["dose_g"], 15)
        self.assertEqual(imported["source"]["provider"], "xrecipe-export")
        with self.assertRaisesRegex(ProviderError, "no upstream"):
            self.imports.refresh_recipe(recipe["id"])
        local = self.store.create_recipe({"name": "Local", "steps": [{"kind": "tare"}]})
        with self.assertRaisesRegex(ProviderError, "no upstream"):
            self.imports.refresh_recipe(local["id"])
        tea_export = xrecipe_document()
        tea_export["fields"] = firestore({"xBloomData": native_tea()["recipeVo"]})[
            "mapValue"
        ]["fields"]
        tea = self.imports.import_export(tea_export, target="tea", temporary=True)
        self.assertEqual(tea["recipe"]["temporary"], 1)
        blocked = self.imports.import_export(xrecipe_document(), target="tea")
        self.assertIn("does not match", blocked["error"])

    def test_refresh_keeps_the_conversion_target(self):
        stub = Stub({"firestore": xrecipe_document()})
        imports = RecipeImports(self.store, HubProviders(stub))
        url = "https://xbloom-app.vercel.app/recipe/synthetic-1"
        recipe = imports.import_recipe(url, target="coffee")["recipe"]
        proposal = imports.refresh_recipe(recipe["id"])
        self.assertFalse(proposal["changed"])
        self.assertEqual(proposal["upstream_proposal"]["mode"], "coffee")
        self.assertIsInstance(RecipeImports(self.store).providers, HubProviders)
