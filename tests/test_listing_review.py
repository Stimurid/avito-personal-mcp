"""Regression tests for Avito adaptive review. All tests are offline."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock,MagicMock

from avito_personal_mcp.listing_review import (
    resolve_policy, safe_avito_image_url, capture_gallery,
)


class PolicyTests(unittest.TestCase):
    def test_default_detailed_is_full(self):
        p = resolve_policy()
        self.assertEqual(p.effective, "full")
    def test_explicit_text_only_is_skipped(self):
        p = resolve_policy("text_only", "evaluation", "User said no photos")
        self.assertEqual(p.effective, "text_only")
        self.assertIn("User said no photos", p.reason)
    def test_quick_price_skips(self):
        self.assertEqual(resolve_policy("auto", "price_only").effective, "text_only")
    def test_availability_skips(self):
        self.assertEqual(resolve_policy("auto", "availability_only").effective, "text_only")
    def test_explicit_full_overrides_quick(self):
        self.assertEqual(resolve_policy("full", "price_only").effective, "full")
    def test_uppercase_mode_supported(self):
        self.assertEqual(resolve_policy("FULL", "price_only").effective, "full")
        self.assertEqual(resolve_policy("TEXT_ONLY").effective, "text_only")
    def test_skip_reason_overrides_auto(self):
        self.assertEqual(resolve_policy("auto", "evaluation", "User forbade photos").effective, "text_only")
    def test_bad_mode_rejected(self):
        with self.assertRaises(ValueError): resolve_policy("fail_closed")
    def test_domain_allowlist(self):
        self.assertTrue(safe_avito_image_url("https://b00.img.avito.st/image/1/2/3"))
        for url in ["http://b00.img.avito.st/image/1/2", "https://evil.img.avito.st.evil.com/image/1/2",
                    "https://www.avito.ru/image/1/2", "file:///etc/passwd", "javascript:alert(1)",
                    "https://b00.img.avito.st/personal"]:
            self.assertFalse(safe_avito_image_url(url))


class GalleryFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_gallery_missing_is_partial_not_no_photos(self):
        root = MagicMock()
        root.count = AsyncMock(return_value=0)
        page = MagicMock()
        page.locator.return_value = root
        status, images = await capture_gallery(page)
        self.assertEqual(status["capture_status"], "PARTIAL")
        self.assertIsNone(status["photo_count"])
        self.assertEqual(images, [])
    async def test_confirmed_empty_gallery_is_no_photos(self):
        thumbs = MagicMock()
        thumbs.count = AsyncMock(return_value=0)
        root = MagicMock()
        root.count = AsyncMock(return_value=1)
        root.locator.return_value = thumbs
        page = MagicMock()
        page.locator.return_value = root
        status, images = await capture_gallery(page)
        self.assertEqual(status["capture_status"], "NO_PHOTOS")
        self.assertEqual(status["photo_count"], 0)
        self.assertEqual(images, [])
    async def test_one_bad_photo_does_not_block_rest(self):
        thumbs = MagicMock()
        thumbs.count = AsyncMock(return_value=1)
        button = MagicMock()
        button.click = AsyncMock(side_effect=RuntimeError("broken thumbnail"))
        root = MagicMock()
        root.count = AsyncMock(return_value=1)
        root.locator.side_effect = lambda sel: thumbs if sel == "li[data-index]" else SimpleNamespace(first=button)
        page = MagicMock()
        page.locator.return_value = root
        status, images = await capture_gallery(page)
        self.assertEqual(status["capture_status"], "PARTIAL")
        self.assertEqual(len(status["failures"]), 1)
        self.assertEqual(images, [])


if __name__ == "__main__": unittest.main()
