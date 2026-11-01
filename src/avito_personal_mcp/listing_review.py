"""Adaptive, read-only listing evidence capture from the user's Avito browser.

The output explicitly distinguishes image ACQUISITION from actual MODEL INSPECTION.
No automatic claim of visual inspection is made by downloading a file.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from urllib.parse import urlsplit

from playwright.async_api import Page

from avito_personal_mcp.listing_detail import (
    discover_listing_detail,
    resolve_listing_url,
)

VALID_MODES = frozenset({"auto", "full", "text_only"})
QUICK_PURPOSES = frozenset({"quick_lookup", "price_only", "availability_only", "search", "summary"})
MAX_GALLERY = 60
MAX_BYTES = 15_000_000


@dataclass(frozen=True)
class Policy:
    requested: str
    effective: str
    reason: str


def resolve_policy(mode: str = "auto", purpose: str = "evaluation",
                   skip_reason: str = "") -> Policy:
    mode = mode.strip().casefold() if isinstance(mode, str) else ""
    if mode not in VALID_MODES:
        raise ValueError("mode must be auto, full or text_only")
    if mode == "text_only" or (mode == "auto" and skip_reason.strip()):
        return Policy(mode, "text_only", skip_reason or "Explicit instruction to omit photographs")
    if mode == "full":
        return Policy(mode, mode, "Explicit full-review request")
    if purpose.strip().casefold() in QUICK_PURPOSES:
        return Policy(mode, "text_only", "Quick task; photographs are not required")
    return Policy(mode, "full", "Detailed evaluation default")


def safe_avito_image_url(value: str) -> bool:
    try:
        u = urlsplit(value)
        return (u.scheme == "https"
                and (u.hostname == "img.avito.st" or u.hostname.endswith(".img.avito.st"))
                and u.path.startswith("/image/"))
    except (TypeError, ValueError, AttributeError):
        return False


async def capture_gallery(page: Page, *, include_bytes: bool = True,
                          selected_index: int | None = None) -> tuple[dict, list[dict]]:
    """Capture all gallery slides using its observed, rendered thumbnail controls.

    Returns a partial result on broken slide/image, never hides an incomplete
    gallery. The image CDN URL is derived only from the page, not constructed.
    """
    root = page.locator('[data-marker="item-view/main-gallery"]')
    if await root.count() == 0:
        return ({"capture_status": "PARTIAL",
                 "photo_count": None, "captured_count": 0,
                 "missing_reason": "Gallery DOM unavailable; cannot assert zero photos"}, [])

    thumbs = root.locator('li[data-index]')
    count = await thumbs.count()
    if count == 0:
        return ({"capture_status": "NO_PHOTOS", "photo_count": 0,
                 "captured_count": 0, "missing_reason": None}, [])
    if count > MAX_GALLERY:
        return ({"capture_status": "PARTIAL", "photo_count": count,
                 "captured_count": 0, "missing_reason": "Gallery exceeds safety limit"}, [])

    gathered: list[dict] = []
    failures: list[dict] = []
    for i in range(count):
        if selected_index is not None and i != selected_index:
            continue
        try:
            # Stable listing marker rather than volatile CSS class.
            thumb = root.locator(f'li[data-index="{i}"] button').first
            await thumb.click(timeout=8000, force=True)
            await page.wait_for_timeout(150)
            imgs = page.locator('#gallery-slider img')
            n = await imgs.count()
            if n == 0:
                raise ValueError("slider image missing")
            # The carousel mounts previous/current/next: centre slide is selected.
            src = await imgs.nth(n // 2).get_attribute("src")
            if not src or not safe_avito_image_url(src):
                raise ValueError("invalid or missing Avito photo URL")
            item: dict = {"index": i + 1, "source_url": src}
            if include_bytes:
                result = await page.context.request.get(src, timeout=20000)
                if result.status != 200:
                    raise ValueError(f"image fetch HTTP {result.status}")
                raw = await result.body()
                if not raw or len(raw) > MAX_BYTES:
                    raise ValueError("image empty or too large")
                mime = result.headers.get("content-type", "").split(";")[0].strip()
                if mime not in ("image/jpeg", "image/png", "image/webp"):
                    raise ValueError("unsupported image content type")
                item.update({"sha256": hashlib.sha256(raw).hexdigest(),
                             "mime_type": mime, "bytes": raw})
            gathered.append(item)
        except Exception as exc:
            failures.append({"index": i + 1, "reason": f"{type(exc).__name__}: {str(exc)[:140]}"})
    target_count = 1 if selected_index is not None else count
    complete = len(gathered) == target_count and not failures
    status = "FULL" if complete else "PARTIAL"
    return ({
        "capture_status": status,
        "photo_count": count,
        "captured_count": len(gathered),
        "failures": failures,
        "missing_reason": None if complete else "One or more photographs unavailable",
    }, gathered)


async def review_listing(page: Page, origin: str, reference: int | str,
                         mode: str = "auto", purpose: str = "evaluation",
                         skip_reason: str = "",
                         selected_index: int | None = None) -> tuple[dict, list[dict]]:
    policy = resolve_policy(mode, purpose, skip_reason)
    listing_id, url = await resolve_listing_url(page, origin, reference)
    # Never navigate an arbitrary unrelated user tab. Close the temporary tab.
    tab = await page.context.new_page()
    try:
        details = await discover_listing_detail(tab, origin, url)
        if policy.effective == "text_only":
            return ({
                "status": "ok", "listing": details,
                "review_mode_requested": policy.requested,
                "review_mode_effective": "text_only",
                "review_status": "SKIPPED_BY_USER" if policy.requested == "text_only" or skip_reason.strip() else "TEXT_ONLY",
                "review_reason": policy.reason,
                "gallery_count": None,
                "photo_count_acquired": 0,
                "visual_inspection": "NOT_PERFORMED",
                "seller_message_authorized": False,
            }, [])
        evidence, images = await capture_gallery(
            tab, include_bytes=True, selected_index=selected_index
        )
        meta = {
            "status": "ok", "listing": details,
            "review_mode_requested": policy.requested,
            "review_mode_effective": policy.effective,
            "review_status": evidence["capture_status"],
            "review_reason": policy.reason,
            "gallery_count": evidence["photo_count"],
            "photo_count_acquired": evidence["captured_count"],
            "gallery_failures": evidence.get("failures", []),
            "missing_reason": evidence.get("missing_reason"),
            "visual_inspection": "PENDING_AGENT",  # downloading != viewing
            "seller_message_authorized": False,
            "photos": [
                {"index": im["index"], "sha256": im.get("sha256"),
                 "mime_type": im.get("mime_type"), "source_url": im["source_url"]}
                for im in images
            ],
        }
        if selected_index is not None:
            meta["requested_photo_index"] = selected_index + 1
            meta["review_status"] = "PARTIAL" if evidence["capture_status"] == "FULL" else evidence["capture_status"]
        return meta, images
    finally:
        await tab.close()
