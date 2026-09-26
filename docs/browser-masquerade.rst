= Browser masquerade: search sequence =

The masquerading search flow of this fork (``searx/network/browser.py``,
``searx/network/human_input.py``, ``searx/network/captcha_vision.py``) as a
PlantUML sequence diagram. Timings in the comments are measured on the
running deployment (6 lanes, distro Chromium 154, Thai residential egress,
2026-09-26).

Render with ``plantuml docs/browser_masquerade_sequence.puml``.

[source,plantuml]
----
include::browser_masquerade_sequence.puml[]
----

Modified parts of the flow vs upstream searxng:

- Engine requests are served by a per-lane masqueraded Chromium, not httpx
  (``outgoing.using_browser``).
- In maximum stealth mode every search is typed into the provider's UI with
  real X input (``outgoing.browser_max_stealth``).
- Image-grid CAPTCHAs are solved by a vision model with quorum voting
  (``outgoing.captcha_vision``).
- After a search, the lane keeps browsing like a reader
  (``outgoing.browser_post_search_browsing``).
- Every lane holds a persistent on-disk Chromium profile, guarded by an
  flock so the same lanes can be shared with Onyx's crawler workers
  (``outgoing.browser_profile_dir``).
