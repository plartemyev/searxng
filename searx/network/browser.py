# SPDX-License-Identifier: AGPL-3.0-or-later
"""Masqueraded Chromium fetch pool for engine requests.

When ``outgoing.using_browser`` is enabled in settings.yml, engine HTTP
requests are served by a real, Playwright-driven Chromium instead of the
curl_cffi client. The browser is tuned to look like a user-started browser
(the same posture the Onyx web crawler uses):

- a distro-packaged Chromium binary (not Playwright's bundled fork, which
  ships automation-friendly defaults detectors fingerprint)
- Playwright's automation-flavored default launch args stripped
- headed under an auto-started Xvfb display when possible (headless is a
  strong bot signal even in "new" headless mode)
- UA and Client Hints derived from the real binary version, and a page-side
  init script aligning ``navigator.platform``, WebGL vendor/renderer,
  plugins and ``userAgentData`` with those claims

GET requests use a fetch-style ``context.request.get`` (browser TLS stack +
cookie jar, no page render). On a bot challenge (Cloudflare interstitial,
403/429) the URL is first navigated in a real page so challenge JS resolves
and clearance cookies land in the context, then the fetch is retried.

The pool owns N browser contexts ("lanes"). Each lane serves one request at
a time; cookies persist per lane, so solved challenges benefit later
requests on the same lane.
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
from urllib.parse import parse_qs, urlsplit

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

_XVFB_DISPLAY = ":99"
_XVFB_GEOMETRY = "1440x900x24"

_xvfb_process = None
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


def _cleanup_stale_x_locks():
    """Remove X lock/socket leftovers from a previous container run.

    The container filesystem survives restarts while processes do not: a
    stale lock for the display makes a freshly started Xvfb exit at once,
    and a stale socket then looks like a working display.
    """
    display_number = _XVFB_DISPLAY.lstrip(":")
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

    Xvfb runs without access control, but python-xlib (pyautogui) still
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


def ensure_display():
    """Return an X display for a headed browser, starting Xvfb if needed.

    Returns None when headed mode is impossible, in which case the caller
    falls back to headless.
    """
    global _xvfb_process
    existing_display = os.environ.get("DISPLAY")
    if existing_display:
        return existing_display
    with _xvfb_lock:
        if _xvfb_process is not None and _xvfb_process.poll() is None:
            return _XVFB_DISPLAY
        xvfb = shutil.which("Xvfb")
        if xvfb is None:
            return None
        try:
            os.makedirs("/tmp/.X11-unix", exist_ok=True)  # noqa: S108
            _cleanup_stale_x_locks()
            _xvfb_process = subprocess.Popen(  # pylint: disable=consider-using-with
                [
                    xvfb,
                    _XVFB_DISPLAY,
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
        socket_path = f"/tmp/.X11-unix/X{_XVFB_DISPLAY.lstrip(':')}"  # noqa: S108
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if _xvfb_process.poll() is not None:
                break
            if os.path.exists(socket_path):
                logger.info(
                    "Started Xvfb on %s for masqueraded browser fetches", _XVFB_DISPLAY
                )
                _bootstrap_xauth()
                return _XVFB_DISPLAY
            time.sleep(0.1)
        logger.warning(
            "Xvfb on %s did not come up; falling back to headless browser",
            _XVFB_DISPLAY,
        )
        try:
            _xvfb_process.kill()
        except OSError:
            pass
        _xvfb_process = None
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


def _stealth_init_script(chrome_major: str, chrome_full: str) -> str:
    """Init script aligning every JS-visible surface with a Windows Chrome
    ``{chrome_major}`` identity. Claims must match the UA/Client Hints set on
    the context."""
    return """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
    Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
    window.chrome = { runtime: {}, loadTimes: function () {}, csi: function () {} };
    const fakePlugin = (name) => {
        const p = { name, description: name,
                    filename: name.toLowerCase().replaceAll(' ', '_'), length: 1 };
        p[0] = { type: 'application/pdf', suffixes: 'pdf', description: name };
        return p;
    };
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const arr = [fakePlugin('PDF Viewer'), fakePlugin('Chrome PDF Viewer'),
                         fakePlugin('Chromium PDF Viewer'),
                         fakePlugin('Microsoft Edge PDF Viewer'),
                         fakePlugin('WebKit built-in PDF')];
            arr.namedItem = (n) => arr.find(p => p.name === n) || null;
            arr.item = (i) => arr[i] || null;
            arr.refresh = () => {};
            return arr;
        }
    });
    Object.defineProperty(navigator, 'mimeTypes', {
        get: () => {
            const arr = [{ type: 'application/pdf', suffixes: 'pdf', description: '' }];
            arr.namedItem = (n) => arr.find(m => m.type === n) || null;
            arr.item = (i) => arr[i] || null;
            return arr;
        }
    });
    if (window.Notification) {
        Object.defineProperty(Notification, 'permission', { get: () => 'default' });
    }
    const patchGL = (proto) => {
        const orig = proto.getParameter;
        proto.getParameter = function (param) {
            // UNMASKED_VENDOR_WEBGL / UNMASKED_RENDERER_WEBGL: headless and VM
            // builds report SwiftShader/llvmpipe, a top bot signal.
            if (param === 37445) return 'Google Inc. (NVIDIA)';
            if (param === 37446) {
                return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)';
            }
            return orig.call(this, param);
        };
    };
    if (window.WebGLRenderingContext) patchGL(WebGLRenderingContext.prototype);
    if (window.WebGL2RenderingContext) patchGL(WebGL2RenderingContext.prototype);
    const brands = [
        { brand: 'Chromium', version: '__MAJOR__' },
        { brand: 'Google Chrome', version: '__MAJOR__' },
        { brand: 'Not:A-Brand', version: '24' },
    ];
    Object.defineProperty(navigator, 'userAgentData', {
        get: () => ({
            brands,
            mobile: false,
            platform: 'Windows',
            getHighEntropyValues: (hints) => Promise.resolve({
                architecture: 'x86',
                bitness: '64',
                model: '',
                mobile: false,
                platform: 'Windows',
                platformVersion: '15.0.0',
                uaFullVersion: '__FULL__',
                fullVersionList: brands,
                wow64: false,
            }),
            toJSON: () => ({ brands, mobile: false, platform: 'Windows' }),
        }),
    });
    """.replace("__MAJOR__", chrome_major).replace("__FULL__", chrome_full)


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
    """JSON API endpoints have no search UI to drive with human input."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if host.startswith("api.") or host.startswith("apis."):
        return True
    return parts.path.lower().endswith((".json", "api.php"))


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
    """One browser context serving one request at a time."""

    def __init__(self, context):
        self.context = context
        self.lock = asyncio.Lock()


