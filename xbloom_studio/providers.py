"""Bounded, read-only adapters for user-directed recipe hub imports.

Imports are always user-directed: one recipe page URL or one search query at
a time, from exact HTTPS origins, without redirects, credentials or bulk
retrieval, and with bounded responses. Nothing is published or written
upstream. Sources are retained with every import, including blocked ones.

Omitted: share links on ``share-h5.xbloom.com`` (and the native settings a
Collective detail links to through them). Reading them requires the app's
client API with an embedded client key rather than a public page interface;
their public use is not established, so those flows are refused with an
explicit reason. Offline xRecipe exports remain supported, see
:mod:`xbloom_studio.provider_exports`.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, NoReturn
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .provider_exports import check_document_bounds, read_export
from .provider_native import native_document
from .recipes import number, validate_recipe

if TYPE_CHECKING:
    from email.message import Message
    from typing import IO
    from urllib.parse import SplitResult
    from urllib.request import Request as RequestType

    from .storage import Store

MAX_RESPONSE = 2 * 1024 * 1024
MAX_URL = 2048
TIMEOUT = 15
USER_AGENT = "xbloom-studio"
COLLECTIVE_API = "https://collective-api.xbloom.com/communityRecipe"
XRECIPE_HOSTS = frozenset(
    {
        "xbloom-app.vercel.app",
        "xbloom-recipe-app.web.app",
        "xbloom-recipe-app.firebaseapp.com",
    }
)
XRECIPE_DOCUMENTS = (
    "https://firestore.googleapis.com/v1/projects/xbloom-recipe-app/databases/"
    "(default)/documents/recipes_master/"
)
SHARE_OMITTED = (
    "xBloom share links are not supported: they need the app's client API, "
    "whose public use is not established. Import the Collective page with "
    "target=host, or an owner-supplied export."
)
HOST_METADATA = frozenset(
    {
        "recipeName", "name", "model", "cupTypeInt", "cupTypeName", "cupType",
        "communityRecipeId", "recipeId", "id", "imageUrl", "official",
        "grandWater", "volume", "userName", "userAvatar", "uploadDate",
        "likesCount", "isLike", "isCollect", "process", "type", "introduce",
        "origin", "varietal", "roast", "flavor", "shareRecipeLink", "author",
        "description", "dose", "grinderSize", "rpm", "pourList", "pours",
    }
)  # fmt: skip
HOST_POUR_FIELDS = frozenset(
    {"theName", "name", "volume", "temperature", "pausing", "pattern"}
)
SOURCE_PATTERNS = {1: "center", 2: "spiral", 3: "ring"}

Fetch = Callable[..., Any]


class ProviderError(ValueError):
    pass


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: RequestType,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> NoReturn:
        raise ProviderError(
            "Provider redirect refused; inspect the new origin explicitly"
        )


def _object(value: Any, message: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProviderError(message)
    return value


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Provider response contains duplicate JSON keys")
        result[key] = value
    return result


class HubProviders:
    """Read and search public recipe pages; adapt sources only when exact."""

    ORIGINS = frozenset(
        {
            "collective.xbloom.com",
            "collective-api.xbloom.com",
            "firestore.googleapis.com",
            *XRECIPE_HOSTS,
        }
    )

    def __init__(self, fetch: Fetch | None = None) -> None:
        self.fetch: Fetch = fetch or self._json

    @classmethod
    def _url(cls, url: object) -> SplitResult:
        if not isinstance(url, str):
            raise ProviderError("URL must be a string")
        if len(url) > MAX_URL:
            raise ProviderError("URL too long")
        try:
            p = urlsplit(url)
            port = p.port
        except ValueError as exc:
            raise ProviderError("Invalid URL") from exc
        if p.hostname == "share-h5.xbloom.com":
            raise ProviderError(SHARE_OMITTED)
        if (
            p.scheme != "https"
            or p.hostname not in cls.ORIGINS
            or p.username
            or p.password
            or port not in (None, 443)
        ):
            raise ProviderError("Only exact supported HTTPS origins are accepted")
        return p

    def _json(self, url: str, data: Any = None) -> Any:
        self._url(url)
        request = Request(
            url,
            data=json.dumps(data).encode() if data is not None else None,
            headers={
                "Content-Type": "application/json",
                "LanguageType": "0",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with build_opener(NoRedirects()).open(request, timeout=TIMEOUT) as response:
                raw = response.read(MAX_RESPONSE + 1)
        except HTTPError as exc:
            exc.close()  # Release the response without reading or exposing it.
            if exc.code in (401, 403):
                raise ProviderError(
                    "Provider access denied; this recipe may require "
                    "authentication. Use an owner-authorized export."
                ) from exc
            if exc.code == 404:
                raise ProviderError(
                    "Provider recipe was not found; check its public detail URL."
                ) from exc
            raise ProviderError(
                f"Provider request failed with HTTP status {exc.code}; "
                "no recipe was substituted."
            ) from exc
        except URLError as exc:
            raise ProviderError(
                "Provider connection failed; no recipe was retrieved or substituted."
            ) from exc
        if len(raw) > MAX_RESPONSE:
            raise ProviderError("Provider response exceeds 2 MiB")
        try:
            result = json.loads(raw, object_pairs_hook=_unique_json_object)
            check_document_bounds(result, measure_size=False)
            return result
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise ProviderError("Provider response is not bounded JSON") from exc

    def capabilities(self) -> dict[str, Any]:
        return {
            "collective": {
                "read": True,
                "search": True,
                "native_settings": False,
                "publish": False,
                "update": False,
                "delete": False,
            },
            "xrecipe": {
                "read": True,
                "search": False,
                "publish": False,
                "update": False,
                "delete": False,
            },
            "xrecipe_export": {
                "offline_import": True,
                "private_access_verified": False,
            },
            "omitted": {"xbloom_share": SHARE_OMITTED},
            "write_reason": "Authenticated online writes require a separately "
            "verified provider/account adapter",
        }

    def search(self, query: str, page: int = 1) -> dict[str, Any]:
        """Search Collective coffee recipes; one user-requested page at a time."""
        if not isinstance(query, str) or len(query) > 256:
            raise ProviderError("query must be a string of at most 256 characters")
        number(page, "page", 1, 1000, True)
        result = _object(
            self.fetch(
                COLLECTIVE_API + "/index/page",
                {"pageIndex": page, "pageSize": 20, "keyword": query, "recipeType": 1},
            ),
            "Collective search response is not an object",
        )
        if result.get("code") != 200:
            raise ProviderError("Collective search rejected")
        data = _object(result.get("data"), "Collective search response has no data")
        if not isinstance(data.get("list"), list):
            raise ProviderError("Collective search response has no result list")
        return {
            "provider": "collective",
            "page": page,
            "results": data["list"],
            "total": data.get("total"),
            "selection_required": True,
        }

    def read(self, url: str, *, native: bool = False) -> dict[str, Any]:
        """Read one recipe page's public source; never follow or guess other URLs."""
        parsed = self._url(url)
        if parsed.hostname == "collective.xbloom.com":
            match = re.fullmatch(r"/recipe/(\d+)/?", parsed.path)
            if not match:
                raise ProviderError(
                    "Use a Collective /recipe/ID?type=1 or type=2 detail URL"
                )
            kind = parse_qs(parsed.query).get("type", ["1"])[0]
            if kind not in ("1", "2"):
                raise ProviderError("Unknown Collective detail type")
            raw = _object(
                self.fetch(
                    COLLECTIVE_API + "/recipe/detail",
                    {"id": int(match[1]), "type": int(kind)},
                ),
                "Collective response is not an object",
            )
            if raw.get("code") != 200:
                raise ProviderError("Collective recipe rejected")
            source = {
                "provider": "collective",
                "url": f"https://collective.xbloom.com/recipe/{match[1]}?type={kind}",
                "provider_id": match[1],
                "raw": raw.get("data"),
            }
            if native:
                # Retained as a blocked conversion with its reason.
                source["native_decode_error"] = SHARE_OMITTED
            return source
        if parsed.hostname in XRECIPE_HOSTS:
            if parsed.query:
                raise ProviderError(
                    "xRecipe history/revision queries require an authenticated "
                    "export; the current recipe was not substituted"
                )
            match = re.fullmatch(r"/recipe/([A-Za-z0-9_-]+)/?", parsed.path)
            if not match:
                raise ProviderError(
                    "Use an xRecipe public /recipe/ID URL; private or scanned "
                    "recipes need an export"
                )
            ident = match[1]
            raw = _object(
                self.fetch(XRECIPE_DOCUMENTS + quote(ident, safe="")),
                "xRecipe response is not an object",
            )
            if "fields" not in raw:
                raise ProviderError(
                    "xRecipe document unavailable or requires authentication"
                )
            source = {
                "provider": "xrecipe",
                "url": f"https://{parsed.hostname}/recipe/{ident}",
                "provider_id": ident,
                "raw_document": raw,
            }
            try:
                source["raw"] = self._firestore({"mapValue": {"fields": raw["fields"]}})
            except (AttributeError, TypeError, ValueError, OverflowError) as exc:
                source["raw"] = None
                source["decode_error"] = str(
                    exc
                    if isinstance(exc, ProviderError)
                    else "Unsupported Firestore value; export source data instead"
                )
            return source
        raise ProviderError("Import a recipe page URL, not an API URL")

    def _firestore(self, value: dict[str, Any]) -> Any:
        """Decode one Firestore REST typed value."""
        if "mapValue" in value:
            return {
                k: self._firestore(v)
                for k, v in value["mapValue"].get("fields", {}).items()
            }
        if "arrayValue" in value:
            return [self._firestore(v) for v in value["arrayValue"].get("values", [])]
        for key in ("stringValue", "booleanValue", "timestampValue"):
            if key in value:
                return value[key]
        if "integerValue" in value:
            return int(value["integerValue"])
        if "doubleValue" in value:
            decoded = float(value["doubleValue"])
            if not math.isfinite(decoded):
                raise ProviderError("Firestore doubleValue must be finite")
            return decoded
        if "nullValue" in value:
            return None
        raise ProviderError("Unsupported Firestore value; export source data instead")

    def normalize(
        self,
        source: dict[str, Any],
        flow_ml_s: float | None = None,
        target: str = "host",
    ) -> dict[str, Any]:
        """Adapt a retained source; refuse rather than guess any missing setting."""
        if target not in ("host", "coffee", "tea"):
            raise ProviderError("Conversion target must be host, coffee or tea")
        if source.get("decode_error"):
            raise ProviderError(source["decode_error"])
        if target != "host":
            if source.get("native_decode_error"):
                raise ProviderError(source["native_decode_error"])
            return native_document(source, target, ProviderError)
        return self._host_document(source, flow_ml_s)

    def _host_document(
        self, source: dict[str, Any], flow_ml_s: float | None
    ) -> dict[str, Any]:
        """Explicit host adaptation; no assumptions about missing native settings."""
        r = _object(source["raw"], "Recipe source must be an object")
        if "xBloomData" in r:
            r = _object(r["xBloomData"], "xBloomData must be an object")
        if set(r) - HOST_METADATA:
            raise ProviderError(
                "Unknown recipe-level fields; source retained without lossy conversion"
            )
        for field, low, high in (
            ("dose", 1, 100),
            ("grinderSize", 1, 80),
            ("rpm", 60, 120),
        ):
            if field in r and r[field] is not None:
                number(r[field], field, low, high)
        if r.get("model") not in (None, "Studio", "xBloom Studio"):
            raise ProviderError(
                "Recipe targets another machine; explicit adaptation required"
            )
        if (
            4 in (r.get("cupTypeInt"), r.get("cupType"))
            or r.get("cupTypeName") == "TEA"
        ):
            raise ProviderError(
                "Tea requires native encoding or an explicitly authored host plan"
            )
        number(flow_ml_s, "flow_ml_s adaptation", 0.1, 10)
        pours = r.get("pourList") or r.get("pours")
        if not isinstance(pours, list) or not pours:
            raise ProviderError("Source has no supported pour list")
        steps: list[dict[str, Any]] = []
        if r.get("dose") or r.get("grinderSize"):
            steps.append(
                {
                    "kind": "user",
                    "instruction": f"Prepare dose {r.get('dose', 'unspecified')} g and "
                    f"grind setting {r.get('grinderSize', 'unspecified')}, RPM "
                    f"{r.get('rpm', 'unspecified')}; place funnel/cup. Host adaptation "
                    "does not automate this grind.",
                }
            )
        for pour in pours:
            if not isinstance(pour, dict) or set(pour) - HOST_POUR_FIELDS:
                raise ProviderError(
                    "Unsupported pour fields; source retained without lossy conversion"
                )
            if not {"volume", "temperature"} <= pour.keys():
                raise ProviderError("Source pour requires volume and temperature")
            number(pour.get("pattern"), "pattern", 1, 3, True)
            if "pausing" in pour:
                number(pour["pausing"], "pausing", 0, 3600)
            pattern = SOURCE_PATTERNS.get(pour["pattern"])
            if pattern is None:
                raise ProviderError("Unknown source pour pattern")
            steps.append(
                {
                    "kind": "water",
                    "volume_ml": pour["volume"],
                    "temperature_c": pour["temperature"],
                    "flow_ml_s": flow_ml_s,
                    "pattern": pattern,
                }
            )
            if pour.get("pausing"):
                steps.append({"kind": "wait", "duration_s": pour["pausing"]})
        document = {
            "name": r.get("recipeName") or r.get("name") or "Imported recipe",
            "mode": "host",
            "coffee": {
                "origin": str(r.get("origin", "")),
                "variety": str(r.get("varietal", "")),
            },
            "source": {
                **source,
                "adaptation": "manual grind/user gate and explicit flow; not native "
                "equivalence",
                "author": r.get("userName") or r.get("author"),
            },
            "steps": steps,
        }
        return validate_recipe(document)

    def online_write(self, **kwargs: Any) -> NoReturn:
        raise ProviderError(self.capabilities()["write_reason"])


