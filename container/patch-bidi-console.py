"""Build-time workaround for a Playwright node-driver crash (BiDi console
entries).

Playwright 1.63.0's driver dies on a page console message whose argument
has no primitive conversion: BidiPage._onLogEntryAdded -> createHandle2 ->
new JSHandle evaluates ``String(value2)`` on the deserialized remote value,
and a null-prototype object (Object.create(null)-style, ordinary in page
JS) makes String() throw "Cannot convert object to primitive value". The
throw is inside the driver's event dispatch, so the whole node process
exits (rc=1) and every browser lane it serves dies with it.

Until upstream guards the preview computation, patch the one statement to
degrade the preview instead of crashing. The patch anchors on the full
statement and fails the build when the bundle does not match: a silently
unapplied workaround on a floating driver version would be worse than a
loud one. Keep playwright pinned in browser.dockerfile for the same
reason.
"""
import pathlib
import playwright
import sys

CRASH_SITE = (
    "this._preview = this._objectId ? preview || "
    "`JSHandle@${this._objectType}` : String(value2);"
)
PATCHED_MARKER = "/*SEARXNG-BIDI-PATCH*/"
REPLACEMENT = (
    "this._preview = this._objectId ? preview || "
    "`JSHandle@${this._objectType}` : (() => { try { "
    "return String(value2); } catch { "
    "return 'unserializable-console-arg'; } })();" + PATCHED_MARKER
)


def main() -> int:
    bundle = (
        pathlib.Path(playwright.__file__).parent
        / "driver"
        / "package"
        / "lib"
        / "coreBundle.js"
    )
    if not bundle.is_file():
        print(f"patch-bidi-console: bundle not found at {bundle}", file=sys.stderr)
        return 1
    source = bundle.read_text(encoding="utf-8")
    if PATCHED_MARKER in source:
        print("patch-bidi-console: already patched, nothing to do")
        return 0
    count = source.count(CRASH_SITE)
    if count != 1:
        print(
            f"patch-bidi-console: expected exactly 1 crash site, found {count};"
            " the driver bundle changed -- re-evaluate the patch against"
            " this playwright version",
            file=sys.stderr,
        )
        return 1
    bundle.write_text(source.replace(CRASH_SITE, REPLACEMENT), encoding="utf-8")
    print(f"patch-bidi-console: patched {bundle}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
