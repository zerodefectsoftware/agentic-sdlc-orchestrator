"""shortener — a URL shortening service with click analytics and reliability controls.

The package root holds no behaviour on purpose. Every module the design names
lives in its own subpackage below, and exactly one implementer owns each of
them; a name that crosses a subpackage boundary is declared in `design.json`
and stubbed here before any body is written.

Entry point: `shortener.main:app`.
"""
