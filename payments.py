#!/usr/bin/env python3
# ================================================================
#  payments.py — Autonomous payment collection
#  Pakistan-friendly: LemonSqueezy + Crypto + Gumroad
#  No Stripe needed. Works with Payoneer.
# ================================================================

import os
import json
import logging
import requests
from datetime import datetime

import shared_memory as sm

log = logging.getLogger(__name__)

LEMONSQUEEZY_KEY  = os.environ.get("LEMONSQUEEZY_KEY", "")
LEMONSQUEEZY_STORE= os.environ.get("LEMONSQUEEZY_STORE_ID", "")
GUMROAD_TOKEN     = os.environ.get("GUMROAD_TOKEN", "")
CRYPTO_WALLET     = os.environ.get("CRYPTO_WALLET_USDT", "")  # Your USDT wallet
NOWPAYMENTS_KEY   = os.environ.get("NOWPAYMENTS_KEY", "")     # crypto payment gateway


# ================================================================
#  LEMONSQUEEZY — Full API, works in Pakistan, pays to Payoneer
# ================================================================

def create_lemonsqueezy_product(name, description, price_usd,
                                 billing="month"):
    """
    Create a LemonSqueezy product + checkout link.
    Fully autonomous. Pays to your Payoneer account.
    """
    if not LEMONSQUEEZY_KEY or not LEMONSQUEEZY_STORE:
        log.warning("No LemonSqueezy credentials — saving draft")
        return _save_payment_draft("LemonSqueezy", name, price_usd)

    headers = {
        "Authorization": f"Bearer {LEMONSQUEEZY_KEY}",
        "Content-Type":  "application/vnd.api+json",
        "Accept":        "application/vnd.api+json"
    }

    try:
        # Create product
        payload = {
            "data": {
                "type": "products",
                "attributes": {
                    "name":        name,
                    "description": description[:500],
                },
                "relationships": {
                    "store": {
                        "data": {
                            "type": "stores",
                            "id":   str(LEMONSQUEEZY_STORE)
                        }
                    }
                }
            }
        }
        resp = requests.post(
            "https://api.lemonsqueezy.com/v1/products",
            headers=headers,
            json=payload,
            timeout=10
        )
        if resp.status_code not in (200, 201):
            log.error(f"LemonSqueezy product failed: {resp.text[:100]}")
            return _save_payment_draft("LemonSqueezy", name, price_usd)

        product_id = resp.json()["data"]["id"]
        log.info(f"✅ LemonSqueezy product: {product_id}")

        # Create variant (price)
        variant_payload = {
            "data": {
                "type": "variants",
                "attributes": {
                    "name":              "Standard",
                    "price":             int(price_usd * 100),
                    "is_subscription":   billing in ("month", "year"),
                    "interval":          billing if billing in ("month","year") else None,
                    "interval_count":    1,
                },
                "relationships": {
                    "product": {
                        "data": {"type": "products", "id": str(product_id)}
                    }
                }
            }
        }
        resp2 = requests.post(
            "https://api.lemonsqueezy.com/v1/variants",
            headers=headers,
            json=variant_payload,
            timeout=10
        )
        variant_id = resp2.json()["data"]["id"] if resp2.status_code in (200,201) else None

        # Get checkout URL
        checkout_url = (
            f"https://{LEMONSQUEEZY_STORE}.lemonsqueezy.com/checkout/buy/"
            f"{variant_id}"
        ) if variant_id else f"https://app.lemonsqueezy.com/products/{product_id}"

        log.info(f"✅ LemonSqueezy checkout: {checkout_url}")
        sm.log_revenue(
            source      = "LemonSqueezy (created)",
            amount      = 0,
            description = f"{name} | ${price_usd}/{billing} | {checkout_url}",
            agent_name  = "PAYMENT_SYSTEM"
        )
        return {
            "platform":     "LemonSqueezy",
            "product_id":   product_id,
            "checkout_url": checkout_url,
            "price":        f"${price_usd}/{billing}"
        }

    except Exception as e:
        log.error(f"create_lemonsqueezy_product error: {e}")
        return _save_payment_draft("LemonSqueezy", name, price_usd)


def get_lemonsqueezy_revenue():
    """Get total revenue from LemonSqueezy."""
    if not LEMONSQUEEZY_KEY:
        return 0.0
    try:
        resp = requests.get(
            "https://api.lemonsqueezy.com/v1/orders",
            headers={"Authorization": f"Bearer {LEMONSQUEEZY_KEY}"},
            params={"filter[status]": "paid"},
            timeout=10
        )
        if resp.status_code == 200:
            orders = resp.json().get("data", [])
            return sum(
                float(o["attributes"].get("total", 0)) / 100
                for o in orders
            )
    except Exception as e:
        log.error(f"get_lemonsqueezy_revenue error: {e}")
    return 0.0


# ================================================================
#  GUMROAD — Simple, works with PayPal in Pakistan
# ================================================================

def create_gumroad_product(name, description, price_usd):
    """Create a Gumroad listing. Pays via PayPal."""
    if not GUMROAD_TOKEN:
        log.warning("No Gumroad token")
        return _save_payment_draft("Gumroad", name, price_usd)

    try:
        resp = requests.post(
            "https://api.gumroad.com/v2/products",
            data={
                "access_token":     GUMROAD_TOKEN,
                "name":             name,
                "description":      description[:1000],
                "price":            int(price_usd * 100),
                "published":        "true",
                "require_shipping": "false",
            },
            timeout=10
        )
        if resp.status_code in (200, 201):
            product = resp.json().get("product", {})
            url     = product.get("short_url", "")
            log.info(f"✅ Gumroad product: {url}")
            sm.log_revenue(
                source      = "Gumroad (created)",
                amount      = 0,
                description = f"{name} | ${price_usd} | {url}",
                agent_name  = "PAYMENT_SYSTEM"
            )
            return {"platform": "Gumroad", "url": url, "price": f"${price_usd}"}
        else:
            log.error(f"Gumroad failed: {resp.text[:100]}")
    except Exception as e:
        log.error(f"create_gumroad_product error: {e}")
    return _save_payment_draft("Gumroad", name, price_usd)


