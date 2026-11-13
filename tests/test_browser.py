"""The computers' pages in a real browser, signed in as the admin, with a
computer whose node this test answers for."""

from __future__ import annotations

lazy import re
lazy from http.cookiejar import CookieJar

from playwright.async_api import Page, async_playwright, expect
from served import Served, admin, client, enrol, node, serving
from test_starlink import connected


async def signed_in(page: Page, served: Served, cookies: CookieJar) -> Page:
    """The page on the computers, with the admin's session."""
    await page.context.add_cookies(
        [
            {"name": cookie.name, "value": cookie.value or "", "url": served.url}
            for cookie in cookies
        ]
    )
    await page.goto(f"{served.url}/computers")
    await page.wait_for_url(re.compile(r"/computers$"))
    return page


async def test_connecting_a_computer_shows_the_commands_and_then_the_computer():
    async with serving() as served, client() as boss, async_playwright() as driver:
        await admin(served, boss)
        browser = await driver.chromium.launch()
        try:
            context = await browser.new_context(locale="zh-CN")
            await context.grant_permissions(["clipboard-read"], origin=served.url)
            page = await signed_in(await context.new_page(), served, boss.cookies.jar)
            frame = page.frame_locator("iframe.frame")
            await frame.locator(".top .actions button").click()
            await frame.locator("input[name=name]").fill("kana-vm")
            await frame.locator(".dstep form button[type=submit]").click()
            commands = frame.locator(".code:visible")
            # the address the page is seen at, and the whole token
            await expect(commands).to_contain_text(f"server={served.url}")
            copied = await commands.get_attribute("data-text")
            token = re.search(r"--option token=(\S+)", copied or "")
            assert token is not None
            await expect(commands).to_contain_text(f"token={token.group(1)}")
            # the button puts the lines on the clipboard, with that address,
            # and says so, the page served over plain http to an address on
            # the network too, where the clipboard interface is not given
            module = page.frame(url=re.compile(r"/ui/computers/"))
            assert module
            await module.evaluate(
                "Object.defineProperty(navigator, 'clipboard', {value: undefined})"
            )
            await commands.locator(".copy").click()
            await expect(commands.locator(".copy")).to_contain_text("已复制")
            clipboard = await page.evaluate("navigator.clipboard.readText()")
            assert f"--option server={served.url}" in clipboard
            assert f"--option token={token.group(1)}" in clipboard
            # the computer coming online takes the drawer to its detail
            async with node(served, token.group(1)) as computer:
                await connected(computer)
                await expect(frame.locator(".drawer .facts")).to_contain_text("在线")
                await expect(frame.locator(".drawer .d-hd")).to_contain_text(
                    "Fedora Linux 44"
                )
            await frame.locator(".overlay .close").click()
            await expect(frame.locator(".tr.row")).to_contain_text("kana-vm")
        finally:
            await browser.close()


async def test_a_computer_is_shared_and_removed_from_its_row():
    async with serving() as served, client() as boss, async_playwright() as driver:
        await admin(served, boss)
        await boss.post(
            f"{served.url}/ui/access/roles/",
            data={"name": "ops", "permissions": ["computers.view"]},
        )
        await enrol(served, boss, "north")
        browser = await driver.chromium.launch()
        try:
            context = await browser.new_context(locale="en-US")
            page = await signed_in(await context.new_page(), served, boss.cookies.jar)
            frame = page.frame_locator("iframe.frame")
            row = frame.locator(".tr.row", has_text="north")
            # the menu works once Alpine has taken the page
            await expect(row.locator(".pop")).not_to_have_attribute(
                "x-cloak", re.compile(".*")
            )
            # renamed in place from its detail, the list follows
            await row.locator(".pc b").click()
            await frame.locator(".drawer .rename").click()
            await frame.locator(".drawer input[name=name]").fill("west")
            await frame.locator(".drawer input[name=name]").press("Enter")
            await expect(frame.locator("#detail-title")).to_have_text("west")
            await frame.locator(".overlay .close").click()
            row = frame.locator(".tr.row", has_text="west")
            await expect(row).to_have_count(1)
            await expect(row.locator(".pop")).not_to_have_attribute(
                "x-cloak", re.compile(".*")
            )
            await row.locator(".acts .more").click()
            await row.locator(".pop button", has_text="Share").click()
            shares = frame.frame_locator("iframe.shares")
            await shares.locator("input[name=grant]").first.check()
            await shares.locator("footer button[type=submit]").click()
            # saved, the sheet goes away
            await expect(frame.locator(".overlay")).to_have_count(0)
            await row.locator(".acts .more").click()
            await row.locator(".pop button.danger").click()
            await frame.locator(".confirm button[type=submit]").click()
            await expect(frame.locator(".tr.row")).to_have_count(0)
            await expect(frame.locator(".overlay")).to_have_count(0)
            # on a phone, a card a computer
            await enrol(served, boss, "south")
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.reload()
            await expect(frame.locator(".phone-line")).to_contain_text("Offline")
        finally:
            await browser.close()


