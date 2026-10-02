"""Idempotent catalog seed.

IDs are stable:
  categories  cat_ig, cat_yt, cat_tt, cat_tg, cat_x, cat_sp
  providers   prov_nova, prov_atlas, prov_prism
  star        star_ig_premium at INR 149 / 1000
  coupon      FALARON10
  global markup 25%
  welcome bonus INR 250
All money is integer paise.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models import (
    SETTING_GLOBAL_MARKUP,
    SETTING_MIN_MARGIN,
    SETTING_WELCOME_BONUS,
    KILL_MAINTENANCE,
    KILL_NEW_ORDERS,
    KILL_PAYMENTS,
    KILL_READ_ONLY,
    KILL_REFILLS,
    Ad,
    AppSetting,
    Category,
    Coupon,
    Provider,
    Service,
    StarPrice,
)
from app.security import encrypt_secret, utcnow

log = logging.getLogger("falaron.seed")


CATEGORIES = [
    ("cat_ig", "Instagram", "📸", "Followers, likes, views, comments", 10),
    ("cat_yt", "YouTube", "▶️", "Views, watch time, subscribers, likes", 20),
    ("cat_tt", "TikTok", "🎵", "Views, likes, followers, shares", 30),
    ("cat_tg", "Telegram", "✈️", "Members, post views, reactions", 40),
    ("cat_x", "X", "𝕏", "Followers, likes, reposts, views", 50),
    ("cat_sp", "Spotify", "🎧", "Plays, followers, saves", 60),
]


def _providers(settings: Settings) -> list[dict]:
    prism_type = "perfectpanel" if settings.has_real_smm_panel else "mock"
    prism_url = settings.smm_api_url if settings.has_real_smm_panel else None
    encrypted = None
    if settings.has_real_smm_panel and settings.smm_api_key_value():
        encrypted = encrypt_secret(settings.smm_api_key_value() or "", settings)
    return [
        {
            "id": "prov_nova",
            "name": "Nova Panel",
            "adapter_type": "mock",
            "base_url": None,
            "encrypted_api_key": None,
            "markup_percent": None,
            "markup_fixed_paise": None,
        },
        {
            "id": "prov_atlas",
            "name": "Atlas Media",
            "adapter_type": "mock",
            "base_url": None,
            "encrypted_api_key": None,
            "markup_percent": 20.0,
            "markup_fixed_paise": None,
        },
        {
            "id": "prov_prism",
            "name": "Prism Exchange",
            "adapter_type": prism_type,
            "base_url": prism_url,
            "encrypted_api_key": encrypted,
            "markup_percent": None,
            "markup_fixed_paise": None,
        },
    ]


# cost rates are provider cost in paise per 1000
# sell prices come from the 25% global markup unless a star/custom markup is set
SERVICES: list[dict] = [
    # Instagram / Nova
    dict(id="svc_ig_followers", category_id="cat_ig", provider_id="prov_nova",
         provider_service_id="nova-ig-followers", name="Instagram Followers",
         description="High-quality Instagram followers. Gradual delivery.",
         service_type="default", min_qty=50, max_qty=100_000, rate_per_1000_paise=8900,
         is_refillable=True, is_cancelable=True, average_time="0-2 hours"),
    dict(id="svc_ig_likes", category_id="cat_ig", provider_id="prov_nova",
         provider_service_id="nova-ig-likes", name="Instagram Likes",
         description="Fast likes for posts and reels.",
         service_type="default", min_qty=20, max_qty=50_000, rate_per_1000_paise=2100,
         is_refillable=True, is_cancelable=True, average_time="0-30 min"),
    dict(id="svc_ig_views", category_id="cat_ig", provider_id="prov_nova",
         provider_service_id="nova-ig-views", name="Instagram Reel Views",
         description="Views for Reels. Instant start.",
         service_type="default", min_qty=100, max_qty=500_000, rate_per_1000_paise=80,
         is_refillable=False, is_cancelable=True, average_time="0-15 min"),
    dict(id="svc_ig_comments", category_id="cat_ig", provider_id="prov_atlas",
         provider_service_id="atlas-ig-comments", name="Instagram Custom Comments",
         description="Custom comments, one per line.",
         service_type="comments", min_qty=5, max_qty=500, rate_per_1000_paise=45_000,
         is_refillable=False, is_cancelable=True, average_time="1-6 hours"),
    dict(id="svc_ig_mentions", category_id="cat_ig", provider_id="prov_atlas",
         provider_service_id="atlas-ig-mentions", name="Instagram Mentions",
         description="Mention a list of usernames on a post.",
         service_type="mentions", min_qty=10, max_qty=1_000, rate_per_1000_paise=18_000,
         is_refillable=False, is_cancelable=True, average_time="1-4 hours"),
    dict(id="star_ig_premium_svc", category_id="cat_ig", provider_id="prov_prism",
         provider_service_id="prism-ig-premium", name="Instagram Followers Premium",
         description="Star service. Refill 30 days. Priority queue.",
         service_type="default", min_qty=100, max_qty=50_000, rate_per_1000_paise=11_000,
         is_refillable=True, is_cancelable=True, average_time="0-1 hour"),
    # YouTube
    dict(id="svc_yt_views", category_id="cat_yt", provider_id="prov_nova",
         provider_service_id="nova-yt-views", name="YouTube Views",
         description="High retention views.",
         service_type="default", min_qty=100, max_qty=200_000, rate_per_1000_paise=3_400,
         is_refillable=True, is_cancelable=True, average_time="0-6 hours"),
    dict(id="svc_yt_likes", category_id="cat_yt", provider_id="prov_nova",
         provider_service_id="nova-yt-likes", name="YouTube Likes",
         description="Likes for videos.",
         service_type="default", min_qty=20, max_qty=20_000, rate_per_1000_paise=4_800,
         is_refillable=True, is_cancelable=True, average_time="0-2 hours"),
    dict(id="svc_yt_subs", category_id="cat_yt", provider_id="prov_atlas",
         provider_service_id="atlas-yt-subs", name="YouTube Subscribers",
         description="Channel subscribers. Refill 30 days.",
         service_type="default", min_qty=50, max_qty=20_000, rate_per_1000_paise=22_000,
         is_refillable=True, is_cancelable=True, average_time="0-12 hours"),
    dict(id="svc_yt_comments", category_id="cat_yt", provider_id="prov_prism",
         provider_service_id="prism-yt-comments", name="YouTube Custom Comments",
         description="Custom comments, one per line.",
         service_type="comments", min_qty=5, max_qty=200, rate_per_1000_paise=55_000,
         is_refillable=False, is_cancelable=True, average_time="2-12 hours"),
    # TikTok
    dict(id="svc_tt_views", category_id="cat_tt", provider_id="prov_nova",
         provider_service_id="nova-tt-views", name="TikTok Views",
         description="Video views, instant start.",
         service_type="default", min_qty=100, max_qty=1_000_000, rate_per_1000_paise=40,
         is_refillable=False, is_cancelable=True, average_time="0-10 min"),
    dict(id="svc_tt_likes", category_id="cat_tt", provider_id="prov_nova",
         provider_service_id="nova-tt-likes", name="TikTok Likes",
         description="Organic-speed likes.",
         service_type="default", min_qty=20, max_qty=100_000, rate_per_1000_paise=1_600,
         is_refillable=True, is_cancelable=True, average_time="0-1 hour"),
    dict(id="svc_tt_followers", category_id="cat_tt", provider_id="prov_atlas",
         provider_service_id="atlas-tt-followers", name="TikTok Followers",
         description="Profile followers. Refill 30 days.",
         service_type="default", min_qty=50, max_qty=50_000, rate_per_1000_paise=9_500,
         is_refillable=True, is_cancelable=True, average_time="0-6 hours"),
    dict(id="svc_tt_shares", category_id="cat_tt", provider_id="prov_prism",
         provider_service_id="prism-tt-shares", name="TikTok Shares",
         description="Shares / reposts.",
         service_type="default", min_qty=20, max_qty=20_000, rate_per_1000_paise=2_200,
         is_refillable=False, is_cancelable=True, average_time="0-3 hours"),
    # Telegram
    dict(id="svc_tg_members", category_id="cat_tg", provider_id="prov_nova",
         provider_service_id="nova-tg-members", name="Telegram Channel Members",
         description="Members for public channels.",
         service_type="default", min_qty=50, max_qty=50_000, rate_per_1000_paise=6_500,
         is_refillable=True, is_cancelable=True, average_time="0-6 hours"),
    dict(id="svc_tg_views", category_id="cat_tg", provider_id="prov_nova",
         provider_service_id="nova-tg-views", name="Telegram Post Views",
         description="Views for a specific post link.",
         service_type="default", min_qty=100, max_qty=200_000, rate_per_1000_paise=90,
         is_refillable=False, is_cancelable=True, average_time="0-20 min"),
    dict(id="svc_tg_reactions", category_id="cat_tg", provider_id="prov_atlas",
         provider_service_id="atlas-tg-reactions", name="Telegram Reactions",
         description="Positive reactions on a post.",
         service_type="default", min_qty=20, max_qty=10_000, rate_per_1000_paise=1_800,
         is_refillable=False, is_cancelable=True, average_time="0-1 hour"),
    # X
    dict(id="svc_x_followers", category_id="cat_x", provider_id="prov_nova",
         provider_service_id="nova-x-followers", name="X Followers",
         description="Profile followers. Refill 30 days.",
         service_type="default", min_qty=50, max_qty=50_000, rate_per_1000_paise=12_000,
         is_refillable=True, is_cancelable=True, average_time="0-8 hours"),
    dict(id="svc_x_likes", category_id="cat_x", provider_id="prov_atlas",
         provider_service_id="atlas-x-likes", name="X Likes",
         description="Likes for a post.",
         service_type="default", min_qty=20, max_qty=20_000, rate_per_1000_paise=2_400,
         is_refillable=True, is_cancelable=True, average_time="0-1 hour"),
    dict(id="svc_x_reposts", category_id="cat_x", provider_id="prov_prism",
         provider_service_id="prism-x-reposts", name="X Reposts",
         description="Reposts / retweets.",
         service_type="default", min_qty=10, max_qty=10_000, rate_per_1000_paise=4_500,
         is_refillable=False, is_cancelable=True, average_time="0-3 hours"),
    dict(id="svc_x_views", category_id="cat_x", provider_id="prov_nova",
         provider_service_id="nova-x-views", name="X Post Views",
         description="Impression views.",
         service_type="default", min_qty=100, max_qty=500_000, rate_per_1000_paise=60,
         is_refillable=False, is_cancelable=True, average_time="0-15 min"),
    # Spotify
    dict(id="svc_sp_plays", category_id="cat_sp", provider_id="prov_nova",
         provider_service_id="nova-sp-plays", name="Spotify Plays",
         description="Track plays from real-looking accounts.",
         service_type="default", min_qty=100, max_qty=100_000, rate_per_1000_paise=1_500,
         is_refillable=False, is_cancelable=True, average_time="0-12 hours"),
    dict(id="svc_sp_followers", category_id="cat_sp", provider_id="prov_atlas",
         provider_service_id="atlas-sp-followers", name="Spotify Followers",
         description="Artist / playlist followers.",
         service_type="default", min_qty=50, max_qty=20_000, rate_per_1000_paise=7_800,
         is_refillable=True, is_cancelable=True, average_time="0-8 hours"),
    dict(id="svc_sp_saves", category_id="cat_sp", provider_id="prov_prism",
         provider_service_id="prism-sp-saves", name="Spotify Saves",
         description="Saves to user libraries.",
         service_type="default", min_qty=20, max_qty=20_000, rate_per_1000_paise=3_200,
         is_refillable=False, is_cancelable=True, average_time="0-6 hours"),
]


STAR_PRICES = [
    # Featured public star: INR 149.00 per 1000
    dict(
        id="star_ig_premium",
        service_id="star_ig_premium_svc",
        user_id=None,
        custom_rate_per_1000_paise=14_900,
        label="Premium",
        is_active=True,
    ),
    dict(
        id="star_yt_views",
        service_id="svc_yt_views",
        user_id=None,
        custom_rate_per_1000_paise=3_990,
        label="Launch",
        is_active=True,
    ),
    dict(
        id="star_tt_likes",
        service_id="svc_tt_likes",
        user_id=None,
        custom_rate_per_1000_paise=1_790,
        label="Boost",
        is_active=True,
    ),
]


SETTINGS = {
    SETTING_GLOBAL_MARKUP: "25",
    SETTING_MIN_MARGIN: "5",
    SETTING_WELCOME_BONUS: "25000",
    KILL_NEW_ORDERS: "0",
    KILL_PAYMENTS: "0",
    KILL_REFILLS: "0",
    KILL_READ_ONLY: "0",
    KILL_MAINTENANCE: "0",
    "support_url": "https://t.me/falaron_support",
    "terms_url": "https://falaron.example/terms",
    "privacy_url": "https://falaron.example/privacy",
    "brand_name": "FALARON",
}


ADS = [
    dict(
        placement="home",
        title="FALARON Star Services",
        caption="Premium Instagram followers from ₹149 / 1,000. 30-day refill, priority queue.",
        image_path="assets/logo.jpg",
        url=None,
        active=True,
        sort=10,
    ),
    dict(
        placement="wallet",
        title="Welcome bonus ₹250",
        caption="New accounts get ₹250 credited to the ledger after accepting terms.",
        image_path=None,
        url=None,
        active=True,
        sort=10,
    ),
    dict(
        placement="rewards",
        title="Referral 8%",
        caption="Earn 8% on your first 3 referred orders plus 5% of their first deposit.",
        image_path="assets/logo.jpg",
        url=None,
        active=True,
        sort=10,
    ),
]


async def seed_production(session: AsyncSession, settings: Settings) -> None:
    """Production boot: NO demo catalog, NO demo providers, NO demo coupon or ads.

    Only the settings rows the platform needs, plus (optionally) the real provider described by
    SMM_API_URL / SMM_API_KEY. The catalog then arrives via provider sync and stays inactive
    until the operator enables it.
    """
    await _ensure_settings(session, settings)
    await _bootstrap_main_provider(session, settings)


async def _bootstrap_main_provider(session: AsyncSession, settings: Settings) -> None:
    if not settings.has_real_smm_panel:
        return
    row = await session.get(Provider, "prov_main")
    encrypted = encrypt_secret(settings.smm_api_key_value() or "", settings)
    if row is None:
        session.add(
            Provider(
                id="prov_main",
                name="Main panel",
                adapter_type="perfectpanel",
                base_url=settings.smm_api_url,
                encrypted_api_key=encrypted,
                is_active=True,
                health_status="unknown",
                created_at=utcnow(),
            )
        )
        log.info("created provider prov_main from environment")
    else:
        row.adapter_type = "perfectpanel"
        row.base_url = settings.smm_api_url
        row.encrypted_api_key = encrypted
    from app.catalog import reset_adapter_cache

    reset_adapter_cache()
    await session.flush()


async def seed_if_empty(session: AsyncSession, settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    if settings.is_production:
        await seed_production(session, settings)
        return
    existing = (await session.execute(select(func.count()).select_from(Category))).scalar_one()
    if int(existing) == 0:
        log.info("seeding empty catalog")
        await _seed_all(session, settings)
        return
    # Keep provider Prism credentials in sync with env on every boot.
    await _sync_prism_credentials(session, settings)
    await _ensure_settings(session, settings)
    await _ensure_coupon(session)
    await _ensure_ads(session)


async def _seed_all(session: AsyncSession, settings: Settings) -> None:
    now = utcnow()
    for cid, name, emoji, desc, order in CATEGORIES:
        session.add(
            Category(
                id=cid,
                name=name,
                emoji=emoji,
                description=desc,
                sort_order=order,
                is_active=True,
            )
        )
    for payload in _providers(settings):
        session.add(Provider(created_at=now, is_active=True, health_status="unknown", **payload))
    for payload in SERVICES:
        session.add(Service(created_at=now, is_active=True, **payload))
    for payload in STAR_PRICES:
        session.add(StarPrice(**payload))
    await _ensure_coupon(session)
    await _ensure_settings(session, settings)
    await _ensure_ads(session)
    await session.flush()
    log.info("seed complete: %s categories, %s services, 3 providers, 3 stars, FALARON10",
             len(CATEGORIES), len(SERVICES))


async def _ensure_coupon(session: AsyncSession) -> None:
    row = await session.get(Coupon, "FALARON10")
    if row is None:
        session.add(
            Coupon(
                code="FALARON10",
                discount_type="percent",
                value=10.0,
                max_uses=10_000,
                used_count=0,
                per_user_limit=1,
                min_order_paise=5_000,
                is_active=True,
                description="10% off your first qualifying order",
            )
        )
        await session.flush()


async def _ensure_ads(session: AsyncSession) -> None:
    existing = (await session.execute(select(func.count()).select_from(Ad))).scalar_one()
    if int(existing) > 0:
        return
    for payload in ADS:
        session.add(Ad(**payload))
    await session.flush()


async def _ensure_settings(session: AsyncSession, settings: Settings) -> None:
    merged = dict(SETTINGS)
    merged["brand_name"] = settings.brand_name
    if settings.is_production:
        merged["support_url"] = settings.support_url or ""
        merged["terms_url"] = settings.terms_url or ""
        merged["privacy_url"] = settings.privacy_url or ""
    else:
        if settings.support_url:
            merged["support_url"] = settings.support_url
        if settings.terms_url:
            merged["terms_url"] = settings.terms_url
        if settings.privacy_url:
            merged["privacy_url"] = settings.privacy_url
    merged[SETTING_GLOBAL_MARKUP] = str(settings.global_markup_percent)
    merged[SETTING_MIN_MARGIN] = str(settings.min_margin_percent)
    merged[SETTING_WELCOME_BONUS] = str(settings.welcome_bonus_paise)
    for key, value in merged.items():
        row = await session.get(AppSetting, key)
        if row is None:
            session.add(AppSetting(key=key, value=str(value), updated_at=utcnow()))
        elif settings.is_production and key in {"terms_url", "privacy_url", "support_url"} and value:
            row.value = str(value)  # env is the source of truth for legal links in production
            row.updated_at = utcnow()
        elif key == "brand_name" and row.value.lower() in {"velora"}:
            row.value = "FALARON"
            row.updated_at = utcnow()
    await session.flush()


async def _sync_prism_credentials(session: AsyncSession, settings: Settings) -> None:
    prism = await session.get(Provider, "prov_prism")
    if prism is None:
        return
    if settings.has_real_smm_panel:
        prism.adapter_type = "perfectpanel"
        prism.base_url = settings.smm_api_url
        prism.encrypted_api_key = encrypt_secret(settings.smm_api_key_value() or "", settings)
        from app.catalog import reset_adapter_cache

        reset_adapter_cache()
        log.info("Prism provider bound to configured SMM panel URL")
    await session.flush()
