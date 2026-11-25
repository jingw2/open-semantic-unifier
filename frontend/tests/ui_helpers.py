"""Shared browser helpers for the workbench acceptance specs."""

APP = "http://127.0.0.1:5173"


def open_route(page, route: str) -> None:
    """Navigate by hash without a full reload when the app is already loaded."""
    if page.url.startswith(APP):
        page.evaluate("route => { window.location.hash = route }", route)
    else:
        page.goto(f"{APP}{route}")
    page.wait_for_load_state("networkidle")