class BrowserFetchPool:
    """Pool of masqueraded browser contexts bound to the asyncio loop.

    Created lazily on first use, from the loop that serves engine requests
    (see :py:obj:`searx.network.client.get_loop`). All public methods are
    coroutines and must run on that loop.
    """

    def __init__(
        self, pool_size: int = 3, verify: bool = True, proxy: str | None = None,
        human_fallback: bool = True,
    ):
        self._pool_size = max(1, pool_size)
        self._verify = verify
        self._proxy = proxy
        self._human_fallback = human_fallback
        self._lanes: list[_Lane] = []
        self._lane_cycle: asyncio.Queue | None = None
        self._init_lock = asyncio.Lock()
        self._playwright = None
        self._browser = None
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

            display = await asyncio.get_running_loop().run_in_executor(
                None, ensure_display
            )
            headed = display is not None
            env = dict(os.environ)
            if display:
                env["DISPLAY"] = display

            try:
                self._playwright = await async_playwright().start()
                launch_kwargs = {
                    "headless": not headed,
                    "ignore_default_args": _OMIT_DEFAULT_ARGS,
                    "args": _LAUNCH_ARGS,
                    "env": env,
                }
                if executable_path:
                    launch_kwargs["executable_path"] = executable_path
                if self._proxy:
                    launch_kwargs["proxy"] = {"server": self._proxy}
                self._browser = await self._playwright.chromium.launch(**launch_kwargs)

                chrome_full = self._browser.version  # e.g. "153.0.8010.52"
                chrome_major = chrome_full.split(".", 1)[0]
                user_agent = (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    f"(KHTML, like Gecko) Chrome/{chrome_major}.0.0.0 Safari/537.36"
                )
                sec_ch_ua = (
                    f'"Chromium";v="{chrome_major}", "Google Chrome";v="{chrome_major}", '
                    '"Not:A-Brand";v="24"'
                )
                extra_headers = {
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;q=0.9,"
                        "image/avif,image/webp,image/apng,*/*;q=0.8,"
                        "application/signed-exchange;v=b3;q=0.7"
                    ),
                    "Accept-Language": "en-US,en;q=0.9",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Sec-CH-UA": sec_ch_ua,
                    "Sec-CH-UA-Mobile": "?0",
                    "Sec-CH-UA-Platform": '"Windows"',
                }
                init_script = _stealth_init_script(chrome_major, chrome_full)

                for _ in range(self._pool_size):
                    context = await self._browser.new_context(
                        user_agent=user_agent,
                        viewport={"width": 1440, "height": 900},
                        locale="en-US",
                        timezone_id="America/Los_Angeles",
                        has_touch=False,
                        java_script_enabled=True,
                        color_scheme="light",
                        ignore_https_errors=not self._verify,
                        extra_http_headers=extra_headers,
                    )
                    await context.add_init_script(init_script)
                    self._lanes.append(_Lane(context))
                self._lane_cycle = asyncio.Queue()
                for lane in self._lanes:
                    self._lane_cycle.put_nowait(lane)
                logger.info(
                    "Browser fetch pool up: %d lane(s), chromium=%s, headed=%s",
                    len(self._lanes),
                    chrome_full,
                    headed,
                )
            except Exception as e:
                # Stop playwright before re-raising: its node driver process
                # keeps running otherwise, one ~130 MiB leak per failed start.
                await self._shutdown_browser()
                raise BrowserFetchError(f"browser pool init failed: {e}") from e

    async def _shutdown_browser(self):
        """Tear down lanes, browser and playwright, terminating the driver.

        Called on close and after a failed launch; without the explicit
        playwright stop the node driver process leaks (about 130 MiB each).
        """
        for lane in self._lanes:
            try:
                await lane.context.close()
            except Exception:  # pylint: disable=broad-except
                pass
        self._lanes.clear()
        self._lane_cycle = None
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:  # pylint: disable=broad-except
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # pylint: disable=broad-except
                pass
            self._playwright = None

    async def _ensure_browser_alive(self):
        """Restart the browser after a crash instead of failing every fetch.

        A dead browser process leaves the lane contexts unusable while
        ``_lanes`` still looks initialized: without this check every fetch
        keeps raising TargetClosedError until the container is restarted.
        """
        if self._browser is not None and self._browser.is_connected():
            return
        async with self._init_lock:
            if self._browser is not None and self._browser.is_connected():
                return
            logger.warning("Browser process is gone; restarting the fetch pool")
            await self._shutdown_browser()
        await self._init()

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
        try:
            async with lane.lock:
                return await self._fetch_on_lane(
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
                )
        finally:
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
    ) -> BrowserResponse:
        timeout_ms = int(timeout_s * 1000)
        request_headers = self._build_request_headers(headers, cookies)
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
                human = await self._fetch_via_human_search(lane, url, timeout_s)
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
                human = await self._fetch_via_human_search(lane, url, timeout_s)
                if human is not None:
                    return human
            rendered = await self._render_page(lane, url, timeout_s)
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

    async def _render_page(self, lane: _Lane, url: str, timeout_s: float):
        """Render `url` in a real page (JS enabled) and return the final DOM.

        Used as last resort when a fetch-style request returned a challenge
        or a JS redirect gate. Returns a BrowserResponse of the rendered
        content, or None when the navigation failed.
        """
        context = lane.context
        try:
            page = await context.new_page()
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
                return BrowserResponse(
                    status_code=status,
                    headers={"content-type": "text/html; charset=utf-8"},
                    content=html_content.encode("utf-8", errors="replace"),
                    url=final_url,
                    method="GET",
                )
            finally:
                await page.close()
        except Exception:  # pylint: disable=broad-except
            logger.warning("Render fallback failed for %s", url, exc_info=True)
            return None

    async def _fetch_via_human_search(self, lane: _Lane, url: str, timeout_s: float):
        """Re-run a GET search through the provider's UI with human input.

        The engine's endpoint returned a challenge or an interstitial.
        Instead of fetching that endpoint again, open the provider's site,
        type the query into its search box and click search with real X
        input (see :py:mod:`searx.network.human_input`), then return the
        rendered results page. A challenge met on the way (its checkbox
        lives in a cross-origin iframe) is clicked through.

        Returns None when the flow is not applicable or failed; the caller
        then falls back to the plain warm-up. Clearance cookies won on the
        way stay in the lane context.
        """
        # pylint: disable=import-outside-toplevel
        from searx.network.human_input import (
            human_search_on_page,
            human_session,
            human_solve_challenge,
            reset_input,
        )

        if _is_api_url(url):
            return None
        query = _search_query_from_url(url)
        if not query:
            return None
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return None
        origin = f"{parts.scheme}://{parts.netloc}/"
        goto_timeout_ms = int(max(5.0, min(timeout_s, 20.0)) * 1000)

        try:
            async with human_session():
                page = await lane.context.new_page()
                try:
                    try:
                        await page.goto(
                            origin, timeout=goto_timeout_ms, wait_until="domcontentloaded"
                        )
                    except Exception:  # pylint: disable=broad-except
                        logger.warning(
                            "human search: homepage %s failed, trying results URL", origin
                        )
                    await human_solve_challenge(page)
                    if await human_search_on_page(page, query):
                        try:
                            await page.wait_for_load_state(
                                "networkidle", timeout=_BOT_CHALLENGE_GRACE_MS
                            )
                        except Exception:  # pylint: disable=broad-except
                            pass
                    else:
                        # No usable search box: navigate the results URL so
                        # at least challenge JS runs in a real page.
                        await page.goto(
                            url, timeout=goto_timeout_ms, wait_until="domcontentloaded"
                        )
                        await human_solve_challenge(page)
                        try:
                            await page.wait_for_load_state(
                                "networkidle", timeout=_BOT_CHALLENGE_GRACE_MS
                            )
                        except Exception:  # pylint: disable=broad-except
                            pass
                    await page.wait_for_timeout(1500)
                    html_content = await page.content()
                    logger.info(
                        "human search fallback: %d bytes for %s", len(html_content), url
                    )
                    return BrowserResponse(
                        status_code=200,
                        headers={"content-type": "text/html; charset=utf-8"},
                        content=html_content.encode("utf-8", errors="replace"),
                        url=page.url,
                        method="GET",
                    )
                finally:
                    await page.close()
        except Exception:  # pylint: disable=broad-except
            logger.warning("human search fallback failed for %s", url, exc_info=True)
            # the usual cause is a stale X connection: drop the cached
            # pyautogui so the next session rebinds to the display
            reset_input()
            return None

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
    def _build_request_headers(headers: dict | None, cookies: dict | None) -> dict:
        """Merge masquerade defaults with engine-provided headers.

        Engine headers win for the keys they set. Hop-by-hop and
        transport-managed headers are dropped: the browser stack computes
        them itself and stale values are a fingerprint signal.
        """
        dropped = {"host", "content-length", "connection", "accept-encoding", "cookie"}
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
