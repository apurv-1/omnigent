"""E2E: Copy link on a message deep-links to ``?message=<id>``.

Covers the shareable message URL flow from issue #4646:

  1. Send a user message, click **Copy link** under its bubble, and assert
     the clipboard holds a session URL with ``?message=<data-message-id>``.
  2. Open that URL in a fresh browser context and assert the target message
     scrolls into view and receives the brief flash highlight
     (``.animate-user-msg-flash``).

Selectors:
  - user bubble: ``data-testid="message-bubble"`` + ``data-role="user"``
  - stable id: ``data-message-id``
  - copy-link button: ``data-testid="copy-message-link"`` (not role
    name "Copy link" - success flips the sr-only label to "Copied!")
  - flash: ``.animate-user-msg-flash`` on the bubble content
"""

from __future__ import annotations

import re
import uuid

import httpx
import pytest
from playwright.sync_api import Browser, Page, Route, expect

from tests.e2e_ui.chat.test_transcript_scroll_persistence import _seed_turns

_COMPOSER_PLACEHOLDER = "Send a message…"
_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'


def _send(page: Page, text: str) -> None:
    """Type ``text`` into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER_PLACEHOLDER)
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


@pytest.mark.parametrize("terminal_view", [False, True])
def test_copy_message_link_opens_and_highlights_target(
    browser: Browser,
    seeded_session: tuple[str, str],
    terminal_view: bool,
) -> None:
    """Copy link → clipboard URL → fresh session lands on + flashes the message."""
    base_url, session_id = seeded_session
    marker = f"deep-link-{uuid.uuid4().hex[:8]}"

    ctx = browser.new_context()
    ctx.grant_permissions(["clipboard-read", "clipboard-write"])
    try:
        page = ctx.new_page()
        page.goto(f"{base_url}/c/{session_id}")

        # A couple of filler turns create vertical room so deep-link scroll
        # is more than a no-op on a single short bubble.
        for i in range(2):
            filler = f"filler-{i}-{uuid.uuid4().hex[:6]}"
            _send(page, filler)
            expect(page.locator(_USER_BUBBLE).filter(has_text=filler)).to_be_visible(
                timeout=15_000
            )

        _send(page, marker)
        # Wait until the optimistic ``pend_*`` id is promoted to the server
        # itemId — a fresh tab cannot resolve ``?message=pend_N``.
        bubble = page.locator(f'{_USER_BUBBLE}:not([data-message-id^="pend_"])').filter(
            has_text=marker
        )
        expect(bubble).to_be_visible(timeout=15_000)

        message_id = bubble.get_attribute("data-message-id")
        assert message_id, "user bubble missing data-message-id"
        assert not message_id.startswith("pend_"), f"still pending id {message_id!r}"

        # test-id stays stable after click; role name "Copy link" does not
        # (tooltip/sr-only become "Copied!"). Wait for the check icon on the
        # stable test-id locator, then read the clipboard.
        copy_link = bubble.get_by_test_id("copy-message-link")
        expect(copy_link).to_be_attached()
        bubble.hover()
        copy_link.click()
        expect(copy_link.locator("svg.lucide-check")).to_have_count(1, timeout=5_000)

        clipboard = page.evaluate("() => navigator.clipboard.readText()")
        assert session_id in clipboard, f"clipboard URL {clipboard!r} missing session id"
        assert re.search(rf"[?&]message={re.escape(message_id)}", clipboard), (
            f"clipboard URL {clipboard!r} does not carry ?message={message_id}"
        )
    finally:
        ctx.close()

    # Fresh context: no shared scroll position — deep link must rehydrate
    # purely from the URL (same authorization as a normal session open).
    fresh = browser.new_context()
    try:
        page = fresh.new_page()
        if terminal_view:
            response = page.request.patch(
                f"{base_url}/v1/sessions/{session_id}",
                data={"labels": {"omnigent.ui": "terminal"}},
            )
            assert response.ok, response.text()
            storage_key = f"omnigent.web.panel-key:{session_id}"
            page.goto(f"{base_url}/c/{session_id}")
            page.get_by_test_id("view-mode-terminal").click(timeout=15_000)
            expect(page.get_by_test_id("main-terminal-view")).to_be_visible(timeout=15_000)
            expect(page.locator(_USER_BUBBLE)).to_have_count(0)
            assert page.evaluate("key => sessionStorage.getItem(key)", storage_key) is not None

        # Wait for the flash class as soon as the page loads — it only lasts
        # ~800ms after the scroll settles, so racing it after other expects
        # can miss the highlight.
        page.goto(clipboard)
        target = page.locator(f'[data-message-id="{message_id}"]')
        expect(target.locator(".animate-user-msg-flash")).to_be_attached(timeout=20_000)
        expect(target).to_be_visible()
        expect(target).to_be_in_viewport(timeout=5_000)
        if terminal_view:
            expect(page.get_by_test_id("main-terminal-view")).not_to_be_visible()
            assert page.evaluate("key => sessionStorage.getItem(key)", storage_key) == "__chat__"
    finally:
        fresh.close()


def test_pending_message_link_is_disabled_until_persisted(
    browser: Browser,
    seeded_session: tuple[str, str],
) -> None:
    """Text copy stays available while the send waits for a permanent item ID."""
    base_url, session_id = seeded_session
    marker = f"pending-link-{uuid.uuid4().hex[:8]}"
    ctx = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
    held_requests: list[Route] = []
    try:
        page = ctx.new_page()
        page.goto(f"{base_url}/c/{session_id}")
        page.route(
            f"**/v1/sessions/{session_id}/events", lambda route: held_requests.append(route)
        )
        _send(page, marker)
        bubble = page.locator(_USER_BUBBLE).filter(has_text=marker)
        expect(bubble.get_by_test_id("copy-message-link")).to_be_disabled()
        expect(bubble).to_have_attribute("data-message-id", re.compile(r"^pend_"))
        bubble.hover()
        copy_text = bubble.get_by_role("button", name="Copy", exact=True)
        copy_text.click()
        expect(copy_text.locator("svg.lucide-check")).to_have_count(1)
        assert page.evaluate("navigator.clipboard.readText()") == marker

        assert len(held_requests) == 1
        held_requests.pop().continue_()
        expect(bubble.get_by_test_id("copy-message-link")).to_be_enabled(timeout=15_000)
        message_id = bubble.get_attribute("data-message-id")
        assert message_id and not message_id.startswith("pend_")
        bubble.get_by_test_id("copy-message-link").click()
        expect(
            bubble.get_by_test_id("copy-message-link").locator("svg.lucide-check")
        ).to_have_count(1)
        clipboard = page.evaluate("navigator.clipboard.readText()")
        assert re.search(rf"[?&]message={re.escape(message_id)}", clipboard)
    finally:
        for request in held_requests:
            request.abort()
        ctx.close()


@pytest.mark.parametrize("target_role", ["user", "assistant"])
def test_message_link_reaches_virtualized_history(
    page: Page,
    seeded_session: tuple[str, str],
    target_role: str,
) -> None:
    """Find both a paged-out user message and a loaded, windowed-out response."""
    base_url, session_id = seeded_session
    _seed_turns(session_id, "deep")
    if target_role == "user":
        response = httpx.get(
            f"{base_url}/v1/sessions/{session_id}/items",
            params={"limit": 20, "order": "asc"},
        )
        response.raise_for_status()
        message_id = next(
            item["id"]
            for item in response.json()["data"]
            if item["type"] == "message" and item.get("role") == "user"
        )
        expected_text = "deep prompt 0"
    else:
        message_id = "resp_deep_060"
        expected_text = "deep reply 60"

    page.set_viewport_size({"width": 1280, "height": 720})
    page.goto(f"{base_url}/c/{session_id}?message={message_id}")
    target = page.locator(f'[data-message-id="{message_id}"]')
    expect(target.locator(".animate-user-msg-flash")).to_be_attached(timeout=20_000)
    expect(target).to_contain_text(expected_text)
    expect(target).to_be_in_viewport(timeout=5_000)
