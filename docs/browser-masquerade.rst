= Browser masquerade: search sequence =

The masquerading search flow of this fork (``searx/network/browser.py``,
``searx/network/human_input.py``, ``searx/network/captcha_vision.py``) as a
PlantUML sequence diagram. Timings in the comments are measured on the
running deployment (6 lanes, distro Firefox 157 on Xvfb :99-:104, Thai
residential egress, 2026-10-19).

Render with ``plantuml docs/browser_masquerade_sequence.puml`` (a rendered
copy is kept next to this file).

[image]
browser_masquerade_sequence.png

[source,plantuml]
----
include::browser_masquerade_sequence.puml[]
----

Modified parts of the flow vs upstream searxng:

- Engine requests are served by a per-lane masqueraded browser, not httpx
  (``outgoing.using_browser``). The binary is a configuration choice:
  ``outgoing.browser_kind`` (``firefox`` -- the default and this
  deployment's fleet -- or ``chromium``); the other kind is the automatic
  fallback when the configured binary is missing. Images build per kind
  with the ``SEARXNG_BROWSER_KIND`` build argument, which also ships as the
  environment default inside the image.
- In maximum stealth mode every search is typed into the provider's UI with
  real X input (``outgoing.browser_max_stealth``). The visit starts from the
  provider's front page for the requested vertical: google web searches
  start from ``google.com``, google image searches from ``/imghp`` -- its
  search box submits into the images vertical (``udm=2``), so the typed
  query lands where the engine's parser looks. The ``google images`` engine
  rides the same desktop interface and human flow (the discontinued WML
  image endpoint answered zero results), and the desktop grid parser reads
  the ``div[data-lpage]`` tiles plus the legacy ``/imgres`` links; an image
  grid gets one extra viewport of scroll before capture so its lazy tiles
  paint.
- The lanes run without an ad blocker: google sees the whole jar, SERP ad
  anchors included. Publisher pages load with third-party ad/metric
  networks and video filtered in code, universally -- every lane whatever
  its browser kind, on crawls (``BrowserFetchPool._apply_page_filter``)
  and on the post-search browsing click-throughs alike; google's own
  pipeline (googlesyndication, doubleclick, GA, GTM) is allowed everywhere.
- Challenges are provider-specific:

  - google: reCAPTCHA Enterprise -- checkbox click, then an image grid
    solved by a vision model; a single vote decides
    (``outgoing.captcha_vision.votes: 1``). The Bearer token comes from the
    ``CAPTCHA_VISION_API_KEY`` environment variable, and endpoint errors
    surface with the server's own message.
  - brave: its own ``/captcha`` page -- an invisible PoW that self-clears
    and reloads the search, then a slider puzzle dragged with real X input;
    the vision model enters only when plain end-drags are refused.
  - brave's SERP payload is parsed from the new SvelteKit IIFE hydration
    form (legacy ``data: [{...}]`` still supported); a page without either
    form suspends the engine instead of retrying into the same wall.

- A solve that outlives its request keeps running in the lane up to
  ``outgoing.captcha_vision.session_budget`` (1500 s) and banks the
  clearance cookies for later searches.
- After a search, the lane keeps browsing like a reader
  (``outgoing.browser_post_search_browsing``); the lane scheduler priority
  is search > crawl > browsing.
- Page crawls are affinity-routed to the lane whose SERP returned the URL
  (lane memory: last 600 result URLs per lane). Search-engine wrapper links
  (google ``/goto?url=``, ``/url?q=``, bing ``/ck/a``, duckduckgo
  ``/l/?uddg=``, yahoo ``/r/``) are refused from any lane without the
  minting SERP in memory -- the tokens are bound to the jar the SERP was
  served to.
- The lane displays self-heal: stale X lock/socket leftovers are cleaned
  only when the lock's PID does not name the display (PID reuse across
  container restarts), and a spawned Xvfb is verified (fresh socket, live
  process, listening socket) with one clean+retry round before the lane
  falls back to headless.
- Engine rate limits surface as a clean 1 h suspension; the suspension path
  never crashes the search thread (non-numeric ``suspended_time`` values
  from engines are nulled in ``searx/search/processors/abstract.py``).
- When ``outgoing.browser_crawl_secret`` (or the ``BROWSER_CRAWL_SECRET``
  env) is set, ``webapp.require_endpoint_secret`` gates every route but
  ``/healthz`` behind it: the ``X-Crawl-Secret`` header or HTTP basic auth
  (any username, the secret as the password) -- what makes a LAN-published
  instance safe to share with clients. A Bruno collection exercising every
  endpoint Onyx drives lives in ``utils/bruno/searxng-fleet`` (GUI or
  ``bru run --env local``).