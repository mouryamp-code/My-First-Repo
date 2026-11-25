"""Configuration and targets loader for scraper."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import AliasChoices, BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

CategorySlug = Literal[
    "cpu",
    "gpu",
    "motherboard",
    "ram",
    "psu",
    "case",
    "ssd",
    "hdd",
    "cooler",
    "monitor",
    "fan",
    "keyboard",
    "mouse",
    "headset",
    "controller",
]
RetailerSlug = Literal[
    "elitehubs",
    "vedant",
    "mdcomputers",
    "computech",
    "amazon",
    "flipkart",
    "pcstudio",
    "primeabgb",
    "sclgaming",
    "tpstech",
    "vishal",
]

CORE_CATEGORIES: tuple[CategorySlug, ...] = (
    "cpu",
    "gpu",
    "motherboard",
    "ram",
    "psu",
    "case",
)
EXPANDED_CATEGORIES: tuple[CategorySlug, ...] = (
    "ssd",
    "hdd",
    "cooler",
    "monitor",
    "fan",
)
PERIPHERAL_CATEGORIES: tuple[CategorySlug, ...] = (
    "keyboard",
    "mouse",
    "headset",
    "controller",
)
ALL_CATEGORIES: tuple[CategorySlug, ...] = CORE_CATEGORIES + EXPANDED_CATEGORIES + PERIPHERAL_CATEGORIES
# The website's two menus: "PC Parts" and "Peripherals" (web/src/lib/format.ts). Used to split
# Amazon syncs into separate runs (`pcforge-scrape run --retailer amazon --group peripherals`).
CATEGORY_GROUPS: dict[str, tuple[CategorySlug, ...]] = {
    "parts": CORE_CATEGORIES + ("ssd", "hdd", "cooler"),
    "peripherals": ("monitor", "fan") + PERIPHERAL_CATEGORIES,
}
# Amazon prices are shown only while they are under 24 hours old (Amazon's rule); older Amazon
# listings become plain links with no price (see LINK_ONLY_LISTING_SQL in db.py and
# web/src/lib/linkOnly.ts). Amazon syncs refresh known listings at least twice a day.
AMAZON_PRICE_MAX_AGE_HOURS = 24
ALL_RETAILERS: tuple[RetailerSlug, ...] = (
    "elitehubs",
    "vedant",
    "mdcomputers",
    "computech",
    "amazon",
    "flipkart",
    "pcstudio",
    "primeabgb",
    "sclgaming",
    "tpstech",
    "vishal",
)

SCRAPER_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SCRAPER_DIR.parent


def _default_quotas() -> dict[CategorySlug, int]:
    return {
        "cpu": 10,
        "gpu": 20,
        "case": 10,
        "ram": 10,
        "psu": 10,
        "motherboard": 20,
    }


def _default_expanded_quotas() -> dict[CategorySlug, int]:
    return {
        "ssd": 10,
        "hdd": 8,
        "cooler": 10,
        "monitor": 10,
        "fan": 8,
    }


class TargetsConfig(BaseModel):
    """Per-category listing quotas and popular-model seed keywords loaded from targets.yaml."""

    quotas: dict[CategorySlug, int] = Field(default_factory=_default_quotas)
    expanded_quotas: dict[CategorySlug, int] = Field(default_factory=_default_expanded_quotas)
    seeds: dict[CategorySlug, list[str]] = Field(default_factory=dict)
    # Amazon only: bigger quotas and extra search terms (brand + category) for the Creators API
    # sync. Store scrapers keep using `quotas` / `seeds` above.
    amazon_quotas: dict[CategorySlug, int] = Field(default_factory=dict)
    amazon_seeds: dict[CategorySlug, list[str]] = Field(default_factory=dict)

    @field_validator("quotas", "expanded_quotas")
    @classmethod
    def _validate_quotas(cls, v: dict[CategorySlug, int]) -> dict[CategorySlug, int]:
        for cat, count in v.items():
            if count <= 0:
                raise ValueError(f"Quota for {cat} must be positive, got {count}")
        return v

    def get_quota(self, category: CategorySlug) -> int:
        """Return target quota for a category."""
        if category in self.quotas:
            return self.quotas[category]
        return self.expanded_quotas.get(category, 10)

    def get_seeds(self, category: CategorySlug) -> list[str]:
        """Return normalized lowercase seed phrases for a category."""
        return [s.strip().lower() for s in self.seeds.get(category, []) if s.strip()]

    def get_amazon_quota(self, category: CategorySlug) -> int:
        """Amazon listing quota: the Amazon-specific value, else the shared one."""
        return self.amazon_quotas.get(category) or self.get_quota(category)

    def get_amazon_seeds(self, category: CategorySlug) -> list[str]:
        """Shared model seeds first (most specific), then Amazon-only brand terms, without duplicates."""
        out: list[str] = []
        for s in self.get_seeds(category) + [x.strip().lower() for x in self.amazon_seeds.get(category, []) if x.strip()]:
            if s not in out:
                out.append(s)
        return out


def _default_affiliate_preferences() -> (
    dict[RetailerSlug, Literal["cuelinks_then_direct", "direct_then_cuelinks"]]
):
    return {
        "elitehubs": "cuelinks_then_direct",
        "vedant": "direct_then_cuelinks",
        "mdcomputers": "cuelinks_then_direct",
        "computech": "direct_then_cuelinks",
        "amazon": "cuelinks_then_direct",
        "flipkart": "cuelinks_then_direct",
        "pcstudio": "cuelinks_then_direct",
        "primeabgb": "cuelinks_then_direct",
        "sclgaming": "cuelinks_then_direct",
        "tpstech": "cuelinks_then_direct",
        "vishal": "cuelinks_then_direct",
    }


class Settings(BaseSettings):
    """Environment-backed runtime settings for scraper."""

    model_config = SettingsConfigDict(
        env_file=(str(REPO_ROOT / ".env"), str(SCRAPER_DIR / ".env"), ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: SecretStr | None = None
    direct_url: SecretStr | None = None
    next_public_site_url: str = "http://localhost:3000"
    revalidate_secret: SecretStr | None = None

    scrape_min_delay: float = 2.0
    scrape_max_delay: float = 5.0
    scrape_max_consecutive_failures: int = 5
    scrape_timeout_seconds: float = 25.0
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )

    # Cuelinks Monetization API
    cuelinks_api_key: SecretStr | None = None
    cuelinks_channel_id: int | None = None
    cuelinks_base_url: str = "https://developers.cuelinks.com/pub_api/v3"

    # Official APIs (Amazon Creators API & Flipkart Affiliate API)
    amazon_application_id: str | None = None
    amazon_creators_client_id: SecretStr | None = None
    amazon_creators_client_secret: SecretStr | None = None
    amazon_creators_credential_version: str = Field(
        default="3.2",
        validation_alias=AliasChoices(
            "AMAZON_CREATORS_CREDENTIAL_VERSION",
            "AMAZON_CREDENTIAL_VERSION",
        ),
    )
    amazon_partner_tag: str | None = None
    amazon_marketplace: str = "www.amazon.in"
    # Amazon sync tuning (set per workflow run; see .github/workflows/amazon-*.yml):
    #   AMAZON_SEARCH=false        refresh-only run (no new-product search)
    #   AMAZON_SEARCH_PAGES=3      result pages read per search term (10 results each, max 10)
    #   AMAZON_REFRESH_KNOWN=true  re-check every known Amazon listing by ASIN (10 per request)
    #   AMAZON_REFRESH_LIMIT=1000  max known listings refreshed per category per run
    amazon_search: bool = True
    amazon_search_pages: int = Field(default=1, ge=1, le=10)
    amazon_refresh_known: bool = True
    amazon_refresh_limit: int = Field(default=1000, ge=0, le=20000)

    flipkart_affiliate_id: str | None = None
    flipkart_affiliate_token: SecretStr | None = None

    # Direct retailer affiliate programs
    vedant_computers_tracking_id: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "VEDANT_COMPUTERS_TRACKING_ID",
            "VEDANT COMPUTERS TRACKING_ID",
        ),
    )
    computech_referral_code: SecretStr | None = Field(
        default=SecretStr(os.environ.get("COMPUTECH_REFERRAL_CODE", "")),
        validation_alias=AliasChoices(
            "COMPUTECH_REFERRAL_CODE",
            "COMPUTECH_REF",
        ),
    )

    # Per-retailer affiliate provider preference ("cuelinks_then_direct" or "direct_then_cuelinks")
    retailer_affiliate_preference: dict[
        RetailerSlug,
        Literal["cuelinks_then_direct", "direct_then_cuelinks"],
    ] = Field(default_factory=_default_affiliate_preferences)

    state_dir: Path = Field(default=SCRAPER_DIR / ".state")
    targets_path: Path = Field(default=SCRAPER_DIR / "targets.yaml")

    def ensure_state_dir(self) -> Path:
        """Ensure scraper/.state directory exists and return its path."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        return self.state_dir


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings singleton."""
    return Settings()


@lru_cache(maxsize=4)
def load_targets(path: Path | None = None) -> TargetsConfig:
    """Load and validate targets.yaml."""
    target_file = path or get_settings().targets_path
    if not target_file.exists():
        return TargetsConfig()
    raw = yaml.safe_load(target_file.read_text(encoding="utf-8")) or {}
    return TargetsConfig.model_validate(raw)

