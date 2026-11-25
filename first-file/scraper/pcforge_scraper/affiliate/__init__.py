"""Affiliate link conversion and direct retailer affiliate URL builders."""

from pcforge_scraper.affiliate.cuelinks import (
    AffiliateProviderType,
    AffiliateResolution,
    CuelinksCampaignStatus,
    CuelinksClient,
    CuelinksConversionResult,
    CuelinksStartupReport,
    build_amazon_affiliate_url,
    build_computech_affiliate_url,
    build_direct_retailer_affiliate_url,
    build_flipkart_affiliate_url,
    build_vedant_affiliate_url,
    is_affiliate_stale,
)

__all__ = [
    "AffiliateProviderType",
    "AffiliateResolution",
    "CuelinksCampaignStatus",
    "CuelinksClient",
    "CuelinksConversionResult",
    "CuelinksStartupReport",
    "build_amazon_affiliate_url",
    "build_computech_affiliate_url",
    "build_direct_retailer_affiliate_url",
    "build_flipkart_affiliate_url",
    "build_vedant_affiliate_url",
    "is_affiliate_stale",
]
