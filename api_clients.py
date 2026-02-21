"""API client wrappers for external services.

Phase 1: Trestle Reverse Phone, ZeroBounce
Phase 2: Google Maps Geocoding, Smarty US Street Address
Phase 4: Postmark transactional email
"""

import logging
import requests

import config

logger = logging.getLogger(__name__)


# ── Trestle Reverse Phone API ───────────────────────────────────────

def trestle_reverse_phone(phone):
    """Lookup a phone number via Trestle Reverse Phone API.

    Returns dict with keys: owner_name, line_type, sms_eligible,
    candidate_email, candidate_address, raw_response.
    Returns None on failure.
    """
    if not config.TRESTLE_API_KEY:
        logger.warning("TRESTLE_API_KEY not configured — skipping enrichment")
        return None

    # Strip leading + from E.164 format — Trestle expects digits only
    clean_phone = phone.lstrip("+")

    url = f"{config.TRESTLE_BASE_URL}/phone"
    params = {"phone": clean_phone}
    headers = {"x-api-key": config.TRESTLE_API_KEY}

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"Trestle API error for {phone}: {e}")
        return None

    # Parse response
    result = {
        "owner_name": None,
        "line_type": None,
        "sms_eligible": False,
        "candidate_email": None,
        "candidate_address": None,
        "raw_response": data,
    }

    # Line type — top-level string: Mobile, Landline, FixedVOIP, NonFixedVOIP, etc.
    line_type_raw = data.get("line_type", "")
    if line_type_raw:
        result["line_type"] = line_type_raw.lower()

    # SMS eligibility — only confirmed mobile
    result["sms_eligible"] = result["line_type"] == "mobile"

    # Owners array — name, emails, addresses live here
    owners = data.get("owners", [])
    if owners:
        owner = owners[0]
        result["owner_name"] = owner.get("name")

        # Candidate email — owner.emails (string or array)
        emails = owner.get("emails", [])
        if isinstance(emails, str) and emails:
            result["candidate_email"] = emails
        elif isinstance(emails, list) and emails:
            first_email = emails[0]
            if isinstance(first_email, dict):
                result["candidate_email"] = first_email.get("email_address") or first_email.get("address")
            elif isinstance(first_email, str):
                result["candidate_email"] = first_email

        # Candidate address — owner.current_addresses[]
        addresses = owner.get("current_addresses", [])
        if addresses:
            addr = addresses[0]
            if isinstance(addr, dict):
                parts = [
                    addr.get("street_line_1", ""),
                    addr.get("street_line_2", ""),
                    addr.get("city", ""),
                    addr.get("state_code", ""),
                    addr.get("postal_code", ""),
                ]
                result["candidate_address"] = ", ".join(p for p in parts if p)
            elif isinstance(addr, str):
                result["candidate_address"] = addr

    return result


# ── ZeroBounce Email Validation ──────────────────────────────────────

def zerobounce_validate(email):
    """Validate an email address via ZeroBounce API.

    Returns dict with keys: status, sub_status, is_valid, raw_response.
    Returns None on failure.
    """
    if not config.ZEROBOUNCE_API_KEY:
        logger.warning("ZEROBOUNCE_API_KEY not configured — skipping validation")
        return None

    url = f"{config.ZEROBOUNCE_BASE_URL}/validate"
    params = {
        "api_key": config.ZEROBOUNCE_API_KEY,
        "email": email,
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"ZeroBounce API error for {email}: {e}")
        return None

    status = (data.get("status") or "").lower()
    sub_status = (data.get("sub_status") or "").lower()

    # valid = proceed, invalid/disposable/role = reject, unknown/catch-all = flag
    is_valid = status == "valid"
    is_invalid = status == "invalid" or sub_status in (
        "disposable", "role_based", "toxic", "spam_trap"
    )

    return {
        "status": status,
        "sub_status": sub_status,
        "is_valid": is_valid,
        "is_invalid": is_invalid,
        "raw_response": data,
    }


# ── Google Maps Geocoding (Phase 2) ─────────────────────────────────

def geocode_address(address):
    """Geocode an address via Google Maps API. Returns dict or None."""
    if not config.GOOGLE_MAPS_API_KEY:
        logger.warning("GOOGLE_MAPS_API_KEY not configured — skipping geocode")
        return None

    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {
        "address": address,
        "key": config.GOOGLE_MAPS_API_KEY,
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"Google Maps geocode error: {e}")
        return None

    results = data.get("results", [])
    if not results:
        return None

    top = results[0]
    location = top.get("geometry", {}).get("location", {})
    location_type = top.get("geometry", {}).get("location_type", "")

    return {
        "formatted_address": top.get("formatted_address"),
        "lat": location.get("lat"),
        "lng": location.get("lng"),
        "confidence": location_type,  # ROOFTOP, RANGE_INTERPOLATED, etc.
        "raw_response": top,
    }


# ── Smarty US Street Address (Phase 2) ──────────────────────────────

def smarty_validate_address(street, city, state, zipcode=None):
    """Validate a US street address via Smarty API. Returns dict or None."""
    if not config.SMARTY_AUTH_ID or not config.SMARTY_AUTH_TOKEN:
        logger.warning("Smarty credentials not configured — skipping address validation")
        return None

    url = "https://us-street.api.smarty.com/street-address"
    params = {
        "auth-id": config.SMARTY_AUTH_ID,
        "auth-token": config.SMARTY_AUTH_TOKEN,
        "street": street,
        "city": city,
        "state": state,
        "candidates": 1,
    }
    if zipcode:
        params["zipcode"] = zipcode

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"Smarty API error: {e}")
        return None

    if not data:
        return {"dpv_match_code": "N", "normalized": None, "raw_response": []}

    top = data[0]
    analysis = top.get("analysis", {})
    components = top.get("components", {})

    normalized = top.get("delivery_line_1", "")
    last_line = top.get("last_line", "")
    if last_line:
        normalized = f"{normalized}, {last_line}"

    return {
        "dpv_match_code": analysis.get("dpv_match_code", "N"),
        "normalized": normalized,
        "components": components,
        "raw_response": top,
    }


# ── Postmark Transactional Email ─────────────────────────────────

def postmark_send(to_email, subject, html_body, text_body=None):
    """Send a transactional email via Postmark API.

    Returns dict with keys: message_id, success, error.
    Returns None on missing config.
    """
    if not config.POSTMARK_SERVER_TOKEN or not config.POSTMARK_FROM_EMAIL:
        logger.warning("Postmark not configured — skipping email send")
        return None

    url = "https://api.postmarkapp.com/email"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Postmark-Server-Token": config.POSTMARK_SERVER_TOKEN,
    }
    payload = {
        "From": config.POSTMARK_FROM_EMAIL,
        "To": to_email,
        "Subject": subject,
        "HtmlBody": html_body,
    }
    if text_body:
        payload["TextBody"] = text_body

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"Postmark send error to {to_email}: {e}")
        return {"message_id": None, "success": False, "error": str(e)}

    message_id = data.get("MessageID")
    error_code = data.get("ErrorCode", 0)

    if error_code == 0:
        logger.info(f"Postmark sent to {to_email}, MessageID={message_id}")
        return {"message_id": message_id, "success": True, "error": None}
    else:
        error_msg = data.get("Message", "Unknown error")
        logger.error(f"Postmark error {error_code}: {error_msg}")
        return {"message_id": None, "success": False, "error": error_msg}
