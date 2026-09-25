# SearXNG image variant with the masqueraded Chromium fetch pool
# (outgoing.using_browser). Debian-based: the browser fetch pool needs a
# real Chromium (distro build), Xvfb for headed mode, and playwright.
FROM docker.io/library/python:3.13-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium xvfb ca-certificates python3-tk \
       fonts-thai-tlwg \
    && rm -rf /var/lib/apt/lists/*
# fonts-thai-tlwg: the pool's geo identity is th-TH, and challenge widgets
# (reCAPTCHA /sorry) render their instruction in the interface language --
# without Thai glyphs the widget screenshot shows tofu boxes instead of text

# same paths / user convention as the official image
ENV __SEARXNG_CONFIG_PATH=/etc/searxng \
    __SEARXNG_DATA_PATH=/var/cache/searxng
RUN groupadd -g 977 searxng \
    && useradd -u 977 -g searxng -d /usr/local/searxng -s /bin/bash searxng \
    && mkdir -p /etc/searxng /var/cache/searxng /tmp/.X11-unix \
    && chown -R searxng:searxng /etc/searxng /var/cache/searxng /tmp/.X11-unix

WORKDIR /usr/local/searxng
# pip: playwright drives the pool's Chromium; pyautogui emits the real
# XTEST mouse/keyboard events for the human-like input fallback
# (searx/network/human_input.py) on the Xvfb display.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt playwright pyautogui granian

COPY searx/ ./searx/
# freeze the version: searx/version.py shells out to git (absent in the
# image) when built from a checkout. GIT_URL must stay the official one:
# Onyx's provider connection test asserts brand.GIT_URL equals it.
RUN printf 'VERSION_STRING = "2026.9.22-browser"\nVERSION_TAG = "2026.9.22"\nDOCKER_TAG = "browser"\nGIT_URL = "https://github.com/searxng/searxng"\nGIT_BRANCH = "master"\n' > searx/version.py
RUN chown -R searxng:searxng /usr/local/searxng

USER searxng
EXPOSE 8080

ENTRYPOINT ["granian", "--interface", "wsgi", "--host", "::", "--port", "8080", "searx.webapp:app"]
