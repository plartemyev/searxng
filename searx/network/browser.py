# SPDX-License-Identifier: AGPL-3.0-or-later
"""Masqueraded Chromium fetch pool for engine requests.

When ``outgoing.using_browser`` is enabled in settings.yml, engine HTTP
requests are served by a real, Playwright-driven Chromium instead of the
curl_cffi client. The browser is tuned to look like a user-started browser
(the same posture the Onyx web crawler uses):

- a distro-packaged Chromium binary (not Playwright's bundled fork, which
  ships automation-friendly defaults detectors fingerprint)
- Playwright's automation-flavored default launch args stripped
- headed under auto-started Xvfb displays, one per lane (headless is a
  strong bot signal even in "new" headless mode)
- UA and Client Hints derived from the real binary version, and a page-side
  init script aligning ``navigator.platform``, WebGL vendor/renderer,
  plugins and ``userAgentData`` with those claims

GET requests use a fetch-style ``context.request.get`` (browser TLS stack +
cookie jar, no page render). On a bot challenge (Cloudflare interstitial,
403/429) the URL is first navigated in a real page so challenge JS resolves
and clearance cookies land in the context, then the fetch is retried.

``outgoing.browser_max_stealth`` turns the masquerading around: supported
search GETs (browser URL, not a data endpoint, query extractable) skip the
fetch-style attempt entirely and are served by driving the provider's
search UI with simulated mouse and keyboard on every request, not only
after a challenge. The response handed to the engine parser is the DOM
captured from the live page. Nothing is re-fetched fetch-style afterwards:
a programmatic replay of the results URL is exactly the automation tell
the interactive visit was meant to avoid, and a data shell served to it
would shadow the good DOM. If the interactive flow fails, the request
fails -- no fetch-style attempt slips out to the search engine.

At pool start the instance's public IP is resolved to a locale (country,
timezone, language) and every lane context is born with that identity, so
the browser's claims agree with where its requests come from.

The pool owns N browser contexts ("lanes"). Each lane serves one request at
a time; cookies persist per lane, so solved challenges benefit later
requests on the same lane. With ``outgoing.browser_profile_dir`` set, each
lane also gets a persistent on-disk Chromium profile (one subdirectory per
lane), keeping cookies and earned clearances across restarts; otherwise
profiles are in-memory only.
"""

# SPDX-License-Identifier: AGPL-3.0-or-later
# pylint: disable=too-many-instance-attributes

__all__ = ["BrowserFetchPool", "get_browser_fetch_pool", "ensure_display", "BrowserFetchError"]

import asyncio
import atexit
import http.client as http
import json
import logging
import os
import random
import re
import shutil
import subprocess
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlencode

from lxml import html
from searx.exceptions import (
    SearxEngineAccessDeniedException,
    SearxEngineTooManyRequestsException,
)
from searx.extended_types import SXNG_URL
from urllib.parse import parse_qs, urljoin, urlsplit

logger = logging.getLogger("searx.network.browser")

# Grace period after a challenge navigation to let challenge JS settle.
_BOT_CHALLENGE_GRACE_MS = 5000
# Default per-request budget when the caller does not provide a timeout.
_DEFAULT_TIMEOUT_S = 20.0
# Floor for caller-provided timeouts: browser fetches are slower than curl.
_MIN_TIMEOUT_S = 10.0

_CHROMIUM_CANDIDATE_PATHS = (
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
)

_XVFB_BASE_DISPLAY = 99
_XVFB_GEOMETRY = "1440x900x24"

# One Xvfb per pool lane. Lanes on separate displays have separate X
# pointers: a lane's simulated clicks can never land in another lane's
# window, which is exactly what happens when headed windows stack on one
# display with no window manager (the topmost window gets the click).
_xvfb_processes: dict[str, subprocess.Popen] = {}
_xvfb_lock = threading.Lock()


class BrowserFetchError(Exception):
    """Raised when the browser pool cannot serve a request."""