def get_gumroad_revenue():
    """Get total Gumroad revenue."""
    if not GUMROAD_TOKEN:
        return 0.0
    try:
        resp = requests.get(
            "https://api.gumroad.com/v2/sales",
            params={"access_token": GUMROAD_TOKEN},
            timeout=10
        )
        if resp.status_code == 200:
            sales = resp.json().get("sales", [])
            return sum(float(s.get("price", 0)) / 100 for s in sales)
    except Exception as e:
        log.error(f"get_gumroad_revenue error: {e}")
    return 0.0


# ================================================================
#  CRYPTO — Accept USDT directly, zero fees, instant
# ================================================================

def create_crypto_payment_link(tool_name, price_usd):
    """
    Create a crypto payment link using NOWPayments.
    Accepts USDT, BTC, ETH — sends to your wallet.
    """
    if not NOWPAYMENTS_KEY:
        # Fallback: generate a simple static payment page
        if CRYPTO_WALLET:
            return {
                "platform":    "Crypto (manual)",
                "wallet":      CRYPTO_WALLET,
                "amount":      price_usd,
                "instruction": f"Send ${price_usd} USDT to {CRYPTO_WALLET}"
            }
        return None

    try:
        resp = requests.post(
            "https://api.nowpayments.io/v1/invoice",
            headers={
                "x-api-key":    NOWPAYMENTS_KEY,
                "Content-Type": "application/json"
            },
            json={
                "price_amount":    price_usd,
                "price_currency":  "usd",
                "pay_currency":    "usdttrc20",  # USDT on TRC20 (cheapest fees)
                "order_id":        f"{tool_name}-{int(datetime.now().timestamp())}",
                "order_description": tool_name,
                "ipn_callback_url": "",
            },
            timeout=10
        )
        if resp.status_code in (200, 201):
            data         = resp.json()
            payment_url  = data.get("invoice_url", "")
            log.info(f"✅ Crypto payment link: {payment_url}")
            return {
                "platform":    "NOWPayments (USDT)",
                "payment_url": payment_url,
                "amount":      f"${price_usd} USDT"
            }
    except Exception as e:
        log.error(f"create_crypto_payment_link error: {e}")

    # Fallback to raw wallet
    if CRYPTO_WALLET:
        return {
            "platform": "USDT (direct)",
            "wallet":   CRYPTO_WALLET,
            "amount":   price_usd
        }
    return None


# ================================================================
#  DRAFT SAVER — When no API keys available
# ================================================================

def _save_payment_draft(platform, name, price_usd):
    """Save listing draft to Google Sheets for manual review."""
    draft = {
        "platform":   platform,
        "name":       name,
        "price":      price_usd,
        "status":     "DRAFT",
        "created_at": datetime.now().isoformat()
    }
    sm.log_revenue(
        source      = f"{platform} (DRAFT)",
        amount      = 0,
        description = f"DRAFT: {name} | ${price_usd} — add API key to automate",
        agent_name  = "PAYMENT_SYSTEM"
    )
    return draft


# ================================================================
#  UNIFIED — Called by Backend Builder after every deployment
# ================================================================

def monetize_tool(tool_name, description, repo_url,
                  landing_url="", price_usd=29.0):
    """
    Full autonomous monetization.
    Creates LemonSqueezy + Gumroad + Crypto payment channels.
    Returns all payment URLs for sellers to use.
    """
    log.info(f"\n💳 Monetizing: {tool_name} at ${price_usd}/month")
    results = {"tool": tool_name, "price": price_usd, "channels": []}

    # 1. LemonSqueezy (primary — subscription)
    ls = create_lemonsqueezy_product(
        name        = tool_name,
        description = description,
        price_usd   = price_usd,
        billing     = "month"
    )
    if ls:
        results["lemonsqueezy"] = ls.get("checkout_url", "")
        results["channels"].append(f"LemonSqueezy: {ls.get('checkout_url','')}")

    # 2. Gumroad (lifetime deal — 10x monthly price)
    gm = create_gumroad_product(
        name        = f"{tool_name} — Lifetime Deal",
        description = f"{description}\n\nRepo: {repo_url}",
        price_usd   = price_usd * 10
    )
    if gm:
        results["gumroad"] = gm.get("url", "")
        results["channels"].append(f"Gumroad: {gm.get('url','')}")

    # 3. Crypto (USDT — for tech buyers)
    crypto = create_crypto_payment_link(tool_name, price_usd)
    if crypto:
        results["crypto"] = crypto.get("payment_url") or crypto.get("wallet","")
        results["channels"].append(f"Crypto: {results['crypto']}")

    log.info(f"✅ {len(results['channels'])} payment channels created:")
    for ch in results["channels"]:
        log.info(f"   {ch}")

    return results


def get_total_autonomous_revenue():
    """CEO calls this every cycle to track real revenue."""
    ls      = get_lemonsqueezy_revenue()
    gumroad = get_gumroad_revenue()
    total   = ls + gumroad

    log.info(f"💰 Revenue — LemonSqueezy: ${ls:.2f} | Gumroad: ${gumroad:.2f} | Total: ${total:.2f}")

    sm.log_revenue(
        source      = "Revenue check",
        amount      = total,
        description = f"LemonSqueezy: ${ls:.2f} + Gumroad: ${gumroad:.2f}",
        agent_name  = "PAYMENT_SYSTEM"
    )
    return total
