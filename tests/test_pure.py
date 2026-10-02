"""Dependency-free tests for the money-critical pure logic.

Run:  python -m unittest tests.test_pure -v      (no third-party packages needed)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.net import UnsafeUrl, validate_provider_url  # noqa: E402
from app.payments.base import GatewayError, SignatureError  # noqa: E402
from app.payments.razorpay import RazorpayGateway  # noqa: E402
from app.payments.signing import hmac_sha256_hex, verify_hmac_sha256_hex  # noqa: E402
from app.pricemath import cap_discount_to_floor, margin_floor_per_1000, scale_to_qty  # noqa: E402
from app.ratelimit import RateLimiter  # noqa: E402
from app.sync_normalize import (  # noqa: E402
    category_id_for,
    looks_malformed,
    map_service_type,
    normalize_service,
)

WHSEC = "whsec_test"


def _paid_body(**over) -> bytes:
    link = {"id": "plink_ABC123xyz", "reference_id": "PAY-1A2B3C4D", "amount": 50000,
            "amount_paid": 50000, "currency": "INR", "status": "paid"}
    link.update(over)
    return json.dumps({"event": "payment_link.paid", "payload": {
        "payment_link": {"entity": link},
        "payment": {"entity": {"amount": link["amount_paid"], "currency": "INR", "status": "captured"}},
    }}).encode()


def _sign(body: bytes, secret: str = WHSEC) -> dict[str, str]:
    return {"X-Razorpay-Signature": hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
            "X-Razorpay-Event-Id": "evt_1"}


class SigningTests(unittest.TestCase):
    def test_roundtrip(self):
        self.assertTrue(verify_hmac_sha256_hex("k", b"body", hmac_sha256_hex("k", b"body")))

    def test_rejects_tampered_body_wrong_key_and_missing(self):
        sig = hmac_sha256_hex("k", b"body")
        self.assertFalse(verify_hmac_sha256_hex("k", b"body!", sig))
        self.assertFalse(verify_hmac_sha256_hex("other", b"body", sig))
        self.assertFalse(verify_hmac_sha256_hex("k", b"body", None))
        self.assertFalse(verify_hmac_sha256_hex("k", b"body", ""))
        self.assertFalse(verify_hmac_sha256_hex(None, b"body", sig))
        self.assertFalse(verify_hmac_sha256_hex("", b"body", sig))


class RazorpayWebhookTests(unittest.TestCase):
    def setUp(self):
        self.gw = RazorpayGateway("key", "secret", WHSEC)

    def test_valid_paid_event(self):
        body = _paid_body()
        ev = self.gw.parse_webhook(_sign(body), body)
        self.assertEqual((ev.kind, ev.reference, ev.amount_paise, ev.currency, ev.event_id),
                         ("paid", "PAY-1A2B3C4D", 50000, "INR", "evt_1"))

    def test_bad_missing_and_wrong_secret_signatures_rejected(self):
        body = _paid_body()
        with self.assertRaises(SignatureError):
            self.gw.parse_webhook({"X-Razorpay-Signature": "00" * 32}, body)
        with self.assertRaises(SignatureError):
            self.gw.parse_webhook({}, body)
        with self.assertRaises(SignatureError):
            self.gw.parse_webhook(_sign(body, "attacker-secret"), body)

    def test_signature_covers_exact_bytes(self):
        body = _paid_body()
        headers = _sign(body)
        with self.assertRaises(SignatureError):
            self.gw.parse_webhook(headers, body.replace(b"50000", b"99999"))

    def test_header_names_are_case_insensitive(self):
        body = _paid_body()
        headers = {k.lower(): v for k, v in _sign(body).items()}
        self.assertEqual(self.gw.parse_webhook(headers, body).kind, "paid")

    def test_unknown_reference_is_ignored_not_credited(self):
        body = _paid_body(reference_id="NOT-OURS")
        self.assertEqual(self.gw.parse_webhook(_sign(body), body).kind, "ignored")

    def test_inconsistent_paid_event_needs_attention(self):
        body = _paid_body(status="created")
        self.assertEqual(self.gw.parse_webhook(_sign(body), body).kind, "attention")
        body = _paid_body(amount_paid=0)
        self.assertEqual(self.gw.parse_webhook(_sign(body), body).kind, "attention")

    def test_refund_and_dispute_escalate_to_human(self):
        for name in ("refund.processed", "payment.dispute.created"):
            body = json.dumps({"event": name, "payload": {}}).encode()
            self.assertEqual(self.gw.parse_webhook(_sign(body), body).kind, "attention")

    def test_expired_and_unknown_events(self):
        body = json.dumps({"event": "payment_link.expired", "payload": {"payment_link": {"entity": {
            "id": "plink_ABC123xyz", "reference_id": "PAY-1A2B3C4D"}}}}).encode()
        self.assertEqual(self.gw.parse_webhook(_sign(body), body).kind, "expired")
        body = json.dumps({"event": "order.paid", "payload": {}}).encode()
        self.assertEqual(self.gw.parse_webhook(_sign(body), body).kind, "ignored")

    def test_non_json_and_non_object_bodies(self):
        for body in (b"not json", b"[1,2,3]"):
            with self.assertRaises(GatewayError):
                self.gw.parse_webhook(_sign(body), body)

    def test_event_id_falls_back_to_body_hash(self):
        body = _paid_body()
        headers = {"X-Razorpay-Signature": _sign(body)["X-Razorpay-Signature"]}
        ev = self.gw.parse_webhook(headers, body)
        self.assertTrue(ev.event_id.startswith("sha256:"))
        self.assertEqual(ev.event_id, self.gw.parse_webhook(headers, body).event_id)  # stable => dedupe works

    def test_requires_all_credentials(self):
        with self.assertRaises(ValueError):
            RazorpayGateway("key", "secret", "")


class PriceMathTests(unittest.TestCase):
    def test_margin_floor(self):
        self.assertEqual(margin_floor_per_1000(10_000, 5.0), 10_500)
        self.assertEqual(margin_floor_per_1000(10_000, 0.0), 10_000)
        self.assertEqual(margin_floor_per_1000(10_000, -50.0), 10_000)  # never below cost

    def test_scale_to_qty_rounds_up(self):
        self.assertEqual(scale_to_qty(1000, 500), 500)
        self.assertEqual(scale_to_qty(333, 1), 1)
        self.assertEqual(scale_to_qty(1000, 0), 0)

    def test_coupon_can_never_breach_the_floor(self):
        self.assertEqual(cap_discount_to_floor(1000, 400, 800), 200)   # capped to headroom
        self.assertEqual(cap_discount_to_floor(1000, 100, 800), 100)   # fits
        self.assertEqual(cap_discount_to_floor(1000, 500, 1000), 0)    # no headroom
        self.assertEqual(cap_discount_to_floor(1000, 500, 1200), 0)    # floor above subtotal
        self.assertEqual(cap_discount_to_floor(1000, -5, 0), 0)
        self.assertEqual(cap_discount_to_floor(0, 100, 0), 0)
        for sub in (500, 1234, 99_999):
            for disc in (1, 77, 5_000, 10**9):
                for floor in (0, 300, 1100):
                    d = cap_discount_to_floor(sub, disc, floor)
                    self.assertGreaterEqual(d, 0)
                    self.assertLessEqual(d, disc)
                    if d:
                        self.assertGreaterEqual(sub - d, floor)


class NetSafetyTests(unittest.TestCase):
    def test_blocks_ssrf_targets(self):
        for url in ("http://localhost/api", "https://127.0.0.1/api", "https://10.0.0.5/api",
                    "https://192.168.1.1/api", "https://169.254.169.254/latest/meta-data",
                    "https://[::1]/api", "https://metadata.google.internal/", "https://db.internal/x",
                    "https://intranet/x", "ftp://example.com/x", "https://user:pw@example.com/x",
                    "https://0.0.0.0/x", ""):
            with self.assertRaises(UnsafeUrl, msg=url):
                validate_provider_url(url)

    def test_allows_public_https(self):
        self.assertEqual(validate_provider_url("https://panel.example.com/api/v2"), "https://panel.example.com/api/v2")

    def test_plain_http_rejected_unless_private_allowed(self):
        with self.assertRaises(UnsafeUrl):
            validate_provider_url("http://panel.example.com/api")
        self.assertTrue(validate_provider_url("http://localhost:9000/api", allow_private=True, require_https=False))


class RateLimiterTests(unittest.TestCase):
    def test_window_and_isolation(self):
        rl = RateLimiter(3, 60.0)
        self.assertTrue(all(rl.allow("a", now=t) for t in (0, 1, 2)))
        self.assertFalse(rl.allow("a", now=3))
        self.assertTrue(rl.allow("b", now=3))            # other keys unaffected
        self.assertGreaterEqual(rl.retry_after("a", now=3), 1)
        self.assertTrue(rl.allow("a", now=61))           # window slid

    def test_disabled_when_limit_zero(self):
        rl = RateLimiter(0, 60.0)
        self.assertTrue(all(rl.allow("a") for _ in range(100)))

    def test_key_flood_does_not_grow_unbounded(self):
        rl = RateLimiter(1, 60.0, max_keys=100)
        for i in range(1000):
            rl.allow(f"k{i}", now=0.0)
        self.assertLessEqual(len(rl._hits), 1000)
        rl.allow("x", now=1000.0)
        self.assertLess(len(rl._hits), 1000)


class SyncNormalizeTests(unittest.TestCase):
    def _row(self, **over):
        base = dict(external_id=12, name="  IG   Followers\n", category="Instagram", rate="0.9",
                    min_qty="100", max_qty="10000", service_type="Default", fx_to_inr=1.0)
        base.update(over)
        return normalize_service(**base)

    def test_clean_row_and_fx(self):
        n = self._row(fx_to_inr=83.0)
        self.assertEqual((n.external_id, n.name, n.rate_per_1000_paise, n.service_type), ("12", "IG Followers", 7470, "default"))

    def test_rejects_garbage(self):
        for bad in (dict(rate=float("nan")), dict(rate=float("inf")), dict(rate=-1), dict(rate=0),
                    dict(rate="abc"), dict(min_qty=0), dict(min_qty=500, max_qty=100), dict(max_qty=10**12),
                    dict(external_id=""), dict(external_id="x" * 40), dict(external_id="a b"),
                    dict(name="   "), dict(fx_to_inr=0), dict(fx_to_inr=float("nan")), dict(rate=10**9)):
            self.assertIsNone(self._row(**bad), msg=str(bad))

    def test_strips_control_characters(self):
        n = self._row(name="Bad\x00Name\x07\tHere")
        self.assertNotIn("\x00", n.name)
        self.assertNotIn("\x07", n.name)

    def test_type_mapping(self):
        self.assertEqual(map_service_type("Default"), "default")
        self.assertEqual(map_service_type("Custom Comments"), "comments")
        self.assertEqual(map_service_type("Mentions Custom List"), "mentions")
        for t in ("Package", "Poll", "Subscriptions", "Comment Likes"):
            self.assertEqual(map_service_type(t), "unsupported")
        self.assertFalse(self._row(service_type="Package").supported)

    def test_category_ids_are_stable_and_bounded(self):
        self.assertEqual(category_id_for("Instagram Followers"), category_id_for("  instagram followers "))
        self.assertLessEqual(len(category_id_for("A" * 500)), 32)
        self.assertNotEqual(category_id_for("A!"), category_id_for("A?"))

    def test_malformed_snapshot_detection(self):
        self.assertIsNotNone(looks_malformed(0, 0, 0))
        self.assertIsNotNone(looks_malformed(100, 20, 0))      # catalog collapsed
        self.assertIsNotNone(looks_malformed(0, 6, 4))         # >30% invalid of 10
        self.assertIsNone(looks_malformed(100, 95, 2))
        self.assertIsNone(looks_malformed(0, 5, 0))


if __name__ == "__main__":
    unittest.main()