async def test_a_stall_shows_what_another_module_puts_in_it_under_its_name():
    async with serving() as served, client() as boss, async_playwright() as driver:
        await admin(served, boss)
        ref, _ = await enrol(served, boss, "kana-vm")
        browser = await driver.chromium.launch()
        try:
            context = await browser.new_context(locale="zh-CN")
            page = await signed_in(await context.new_page(), served, boss.cookies.jar)
            await page.goto(f"{served.url}/overview")
            stalls = page.frame_locator("iframe.frame").frame_locator(
                "iframe[src='/ui/computers/stall']"
            )
            stall = stalls.locator(".stall", has_text="kana-vm")
            await expect(stall.locator(".stall-hd .name")).to_have_text("kana-vm")
            # the probe's page for this computer, by the ref it was given
            goods = stall.frame_locator("iframe.goods")
            await expect(goods.locator("#goods")).to_have_text(ref)
            # the stalls drawn again as a computer is renamed: what the probe
            # shows stays as it was, not loaded again
            shown = page.frame(url=re.compile(rf"/ui/probe/stall/{ref}$"))
            assert shown
            await shown.evaluate("() => { window.kept = true; }")
            await boss.post(
                f"{served.url}/ui/computers/{ref}/name", data={"name": "kana-pc"}
            )
            await expect(stalls.locator(".stall-hd .name")).to_have_text("kana-pc")
            shown = page.frame(url=re.compile(rf"/ui/probe/stall/{ref}$"))
            assert shown and await shown.evaluate("() => window.kept === true")
        finally:
            await browser.close()


async def test_a_computer_alone_has_an_empty_stall_inviting_another():
    async with serving() as served, client() as boss, async_playwright() as driver:
        await admin(served, boss)
        await enrol(served, boss, "kana-vm")
        browser = await driver.chromium.launch()
        try:
            context = await browser.new_context(locale="zh-CN")
            page = await signed_in(await context.new_page(), served, boss.cookies.jar)
            await page.goto(f"{served.url}/overview")
            stalls = page.frame_locator("iframe.frame").frame_locator(
                "iframe[src='/ui/computers/stall']"
            )
            await stalls.locator(".stall.spare a").click()
            await page.wait_for_url(re.compile(r"/computers$"))
        finally:
            await browser.close()


async def test_a_direct_computer_link_opens_a_styled_drawer_and_updates_the_address():
    async with serving() as served, client() as boss, async_playwright() as driver:
        await admin(served, boss)
        ref, _ = await enrol(served, boss, "Build computer")
        browser = await driver.chromium.launch()
        try:
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800}, locale="en-US"
            )
            page = await signed_in(await context.new_page(), served, boss.cookies.jar)
            await page.goto(f"{served.url}/computers/{ref}")
            frame = page.frame_locator("iframe.frame")
            await expect(frame.locator("#detail-title")).to_have_text("Build computer")
            await expect(frame.locator(".top")).to_be_visible()
            await expect(frame.locator('link[href*="computers.css"]')).to_have_count(1)
            drawer = await frame.locator(".drawer").bounding_box()
            assert drawer is not None and 300 <= drawer["width"] <= 760
            icon = await frame.locator(".d-hd .os svg").bounding_box()
            assert icon is not None and icon["width"] <= 48 and icon["height"] <= 48
            await page.wait_for_url(re.compile(rf"/computers/{ref}$"))
            await frame.locator(".close").click()
            await expect(frame.locator("#table")).to_be_visible()
            # The list's existing htmx action still swaps just the drawer.
            await frame.locator(f'[hx-get="/ui/computers/{ref}"]').first.click()
            await expect(frame.locator("#detail-title")).to_have_text("Build computer")
            await expect(frame.locator(".top")).to_have_count(1)
        finally:
            await browser.close()
