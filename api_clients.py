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

def _format_address(addr):
    """Format a Trestle address dict into a readable string."""
    if not isinstance(addr, dict):
        return str(addr) if addr else None
    parts = [
        addr.get("street_line_1", ""),
        addr.get("street_line_2", ""),
        addr.get("city", ""),
        addr.get("state_code", ""),
        addr.get("postal_code", ""),
    ]
    return ", ".join(p for p in parts if p) or None


def _parse_emails(emails):
    """Extract email strings from Trestle emails field (string or list)."""
    if isinstance(emails, str) and emails:
        return [emails]
    if isinstance(emails, list):
        result = []
        for e in emails:
            if isinstance(e, dict):
                addr = e.get("email_address") or e.get("address")
                if addr:
                    result.append(addr)
            elif isinstance(e, str) and e:
                result.append(e)
        return result
    return []


def trestle_reverse_phone(phone):
    """Lookup a phone number via Trestle Reverse Phone API.

    Returns a rich dict with all available caller intelligence.
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

    # Parse top-level phone fields
    line_type_raw = (data.get("line_type") or "").lower()

    result = {
        # Phone-level
        "is_valid": data.get("is_valid"),
        "line_type": line_type_raw,
        "carrier": data.get("carrier"),
        "is_prepaid": data.get("is_prepaid"),
        "is_commercial": data.get("is_commercial"),
        "sms_eligible": line_type_raw == "mobile",

        # Primary owner (populated below)
        "owner_name": None,
        "firstname": None,
        "lastname": None,
        "middlename": None,
        "alternate_names": [],
        "age_range": None,
        "gender": None,
        "owner_type": None,
        "confidence_score": None,
        "link_to_phone_start_date": None,

        # Contact info
        "candidate_email": None,
        "all_emails": [],
        "candidate_address": None,
        "all_addresses": [],
        "alternate_phones": [],

        # Lat/long from Trestle address (before Google geocode)
        "trestle_lat": None,
        "trestle_lng": None,
        "trestle_accuracy": None,

        # Additional owners (for identity disambiguation)
        "owner_count": 0,
        "all_owners_summary": [],

        "raw_response": data,
    }

    owners = data.get("owners", [])
    result["owner_count"] = len(owners)

    if not owners:
        return result

    # ── Primary owner (highest confidence) ───────────────────────
    owner = owners[0]
    result["owner_name"] = owner.get("name")
    result["firstname"] = owner.get("firstname")
    result["lastname"] = owner.get("lastname")
    result["middlename"] = owner.get("middlename")
    result["alternate_names"] = owner.get("alternate_names", [])
    result["age_range"] = owner.get("age_range")
    result["gender"] = owner.get("gender")
    result["owner_type"] = owner.get("type")  # Person or Business
    result["confidence_score"] = owner.get("phone_to_name_confidence_score")
    result["link_to_phone_start_date"] = owner.get("link_to_phone_start_date")

    # Emails — all of them, first one is candidate
    all_emails = _parse_emails(owner.get("emails", []))
    result["all_emails"] = all_emails
    result["candidate_email"] = all_emails[0] if all_emails else None

    # Addresses — all of them, first one is candidate
    addresses = owner.get("current_addresses", [])
    all_addrs = []
    for addr in addresses:
        formatted = _format_address(addr)
        if formatted:
            entry = {"formatted": formatted}
            lat_long = addr.get("lat_long", {}) if isinstance(addr, dict) else {}
            if lat_long:
                entry["lat"] = lat_long.get("latitude")
                entry["lng"] = lat_long.get("longitude")
                entry["accuracy"] = lat_long.get("accuracy")
            entry["delivery_point"] = addr.get("delivery_point") if isinstance(addr, dict) else None
            entry["link_date"] = addr.get("link_to_person_start_date") if isinstance(addr, dict) else None
            all_addrs.append(entry)
    result["all_addresses"] = all_addrs
    if all_addrs:
        result["candidate_address"] = all_addrs[0]["formatted"]
        result["trestle_lat"] = all_addrs[0].get("lat")
        result["trestle_lng"] = all_addrs[0].get("lng")
        result["trestle_accuracy"] = all_addrs[0].get("accuracy")

    # Alternate phones
    alt_phones = owner.get("alternate_phones", [])
    result["alternate_phones"] = [
        {"number": p.get("phoneNumber") or p.get("phone_number"),
         "type": (p.get("lineType") or p.get("line_type") or "").lower()}
        for p in alt_phones if isinstance(p, dict)
    ]

    # ── All owners summary (for multi-owner numbers) ─────────────
    for o in owners:
        result["all_owners_summary"].append({
            "name": o.get("name"),
            "confidence": o.get("phone_to_name_confidence_score"),
            "type": o.get("type"),
            "age_range": o.get("age_range"),
            "email_count": len(_parse_emails(o.get("emails", []))),
            "address_count": len(o.get("current_addresses", [])),
        })

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
