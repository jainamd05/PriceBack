"""Runtime configuration, read from environment variables (and .env for local dev)."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # PayPal (sandbox only unless PAYPAL_ALLOW_LIVE=true)
    paypal_client_id: str
    paypal_client_secret: str
    paypal_base_url: str
    paypal_webhook_id: str
    paypal_allow_live: bool
    allow_unverified_webhooks: bool
    public_base_url: str
    # Channel3 product data
    channel3_api_key: str
    channel3_base_url: str
    catalog_query: str
    catalog_size: int
    # LLM (Google Gemini REST API; optional, rules-based fallback if unset)
    gemini_api_key: str
    llm_model: str
    # App
    admin_token: str
    db_path: str
    watch_interval_seconds: int
    default_daily_budget: float
    # Shopper accounts + notifications
    brevo_api_key: str             # transactional email over HTTPS (Render's free plan blocks SMTP)
    mail_from: str                 # the From address (verified in Brevo, or your SMTP login)
    smtp_host: str                 # optional: send with smtplib instead (e.g. smtp.gmail.com); blocked on Render free
    smtp_port: int
    smtp_user: str
    smtp_password: str
    mail_from_name: str
    zapier_hook_url: str           # optional: every shopper event is also POSTed to this Zapier Catch Hook
    demo_buyer_email: str          # PayPal *sandbox* buyer login shown to testers (fake money only)
    demo_buyer_password: str
    demo_self_serve: bool          # shoppers may simulate a price drop on their own order


def get_settings() -> Settings:
    """Read env on every call so tests can monkeypatch it."""
    return Settings(
        paypal_client_id=os.getenv("PAYPAL_CLIENT_ID", ""),
        paypal_client_secret=os.getenv("PAYPAL_CLIENT_SECRET", ""),
        paypal_base_url=os.getenv("PAYPAL_BASE_URL", "https://api-m.sandbox.paypal.com").rstrip("/"),
        paypal_webhook_id=os.getenv("PAYPAL_WEBHOOK_ID", ""),
        paypal_allow_live=_bool("PAYPAL_ALLOW_LIVE"),
        allow_unverified_webhooks=_bool("ALLOW_UNVERIFIED_WEBHOOKS"),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
        channel3_api_key=os.getenv("CHANNEL3_API_KEY", ""),
        channel3_base_url=os.getenv("CHANNEL3_BASE_URL", "https://api.trychannel3.com").rstrip("/"),
        catalog_query=os.getenv("CATALOG_QUERY", "wireless headphones"),
        catalog_size=int(os.getenv("CATALOG_SIZE", "8")),
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        llm_model=os.getenv("LLM_MODEL", "gemini-3.1-flash-lite"),
        admin_token=os.getenv("ADMIN_TOKEN", ""),
        db_path=os.getenv("PRICEBACK_DB", "priceback.db"),
        watch_interval_seconds=int(os.getenv("WATCH_INTERVAL_SECONDS", "0")),
        default_daily_budget=float(os.getenv("DEFAULT_DAILY_BUDGET", "100")),
        brevo_api_key=os.getenv("BREVO_API_KEY", "").strip(),
        mail_from=(os.getenv("MAIL_FROM", "").strip() or os.getenv("SMTP_USER", "").strip()),
        smtp_host=os.getenv("SMTP_HOST", "").strip(),
        smtp_port=int(os.getenv("SMTP_PORT", "587") or 587),
        smtp_user=os.getenv("SMTP_USER", "").strip(),
        smtp_password=os.getenv("SMTP_PASSWORD", "").replace(" ", ""),
        mail_from_name=os.getenv("MAIL_FROM_NAME", "PriceBack").strip() or "PriceBack",
        zapier_hook_url=os.getenv("ZAPIER_HOOK_URL", "").strip(),
        demo_buyer_email=os.getenv("DEMO_BUYER_EMAIL", "").strip(),
        demo_buyer_password=os.getenv("DEMO_BUYER_PASSWORD", "").strip(),
        demo_self_serve=_bool("DEMO_SELF_SERVE", True),
    )
