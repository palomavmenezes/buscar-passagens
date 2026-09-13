from __future__ import annotations

import random


def _closed(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "has been closed" in message or "targetclosed" in message or "target closed" in message


def host_page(root, fallback=None):
    if root is None:
        return fallback
    if hasattr(root, "mouse"):
        return root
    return getattr(root, "page", fallback)


async def _window_size(page) -> tuple[int, int]:
    try:
        size = await page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight})")
        return int(size.get("w") or 1280), int(size.get("h") or 800)
    except Exception:
        vp = page.viewport_size or {}
        return int(vp.get("width") or 1280), int(vp.get("height") or 800)


async def human_pause(page, low: float = 0.35, high: float = 1.2, wander: bool = True) -> None:
    wait = random.uniform(low, high)
    if random.random() < 0.18:
        wait += random.uniform(0.35, 1.7)
    await page.wait_for_timeout(int(wait * 1000))
    if wander and random.random() < 0.22:
        await wander_mouse(page)


async def wander_mouse(page) -> None:
    try:
        width, height = await _window_size(page)
        x = random.randint(30, max(60, width - 40))
        y = random.randint(70, max(100, height - 50))
        await page.mouse.move(x, y, steps=random.randint(8, 26))
    except Exception:
        pass


async def _center(locator) -> tuple[float, float] | None:
    try:
        box = await locator.bounding_box()
    except Exception:
        return None
    if not box:
        return None
    jitter_x = random.uniform(-min(8, box["width"] / 5), min(8, box["width"] / 5))
    jitter_y = random.uniform(-min(6, box["height"] / 5), min(6, box["height"] / 5))
    return box["x"] + box["width"] / 2 + jitter_x, box["y"] + box["height"] / 2 + jitter_y


async def move_to_locator(page, locator) -> bool:
    point = await _center(locator)
    if point is None:
        return False
    try:
        await page.mouse.move(point[0], point[1], steps=random.randint(12, 32))
        await human_pause(page, 0.08, 0.28, wander=False)
        return True
    except Exception:
        return False


async def human_click(locator, page=None, timeout: int = 8000) -> bool:
    host = host_page(locator, page)
    try:
        await locator.scroll_into_view_if_needed(timeout=min(timeout, 4000))
    except Exception:
        pass
    if host is not None:
        await move_to_locator(host, locator)
        await human_pause(host, 0.12, 0.42, wander=False)
    try:
        await locator.hover(timeout=min(timeout, 2500))
    except Exception:
        pass
    try:
        await locator.click(timeout=timeout)
        return True
    except Exception:
        try:
            await locator.click(timeout=timeout, force=True)
            return True
        except Exception:
            return False


async def human_type(page, locator, text: str) -> None:
    await human_click(locator, page=page, timeout=4000)
    await human_pause(page, 0.16, 0.48, wander=False)
    try:
        await locator.fill("")
    except Exception:
        await page.keyboard.press("Control+A")
        await human_pause(page, 0.05, 0.14, wander=False)
        await page.keyboard.press("Backspace")
    await human_pause(page, 0.12, 0.38, wander=False)
    for index, char in enumerate(text):
        delay = random.randint(70, 260)
        if random.random() < 0.14:
            delay += random.randint(160, 540)
        if index and random.random() < 0.07:
            delay += random.randint(220, 780)
        await page.keyboard.type(char, delay=0)
        await page.wait_for_timeout(delay)
    await human_pause(page, 0.18, 0.55, wander=True)


async def browse_results(page, card_selector: str = '[data-testid^="wrapper-card-header-"]') -> None:
    try:
        cards = page.locator(card_selector)
        count = await cards.count()
    except Exception:
        count = 0
    if count:
        look = min(count, random.randint(6, 14))
        print(f"Olho {look} voos na lista, rolando devagar de um para o outro.", flush=True)
        for index in range(look):
            card = cards.nth(index)
            try:
                await card.scroll_into_view_if_needed()
            except Exception:
                pass
            await move_to_locator(page, card)
            await human_pause(page, 0.45, 1.9, wander=index % 3 == 0)
            if random.random() < 0.22:
                try:
                    await page.mouse.wheel(0, random.randint(40, 140))
                except Exception:
                    pass
                await human_pause(page, 0.25, 0.9, wander=True)
        if random.random() < 0.55:
            try:
                await page.mouse.wheel(0, -random.randint(80, 280))
            except Exception:
                pass
        return
    print("Olho os voos na tela, rolando devagar.", flush=True)
    try:
        height = int(
            await page.evaluate(
                "() => Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
            )
        )
    except Exception:
        height = 1800
    traveled = 0
    limit = min(height, random.randint(1200, 2800))
    while traveled < limit:
        step = random.randint(70, 210)
        try:
            await page.mouse.wheel(0, step)
        except Exception:
            break
        traveled += step
        await page.wait_for_timeout(random.randint(240, 980))
        if random.random() < 0.3:
            await wander_mouse(page)
            await page.wait_for_timeout(random.randint(320, 1500))
    if random.random() < 0.7:
        try:
            await page.mouse.wheel(0, -random.randint(90, 380))
        except Exception:
            pass
        await human_pause(page, 0.4, 1.3)


def irregular_gap_seconds(base: float) -> float:
    """Pausa entre buscas sem cadência fixa."""
    floor = max(38.0, base * random.uniform(0.45, 0.85))
    ceil = max(floor + 25.0, base * random.uniform(1.4, 2.8))
    wait = random.uniform(floor, ceil)
    roll = random.random()
    if roll < 0.1:
        wait += random.uniform(80, 240)
    elif roll < 0.22:
        wait += random.uniform(20, 95)
    elif roll < 0.3:
        wait = max(28.0, wait * random.uniform(0.55, 0.8))
    return wait


def irregular_region_seconds() -> float:
    return random.uniform(140, 560)


async def idle_on_page(page, seconds: float) -> None:
    remaining = max(12.0, seconds)
    while remaining > 0:
        slice_s = min(remaining, random.uniform(7.0, 26.0))
        await page.wait_for_timeout(int(slice_s * 1000))
        remaining -= slice_s
        roll = random.random()
        if roll < 0.48:
            await wander_mouse(page)
        elif roll < 0.72:
            try:
                await page.mouse.wheel(0, random.choice([-1, 1]) * random.randint(36, 170))
            except Exception:
                pass


async def rest_like_a_person(page, home: str, seconds: float, goto) -> bool:
    wait = irregular_gap_seconds(seconds)
    print(
        f"Fico {wait / 60:.1f} min na tela de resultados, olhando os voos, antes da próxima busca.",
        flush=True,
    )
    try:
        await browse_results(page)
        remaining = max(8.0, wait - 8.0)
        await idle_on_page(page, remaining)
        await goto(page, home)
        await human_pause(page, 2.2, 7.8)
        await wander_mouse(page)
        return True
    except Exception as exc:
        if _closed(exc):
            print("A janela do Chrome fechou no intervalo. O que já salvou permanece.", flush=True)
            return False
        raise