def _discover_chromium():
    """Locate a Chromium binary: explicit env config, then distro paths."""
    env_path = os.environ.get("SEARXNG_CHROMIUM_EXECUTABLE_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path
    for candidate in _CHROMIUM_CANDIDATE_PATHS:
        if os.path.isfile(candidate):
            return candidate
    return None


def _lane_display_number(lane_index: int) -> int:
    """X display number assigned to a pool lane (:99, :100, ...)."""
    return _XVFB_BASE_DISPLAY + max(0, lane_index)


def _cleanup_stale_x_locks(display_number: int) -> None:
    """Remove X lock/socket leftovers from a previous container run.

    The container filesystem survives restarts while processes do not: a
    stale lock for the display makes a freshly started Xvfb exit at once,
    and a stale socket then looks like a working display.
    """
    stale_paths = (
        f"/tmp/.X{display_number}-lock",  # noqa: S108
        f"/tmp/.X11-unix/X{display_number}",  # noqa: S108
    )
    for path in stale_paths:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Could not remove stale X server file %s", path)


def _bootstrap_xauth():
    """Make the Xvfb display connectable for python-xlib clients.

    Xvfb runs without access control, but python-xlib (the human input
    layer) still
    tries to read an authority file and hard-fails when ``~/.Xauthority``
    does not exist. An empty file satisfies it.
    """
    os.environ.setdefault("HOME", "/tmp")
    home = os.environ["HOME"]
    try:
        os.makedirs(home, exist_ok=True)
    except OSError:
        return
    xauth_path = os.environ.get("XAUTHORITY") or os.path.join(home, ".Xauthority")
    try:
        if not os.path.exists(xauth_path):
            with open(xauth_path, "ab"):
                pass
        os.environ["XAUTHORITY"] = xauth_path
    except OSError:
        logger.warning("Could not create an empty Xauthority file at %s", xauth_path)


def ensure_display(lane_index: int = 0):
    """Return the X display for pool lane ``lane_index``, starting Xvfb.

    Every lane gets its own display (:99, :100, ...), started on first use
    and reused while it lives. A display provided by the operator via
    ``$DISPLAY`` (e.g. a dev desktop) serves lane 0 only: the pool needs
    one display per lane and may not steal the operator's.

    Returns None when headed mode is impossible, in which case the caller
    falls back to headless.
    """
    if lane_index == 0:
        existing_display = os.environ.get("DISPLAY")
        if existing_display:
            return existing_display
    display = f":{_lane_display_number(lane_index)}"
    with _xvfb_lock:
        process = _xvfb_processes.get(display)
        if process is not None and process.poll() is None:
            return display
        xvfb = shutil.which("Xvfb")
        if xvfb is None:
            return None
        try:
            os.makedirs("/tmp/.X11-unix", exist_ok=True)  # noqa: S108
            _cleanup_stale_x_locks(_lane_display_number(lane_index))
            process = subprocess.Popen(  # pylint: disable=consider-using-with
                [
                    xvfb,
                    display,
                    "-screen",
                    "0",
                    _XVFB_GEOMETRY,
                    "-nolisten",
                    "tcp",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            logger.warning("Failed to start Xvfb; falling back to headless browser")
            return None
        # Wait for a live process AND a fresh socket: a leftover socket from
        # a stopped X server must not count as a working display.
        socket_path = f"/tmp/.X11-unix/X{display.lstrip(':')}"  # noqa: S108
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            if os.path.exists(socket_path):
                logger.info(
                    "Started Xvfb on %s for lane %d", display, lane_index
                )
                _bootstrap_xauth()
                _xvfb_processes[display] = process
                return display
            time.sleep(0.1)
        logger.warning(
            "Xvfb on %s did not come up; falling back to headless browser",
            display,
        )
        try:
            process.kill()
        except OSError:
            pass
        _xvfb_processes.pop(display, None)
        return None


# Playwright's default launch args tilt toward automation and test farms.
# Dropping these makes the launched browser arg-for-arg closer to a
# user-started one.
_OMIT_DEFAULT_ARGS = [
    "--enable-automation",
    "--disable-background-networking",
    "--disable-extensions",
    "--disable-dev-shm-usage",
    "--disable-default-apps",
    "--disable-component-update",
    "--disable-client-side-phishing-detection",
    "--disable-breakpad",
    "--disable-back-forward-cache",
    "--disable-backgrounding-occluded-windows",
    "--disable-background-timer-throttling",
    "--disable-component-extensions-with-background-pages",
    "--disable-ipc-flooding-protection",
    "--disable-popup-blocking",
    "--disable-prompt-on-repost",
    "--disable-renderer-backgrounding",
    "--use-mock-keychain",
    "--unsafely-disable-devtools-self-xss-warnings",
    "--password-store=basic",
    "--disable-search-engine-choice-screen",
    "--export-tagged-pdf",
    "--no-service-autorun",
    # NOTE: do NOT omit --no-first-run: without it a fresh profile runs the
    # first-run flow, which stalls the CDP handshake on current Chromium.
    "--metrics-recording-only",
    "--force-color-profile=srgb",
    "--disable-hang-monitor",
    "--allow-pre-commit-input",
    "--disable-field-trial-config",
    (
        "--disable-features=AcceptCHFrame,AutoExpandDetailsElement,"
        "AvoidUnnecessaryBeforeUnloadCheckSync,"
        "CertificateTransparencyComponentUpdater,DestroyProfileOnBrowserClose,"
        "DialMediaRouteProvider,ExtensionManifestV2Disabled,"
        "GlobalMediaControls,HttpsUpgrades,ImprovedCookieControls,"
        "LazyFrameLoading,LensOverlay,MediaRouter,PaintHolding,"
        "ThirdPartyStoragePartitioning,Translate"
    ),
]

_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-blink-features=AutomationControlled",
    "--disable-infobars",
]


def _stealth_init_script(language_tags: list[str]) -> str:
    """Init script with anti-automation patches only.

    The lane presents the binary's real identity (distro Chromium, Linux,
    IP-derived locale): kernel, TLS stack and Client-Hint headers already
    say Linux Chrome, so anything claimed in JS must agree -- a Windows
    persona here would be contradicted on the wire by every other layer.
    """
    return """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'languages',
                          {get: () => __LANGUAGES__});
    const patchGL = (proto) => {
        const orig = proto.getParameter;
        proto.getParameter = function (param) {
            // UNMASKED_VENDOR_WEBGL / UNMASKED_RENDERER_WEBGL: the VM has
            // no GPU and would report llvmpipe, a classic bot signal.
            if (param === 37445) return 'Google Inc. (NVIDIA)';
            if (param === 37446) {
                return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1650/PCIe/SSE2,'
                       + ' OpenGL 4.5.0 NVIDIA 550.107.02)';
            }
            return orig.call(this, param);
        };
    };
    if (window.WebGLRenderingContext) patchGL(WebGLRenderingContext.prototype);
    if (window.WebGL2RenderingContext) patchGL(WebGL2RenderingContext.prototype);
    """.replace("__LANGUAGES__", json.dumps(language_tags))


def _language_tags(accept_language: str) -> list[str]:
    """Language tags in priority order, from the geo Accept-Language header,
    so ``navigator.languages`` agrees with what goes on the wire."""
    tags = [part.split(";")[0].strip() for part in accept_language.split(",")]
    return [tag for tag in tags if tag] or ["en-US"]


# A Cloudflare challenge interstitial is identified by its page title and by
# Cloudflare-specific strings in the page body.
_CF_CHALLENGE_TITLE_RE = re.compile(
    r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL
)
_CF_CHALLENGE_TITLE_MARKERS = (
    "just a moment...",
    "attention required",
    "please wait",
    "checking your browser",
    "verifying you are human",
)
_CF_CHALLENGE_BODY_MARKERS = (
    "challenges.cloudflare.com",
    "/cdn-cgi/challenge-platform/",
    "cf-chl-bypass",
    'id="challenge-form"',
    'id="challenge-error-text"',
    'id="cf-challenge-running"',
    'id="challenge-running"',
    'class="cf-turnstile"',
    # google serves its CAPTCHA wall as HTTP 200: catch it so the human
    # fallback runs instead of the engine parsing the sorry page
    "unusual traffic from your computer network",
    "/sorry/index",
)
_SCRIPT_BLOCK_RE = re.compile(r"<script\b.*?</script>", re.IGNORECASE | re.DOTALL)

# Query parameter names engines use to carry the search terms. The human
# fallback re-runs the search through the provider's UI, which needs the raw
# query rather than the engine endpoint's URL.
_QUERY_PARAM_KEYS = ("q", "p", "query", "text", "s", "search", "wd", "k")


def _search_query_from_url(url: str) -> str | None:
    """Extract the search terms from an engine request URL, if any."""
    try:
        params = parse_qs(urlsplit(url).query)
    except ValueError:
        return None
    for key in _QUERY_PARAM_KEYS:
        values = params.get(key)
        if values and values[0].strip():
            return values[0].strip()
    return None


def _is_api_url(url: str) -> bool:
    """Data endpoints have no search UI to drive with human input.

    JSON endpoints, and the AJAX data routes image engines read (result
    fragments and metadata carry no UI to type into): driving them like a
    human is impossible, so they keep the fetch path through the
    masqueraded browser.
    """
    parts = urlsplit(url)
    host = parts.hostname or ""
    if host.startswith("api.") or host.startswith("apis."):
        return True
    path = parts.path.lower()
    if path.endswith((".json", ".js", "api.php")):
        return True
    if any(marker in path for marker in ("/async", "/suggest", "/complete", "/autocomplete")):
        return True
    # duckduckgo's autocomplete endpoint
    return path == "/ac" or path.startswith("/ac/")


def _is_challenge_url(url: str | None) -> bool:
    """Is this URL a challenge / rate-limit interstitial?"""
    lowered = (url or "").lower()
    return "/sorry" in lowered or "unusual traffic" in lowered


def _homepage_url_from_search_url(url: str) -> str:
    """The provider front page, carrying the engine URL's locale parameters.

    The human flow starts from the site's front page, like an address-bar
    entry, and drives the search box from there. The engine URL's locale
    parameters ride along on the homepage -- google's ``hl``/``gl``
    (interface language / result country), bing's ``mkt``/``setlang`` -- so
    the provider renders its UI in the language the search asked for, and
    its own search form submits those parameters with the typed query.
    ``cr=countryXX`` from the engine URL is the searxng spelling of
    ``gl``.
    """
    parts = urlsplit(url)
    params = parse_qs(parts.query)
    carried: list[tuple[str, str]] = []
    for key in ("hl", "gl", "mkt", "setlang"):
        values = params.get(key)
        if values and values[0].strip():
            carried.append((key, values[0].strip()))
    cr_values = params.get("cr")
    if cr_values and not any(key == "gl" for key, _ in carried):
        cr = cr_values[0].strip()
        if cr.lower().startswith("country") and len(cr) > len("country"):
            carried.append(("gl", cr[len("country"):].upper()))
    base = f"{parts.scheme}://{parts.netloc}/"
    return base + ("?" + urlencode(carried) if carried else "")


def _is_human_search_candidate(url: str) -> bool:
    """Can this request be served by driving the provider's search UI?

    Same criteria the human fallback enforces: an http(s) browser URL (not
    a data endpoint) whose search terms can be extracted, so the UI
    visit can type the query the engine endpoint would have carried.

    duckduckgo.com is exempt: it does not bot-wall fetch-style clients,
    and its image engine bootstraps every search with a vqd token fetched
    from the front page -- a full interactive session there would blow the
    engine's short timeout for no stealth gain.
    """
    parts = urlsplit(url)
    if (parts.hostname or "").lower() == "duckduckgo.com":
        return False
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    if _is_api_url(url):
        return False
    return bool(_search_query_from_url(url))


# -- post-search browsing ----------------------------------------------------
# Engines score a cookie jar by what it does around searches, not by the
# searches alone. After a browser-served search returns its results, a
# background task keeps the lane behaving like a reader: click an organic
# result, scroll and drift the pointer over the page, come back to the
# results, maybe visit more results. The HTTP traffic is pure observation;
# every click and scroll is real X input (see searx/network/human_input.py).

_POST_SEARCH_DWELL_RANGE = (10.0, 30.0)  # seconds per visited result page
_POST_SEARCH_EXTRA_VISITS = (1, 3)  # more result pages after the first
_POST_SEARCH_BUDGET_S = 180.0  # wall-clock cap for a whole browsing session
_POST_SEARCH_MAX_LINKS = 60  # candidate result links kept per SERP
# Same-site links that are the engine's outbound redirect wrappers: these
# ARE the organic click targets on the results page. Any other same-site
# link (verticals, related searches, settings) is not a reader's click.
_REDIRECT_HINTS = ("/ck/a", "/l/?uddg", "/url?q=", "/interstitial", "/proxy?")
# Clicking a bare image or archive URL is not reading; skip those links.
_POST_SEARCH_SKIP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".css", ".js",
    ".zip", ".rar", ".7z", ".gz", ".pdf", ".exe", ".dmg", ".iso", ".mp4",
)


def _is_browsable_search_url(url: str) -> bool:
    """A search-results GET worth continuing with human-like browsing.

    Like :py:func:`_is_human_search_candidate`, but duckduckgo included:
    its jar earns reputation the same way, it just skips the interactive
    search itself.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False
    if _is_api_url(url):
        return False
    return bool(_search_query_from_url(url))


def _registrable_site(host: str | None) -> str:
    """Last two labels of a hostname -- 'same engine site' granularity.

    A coarse heuristic on purpose: the engines browsed here (google, bing,
    ddg, brave) sit on plain second-level domains.
    """
    labels = [part for part in (host or "").lower().split(".") if part]
    return ".".join(labels[-2:])


def _browsable_links(hrefs, serp_url: str) -> list[str]:
    """Raw result hrefs worth a human click-through.

    Keeps off-site links and the engine's outbound redirect wrappers;
    drops engine-internal links (verticals, related searches, account
    pages), non-page assets, self links and duplicates. Returns the raw
    attribute values, so a locator can match the anchor exactly.
    """
    base_site = _registrable_site(urlsplit(serp_url).hostname)
    seen: set[str] = set()
    out: list[str] = []
    for href in hrefs or []:
        raw = (href or "").strip()
        if not raw:
            continue
        resolved = urljoin(serp_url, raw)
        parts = urlsplit(resolved)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            continue
        if _registrable_site(parts.hostname) == base_site:
            tail = f"{parts.path}?{parts.query}"
            if not any(hint in tail for hint in _REDIRECT_HINTS):
                continue
        if parts.path.lower().endswith(_POST_SEARCH_SKIP_EXTENSIONS):
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        out.append(raw)
        if len(out) >= _POST_SEARCH_MAX_LINKS:
            break
    return out


def _link_locator(page, raw_href: str):
    """Locator for the exact anchor attribute value seen in the DOM."""
    safe = raw_href.replace("\\", "\\\\").replace('"', '\\"')
    return page.locator(f'a[href="{safe}"]').first


def _looks_like_bot_challenge(status_code, headers) -> bool:
    """Heuristic: does this response look like a challenge / rate limit?"""
    if headers.get("cf-ray") is not None:
        return True
    if (headers.get("cf-mitigated") or "").lower() == "challenge":
        return True
    return status_code in (403, 429, 503)


def _looks_like_unresolved_challenge_body(body: bytes) -> bool:
    """Did the body come back as an unresolved challenge interstitial?"""
    if not body:
        return False
    text = body[:16384].decode("utf-8", errors="ignore")
    title_match = _CF_CHALLENGE_TITLE_RE.search(text)
    if title_match:
        title = title_match.group(1).strip().lower()
        if any(marker in title for marker in _CF_CHALLENGE_TITLE_MARKERS):
            return True
    body_wo_scripts = _SCRIPT_BLOCK_RE.sub("", text)
    return any(marker in body_wo_scripts for marker in _CF_CHALLENGE_BODY_MARKERS)


# A 200 response that is actually a meta/JS redirect gate ("enable JS or
# click here"): served by Google & friends instead of a 4xx. The only way
# through is rendering the page with a real JS engine.
_JS_INTERSTITIAL_MARKERS = (
    "if you are not redirected within",
    "please click here if you are not redirected",
    "enable javascript",
)


def _looks_like_js_interstitial(body: bytes) -> bool:
    if not body:
        return False
    text = body[:16384].decode("utf-8", errors="ignore")
    body_wo_scripts = _SCRIPT_BLOCK_RE.sub("", text)
    return any(marker in body_wo_scripts.lower() for marker in _JS_INTERSTITIAL_MARKERS)


# Identity headers the masqueraded browser owns. Engine-supplied values are
# dropped from fetch requests unless the engine opted out: the lane's
# identity is the binary's own (Linux Chromium, IP-derived locale), and a
# per-request engine UA on the same cookie jar would contradict everything
# else the lane emits.
_IDENTITY_HEADERS = frozenset(
    {
        "user-agent",
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
        "sec-fetch-dest",
        "sec-fetch-mode",
        "sec-fetch-site",
        "sec-fetch-user",
    }
)

# The lane identity must agree with where its requests come from: at pool
# start the public IP is resolved to country / timezone / language and every
# context is created with that locale. Playwright's en-US /
# America/Los_Angeles defaults on a non-US datacenter IP are a classic
# inconsistency search engines score.
_GEO_SERVICES = ("https://get.geojs.io/v1/ip/geo.json", "https://ipwho.is/", "https://ipinfo.io/json")
_GEO_FALLBACK: dict = {
    "locale": "en-US",
    "timezone": "America/Los_Angeles",
    "accept_language": "en-US,en;q=0.9",
    "geolocation": None,
    "source": "fallback",
}

# Primary browser language per country for the geo services that do not
# return a language list. Unknown countries stay English: a wrong language
# claim is worse than a common one.
_COUNTRY_PRIMARY_LANGUAGE: dict[str, str] = {
    # Americas
    "AR": "es", "BO": "es", "BR": "pt", "CA": "en", "CL": "es", "CO": "es",
    "CR": "es", "CU": "es", "DO": "es", "EC": "es", "GT": "es", "HN": "es",
    "MX": "es", "NI": "es", "PA": "es", "PE": "es", "PR": "es", "PY": "es",
    "SV": "es", "US": "en", "UY": "es", "VE": "es",
    # Europe
    "AL": "sq", "AT": "de", "BA": "bs", "BE": "nl", "BG": "bg", "BY": "be",
    "CH": "de", "CY": "el", "CZ": "cs", "DE": "de", "DK": "da", "EE": "et",
    "ES": "es", "FI": "fi", "FR": "fr", "GR": "el", "HR": "hr", "HU": "hu",
    "IE": "en", "IS": "is", "IT": "it", "LT": "lt", "LV": "lv", "MD": "ro",
    "MK": "mk", "MT": "mt", "NL": "nl", "NO": "no", "PL": "pl", "PT": "pt",
    "RO": "ro", "RS": "sr", "SE": "sv", "SI": "sl", "SK": "sk", "UA": "uk",
    "UK": "en", "GB": "en",
    # Asia / Middle East
    "AM": "hy", "AZ": "az", "BD": "bn", "CN": "zh", "GE": "ka", "HK": "zh",
    "ID": "id", "IL": "he", "IN": "en", "IQ": "ar", "IR": "fa", "JO": "ar",
    "JP": "ja", "KG": "ky", "KH": "km", "KR": "ko", "KW": "ar", "KZ": "kk",
    "LA": "lo", "LB": "ar", "LK": "si", "MM": "my", "MN": "mn", "MO": "zh",
    "MV": "dv", "MY": "ms", "NP": "ne", "OM": "ar", "PH": "en", "PK": "en",
    "QA": "ar", "SA": "ar", "SG": "en", "SY": "ar", "TH": "th", "TJ": "tg",
    "TM": "tk", "TR": "tr", "TW": "zh", "UZ": "uz", "VN": "vi", "YE": "ar",
    # Africa
    "AO": "pt", "CI": "fr", "DZ": "ar", "EG": "ar", "ET": "am", "GA": "fr",
    "GH": "en", "KE": "en", "LY": "ar", "MA": "ar", "MG": "mg", "ML": "fr",
    "MZ": "pt", "NG": "en", "SD": "ar", "SN": "fr", "TN": "ar", "TZ": "sw",
    "UG": "en", "ZA": "en", "ZM": "en", "ZW": "en",
    # Oceania
    "AU": "en", "FJ": "en", "NZ": "en", "PG": "en",
}

# The resolved identity is cached on disk: geo lookups must survive
# container restarts AND recreations without re-querying the services
# (they rate-limit, and the identity does not change with the process).
_GEO_CACHE_TTL_S = 48 * 3600.0
_GEO_CACHE_PATH = os.path.join(
    os.environ.get("__SEARXNG_DATA_PATH") or "/var/cache/searxng", "ip_locale.json"
)
_ip_locale_cache: dict | None = None


def _ip_locale_from_payload(data: dict) -> dict | None:
    """Build the locale identity from one geo-IP service payload.

    Handled providers: geojs.io (``country_code``, ``timezone``,
    ``latitude``/``longitude``), ipwho.is (``country_code``, ``timezone``
    object with an ``id``, numeric coordinates) and ipinfo.io
    (``country``, ``timezone``, "lat,long" ``loc``). Language: the
    ``languages`` field when a provider carries one, else the country's
    primary language from the static table, else English.
    """
    country = str(data.get("country_code") or data.get("country") or "").strip().upper()
    if len(country) != 2:
        return None
    codes = [
        code.strip().replace("_", "-")
        for code in str(data.get("languages") or "").split(",")
        if code.strip()
    ]
    # no language list from the provider: claim the country's primary
    # language; a country outside the table stays English (en-US)
    primary = codes[0] if codes else _COUNTRY_PRIMARY_LANGUAGE.get(country)
    if primary and "-" in primary:
        locale = primary
    elif primary:
        locale = f"{primary}-{country}"
    else:
        locale = "en-US"
    others = [code for code in codes if code != locale]
    lang_parts = [locale]
    if others:
        lang_parts.append(f"{others[0]};q=0.9")
        if not locale.startswith("en"):
            lang_parts.append("en;q=0.7")
    elif not locale.startswith("en"):
        lang_parts.append("en;q=0.9")
    else:
        lang_parts.append("en;q=0.8")
    accept_language = ",".join(lang_parts)
    timezone = data.get("timezone")
    if isinstance(timezone, dict):
        # ipwho.is: {"id": "Asia/Bangkok", ...}
        timezone = timezone.get("id")
    timezone = str(timezone or "").strip() or "America/Los_Angeles"
    latitude = data.get("latitude")
    longitude = data.get("longitude")
    if latitude is None or longitude is None:
        loc_lat, _, loc_lon = str(data.get("loc") or "").partition(",")
        latitude, longitude = loc_lat, loc_lon
    try:
        geolocation = {
            "latitude": float(latitude),
            "longitude": float(longitude),
            "accuracy": 40.0,
        }
    except (TypeError, ValueError):
        geolocation = None
    return {
        "locale": locale,
        "timezone": timezone,
        "accept_language": accept_language,
        "geolocation": geolocation,
    }


def _geo_cache_read() -> dict | None:
    """Return the cached locale identity when fresh and well-formed."""
    try:
        with open(_GEO_CACHE_PATH, encoding="utf-8") as cache_file:
            payload = json.load(cache_file)
        fetched_at = float(payload.get("fetched_at", 0.0))
        if time.time() - fetched_at > _GEO_CACHE_TTL_S:
            return None
        locale = _ip_locale_from_payload(payload.get("data") or {})
    except (OSError, TypeError, ValueError):
        return None
    if locale is None:
        return None
    age_days = max(0.0, time.time() - fetched_at) / 86400.0
    locale["source"] = f"cache, {age_days:.1f}d old"
    return locale


def _geo_cache_write(data: dict) -> None:
    """Persist the raw geo payload; best effort (read-only fs is fine).

    Atomic via tmp file + rename, so a reader never sees a partial write.
    """
    try:
        os.makedirs(os.path.dirname(_GEO_CACHE_PATH), exist_ok=True)
        tmp_path = _GEO_CACHE_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as cache_file:
            json.dump({"fetched_at": time.time(), "data": data}, cache_file)
        os.replace(tmp_path, _GEO_CACHE_PATH)
    except OSError:
        pass


def _detect_ip_locale() -> dict:
    """Resolve the public IP to a locale identity (blocking, one-time).

    The resolved identity is cached in process memory and on disk: the
    first pool start pays the lookup, later starts read the cache until
    it expires (``_GEO_CACHE_TTL_S``). The providers are tried in order;
    when all fail the static fallback applies.
    """
    global _ip_locale_cache  # pylint: disable=global-statement
    if _ip_locale_cache is not None:
        return _ip_locale_cache
    cached = _geo_cache_read()
    if cached is not None:
        _ip_locale_cache = cached
    else:
        # import inside the function: urllib pulls the SSL machinery and
        # the pool module must stay importable without it (tests)
        import urllib.request

        detected: dict | None = None
        source = _GEO_FALLBACK["source"]
        raw_data: dict | None = None
        for service in _GEO_SERVICES:
            try:
                with urllib.request.urlopen(service, timeout=8) as resp:  # noqa: S310
                    data = json.loads(resp.read().decode("utf-8", "replace"))
            except Exception:  # pylint: disable=broad-except
                continue
            detected = _ip_locale_from_payload(data)
            if detected is not None:
                source = service
                raw_data = data
                break
        if detected is not None:
            detected["source"] = source
            _geo_cache_write(raw_data or {})
        _ip_locale_cache = detected if detected is not None else dict(_GEO_FALLBACK)
    logger.info(
        "Browser pool identity: locale=%s timezone=%s accept-language=%s"
        " geolocation=%s (via %s)",
        _ip_locale_cache["locale"],
        _ip_locale_cache["timezone"],
        _ip_locale_cache["accept_language"],
        bool(_ip_locale_cache["geolocation"]),
        _ip_locale_cache["source"],
    )
    return _ip_locale_cache


class BrowserResponse:
    """Duck-typed replacement for :class:`SXNG_Response` (curl_cffi).

    Implements the response surface SearXNG engines and processors rely on:
    ``status_code``, ``ok``, ``reason``, ``headers``, ``content``, ``text``,
    ``url``, ``json()``, ``html()``, ``raise_for_status()``, ``cookies``,
    ``history``, ``request``, ``http_version`` and the ``search_params``
    attribute set by the online processor.
    """

    def __init__(
        self,
        status_code: int,
        headers: dict,
        content: bytes,
        url: str,
        method: str,
        reason: str | None = None,
        cookies: dict | None = None,
    ):
        self.status_code = status_code
        self.headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        self.content = content
        self.url = SXNG_URL(url)
        self.request = SimpleNamespace(method=method.upper(), url=url)
        self.reason = (
            reason if reason is not None else http.responses.get(status_code, "")
        )
        self.cookies = dict(cookies or {})
        self.history = []
        self.http_version = "HTTP/2"
        self.next_request = None
        self.encoding = None
        self.search_params = None
        self.ok = 200 <= status_code < 400
        self.is_redirect = False

    def _decoded_text(self) -> str:
        if self.encoding:
            return self.content.decode(self.encoding, errors="replace")
        content_type = self.headers.get("content-type", "")
        charset = None
        if "charset=" in content_type:
            charset = (
                content_type.split("charset=", 1)[1].split(";", 1)[0].strip().strip('"')
            )
        try:
            if charset:
                return self.content.decode(charset, errors="replace")
            return self.content.decode("utf-8", errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    @property
    def text(self) -> str:
        return self._decoded_text()

    def json(self, **kwargs):
        return json.loads(self.text, **kwargs)

    def html(self):
        """Parses the result into a HTML document via :py:obj:`lxml.html`."""
        return html.fromstring(self.content)

    def raise_for_status(self):
        if self.status_code >= 400:
            from curl_cffi.requests.exceptions import HTTPError

            raise HTTPError(f"{self.status_code} {self.reason}".strip(), response=self)

    def iter_lines(self):
        for line in self.text.splitlines():
            yield line

    def iter_content(self, chunk_size=None):
        for i in range(0, len(self.content), chunk_size or 8192):
            yield self.content[i : i + (chunk_size or 8192)]

    def close(self):
        pass

    def __repr__(self):
        return f"<BrowserResponse [{self.status_code} {self.url}]>"


class _Lane:
    """One browser process on its own X display, serving one request.

    One browser per lane (not one browser with N contexts) is what makes
    per-lane displays possible: a Chromium process binds a single display.
    In exchange, a lane crash takes down only its own lane. ``browser`` may
    be None for a persistent-context lane on some playwright versions; the
    context is then the aliveness signal.
    """

    def __init__(self, browser, context, display: str | None):
        self.browser = browser
        self.context = context
        self.display = display
        # SERP page handed over by a human-path fetch for post-search
        # browsing; the browsing session consumes (and closes) it.
        self.serp_page = None
        self.lock = asyncio.Lock()


class BrowserFetchPool:
    """Pool of masqueraded browser contexts bound to the asyncio loop.

    Created lazily on first use, from the loop that serves engine requests
    (see :py:obj:`searx.network.client.get_loop`). All public methods are
    coroutines and must run on that loop.
    """

    def __init__(
        self, pool_size: int = 3, verify: bool = True, proxy: str | None = None,
        human_fallback: bool = True, max_stealth: bool = False,
        profile_dir: str | None = None, post_search_browsing: bool = False,
    ):
        self._pool_size = max(1, pool_size)
        self._verify = verify
        self._proxy = proxy
        self._human_fallback = human_fallback
        self._max_stealth = max_stealth
        self._profile_dir = profile_dir
        self._post_search = post_search_browsing
        self._browsing_tasks: set[asyncio.Task] = set()
        self._lanes: list[_Lane] = []
        self._lane_cycle: asyncio.Queue | None = None
        self._init_lock = asyncio.Lock()
        self._playwright = None
        self._closed = False

    async def _init(self):
        if self._lanes:
            return
        async with self._init_lock:
            if self._lanes:
                return
            try:
                from playwright.async_api import async_playwright
            except ImportError as e:
                raise BrowserFetchError(
                    "outgoing.using_browser is enabled but the playwright "
                    "package is not installed. Install it with: "
                    "pip install playwright"
                ) from e

            executable_path = _discover_chromium()
            if executable_path is None:
                logger.warning(
                    "No distro Chromium found (looked in %s); using Playwright's"
                    " bundled browser, which is easier for bot detectors to"
                    " fingerprint",
                    ", ".join(_CHROMIUM_CANDIDATE_PATHS),
                )

            try:
                self._playwright = await async_playwright().start()
                for lane_index in range(self._pool_size):
                    self._lanes.append(
                        await self._launch_lane(lane_index, executable_path)
                    )
                self._lane_cycle = asyncio.Queue()
                for lane in self._lanes:
                    self._lane_cycle.put_nowait(lane)
                logger.info(
                    "Browser fetch pool up: %d lane(s) on displays %s, chromium=%s",
                    len(self._lanes),
                    ",".join(lane.display or "headless" for lane in self._lanes),
                    self._lanes[0].browser.version if self._lanes[0].browser else "unknown",
                )
            except Exception as e:
                # Stop playwright before re-raising: its node driver process
                # keeps running otherwise, one ~130 MiB leak per failed start.
                await self._shutdown_browser()
                raise BrowserFetchError(f"browser pool init failed: {e}") from e

    def _lane_profile_dir(self, lane_index: int) -> str | None:
        """This lane's persistent profile directory, or None for an
        ephemeral in-memory context (no ``outgoing.browser_profile_dir``, or
        the directory is not usable).

        One subdirectory per lane keeps the cookie jars fully isolated;
        Chromium holds a singleton lock per profile, so the 1:1 mapping
        also prevents two browsers from ever sharing one jar.
        """
        if not self._profile_dir:
            return None
        path = os.path.join(self._profile_dir, f"lane-{lane_index}")
        try:
            os.makedirs(path, exist_ok=True)
            if not os.access(path, os.W_OK):
                raise OSError("not writable")
        except OSError as err:
            logger.warning(
                "browser profile dir %s unusable (%s); lane %s runs with an"
                " ephemeral context",
                self._profile_dir, err, lane_index,
            )
            return None
        return path

    async def _launch_lane(self, lane_index: int, executable_path: str | None) -> _Lane:
        """Launch one lane: its own browser process on its own X display."""
        loop = asyncio.get_running_loop()
        display = await loop.run_in_executor(None, ensure_display, lane_index)
        env = dict(os.environ)
        if display:
            env["DISPLAY"] = display
        launch_kwargs = {
            "headless": display is None,
            "ignore_default_args": _OMIT_DEFAULT_ARGS,
            "args": _LAUNCH_ARGS,
            "env": env,
        }
        if executable_path:
            launch_kwargs["executable_path"] = executable_path
        if self._proxy:
            launch_kwargs["proxy"] = {"server": self._proxy}
        geo = await loop.run_in_executor(None, _detect_ip_locale)
        profile_path = self._lane_profile_dir(lane_index)
        if profile_path:
            # persistent profile: cookies, storage and earned clearances
            # survive lane crashes and process restarts
            context = await self._playwright.chromium.launch_persistent_context(
                profile_path, **launch_kwargs, **self._context_kwargs(geo)
            )
            browser = context.browser
        else:
            browser = await self._playwright.chromium.launch(**launch_kwargs)
            context = await browser.new_context(
                **self._context_kwargs(geo)
            )
        await context.add_init_script(_stealth_init_script(_language_tags(geo["accept_language"])))
        return _Lane(browser, context, display)

    def _context_kwargs(self, geo: dict) -> dict:
        """Context options matching the IP-derived identity. The UA is left
        at the binary's own value: kernel, TLS and Client-Hint headers say
        Linux Chromium, so no override is needed or wanted."""
        extra_headers = {
            # Accept-Language only: Chromium sets Accept and the
            # Sec-Fetch-* headers per request itself. Forcing
            # navigation headers at context level stamps
            # "Sec-Fetch-Dest: document" onto every script and XHR,
            # which is an egregious automation fingerprint.
            "Accept-Language": geo["accept_language"],
        }
        context_kwargs: dict = {
            "viewport": {"width": 1440, "height": 900},
            "locale": geo["locale"],
            "timezone_id": geo["timezone"],
            "has_touch": False,
            "java_script_enabled": True,
            "color_scheme": "light",
            "ignore_https_errors": not self._verify,
            "extra_http_headers": extra_headers,
        }
        if geo["geolocation"]:
            context_kwargs["geolocation"] = geo["geolocation"]
        return context_kwargs

    async def _shutdown_browser(self):
        """Tear down lanes, browsers and playwright, terminating the driver.

        Called on close and after a failed launch; without the explicit
        playwright stop the node driver process leaks (about 130 MiB each).
        """
        for task in list(self._browsing_tasks):
            task.cancel()
        if self._browsing_tasks:
            # let the sessions run their finally blocks (page close) while
            # the contexts still exist
            await asyncio.gather(*list(self._browsing_tasks), return_exceptions=True)
        for lane in self._lanes:
            try:
                await lane.context.close()
            except Exception:  # pylint: disable=broad-except
                pass
            if lane.browser is not None:
                try:
                    await lane.browser.close()
                except Exception:  # pylint: disable=broad-except
                    pass
        self._lanes.clear()
        self._lane_cycle = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # pylint: disable=broad-except
                pass
            self._playwright = None

    async def _ensure_browser_alive(self):
        """Restart lanes whose browser process died after a crash.

        A dead browser process leaves its lane unusable while ``_lanes``
        still looks initialized: without this check every fetch on the lane
        keeps raising TargetClosedError until the container is restarted.
        Only affected lanes restart -- one crashed lane must not take down
        the whole pool.
        """
        if all(self._lane_is_alive(lane) for lane in self._lanes):
            return
        async with self._init_lock:
            for lane in self._lanes:
                if self._lane_is_alive(lane):
                    continue
                logger.warning("Lane's browser process is gone; restarting the lane")
                await self._restart_lane(lane)

    @staticmethod
    def _lane_is_alive(lane: _Lane) -> bool:
        if lane.browser is not None:
            return lane.browser.is_connected()
        # persistent contexts may not expose their browser: a dead browser
        # process closes its context, so the context state is the signal
        try:
            return not lane.context.is_closed()
        except Exception:  # pylint: disable=broad-except
            return False

    async def _restart_lane(self, lane: _Lane) -> None:
        """Rebuild a lane in place: the lane cycle queue holds this object."""
        lane_index = self._lanes.index(lane)
        try:
            await lane.context.close()
        except Exception:  # pylint: disable=broad-except
            pass
        if lane.browser is not None:
            try:
                await lane.browser.close()
            except Exception:  # pylint: disable=broad-except
                pass
        replacement = await self._launch_lane(lane_index, _discover_chromium())
        lane.browser = replacement.browser
        lane.context = replacement.context
        lane.display = replacement.display

    async def close(self):
        if self._closed:
            return
        self._closed = True
        await self._shutdown_browser()

    # pylint: disable=too-many-arguments, too-many-locals
    async def fetch(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        params: dict | None = None,
        data=None,
        json_body=None,
        content=None,
        cookies: dict | None = None,
        timeout: float | None = None,
        allow_redirects: bool = True,
        max_redirects: int = 30,
        keep_identity_headers: bool = False,
    ) -> BrowserResponse:
        await self._init()
        await self._ensure_browser_alive()
        if params:
            query = urlencode({k: v for k, v in params.items() if v is not None})
            if query:
                url = url + ("&" if "?" in url else "?") + query

        timeout_s = max(timeout or 0, _MIN_TIMEOUT_S)

        # One lane per in-flight request: wait for the next free lane.
        lane = await self._lane_cycle.get()
        # Decided up front so the interactive path knows to hand its page
        # over; the actual spawn happens once the response is captured.
        keep_page = method.upper() == "GET" and self._post_search_wanted(url)
        browsing = False
        try:
            async with lane.lock:
                if self._max_stealth and method.upper() == "GET":
                    # Maximum stealth mode: the interactive visit comes
                    # first, not as a challenge fallback. When it fails, the
                    # request fails: no fetch-style attempt may slip out to
                    # the search engine behind a human visit.
                    if _is_human_search_candidate(url):
                        human = await self._fetch_via_human_search(
                            lane, url, timeout_s, keep_page=keep_page
                        )
                        if human is not None:
                            browsing = await self._maybe_start_post_search_browsing(
                                lane, human
                            )
                            return human
                        raise BrowserFetchError(
                            "max stealth: human search failed for"
                            f" {url}; refusing a fetch-style request"
                        )
                response = await self._fetch_on_lane(
                    lane,
                    method.upper(),
                    url,
                    headers=headers,
                    data=data,
                    json_body=json_body,
                    content=content,
                    cookies=cookies,
                    timeout_s=timeout_s,
                    allow_redirects=allow_redirects,
                    max_redirects=max_redirects,
                    keep_identity_headers=keep_identity_headers,
                    keep_page=keep_page,
                )
                browsing = await self._maybe_start_post_search_browsing(lane, response)
                return response
        finally:
            if not browsing:
                self._lane_cycle.put_nowait(lane)

    async def _fetch_on_lane(
        self,
        lane: _Lane,
        method: str,
        url: str,
        *,
        headers=None,
        data=None,
        json_body=None,
        content=None,
        cookies=None,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        allow_redirects: bool = True,
        max_redirects: int = 30,
        keep_identity_headers: bool = False,
        keep_page: bool = False,
    ) -> BrowserResponse:
        timeout_ms = int(timeout_s * 1000)
        request_headers = self._build_request_headers(
            headers, cookies, keep_identity_headers
        )
        context = lane.context
        request = context.request
        request_kwargs = {
            "headers": request_headers,
            "timeout": timeout_ms,
            "max_redirects": 0 if not allow_redirects else max_redirects,
        }
        is_post_like = method in ("POST", "PUT", "PATCH")
        if is_post_like:
            if json_body is not None:
                request_kwargs["data"] = json.dumps(json_body)
                request_headers.setdefault("content-type", "application/json")
            elif content is not None:
                request_kwargs["data"] = content
            elif data is not None:
                if isinstance(data, dict):
                    # form-encode dicts: the curl client sends dict data as
                    # application/x-www-form-urlencoded, match that here
                    request_kwargs["data"] = urlencode(data)
                    request_headers.setdefault(
                        "content-type", "application/x-www-form-urlencoded"
                    )
                else:
                    request_kwargs["data"] = data

        try:
            if method in ("GET", "HEAD", "OPTIONS"):
                api_response = await request.get(url, **request_kwargs)
            elif method == "POST":
                api_response = await request.post(url, **request_kwargs)
            elif method == "PUT":
                api_response = await request.put(url, **request_kwargs)
            elif method == "PATCH":
                api_response = await request.patch(url, **request_kwargs)
            elif method == "DELETE":
                api_response = await request.delete(url, **request_kwargs)
            else:
                raise BrowserFetchError(
                    f"Unsupported method for browser fetch: {method}"
                )
        except BrowserFetchError:
            raise
        except Exception as e:
            raise BrowserFetchError(f"browser fetch failed: {e}") from e

        body = await api_response.body()
        status = api_response.status
        response_headers = {
            str(k).lower(): str(v) for k, v in api_response.headers.items()
        }

        needs_warm_up = _looks_like_bot_challenge(status, response_headers) or (
            _looks_like_unresolved_challenge_body(body)
        )
        if needs_warm_up:
            logger.debug(
                "bot challenge on %s (%s); rendering page and retrying", url, status
            )
            if self._human_fallback and method == "GET":
                # Drive the provider's search UI like a human: often the only
                # thing that passes, and cheaper than a doomed warm-up plus
                # a suspended engine.
                human = await self._fetch_via_human_search(
                    lane, url, timeout_s, keep_page=keep_page
                )
                if human is not None:
                    return human
            warmed = await self._warm_up_and_retry(
                lane, method, url, request_kwargs=request_kwargs
            )
            if warmed is not None:
                api_response, body, status, response_headers = warmed

        if status == 200 and _looks_like_js_interstitial(body):
            # a JS redirect gate: render the page with the real JS engine,
            # the browser follows the redirect and lands on the content
            if self._human_fallback and method == "GET":
                human = await self._fetch_via_human_search(
                    lane, url, timeout_s, keep_page=keep_page
                )
                if human is not None:
                    return human
            rendered = await self._render_page(lane, url, timeout_s, keep_page=keep_page)
            if rendered is not None:
                return rendered

        if status in (402, 403):
            raise SearxEngineAccessDeniedException(message="HTTP error " + str(status))
        if status == 429:
            raise SearxEngineTooManyRequestsException()

        cookies_map = {c["name"]: c["value"] for c in await context.cookies(url)}
        return BrowserResponse(
            status_code=status,
            headers=response_headers,
            content=body,
            url=api_response.url,
            method=method,
            cookies=cookies_map,
        )

    async def _render_page(
        self, lane: _Lane, url: str, timeout_s: float, keep_page: bool = False
    ):
        """Render `url` in a real page (JS enabled) and return the final DOM.

        Used as last resort when a fetch-style request returned a challenge
        or a JS redirect gate. Returns a BrowserResponse of the rendered
        content, or None when the navigation failed. With ``keep_page`` the
        page is handed to post-search browsing instead of being closed.
        """
        context = lane.context
        try:
            page = await context.new_page()
            handed_over = False
            try:
                response = await page.goto(
                    url, timeout=timeout_s * 1000, wait_until="commit"
                )
                status = response.status if response else 200
                if status in (403, 429, 503):
                    await page.wait_for_timeout(_BOT_CHALLENGE_GRACE_MS)
                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=_BOT_CHALLENGE_GRACE_MS
                    )
                except Exception:  # pylint: disable=broad-except
                    pass
                html_content = await page.content()
                final_url = page.url
                rendered = BrowserResponse(
                    status_code=status,
                    headers={"content-type": "text/html; charset=utf-8"},
                    content=html_content.encode("utf-8", errors="replace"),
                    url=final_url,
                    method="GET",
                )
                if keep_page and self._post_search_wanted(final_url):
                    lane.serp_page = page
                    handed_over = True
                return rendered
            finally:
                if not handed_over:
                    await page.close()
        except Exception:  # pylint: disable=broad-except
            logger.warning("Render fallback failed for %s", url, exc_info=True)
            return None

    async def _settle_after_search(
        self, page, pointer, timeout_s: float
    ) -> None:
        """Wait out the post-click navigation, solving challenges on the way.

        The search submit navigates asynchronously: a ``networkidle`` or URL
        check entered immediately still describes the search page. First
        wait for the URL to actually leave the search page, then poll until
        it leaves the challenge interstitial (clicking its checkbox with the
        real mouse when one appears), or until the budget is spent -- the
        captured DOM then tells the engine what happened.
        """
        # pylint: disable=import-outside-toplevel
        from searx.network.human_input import human_clear_challenge

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(8.0, min(timeout_s, 30.0))
        start_url = page.url

        # phase 1: the submission must navigate somewhere
        while loop.time() < deadline and page.url == start_url:
            try:
                await page.wait_for_url(
                    lambda url: url != start_url, timeout=2000
                )
            except Exception:  # pylint: disable=broad-except
                pass
        if page.url == start_url:
            logger.warning(
                "human search: submission never navigated away from %s", start_url
            )
            return

        # phase 2: challenges on the arrival page
        iterations = 0
        while loop.time() < deadline:
            iterations += 1
            if not _is_challenge_url(page.url):
                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=_BOT_CHALLENGE_GRACE_MS
                    )
                except Exception:  # pylint: disable=broad-except
                    pass
                if not _is_challenge_url(page.url):
                    return
            solved = await human_clear_challenge(
                page, pointer, settle_ms=_BOT_CHALLENGE_GRACE_MS
            )
            if not solved:
                await asyncio.sleep(random.uniform(0.6, 1.2))  # noqa: S311

    async def _fetch_via_human_search(
        self, lane: _Lane, url: str, timeout_s: float, keep_page: bool = False
    ):
        """Serve a GET search by driving the provider like a human.

        Used as the challenge fallback for challenged/interstitial GETs and,
        in maximum stealth mode (``outgoing.browser_max_stealth``), up front
        for every supported request. Open the provider's front page (like an
        address-bar entry, carrying the request's ``hl``/``gl`` locale),
        type the query into its search box and click search with real X
        input (see :py:mod:`searx.network.human_input`), solving any
        challenge checkbox met on the way, then read the results like a
        human would.

        The response returned to the engine parser is the DOM captured from
        the live page (:py:meth:`playwright.page.Page.content`). Nothing is
        re-fetched fetch-style afterwards: a programmatic replay of the
        results URL is exactly the automation tell the interactive visit was
        meant to avoid, and a data shell served to a replay would shadow
        the good DOM.

        Returns None when the flow is not applicable or failed entirely.
        """
        # pylint: disable=import-outside-toplevel
        from searx.network.human_input import (
            human_clear_challenge,
            human_read_results,
            human_search_on_page,
            human_session,
        )

        if _is_api_url(url):
            return None
        query = _search_query_from_url(url)
        if not query:
            return None
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return None
        homepage = _homepage_url_from_search_url(url)
        goto_timeout_ms = int(max(5.0, min(timeout_s, 20.0)) * 1000)

        rendered_html: bytes | None = None
        rendered_url = homepage
        try:
            # Each lane runs on its own X display with its own pointer, so
            # human sessions on different lanes run concurrently -- no
            # process-wide lock, no cross-window click theft.
            async with human_session(lane.display) as pointer:
                page = await lane.context.new_page()
                handed_over = False
                try:
                    try:
                        await page.goto(
                            homepage, timeout=goto_timeout_ms, wait_until="domcontentloaded"
                        )
                    except Exception:  # pylint: disable=broad-except
                        logger.warning(
                            "human search: homepage %s failed, trying results URL", homepage
                        )
                    await human_clear_challenge(page, pointer)
                    if await human_search_on_page(page, query, pointer):
                        await self._settle_after_search(page, pointer, timeout_s)
                    else:
                        # No usable search box: navigate the results URL so
                        # at least challenge JS runs in a real page.
                        await page.goto(
                            url, timeout=goto_timeout_ms, wait_until="domcontentloaded"
                        )
                        await human_clear_challenge(page, pointer)
                        try:
                            await page.wait_for_load_state(
                                "networkidle", timeout=_BOT_CHALLENGE_GRACE_MS
                            )
                        except Exception:  # pylint: disable=broad-except
                            pass
                    await human_read_results(page, pointer)
                    if _is_challenge_url(page.url):
                        # a flagged search can be routed to /sorry late, after
                        # the results already rendered
                        await self._settle_after_search(page, pointer, timeout_s)
                        await human_read_results(page, pointer)
                    rendered_url = page.url
                    rendered_html = (await page.content()).encode(
                        "utf-8", errors="replace"
                    )
                    if keep_page:
                        # the live results page becomes the starting point of
                        # post-search browsing; the session closes it
                        lane.serp_page = page
                        handed_over = True
                finally:
                    if not handed_over:
                        await page.close()
        except Exception:  # pylint: disable=broad-except
            logger.warning("human search fallback failed for %s", url, exc_info=True)
            return None

        if rendered_html:
            logger.info(
                "human search: captured rendered DOM for %s (%s bytes)",
                rendered_url,
                len(rendered_html),
            )
            return BrowserResponse(
                status_code=200,
                headers={"content-type": "text/html; charset=utf-8"},
                content=rendered_html,
                url=rendered_url,
                method="GET",
            )
        return None

    def _post_search_wanted(self, url: str) -> bool:
        """Should this search get a follow-up browsing session?

        Skipped when disabled, the pool is closing, or no lane would remain
        idle for actual searches: browsing must never starve the pool.
        """
        return (
            self._post_search
            and not self._closed
            and self._lane_cycle is not None
            and self._lane_cycle.qsize() > 0
            and _is_browsable_search_url(url)
        )

    async def _maybe_start_post_search_browsing(self, lane: _Lane, response) -> bool:
        """Spawn the background imitation for a captured search response.

        Returns True when the lane must stay checked out until the browsing
        session hands it back. Never raises and never blocks the request:
        the response is already captured, browsing is a bonus.
        """
        try:
            if not (
                isinstance(response, BrowserResponse)
                and response.status_code == 200
                and "text/html" in (response.headers.get("content-type") or "")
            ):
                await self._discard_serp_page(lane)
                return False
            if not self._post_search_wanted(response.url):
                await self._discard_serp_page(lane)
                return False
            page, lane.serp_page = lane.serp_page, None
            task = asyncio.create_task(
                self._post_search_browsing_session(lane, page, response.url),
                name=f"post-search-browsing-{lane.display or 'headless'}",
            )
            self._browsing_tasks.add(task)
            task.add_done_callback(self._browsing_tasks.discard)
            logger.info(
                "post-search browsing: lane %s stays on %s",
                lane.display or "headless",
                response.url,
            )
            return True
        except Exception:  # pylint: disable=broad-except
            logger.debug("post-search browsing spawn failed", exc_info=True)
            return False

    async def _discard_serp_page(self, lane: _Lane) -> None:
        page, lane.serp_page = lane.serp_page, None
        if page is not None:
            try:
                await page.close()
            except Exception:  # pylint: disable=broad-except
                pass

    async def _post_search_browsing_session(
        self, lane: _Lane, page, serp_url: str
    ) -> None:
        """Imitate a reader continuing past the results page.

        Runs while the lane stays checked out: click an organic result,
        scroll and drift the pointer over it, return to the results, maybe
        visit more. On completion the page is closed and the lane returns
        to the cycle.
        """
        # pylint: disable=import-outside-toplevel
        from searx.network.human_input import HumanInputError, human_session

        started = time.monotonic()
        visits = 0
        try:
            try:
                async with human_session(lane.display) as pointer:
                    visits = await self._browse_serp(lane, page, serp_url, pointer)
            except HumanInputError:
                # headless lane: CDP-trusted input instead of XTEST
                visits = await self._browse_serp(lane, page, serp_url, None)
        except asyncio.CancelledError:
            logger.debug("post-search browsing cancelled on lane %s", lane.display)
            raise
        except Exception:  # pylint: disable=broad-except
            logger.debug(
                "post-search browsing failed on lane %s", lane.display, exc_info=True
            )
        finally:
            await self._discard_serp_page(lane)
            if not self._closed and self._lane_cycle is not None:
                try:
                    self._lane_cycle.put_nowait(lane)
                except Exception:  # pylint: disable=broad-except
                    pass
            logger.info(
                "post-search browsing done on lane %s: %d visit(s) in %.0fs",
                lane.display or "headless",
                visits,
                time.monotonic() - started,
            )

    async def _browse_serp(self, lane: _Lane, page, serp_url: str, pointer) -> int:
        """Click through result pages; returns the number of visits made."""
        if self._closed:
            return 0
        if page is None:
            page = await lane.context.new_page()
            lane.serp_page = page
            try:
                await page.goto(serp_url, timeout=20000, wait_until="domcontentloaded")
            except Exception:  # pylint: disable=broad-except
                logger.debug(
                    "post-search browsing: results page %s failed to open", serp_url
                )
                return 0
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:  # pylint: disable=broad-except
            pass
        try:
            raw_hrefs = await page.eval_on_selector_all(
                "a[href]", "els => els.map(e => e.getAttribute('href'))"
            )
        except Exception:  # pylint: disable=broad-except
            return 0
        links = _browsable_links(raw_hrefs, page.url)
        if not links:
            logger.debug(
                "post-search browsing: no result links found on %s", page.url
            )
            return 0
        random.shuffle(links)
        deadline = time.monotonic() + _POST_SEARCH_BUDGET_S
        visits_left = 1 + random.randint(*_POST_SEARCH_EXTRA_VISITS)
        visits = 0
        for raw_href in links:
            if visits_left <= 0 or time.monotonic() > deadline - 15.0:
                break
            if await self._visit_result_page(page, pointer, raw_href, deadline):
                visits += 1
                visits_left -= 1
        return visits

    async def _visit_result_page(self, page, pointer, raw_href: str, deadline: float) -> bool:
        """One result click-through: open, dwell, return to the results."""
        # pylint: disable=import-outside-toplevel
        from searx.network.human_input import _human_click_locator, _human_idle

        serp_url = page.url
        locator = _link_locator(page, raw_href)
        try:
            await locator.scroll_into_view_if_needed(timeout=4000)
        except Exception:  # pylint: disable=broad-except
            return False
        if pointer is not None:
            clicked = await _human_click_locator(locator, pointer)
        else:
            clicked = await self._cdp_click(page, locator)
        if not clicked:
            return False
        opened = await self._wait_click_target(page, serp_url, deadline)
        if opened is None:
            return False
        if _is_challenge_url(opened.url):
            # a rebuffed click-through: back to the results, no dwelling
            await self._return_to_serp(page, opened, serp_url)
            return False
        dwell = min(
            random.uniform(*_POST_SEARCH_DWELL_RANGE),
            max(5.0, deadline - time.monotonic()),
        )
        await self._dwell_on_page(opened, pointer, dwell)
        await self._return_to_serp(page, opened, serp_url)
        if pointer is not None:
            await _human_idle(pointer, random.uniform(1.0, 4.0))
        else:
            await asyncio.sleep(random.uniform(1.0, 4.0))
        return True

    @staticmethod
    async def _wait_click_target(page, serp_url: str, deadline: float):
        """What the click did: a new tab, a same-tab navigation, or nothing."""
        before = {p for p in page.context.pages if not p.is_closed()}
        for _ in range(20):
            await asyncio.sleep(0.5)
            fresh = [
                p for p in page.context.pages if p not in before and not p.is_closed()
            ]
            if fresh:
                target = fresh[0]
                try:
                    await target.wait_for_url(
                        lambda url: url != "about:blank", timeout=8000
                    )
                except Exception:  # pylint: disable=broad-except
                    pass
                if target.url == "about:blank":
                    return None  # opened but never navigated: dead tab
                return target
            if page.url != serp_url:
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:  # pylint: disable=broad-except
                    pass
                return page
            if time.monotonic() > deadline:
                return None
        return None

    @staticmethod
    async def _return_to_serp(page, opened, serp_url: str) -> None:
        """Back on the results page: close the tab, or go back in history."""
        if opened is not page:
            try:
                await opened.close()
            except Exception:  # pylint: disable=broad-except
                pass
            return
        try:
            await page.go_back(timeout=15000, wait_until="domcontentloaded")
        except Exception:  # pylint: disable=broad-except
            pass
        if page.url != serp_url:
            try:
                await page.goto(serp_url, timeout=15000, wait_until="domcontentloaded")
            except Exception:  # pylint: disable=broad-except
                pass

    @staticmethod
    async def _cdp_click(page, locator) -> bool:
        """Headless fallback: CDP-trusted click at the element center."""
        try:
            box = await locator.bounding_box()
        except Exception:  # pylint: disable=broad-except
            return False
        if not box or box["width"] <= 1 or box["height"] <= 1:
            return False
        await page.mouse.move(
            box["x"] + box["width"] / 2,
            box["y"] + box["height"] / 2,
            steps=random.randint(8, 20),
        )
        await asyncio.sleep(random.uniform(0.08, 0.3))
        await page.mouse.down()
        await asyncio.sleep(random.uniform(0.04, 0.12))
        await page.mouse.up()
        return True

    @staticmethod
    async def _dwell_on_page(page, pointer, seconds: float) -> None:
        """Read a page: wheel scrolls with idle hand drifts in between."""
        # pylint: disable=import-outside-toplevel
        from searx.network.human_input import _human_idle

        if pointer is None:
            end = time.monotonic() + seconds
            viewport = page.viewport_size or {"width": 1280, "height": 720}
            while time.monotonic() < end:
                await page.mouse.wheel(0, random.randint(300, 900))
                await asyncio.sleep(random.uniform(0.6, 1.6))
                if random.random() < 0.5:
                    await page.mouse.move(
                        random.uniform(0, viewport["width"]),
                        random.uniform(0, viewport["height"]),
                        steps=random.randint(5, 15),
                    )
            return
        await _human_idle(pointer, random.uniform(0.8, 2.0))  # first look
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            pointer.scroll(-random.randint(2, 6))  # scroll down
            await asyncio.sleep(random.uniform(0.6, 1.6))
            if random.random() < 0.3:
                pointer.scroll(random.randint(1, 3))  # re-read a little
                await asyncio.sleep(random.uniform(0.3, 0.9))
            if random.random() < 0.5:
                await _human_idle(pointer, random.uniform(0.7, 1.8))

    async def _warm_up_and_retry(
        self, lane: _Lane, method: str, url: str, *, request_kwargs: dict
    ):
        """Navigate the challenge in a real page, then re-fetch fetch-style.

        The page visit lets the challenge JS run (and set clearance cookies
        on the context); the follow-up fetch reuses them. For POST-like
        methods the challenge page is fetched with GET (POST bodies cannot be
        replayed by a navigation).
        """
        context = lane.context
        try:
            page = await context.new_page()
            try:
                response = await page.goto(
                    url, timeout=_DEFAULT_TIMEOUT_S * 1000, wait_until="commit"
                )
                status = response.status if response else None
                if status is not None and status in (403, 429, 503):
                    await page.wait_for_timeout(_BOT_CHALLENGE_GRACE_MS)
                try:
                    await page.wait_for_load_state(
                        "networkidle", timeout=_BOT_CHALLENGE_GRACE_MS
                    )
                except Exception:  # pylint: disable=broad-except
                    pass
            finally:
                await page.close()
        except Exception:  # pylint: disable=broad-except
            logger.warning(
                "Challenge warm-up navigation failed for %s", url, exc_info=True
            )
            return None

        try:
            retry_kwargs = dict(request_kwargs)
            retry_kwargs["max_redirects"] = max(
                retry_kwargs.get("max_redirects", 30), 1
            )
            if method == "POST":
                retry_response = await context.request.post(url, **retry_kwargs)
            else:
                retry_response = await context.request.get(url, **retry_kwargs)
            body = await retry_response.body()
            return retry_response, body, retry_response.status, retry_response.headers
        except Exception:  # pylint: disable=broad-except
            logger.warning("Post-warm-up refetch failed for %s", url, exc_info=True)
            return None

    @staticmethod
    def _build_request_headers(
        headers: dict | None, cookies: dict | None, keep_identity_headers: bool = False
    ) -> dict:
        """Merge masquerade defaults with engine-provided headers.

        Engine headers win for the keys they set. Hop-by-hop and
        transport-managed headers are dropped: the browser stack computes
        them itself and stale values are a fingerprint signal.

        By default identity headers (User-Agent, Client Hints, Sec-Fetch-*)
        are dropped as well: the masqueraded browser owns the identity its
        cookies were earned with, and an engine-supplied UA switches the
        identity per request on the same cookie jar -- an easy detector
        flag. Engines that need their own UA to get a parseable layout opt
        out with the module attribute ``browser_keep_identity_headers``.
        """
        dropped = {"host", "content-length", "connection", "accept-encoding", "cookie"}
        if not keep_identity_headers:
            dropped = dropped | _IDENTITY_HEADERS
        merged: dict[str, str] = {}
        for key, value in (headers or {}).items():
            if str(key).lower() in dropped:
                continue
            merged[str(key).lower()] = str(value)
        if cookies:
            merged["cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        return merged


_POOL: BrowserFetchPool | None = None


def get_browser_fetch_pool() -> BrowserFetchPool:
    """Return the process-wide browser fetch pool (created on first use)."""
    global _POOL  # pylint: disable=global-statement
    from searx import get_setting

    if _POOL is None:
        _POOL = BrowserFetchPool(
            pool_size=get_setting("outgoing.browser_pool_size", 3),
            verify=get_setting("outgoing.verify", True),
            proxy=_proxy_from_proxies_setting(get_setting("outgoing.proxies", None)),
            human_fallback=get_setting("outgoing.browser_human_fallback", True),
            max_stealth=get_setting("outgoing.browser_max_stealth", False),
            profile_dir=get_setting("outgoing.browser_profile_dir", "") or None,
            post_search_browsing=get_setting("outgoing.browser_post_search_browsing", False),
        )
    return _POOL


def _proxy_from_proxies_setting(proxies) -> str | None:
    """Extract a single proxy URL for the browser from outgoing settings."""
    if not proxies:
        return None
    if isinstance(proxies, str):
        return proxies
    if isinstance(proxies, dict):
        for key in ("all://", "https://", "http://"):
            value = proxies.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, list) and value:
                return value[0]
    return None


def close_browser_fetch_pool_sync():
    """Best-effort pool shutdown for process exit."""
    global _POOL  # pylint: disable=global-statement
    from searx.network.client import get_loop

    pool, _POOL = _POOL, None
    loop = get_loop()
    if pool is None or loop is None or loop.is_closed():
        return
    try:
        future = asyncio.run_coroutine_threadsafe(pool.close(), loop)
        future.result(5)
    except Exception:  # pylint: disable=broad-except
        pass


atexit.register(close_browser_fetch_pool_sync)