class RecipeImports:
    """Import, re-import and refresh recipes into a store; never connects BLE.

    A conversion that cannot be exact is saved as a blocked import with its
    source and reason. Refresh returns a reviewable proposal and never edits
    the local recipe.
    """

    def __init__(self, store: Store, providers: HubProviders | None = None) -> None:
        self.store = store
        self.providers = providers or HubProviders()

    def import_recipe(
        self,
        url: str,
        flow_ml_s: float | None = None,
        temporary: bool = False,
        target: str = "host",
    ) -> dict[str, Any]:
        source = self.providers.read(url, native=target != "host")
        return self._import_source(source, flow_ml_s, temporary, target)

    def import_export(
        self,
        document: Any,
        flow_ml_s: float | None = None,
        temporary: bool = False,
        target: str = "host",
    ) -> dict[str, Any]:
        """Import an owner-supplied export offline; no upstream refresh URL."""
        source = read_export(document, self.providers._firestore)
        return self._import_source(source, flow_ml_s, temporary, target)

    def _convert(
        self, source: dict[str, Any], flow_ml_s: float | None, target: str
    ) -> dict[str, Any]:
        return self.providers.normalize(source, flow_ml_s, target=target)

    def _import_source(
        self,
        source: dict[str, Any],
        flow_ml_s: float | None,
        temporary: bool,
        target: str,
    ) -> dict[str, Any]:
        try:
            document = self._convert(source, flow_ml_s, target)
        except (ValueError, KeyError, TypeError) as exc:
            return self.store.save_import(source, error=str(exc))
        return self.store.save_import(
            source, document=document, recipe_temporary=temporary
        )

    def refresh_recipe(
        self, reference: str, flow_ml_s: float | None = None, target: str | None = None
    ) -> dict[str, Any]:
        current = self.store.get_recipe(reference)
        provenance = current["document"].get("source")
        url = provenance.get("url") if isinstance(provenance, dict) else None
        if not isinstance(url, str) or not url.strip() or provenance is None:
            raise ProviderError(
                "Recipe has no upstream source URL; refresh requires an imported "
                "recipe. Local recipes can be edited directly."
            )
        target = target or provenance.get("conversion_target", "host")
        source = self.providers.read(url, native=target != "host")
        try:
            document = self._convert(source, flow_ml_s, str(target))
        except (ValueError, KeyError, TypeError) as exc:
            return {
                "recipe_id": current["id"],
                "blocked_import": self.store.save_import(source, error=str(exc)),
                "requires_explicit_update": True,
            }
        imported = self.store.save_import(source, document=document)
        return {
            "import_id": imported["id"],
            "recipe_id": current["id"],
            "expected_revision": current["revision"],
            "local": current["document"],
            "upstream_proposal": document,
            "changed": current["document"] != document,
            "requires_explicit_update": True,
        }
