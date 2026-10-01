# SearXNG image variant with the masqueraded Firefox fetch pool
# (outgoing.using_browser). Arch-based: the browser fetch pool needs a
# real Firefox (distro build, driven over WebDriver BiDi), Xvfb for
# headed mode, and playwright.
FROM docker.io/library/archlinux:base-devel

# pacman -Syu (not -Sy): a partial upgrade against stale mirrors breaks
# glibc/pyc mismatches. The image's default mirrorlist resolves to slow
# hosts from this network (14 min to fetch firefox's dep tree); pin the
# GeoIP CDN mirror, which sustains ~26 MB/s here. Packages:
# - firefox: the distro build the pool masquerades as (no Playwright
#   fork); driven through Playwright's "moz-firefox" channel, which
#   speaks Firefox's native WebDriver BiDi protocol.
# - firefox-ublock-origin: uBlock as a distribution extension, enabled
#   for every profile -- most desktop Firefox installs block ads, and
#   so does this fleet (see _FIREFOX_USER_PREFS in browser.py, which
#   re-enables distro add-ons that Playwright's test profile disables).
# - otf-fira-sans / otf-fira-mono: the Fira family (renamed from
#   ttf-fira-* in current Arch repos).
# - noto-fonts: Thai glyphs (the pool's geo identity is th-TH; challenge
#   widgets render their instructions in the interface language --
#   without Thai glyphs the widget screenshot shows tofu boxes) plus
#   the general Latin/Greek/Cyrillic coverage.
# - noto-fonts-cjk: Simplified Chinese coverage for zh-CN searches.
# - xorg-server-xvfb: one headed browser per lane on its own display.
# - openbox: a window manager per lane display -- without one GTK places
#   Firefox at (26,26) with a 1280x810 default (odd geometry, no
#   decorations, no focus management); the pool's openbox rc maximizes
#   the browser like a real desktop.
# - ttf-dejavu noto-fonts-extra: a desktop-plausible installed font set
#   (the base image's fontconfig has almost no fallbacks otherwise --
#   a tiny font list is measurable from JS and unusual).
# - tk: pyautogui/mouseinfo import tkinter at import time.
# - mesa: software GL for Firefox's WebGL under Xvfb (no GPU here).
RUN printf 'Server = https://geo.mirror.pkgbuild.com/$repo/os/$arch\n' \
        > /etc/pacman.d/mirrorlist \
    && pacman -Syu --noconfirm \
        firefox firefox-ublock-origin \
        otf-fira-sans otf-fira-mono \
        noto-fonts noto-fonts-cjk noto-fonts-extra ttf-dejavu \
        xorg-server-xvfb openbox tk mesa \
        python python-pip \
    && rm -rf /var/cache/pacman/pkg/* /var/lib/pacman/sync/*

ENV LANG=C.UTF-8
# Docker's seccomp profile blocks the unprivileged user namespaces the
# Firefox content sandbox needs; without this the content processes die
# at startup. Invisible to pages (JS cannot observe the sandbox), same
# posture as Chromium's --no-sandbox in a container.
ENV MOZ_DISABLE_CONTENT_SANDBOX=1

# same paths / user convention as the official image. The Arch upgrade
# ships a systemd-imds system account that lands on UID/GID 977 (a
# host-metadata agent nothing in a container uses): remove it so the
# fleet's documented 977 identity stays stable across both images.
ENV __SEARXNG_CONFIG_PATH=/etc/searxng \
    __SEARXNG_DATA_PATH=/var/cache/searxng
RUN if getent passwd systemd-imds >/dev/null; then userdel systemd-imds; fi \
    && if getent group systemd-imds >/dev/null; then groupdel systemd-imds; fi \
    && groupadd -g 977 searxng \
    && useradd -u 977 -g searxng -d /usr/local/searxng -s /bin/bash searxng \
    && mkdir -p /etc/searxng /var/cache/searxng /tmp/.X11-unix \
    && chown -R searxng:searxng /etc/searxng /var/cache/searxng /tmp/.X11-unix

WORKDIR /usr/local/searxng
# pip: playwright drives the pool's Firefox (the "moz-firefox" channel
# speaks WebDriver BiDi, which the distro Firefox implements natively --
# no Playwright browser download needed); python-xlib ships the XTEST
# input layer for the human-like input fallback
# (searx/network/human_input.py) on the Xvfb displays.
COPY requirements.txt ./
# --break-system-packages: Arch marks its python as externally managed
# (PEP 668); inside a single-purpose container image there is no system
# package set to break.
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt playwright pyautogui granian

# Wrap the playwright node driver (see container/node-driver-wrapper.sh):
# the driver exits silently on some failures and its stderr would be
# lost; the wrapper logs every exit with its code to /tmp/node-driver.log
# and enables node diagnostic reports for fatal errors / uncaught
# exceptions (written to $NODE_REPORT_DIR).
COPY container/node-driver-wrapper.sh /usr/local/share/node-driver-wrapper.sh
ENV NODE_OPTIONS="--report-on-fatalerror --report-uncaught-exception" \
    NODE_REPORT_DIR=/tmp/node-reports
RUN d="$(python3 -c 'import playwright, os; print(os.path.join(os.path.dirname(playwright.__file__), "driver"))')" \
    && mv "$d/node" "$d/node.real" \
    && install -m 0755 /usr/local/share/node-driver-wrapper.sh "$d/node"

COPY searx/ ./searx/
# openbox rc: maximize every window on the lane displays (see
# _ensure_window_manager in searx/network/browser.py)
RUN mkdir -p openbox \
    && printf '<?xml version="1.0" encoding="UTF-8"?>\n\
<openbox_config xmlns="http://openbox.org/3.4/rc">\n\
  <applications>\n\
    <application class="*">\n\
      <focus>yes</focus>\n\
      <maximized>yes</maximized>\n\
    </application>\n\
  </applications>\n\
</openbox_config>\n' > openbox/rc.xml
# freeze the version: searx/version.py shells out to git (absent in the
# image) when built from a checkout. GIT_URL must stay the official one:
# Onyx's provider connection test asserts brand.GIT_URL equals it.
RUN printf 'VERSION_STRING = "2026.10.1-browser-ff"\nVERSION_TAG = "2026.10.1"\nDOCKER_TAG = "browser"\nGIT_URL = "https://github.com/searxng/searxng"\nGIT_BRANCH = "master"\n' > searx/version.py
RUN chown -R searxng:searxng /usr/local/searxng

# Persistent per-lane Firefox profiles (outgoing.browser_profile_dir): the
# deployment mounts a docker volume here; pre-owning the path means the
# volume inherits the searxng ownership instead of root's.
RUN mkdir -p /var/lib/searxng/lanes && chown -R searxng:searxng /var/lib/searxng

USER searxng
EXPOSE 8080

ENTRYPOINT ["granian", "--interface", "wsgi", "--host", "::", "--port", "8080", "searx.webapp:app"]
