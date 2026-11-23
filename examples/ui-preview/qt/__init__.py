"""Native Qt pages for the UI preview, laid out like the real ``ui`` package.

``components`` holds reusable widgets (future ``ui/components``), ``features``
holds pages owned by a visible workflow (future ``ui/features``), and
``sample`` holds the deterministic data adapters the preview feeds them. The
widgets take a small data-source object, so the real app can pass a
runtime/daemon adapter instead of the sample one.
"""
