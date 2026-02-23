#!/usr/bin/env python3
"""Veronica Mars — Voice AI Agent for email & address collection.

State machine architecture: all routing lives in code. The LLM only
handles personality and natural language. Tools force every transition
via swml_change_step(). The AI never chooses where to go.
"""

import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from signalwire_agents import AgentBase
from signalwire_agents.core.function_result import SwaigFunctionResult

import config
from api_clients import (
    trestle_reverse_phone, zerobounce_validate, postmark_send,
    geocode_address, smarty_validate_address,
)
from state_store import (
    get_caller_by_phone, upsert_caller, caller_is_stale,
    load_call_state, save_call_state, delete_call_state, cleanup_stale_states,
    log_consent,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, force=True)
logger = logging.getLogger(__name__)

config.validate()

# NATO phonetic alphabet for email readback
NATO = {
    "A": "Alpha", "B": "Bravo", "C": "Charlie", "D": "Delta",
    "E": "Echo", "F": "Foxtrot", "G": "Golf", "H": "Hotel",
    "I": "India", "J": "Juliet", "K": "Kilo", "L": "Lima",
    "M": "Mike", "N": "November", "O": "Oscar", "P": "Papa",
    "Q": "Quebec", "R": "Romeo", "S": "Sierra", "T": "Tango",
    "U": "Uniform", "V": "Victor", "W": "Whiskey", "X": "X-ray",
    "Y": "Yankee", "Z": "Zulu",
    "0": "Zero", "1": "One", "2": "Two", "3": "Three", "4": "Four",
    "5": "Five", "6": "Six", "7": "Seven", "8": "Eight", "9": "Nine",
}


def nato_spell_email(email):
    """Convert email to NATO phonetic spelling for voice readback."""
    parts = []
    for char in email.lower():
        if char == "@":
            parts.append("at")
        elif char == ".":
            parts.append("dot")
        elif char == "-":
            parts.append("dash")
        elif char == "_":
            parts.append("underscore")
        elif char.upper() in NATO:
            parts.append(NATO[char.upper()])
        else:
            parts.append(char)
    return " ".join(parts)


def normalize_spoken_email(spoken):
    """Normalize ASR-captured email from voice spelling.

    Handles common speech-to-text patterns:
    - 'at sign' / 'at' → @
    - 'dot com' → .com
    - 'dash' / 'hyphen' → -
    - 'underscore' → _
    """
    text = spoken.lower().strip()
    # Remove filler words
    text = re.sub(r'\b(um|uh|like|so)\b', '', text)
    # Normalize @ sign
    text = re.sub(r'\bat\s*sign\b', '@', text)
    text = re.sub(r'\s+at\s+', '@', text)
    # Normalize dots
    text = re.sub(r'\bdot\s+', '.', text)
    text = re.sub(r'\bperiod\s+', '.', text)
    # Normalize special chars
    text = re.sub(r'\bdash\b', '-', text)
    text = re.sub(r'\bhyphen\b', '-', text)
    text = re.sub(r'\bunderscore\b', '_', text)
    # Collapse whitespace and remove remaining spaces (email has none)
    text = re.sub(r'\s+', '', text)
    return text


def _extract_trestle_extras(trestle):
    """Build a dict of rich Trestle data for global_data / LLM context.

    Only includes fields that are useful for the conversation —
    the LLM can reference these naturally (e.g. first name, age, gender).
    """
    if not trestle:
        return {}

    extras = {}

    # Name components — let Veronica use first name naturally
    if trestle.get("firstname"):
        extras["firstname"] = trestle["firstname"]
    if trestle.get("lastname"):
        extras["lastname"] = trestle["lastname"]
    if trestle.get("middlename"):
        extras["middlename"] = trestle["middlename"]
    if trestle.get("alternate_names"):
        extras["alternate_names"] = trestle["alternate_names"]

    # Demographics
    if trestle.get("age_range"):
        extras["age_range"] = trestle["age_range"]
    if trestle.get("gender"):
        extras["gender"] = trestle["gender"]
    if trestle.get("owner_type"):
        extras["owner_type"] = trestle["owner_type"]

    # Phone intel
    if trestle.get("carrier"):
        extras["carrier"] = trestle["carrier"]
    if trestle.get("is_prepaid") is not None:
        extras["is_prepaid"] = trestle["is_prepaid"]
    if trestle.get("is_commercial") is not None:
        extras["is_commercial"] = trestle["is_commercial"]
    if trestle.get("confidence_score") is not None:
        extras["confidence_score"] = trestle["confidence_score"]

    # All emails (LLM can reference if primary is rejected)
    if trestle.get("all_emails"):
        extras["all_emails"] = trestle["all_emails"]

    # All addresses with geocode data
    if trestle.get("all_addresses"):
        extras["all_addresses"] = trestle["all_addresses"]

    # Alternate phones
    if trestle.get("alternate_phones"):
        extras["alternate_phones"] = trestle["alternate_phones"]

    # Multi-owner info
    if trestle.get("owner_count", 0) > 1:
        extras["owner_count"] = trestle["owner_count"]
        extras["all_owners_summary"] = trestle.get("all_owners_summary", [])

    return extras


def _log_trestle(trestle):
    """Log rich Trestle data during pre-call enrichment."""
    if not trestle:
        return

    logger.info(f"  trestle: name={trestle.get('owner_name')} "
                f"({trestle.get('firstname')} {trestle.get('middlename', '')} {trestle.get('lastname')})")
    logger.info(f"  trestle: type={trestle.get('owner_type')} "
                f"confidence={trestle.get('confidence_score')} "
                f"age={trestle.get('age_range')} gender={trestle.get('gender')}")
    logger.info(f"  trestle: carrier={trestle.get('carrier')} "
                f"prepaid={trestle.get('is_prepaid')} commercial={trestle.get('is_commercial')}")
    logger.info(f"  trestle: emails={trestle.get('all_emails', [])}")
    logger.info(f"  trestle: addresses={len(trestle.get('all_addresses', []))} on file")
    for i, addr in enumerate(trestle.get("all_addresses", [])):
        logger.info(f"  trestle:   [{i}] {addr.get('formatted')} "
                    f"lat={addr.get('lat')} lng={addr.get('lng')}")
    logger.info(f"  trestle: alt_phones={trestle.get('alternate_phones', [])}")
    logger.info(f"  trestle: alt_names={trestle.get('alternate_names', [])}")
    logger.info(f"  trestle: owners={trestle.get('owner_count', 0)}")
    if trestle.get("owner_count", 0) > 1:
        for o in trestle.get("all_owners_summary", []):
            logger.info(f"  trestle:   owner: {o.get('name')} "
                        f"confidence={o.get('confidence')} type={o.get('type')}")


class VeronicaAgent(AgentBase):
    """Veronica Mars — AI voice agent for email & address collection."""

    def __init__(self):
        super().__init__(
            name="veronica-mars",
            route="/swml",
            record_call=True,
            record_format="wav",
            record_stereo=True,
        )

        # AI model
        self.set_param("ai_model", config.AI_MODEL)
        self.set_param("auto_correct", True)
        self.set_param("redact_prompt",
            "social security numbers, credit card numbers, dates of birth, "
            "bank account numbers, driver's license numbers"
        )

        # ── Personality prompt (no routing logic) ────────────────────
        self.prompt_add_section("Identity",
            "You are Veronica Mars. Private investigator. Neptune, California. "
            "Sharp, witty, a little world-weary — but underneath the armor, you care. "
            "You are not a customer service agent. You are not a chatbot. "
            "You are Veronica Mars, and you already did your homework before the caller dialed."
        )

        self.prompt_add_section("Voice and Tone", bullets=[
            "Warm but guarded. Lead with a quip, drop the wit when someone needs help.",
            "Short sentences. Punchy. Film noir energy. Never ramble.",
            "Never robotic. Never corporate. Never 'I'd be happy to assist you with that today.'",
            "Use the caller's name naturally — like you earned the right to.",
            "This is a PHONE CALL. Keep every response to 1-2 short sentences.",
        ])

        self.prompt_add_section("Rules", bullets=[
            "Never say 'our records indicate' or 'the system shows.'",
            "Never break character to explain the technology.",
            "Never make up information. Work with what you have.",
            "If you need to reference something you know, say it like a detective would: "
            "'I've got you at...' not 'our records show...'",
        ])

        self.prompt_add_section("Caller File", "${global_data}")

        # Voice
        self.add_language("English", "en-US", "azure.en-US-AvaNeural")

        # Hints for ASR
        self.add_hints([
            "Veronica", "Mars", "Neptune", "Mars Investigations",
            "at sign", "dot com", "dot net", "dot org", "underscore", "hyphen",
        ])

        # Post-prompt for call summary
        self.set_post_prompt(
            "Summarize: caller name, email collected (or not), address collected (or not), "
            "consent given, any follow-up needed. Keep it to 2-3 sentences."
        )

        # State machine
        self._define_state_machine()

        # Tools
        self._define_tools()

        # Per-call dynamic config
        self.set_dynamic_config_callback(self._per_call_config)

    # ── State Machine ────────────────────────────────────────────────

    def _define_state_machine(self):
        """Define conversation steps. All transitions forced by tools."""
        contexts = self.define_contexts()
        ctx = contexts.add_context("default")

        # GREETING — customized per-call by _per_call_config
        greeting = ctx.add_step("greeting")
        greeting.add_section("Task", "Greet the caller and confirm identity")
        greeting.add_bullets("Process", [
            "Greet the caller in character as Veronica Mars",
            "Confirm you're speaking with the right person",
            "Call confirm_identity with the result",
        ])
        greeting.set_functions(["confirm_identity"])
        greeting.set_valid_steps([])  # All transitions forced by tool

        # EMAIL CONFIRM — offer candidate email on file
        email_confirm = ctx.add_step("email_confirm")
        email_confirm.add_section("Task", "Confirm the email address on file")
        email_confirm.add_bullets("Process", [
            "Offer the candidate email casually: 'I've got ${global_data.candidate_email} in the file. That still good?'",
            "Do NOT say 'our records show' — you pulled the file, you know the address",
            "Call process_email_confirmation with their response",
        ])
        email_confirm.set_functions(["process_email_confirmation"])
        email_confirm.set_valid_steps([])

        # EMAIL COLLECTION — bridge step, routes by sms_eligible
        email_collection = ctx.add_step("email_collection")
        email_collection.add_section("Task", "Route to email collection method")
        email_collection.add_bullets("Process", [
            "Call initiate_email_collection immediately — it determines the path",
        ])
        email_collection.set_functions(["initiate_email_collection"])
        email_collection.set_valid_steps([])

        # SMS CONSENT (Phase 3 — placeholder step)
        sms_consent = ctx.add_step("sms_consent")
        sms_consent.add_section("Task", "Request SMS consent with required disclosure")
        sms_consent.add_bullets("Process", [
            "Say EXACTLY: 'I can text you a link to drop your email in — easier than spelling it out. "
            "Just so you know, message and data rates may apply. You okay with that?'",
            "Do NOT paraphrase the rate disclosure. Do NOT omit it.",
            "Call process_sms_consent with their response",
        ])
        sms_consent.set_functions(["process_sms_consent"])
        sms_consent.set_valid_steps([])

        # VOICE SPELLING — collect email by voice
        voice_spelling = ctx.add_step("voice_spelling")
        voice_spelling.add_section("Task", "Collect email address by voice spelling")
        voice_spelling.add_bullets("Process", [
            "Ask the caller to spell out their email address letter by letter.",
            "Listen carefully. The caller will say individual letters, 'at' for @, 'dot' for periods.",
            "RECONSTRUCT the email from the spoken letters — join them into a valid address. "
            "Example: caller says 'J O H N at G M A I L dot C O M' → john@gmail.com",
            "Fix obvious TLD errors: .con → .com, .nrt → .net, .ogr → .org",
            "Call submit_spelled_email with the RECONSTRUCTED email (not raw speech).",
            "The tool will read it back to you in NATO phonetics — speak that readback to the caller and ask them to confirm.",
        ])
        voice_spelling.set_functions(["submit_spelled_email"])
        voice_spelling.set_valid_steps([])

        # ZEROBOUNCE CHECK — bridge step, runs validation
        zerobounce_check = ctx.add_step("zerobounce_check")
        zerobounce_check.add_section("Task", "Validate the email address")
        zerobounce_check.add_bullets("Process", [
            "Call validate_email immediately — it checks the email",
        ])
        zerobounce_check.set_functions(["validate_email"])
        zerobounce_check.set_valid_steps([])

        # EMAIL SEND CONSENT — hard gate before Postmark fires
        email_send_consent = ctx.add_step("email_send_consent")
        email_send_consent.add_section("Task", "Ask for email send consent")
        email_send_consent.add_bullets("Process", [
            "Ask: 'Want me to send a confirmation to that address?'",
            "This is a hard yes/no gate. Do not assume consent.",
            "Call process_email_consent with their answer",
        ])
        email_send_consent.set_functions(["process_email_consent"])
        email_send_consent.set_valid_steps([])

        # CONFIRM ADDRESS
        confirm_address = ctx.add_step("confirm_address")
        confirm_address.add_section("Task", "Confirm the address on file")
        confirm_address.add_bullets("Process", [
            "Ask: 'I've got you at ${global_data.candidate_address}. That still home base?'",
            "Call process_address_confirmation with their response",
        ])
        confirm_address.set_functions(["process_address_confirmation"])
        confirm_address.set_valid_steps([])

        # ADDRESS COLLECTION
        address_collection = ctx.add_step("address_collection")
        address_collection.add_section("Task", "Collect the caller's home address")
        address_collection.add_bullets("Process", [
            "Ask: 'What's your current home address?'",
            "Let them speak naturally — they'll give street, city, state, zip.",
            "RECONSTRUCT the full address from what they say. "
            "Example: caller says 'one twenty three Main Street, Springfield, Illinois, six two seven oh four' "
            "→ 123 Main St, Springfield, IL 62704",
            "Convert spoken numbers to digits, state names to abbreviations.",
            "Call submit_address with the RECONSTRUCTED address.",
            "The tool will read back a normalized version — speak that to the caller and ask them to confirm.",
        ])
        address_collection.set_functions(["submit_address"])
        address_collection.set_valid_steps([])

        # ADDRESS VALIDATION
        address_validation = ctx.add_step("address_validation")
        address_validation.add_section("Task", "Validate the collected address")
        address_validation.add_bullets("Process", [
            "Call validate_address immediately",
        ])
        address_validation.set_functions(["validate_address"])
        address_validation.set_valid_steps([])

        # WRAP UP
        wrap_up = ctx.add_step("wrap_up")
        wrap_up.add_section("Task", "Wrap up the call")
        wrap_up.add_bullets("Process", [
            "Thank the caller naturally — in character",
            "If follow-up is needed, mention you'll be in touch",
            "Say goodbye — short, warm, Veronica",
        ])
        wrap_up.set_functions("none")
        wrap_up.set_valid_steps([])

    # ── Address Enrichment ────────────────────────────────────────────

    def _enrich_address(self, address):
        """Geocode via Google Maps, then USPS-validate via Smarty.

        Returns (normalized, lat, lng, confidence, dpv_match_code).
        All None if no address or APIs unconfigured.
        """
        if not address:
            logger.info(f"  geocode: no address to geocode")
            return None, None, None, None, None

        # Google Maps Geocoding
        geo = geocode_address(address)
        if geo:
            logger.info(f"  geocode: OK → {geo['formatted_address']}")
            logger.info(f"  geocode: lat={geo['lat']} lng={geo['lng']} confidence={geo['confidence']}")
        else:
            logger.info(f"  geocode: FAILED or not configured for '{address}'")
            return None, None, None, None, None

        normalized = geo["formatted_address"]
        lat = geo["lat"]
        lng = geo["lng"]
        confidence = geo["confidence"]

        # Smarty USPS validation — parse the geocoded address
        # Google returns "123 Main St, City, ST 12345, USA" format
        dpv = None
        parts = [p.strip() for p in normalized.split(",")]
        if len(parts) >= 3:
            street = parts[0]
            city = parts[1]
            # State + zip might be "CA 90210" or just "CA"
            state_zip = parts[2].replace("USA", "").strip()
            state_parts = state_zip.split()
            state = state_parts[0] if state_parts else ""
            zipcode = state_parts[1] if len(state_parts) > 1 else None

            smarty = smarty_validate_address(street, city, state, zipcode)
            if smarty:
                dpv = smarty["dpv_match_code"]
                if smarty.get("normalized"):
                    normalized = smarty["normalized"]
                logger.info(f"  smarty: dpv={dpv} normalized={normalized}")
            else:
                logger.info(f"  smarty: FAILED or not configured")
        else:
            logger.info(f"  smarty: skipped — couldn't parse geocoded address into components")

        return normalized, lat, lng, confidence, dpv

    # ── Per-Call Config ──────────────────────────────────────────────

    def _per_call_config(self, query_params, body_params, headers, agent):
        """Runs before every request (SWML + SWAIG). Only do enrichment on initial SWML.

        The SDK calls this callback for both the initial SWML request AND
        every SWAIG tool call. We only want enrichment/step customization
        on the initial call — SWAIG calls already have global_data set.
        """
        # Only run on the initial SWML request — skip SWAIG tool calls and post-prompt
        bp = body_params or {}
        is_swaig = bool(bp.get("function"))
        is_post_prompt = bool(bp.get("post_prompt_data") or bp.get("summary"))
        call_data = bp.get("call", {})
        caller_phone = call_data.get("from", "")

        if is_swaig or is_post_prompt or not caller_phone:
            return

        call_id = call_data.get("id", "unknown")

        logger.info(f"━━━ PRE-CALL ENRICHMENT ━━━ phone={caller_phone} call_id={call_id}")

        # ── Data store lookup ────────────────────────────────────────
        caller = get_caller_by_phone(caller_phone) if caller_phone else None
        logger.info(f"  data_store: {'HIT' if caller else 'MISS'} for {caller_phone}")

        owner_name = None
        candidate_email = None
        candidate_address = None
        address_normalized = None
        geocode_lat = None
        geocode_lng = None
        geocode_confidence = None
        dpv_match_code = None
        line_type = None
        sms_eligible = False
        record_source = "new"

        if caller and not caller_is_stale(caller):
            # RETURNING + FRESH — use stored record
            owner_name = caller.get("owner_name")
            candidate_email = caller.get("validated_email") or caller.get("candidate_email")
            candidate_address = caller.get("address_normalized") or caller.get("candidate_address")
            address_normalized = caller.get("address_normalized")
            geocode_lat = caller.get("geocode_lat")
            geocode_lng = caller.get("geocode_lng")
            geocode_confidence = caller.get("geocode_confidence")
            dpv_match_code = caller.get("dpv_match_code")
            line_type = caller.get("line_type")
            sms_eligible = bool(caller.get("sms_eligible"))
            record_source = "returning"
            # Rebuild extras from stored Trestle raw data so LLM gets rich context
            trestle_extras = {}
            stored_raw = caller.get("trestle_raw")
            if stored_raw:
                try:
                    raw_parsed = json.loads(stored_raw) if isinstance(stored_raw, str) else stored_raw
                    # Build a minimal trestle-like dict for _extract_trestle_extras
                    from api_clients import _parse_emails, _format_address
                    owners = raw_parsed.get("owners", [])
                    if owners:
                        o = owners[0]
                        pseudo_trestle = {
                            "firstname": o.get("firstname"),
                            "lastname": o.get("lastname"),
                            "middlename": o.get("middlename"),
                            "alternate_names": o.get("alternate_names", []),
                            "age_range": o.get("age_range"),
                            "gender": o.get("gender"),
                            "owner_type": o.get("type"),
                            "confidence_score": o.get("phone_to_name_confidence_score"),
                            "carrier": raw_parsed.get("carrier"),
                            "is_prepaid": raw_parsed.get("is_prepaid"),
                            "is_commercial": raw_parsed.get("is_commercial"),
                            "all_emails": _parse_emails(o.get("emails", [])),
                            "all_addresses": [],
                            "alternate_phones": [
                                {"number": p.get("phoneNumber") or p.get("phone_number"),
                                 "type": (p.get("lineType") or p.get("line_type") or "").lower()}
                                for p in o.get("alternate_phones", []) if isinstance(p, dict)
                            ],
                            "owner_count": len(owners),
                            "all_owners_summary": [
                                {"name": ow.get("name"), "confidence": ow.get("phone_to_name_confidence_score"),
                                 "type": ow.get("type"), "age_range": ow.get("age_range")}
                                for ow in owners
                            ],
                        }
                        trestle_extras = _extract_trestle_extras(pseudo_trestle)
                except Exception as e:
                    logger.warning(f"  trestle_raw parse failed: {e}")
            logger.info(f"  path: RETURNING (fresh)")
            logger.info(f"  stored: name={owner_name} email={candidate_email} address={candidate_address}")
            logger.info(f"  stored: geocode={geocode_lat},{geocode_lng} confidence={geocode_confidence} dpv={dpv_match_code}")

            # Backfill geocode if we have an address but no geocode data
            raw_address = caller.get("candidate_address")
            if raw_address and not geocode_lat:
                logger.info(f"  geocode: BACKFILL — address on file but never geocoded")
                address_normalized, geocode_lat, geocode_lng, geocode_confidence, dpv_match_code = \
                    self._enrich_address(raw_address)
                if geocode_lat:
                    upsert_caller(caller_phone,
                        address_normalized=address_normalized,
                        geocode_lat=geocode_lat,
                        geocode_lng=geocode_lng,
                        geocode_confidence=geocode_confidence,
                        dpv_match_code=dpv_match_code,
                    )
                    candidate_address = address_normalized or candidate_address
            else:
                logger.info(f"  API calls: NONE (fresh record, geocode present)")

        elif caller and caller_is_stale(caller):
            # RETURNING + STALE — re-enrich
            record_source = "refreshed"
            logger.info(f"  path: RETURNING (stale) — re-enriching")

            trestle = trestle_reverse_phone(caller_phone)
            logger.info(f"  trestle: {'OK' if trestle else 'FAILED'}")
            trestle_extras = {}
            if trestle:
                owner_name = trestle["owner_name"] or caller.get("owner_name")
                candidate_email = trestle["candidate_email"] or caller.get("validated_email") or caller.get("candidate_email")
                candidate_address = trestle["candidate_address"] or caller.get("address_normalized") or caller.get("candidate_address")
                line_type = trestle["line_type"] or caller.get("line_type")
                sms_eligible = trestle["sms_eligible"]
                trestle_extras = _extract_trestle_extras(trestle)
                _log_trestle(trestle)

                # Geocode + Smarty if we got an address
                address_normalized, geocode_lat, geocode_lng, geocode_confidence, dpv_match_code = \
                    self._enrich_address(candidate_address)

                upsert_caller(caller_phone,
                    owner_name=owner_name,
                    line_type=line_type,
                    sms_eligible=sms_eligible,
                    candidate_email=trestle["candidate_email"],
                    candidate_address=trestle["candidate_address"],
                    address_normalized=address_normalized,
                    geocode_lat=geocode_lat,
                    geocode_lng=geocode_lng,
                    geocode_confidence=geocode_confidence,
                    dpv_match_code=dpv_match_code,
                    trestle_raw=json.dumps(trestle["raw_response"]),
                    last_enriched_at=datetime.now(timezone.utc).isoformat(),
                    last_call_at=datetime.now(timezone.utc).isoformat(),
                )
            else:
                # Trestle failed, use stale record
                owner_name = caller.get("owner_name")
                candidate_email = caller.get("validated_email") or caller.get("candidate_email")
                candidate_address = caller.get("address_normalized") or caller.get("candidate_address")
                address_normalized = caller.get("address_normalized")
                geocode_lat = caller.get("geocode_lat")
                geocode_lng = caller.get("geocode_lng")
                geocode_confidence = caller.get("geocode_confidence")
                dpv_match_code = caller.get("dpv_match_code")
                line_type = caller.get("line_type")
                sms_eligible = bool(caller.get("sms_eligible"))
                logger.info(f"  trestle: FAILED — falling back to stale record")

        else:
            # NEW CALLER — full Trestle enrichment
            logger.info(f"  path: NEW CALLER — full enrichment")
            trestle = trestle_reverse_phone(caller_phone) if caller_phone else None
            logger.info(f"  trestle: {'OK' if trestle else 'FAILED'}")
            trestle_extras = {}
            if trestle:
                owner_name = trestle["owner_name"]
                candidate_email = trestle["candidate_email"]
                candidate_address = trestle["candidate_address"]
                line_type = trestle["line_type"]
                sms_eligible = trestle["sms_eligible"]
                trestle_extras = _extract_trestle_extras(trestle)
                _log_trestle(trestle)

                # Geocode + Smarty if we got an address
                address_normalized, geocode_lat, geocode_lng, geocode_confidence, dpv_match_code = \
                    self._enrich_address(candidate_address)

                upsert_caller(caller_phone,
                    owner_name=owner_name,
                    line_type=line_type,
                    sms_eligible=sms_eligible,
                    candidate_email=candidate_email,
                    candidate_address=candidate_address,
                    address_normalized=address_normalized,
                    geocode_lat=geocode_lat,
                    geocode_lng=geocode_lng,
                    geocode_confidence=geocode_confidence,
                    dpv_match_code=dpv_match_code,
                    trestle_raw=json.dumps(trestle["raw_response"]),
                    last_enriched_at=datetime.now(timezone.utc).isoformat(),
                    last_call_at=datetime.now(timezone.utc).isoformat(),
                )
            else:
                logger.info(f"  trestle: FAILED or no phone — no enrichment data")

        # ── Summary ──────────────────────────────────────────────────
        display_address = address_normalized or candidate_address
        logger.info(f"  ── ENRICHMENT RESULT ──")
        logger.info(f"  name:     {owner_name or '(none)'}")
        logger.info(f"  email:    {candidate_email or '(none)'}")
        logger.info(f"  address:  {display_address or '(none)'}")
        logger.info(f"  geocode:  {geocode_lat},{geocode_lng} confidence={geocode_confidence or '(none)'}")
        logger.info(f"  dpv:      {dpv_match_code or '(none)'}")
        logger.info(f"  line:     {line_type or '(none)'} sms={sms_eligible}")
        logger.info(f"  source:   {record_source}")
        logger.info(f"━━━ END PRE-CALL ━━━")

        # Populate global_data for LLM context — tools get call_id from raw_data
        global_data = {
            "caller_phone": caller_phone,
            "owner_name": owner_name or "Unknown",
            "candidate_email": candidate_email,
            "candidate_address": display_address or candidate_address,
            "line_type": line_type or "unknown",
            "sms_eligible": sms_eligible,
            "record_source": record_source,
        }
        # Merge rich Trestle data so the LLM can reference it naturally
        global_data.update(trestle_extras)
        agent.set_global_data(global_data)

        # ── Customize greeting step ─────────────────────────────────
        ctx = agent._contexts_builder.get_context("default")
        greeting = ctx.get_step("greeting")
        greeting.clear_sections()

        if record_source == "returning" and owner_name:
            greeting.add_section("Task", "Welcome back a returning caller")
            greeting.add_bullets("Process", [
                f"You remember {owner_name}. Reference the prior contact naturally.",
                f"Confirm identity: 'Am I speaking with {owner_name}?'",
                "Call confirm_identity with the result",
            ])
        elif owner_name:
            greeting.add_section("Task", "Greet a new caller you've already researched")
            greeting.add_bullets("Process", [
                f"You pulled the file on this number. You've got {owner_name}.",
                f"Confirm identity: 'Am I speaking with {owner_name}?'",
                "Call confirm_identity with the result",
            ])
        else:
            greeting.add_section("Task", "Greet an unknown caller")
            greeting.add_bullets("Process", [
                "No name on file. Greet generically but in character.",
                "'Mars Investigations. Veronica Mars speaking. Who am I talking to?'",
                "Call confirm_identity with their response",
            ])

        # ── Remove steps that don't apply ────────────────────────────

        # Remove confirm_address if no candidate address on file
        if not (display_address or candidate_address):
            try:
                ctx.remove_step("confirm_address")
            except Exception:
                pass

        # Phase 1: always remove SMS steps (Phase 3)
        for step_name in ["sms_consent"]:
            try:
                ctx.remove_step(step_name)
            except Exception:
                pass

        # Remove email_confirm if no candidate email
        if not candidate_email:
            try:
                ctx.remove_step("email_confirm")
            except Exception:
                pass

    # ── Tools ────────────────────────────────────────────────────────

    def _define_tools(self):
        """Define all SWAIG tool functions. Tools force every transition."""

        def _get_call_context(raw_data):
            """Extract call_id and global_data from raw SWAIG request.

            call_id comes from raw_data (SDK provides it), NOT global_data.
            """
            rd = raw_data or {}
            call_id = rd.get("call_id", "unknown")
            global_data = rd.get("global_data", {})
            caller_phone = global_data.get("caller_phone", "")
            return call_id, global_data, caller_phone

        # ── confirm_identity ─────────────────────────────────────────

        @self.tool(
            name="confirm_identity",
            description="Record whether the caller confirmed their identity",
            wait_file="/sounds/typing.mp3",
            fillers={"en-US": ["Pulling up the file", "Let me check my notes"]},
            parameters={
                "type": "object",
                "properties": {
                    "confirmed": {
                        "type": "boolean",
                        "description": "True if caller confirmed they are the person on file, "
                                       "false if they denied or are a different person",
                    },
                    "caller_name": {
                        "type": "string",
                        "description": "The name the caller gave, if different from file",
                    },
                },
                "required": ["confirmed"],
            },
        )
        def confirm_identity(args, raw_data):
            confirmed = args.get("confirmed", False)
            caller_name = args.get("caller_name")
            call_id, global_data, caller_phone = _get_call_context(raw_data)

            state = load_call_state(call_id)
            state["identity_confirmed"] = confirmed
            if not confirmed:
                state["identity_mismatch"] = True
                if caller_name:
                    state["owner_name"] = caller_name

            save_call_state(call_id, state)

            # Update global_data if name changed
            updates = {}
            if caller_name and not confirmed:
                updates["owner_name"] = caller_name

            result = SwaigFunctionResult("Identity noted." if confirmed else "Noted — different person.")

            if updates:
                gd = dict(global_data)
                gd.update(updates)
                result.update_global_data(gd)

            # Route: if candidate email exists → email_confirm, else → email_collection
            candidate_email = global_data.get("candidate_email")
            if candidate_email:
                result.swml_change_step("email_confirm")
            else:
                result.swml_change_step("email_collection")

            logger.info(f"confirm_identity: confirmed={confirmed}, next={'email_confirm' if candidate_email else 'email_collection'}")
            return result

        # ── process_email_confirmation ────────────────────────────────

        @self.tool(
            name="process_email_confirmation",
            description="Record whether the caller accepted the candidate email on file",
            wait_file="/sounds/typing.mp3",
            fillers={"en-US": ["Got it", "Noted"]},
            parameters={
                "type": "object",
                "properties": {
                    "confirmed": {
                        "type": "boolean",
                        "description": "True if caller accepted the email, false if they want a different one",
                    },
                },
                "required": ["confirmed"],
            },
        )
        def process_email_confirmation(args, raw_data):
            confirmed = args.get("confirmed", False)
            call_id, global_data, caller_phone = _get_call_context(raw_data)

            state = load_call_state(call_id)

            if confirmed:
                # Use candidate email as working email
                state["working_email"] = global_data.get("candidate_email")
                state["email_source"] = "trestle_confirmed"
                save_call_state(call_id, state)

                result = SwaigFunctionResult("Email confirmed.")
                result.update_global_data({
                    **global_data,
                    "working_email": state["working_email"],
                })
                result.swml_change_step("zerobounce_check")
                logger.info(f"email_confirm: accepted candidate → zerobounce_check")
            else:
                # Clear stale candidate so LLM stops referencing it
                save_call_state(call_id, state)
                result = SwaigFunctionResult("No problem — let's get the right one.")
                result.update_global_data({
                    **global_data,
                    "candidate_email": None,
                    "working_email": None,
                })
                result.swml_change_step("email_collection")
                logger.info(f"email_confirm: rejected candidate '{global_data.get('candidate_email')}' → email_collection")

            return result

        # ── initiate_email_collection (bridge) ───────────────────────

        @self.tool(
            name="initiate_email_collection",
            description="Determine email collection method based on line type. Call this immediately.",
            wait_file="/sounds/typing.mp3",
            parameters={"type": "object", "properties": {}},
        )
        def initiate_email_collection(args, raw_data):
            call_id, global_data, caller_phone = _get_call_context(raw_data)
            sms_eligible = global_data.get("sms_eligible", False)

            # Phase 1: always voice spelling (SMS is Phase 3)
            # When Phase 3 is built: if sms_eligible → sms_consent, else → voice_spelling
            result = SwaigFunctionResult("Collecting email by voice.")
            result.swml_change_step("voice_spelling")
            logger.info(f"initiate_email_collection: sms_eligible={sms_eligible}, routing to voice_spelling (Phase 1)")
            return result

        # ── submit_spelled_email ─────────────────────────────────────

        @self.tool(
            name="submit_spelled_email",
            description="Submit the email address the caller spelled out by voice",
            wait_file="/sounds/typing.mp3",
            fillers={"en-US": ["Let me write that down", "Got it, one second"]},
            parameters={
                "type": "object",
                "properties": {
                    "email": {
                        "type": "string",
                        "description": (
                            "The reconstructed email address. You MUST join individual spoken letters "
                            "into a complete email. 'at'/'at sign' → @, 'dot'/'period' → '.'. "
                            "Fix obvious TLD errors: .con → .com, .nrt → .net, .ogr → .org. "
                            "Example: caller says 'B R I A N at Y A H O O dot C O M' → brian@yahoo.com. "
                            "Do NOT pass raw speech — pass the valid reconstructed email."
                        ),
                    },
                    "confirmed": {
                        "type": "boolean",
                        "description": "True only after the caller confirmed the NATO phonetic readback",
                    },
                },
                "required": ["email"],
            },
        )
        def submit_spelled_email(args, raw_data):
            raw_email = args.get("email", "").strip()
            confirmed = args.get("confirmed", False)
            call_id, global_data, caller_phone = _get_call_context(raw_data)

            state = load_call_state(call_id)

            # Normalize the spoken email
            email = normalize_spoken_email(raw_email)

            # Basic format check
            if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
                state["spelling_attempts"] = state.get("spelling_attempts", 0) + 1
                save_call_state(call_id, state)

                if state["spelling_attempts"] >= 3:
                    state["follow_up_required"] = True
                    state["follow_up_reason"] = "email_not_captured"
                    save_call_state(call_id, state)
                    result = SwaigFunctionResult(
                        "Email couldn't be captured after multiple attempts. "
                        "Follow-up will be scheduled."
                    )
                    result.swml_change_step("wrap_up")
                    logger.info("submit_spelled_email: 3rd failure → wrap_up")
                    return result

                # Generate NATO readback for the attempt
                nato = nato_spell_email(email) if "@" in email else email
                result = SwaigFunctionResult(
                    f"That doesn't look like a valid email. I got: {nato}. "
                    "Ask them to try spelling it again."
                )
                result.swml_change_step("voice_spelling")
                return result

            if not confirmed:
                # Read back in NATO and ask for confirmation
                nato = nato_spell_email(email)
                result = SwaigFunctionResult(
                    f"Read back to the caller: '{nato}'. "
                    "Ask if that's correct. Then call submit_spelled_email again with confirmed=true."
                )
                return result

            # Confirmed — store and validate
            state["working_email"] = email
            state["email_source"] = "voice_spelling"
            state["spelling_attempts"] = state.get("spelling_attempts", 0) + 1
            save_call_state(call_id, state)

            result = SwaigFunctionResult("Got it.")
            result.update_global_data({**global_data, "working_email": email})
            result.swml_change_step("zerobounce_check")
            logger.info(f"submit_spelled_email: confirmed '{email}' → zerobounce_check")
            return result

        # ── validate_email (bridge — calls ZeroBounce) ───────────────

        @self.tool(
            name="validate_email",
            description="Validate the working email via ZeroBounce. Call this immediately.",
            wait_file="/sounds/typing.mp3",
            fillers={"en-US": ["Let me check that real quick", "Running that through my system", "Verifying that one"]},
            parameters={"type": "object", "properties": {}},
        )
        def validate_email(args, raw_data):
            call_id, global_data, caller_phone = _get_call_context(raw_data)

            state = load_call_state(call_id)
            email = state.get("working_email") or global_data.get("working_email")

            if not email:
                result = SwaigFunctionResult("No email to validate.")
                result.swml_change_step("email_collection")
                return result

            # Call ZeroBounce
            zb = zerobounce_validate(email)

            if zb is None:
                # API failed — proceed with unknown status
                state["zb_status"] = "api_error"
                save_call_state(call_id, state)
                result = SwaigFunctionResult("Email check passed.")
                result.update_global_data({
                    **global_data,
                    "candidate_email": email,
                    "working_email": email,
                })
                result.swml_change_step("email_send_consent")
                logger.info(f"validate_email: API error for '{email}', proceeding → email_send_consent")
                return result

            state["zb_status"] = zb["status"]
            state["zb_sub_status"] = zb["sub_status"]
            save_call_state(call_id, state)

            if zb["is_valid"]:
                # Valid — update global_data so LLM knows the confirmed email
                result = SwaigFunctionResult("Email checks out.")
                result.update_global_data({
                    **global_data,
                    "candidate_email": email,
                    "working_email": email,
                })
                result.swml_change_step("email_send_consent")
                logger.info(f"validate_email: '{email}' valid → email_send_consent")
                return result

            if not zb["is_invalid"]:
                # Unknown/catch-all — proceed but flag for post-call re-check
                state["follow_up_required"] = True
                state["follow_up_reason"] = "email_validation_failed"
                save_call_state(call_id, state)
                result = SwaigFunctionResult("Email checks out.")
                result.update_global_data({
                    **global_data,
                    "candidate_email": email,
                    "working_email": email,
                })
                result.swml_change_step("email_send_consent")
                logger.info(f"validate_email: '{email}' unknown status, proceeding → email_send_consent")
                return result

            # Invalid — retry or give up
            state["email_attempts"] = state.get("email_attempts", 0) + 1
            save_call_state(call_id, state)

            if state["email_attempts"] >= 2:
                state["follow_up_required"] = True
                state["follow_up_reason"] = "email_validation_failed"
                save_call_state(call_id, state)
                result = SwaigFunctionResult(
                    "Email validation failed after retries. Follow-up will be scheduled. "
                    "Tell the caller: 'We're not going to crack this one tonight. "
                    "I'll reach back out — we'll get it sorted.'"
                )
                result.swml_change_step("wrap_up")
                logger.info(f"validate_email: invalid, retries exhausted → wrap_up")
                return result

            # Can retry — back to collection
            result = SwaigFunctionResult(
                "That email didn't check out. Tell the caller: "
                "'That one's not checking out on my end. Happens. You got another one I can try?'"
            )
            result.update_global_data({**global_data, "working_email": None})
            result.swml_change_step("email_collection")
            logger.info(f"validate_email: invalid, attempt {state['email_attempts']} → email_collection")
            return result

        # ── process_email_consent ────────────────────────────────────

        @self.tool(
            name="process_email_consent",
            description="Record whether the caller consented to receiving a confirmation email",
            wait_file="/sounds/typing.mp3",
            fillers={"en-US": ["Noted", "On it"]},
            parameters={
                "type": "object",
                "properties": {
                    "consented": {
                        "type": "boolean",
                        "description": "True if caller said yes to receiving the email, false if no",
                    },
                },
                "required": ["consented"],
            },
        )
        def process_email_consent(args, raw_data):
            consented = args.get("consented", False)
            call_id, global_data, caller_phone = _get_call_context(raw_data)

            state = load_call_state(call_id)
            logger.info(f"process_email_consent: call_id={call_id} caller_phone={caller_phone}")
            logger.info(f"process_email_consent: state.working_email={state.get('working_email')}")
            logger.info(f"process_email_consent: global_data.working_email={global_data.get('working_email')}")

            state["email_consent"] = consented
            save_call_state(call_id, state)

            # Audit trail
            log_consent(caller_phone, call_id, "email_send", consented)

            if consented:
                # Get email: state → global_data.working_email → global_data.candidate_email
                email = (
                    state.get("working_email")
                    or global_data.get("working_email")
                    or global_data.get("candidate_email")
                )
                logger.info(f"process_email_consent: resolved email={email}")

                if email and caller_phone:
                    upsert_caller(caller_phone,
                        validated_email=email,
                        last_call_at=datetime.now(timezone.utc).isoformat(),
                    )

                # Send the confirmation email via Postmark
                owner_name = global_data.get("owner_name", "there")
                if email:
                    logger.info(f"process_email_consent: sending Postmark to {email}")
                    pm = postmark_send(
                        to_email=email,
                        subject="Mars Investigations — Confirmation",
                        html_body=(
                            f"<p>Hey {owner_name},</p>"
                            f"<p>This is Veronica Mars confirming we've got your email on file.</p>"
                            f"<p>If you didn't just speak with a sharp-tongued PI from Neptune, "
                            f"someone's got some explaining to do.</p>"
                            f"<p>— V</p>"
                            f"<p><em>Mars Investigations</em></p>"
                        ),
                        text_body=(
                            f"Hey {owner_name},\n\n"
                            f"This is Veronica Mars confirming we've got your email on file.\n\n"
                            f"If you didn't just speak with a sharp-tongued PI from Neptune, "
                            f"someone's got some explaining to do.\n\n"
                            f"— V\n"
                            f"Mars Investigations"
                        ),
                    )
                    if pm and pm["success"]:
                        state["postmark_message_id"] = pm["message_id"]
                        save_call_state(call_id, state)
                        logger.info(f"process_email_consent: Postmark sent, MessageID={pm['message_id']}")
                    elif pm:
                        logger.error(f"process_email_consent: Postmark failed: {pm['error']}")
                    else:
                        logger.warning("process_email_consent: Postmark not configured, skipping send")
                else:
                    logger.error(f"process_email_consent: no email to send to! state keys={list(state.keys())}")

                result = SwaigFunctionResult("Consent recorded. Confirmation email sent.")
            else:
                result = SwaigFunctionResult("Understood — no email will be sent.")

            # Route to address flow
            candidate_addr = global_data.get("candidate_address")
            if candidate_addr:
                result.swml_change_step("confirm_address")
                logger.info(f"process_email_consent: consented={consented} → confirm_address")
            else:
                result.swml_change_step("address_collection")
                logger.info(f"process_email_consent: consented={consented} → address_collection")
            return result

        # ── Phase 3 placeholder: process_sms_consent ─────────────────

        @self.tool(
            name="process_sms_consent",
            description="Record SMS consent and send tokenized link (Phase 3)",
            wait_file="/sounds/typing.mp3",
            fillers={"en-US": ["Sending that over now", "One sec, firing off the text"]},
            parameters={
                "type": "object",
                "properties": {
                    "consented": {
                        "type": "boolean",
                        "description": "True if caller consented to SMS",
                    },
                },
                "required": ["consented"],
            },
        )
        def process_sms_consent(args, raw_data):
            consented = args.get("consented", False)
            call_id, global_data, caller_phone = _get_call_context(raw_data)

            state = load_call_state(call_id)
            state["sms_consent"] = consented
            save_call_state(call_id, state)

            log_consent(caller_phone, call_id, "sms", consented)

            if consented:
                # Phase 3: send SMS with tokenized link, transition to sms_wait
                result = SwaigFunctionResult("SMS consent recorded. (Phase 3: would send SMS link)")
                result.swml_change_step("voice_spelling")  # Fallback until Phase 3
            else:
                result = SwaigFunctionResult("No problem — let's do it the old-fashioned way.")
                result.swml_change_step("voice_spelling")

            return result

        # ── Address tools ─────────────────────────────────────────────

        @self.tool(
            name="process_address_confirmation",
            description="Record whether the caller confirmed, denied, or declined the address on file",
            wait_file="/sounds/typing.mp3",
            fillers={"en-US": ["Updating the file", "Got it"]},
            parameters={
                "type": "object",
                "properties": {
                    "response": {
                        "type": "string",
                        "enum": ["confirmed", "denied", "declined"],
                        "description": "Whether caller confirmed, denied, or declined to confirm",
                    },
                },
                "required": ["response"],
            },
        )
        def process_address_confirmation(args, raw_data):
            response = args.get("response", "declined")
            call_id, global_data, caller_phone = _get_call_context(raw_data)
            state = load_call_state(call_id)

            logger.info(f"process_address_confirmation: response={response}")

            if response == "confirmed":
                # Use the pre-enriched address from global_data
                address = global_data.get("candidate_address", "")
                state["collected_address"] = address
                state["address_source"] = "confirmed_on_file"
                save_call_state(call_id, state)

                result = SwaigFunctionResult("Address confirmed. Let me verify it.")
                result.swml_change_step("address_validation")
                logger.info(f"process_address_confirmation: confirmed → address_validation")
                return result

            elif response == "denied":
                result = SwaigFunctionResult("No problem. Let's get the right one.")
                result.swml_change_step("address_collection")
                logger.info(f"process_address_confirmation: denied → address_collection")
                return result

            else:  # declined
                result = SwaigFunctionResult("That's fine, we can skip that for now.")
                result.swml_change_step("wrap_up")
                logger.info(f"process_address_confirmation: declined → wrap_up")
                return result

        @self.tool(
            name="submit_address",
            description="Submit a caller-provided address",
            wait_file="/sounds/typing.mp3",
            fillers={"en-US": ["Writing that down", "Let me get that on file"]},
            parameters={
                "type": "object",
                "properties": {
                    "address": {
                        "type": "string",
                        "description": (
                            "The reconstructed home address. Convert spoken numbers to digits, "
                            "state names to abbreviations. "
                            "Example: 'one twenty three Main Street Springfield Illinois six two seven oh four' "
                            "→ '123 Main St, Springfield, IL 62704'. "
                            "Pass a clean, structured address — not raw speech."
                        ),
                    },
                    "confirmed": {
                        "type": "boolean",
                        "description": "True only after the caller confirmed the readback of the normalized address",
                    },
                },
                "required": ["address"],
            },
        )
        def submit_address(args, raw_data):
            raw_address = args.get("address", "").strip()
            confirmed = args.get("confirmed", False)
            call_id, global_data, caller_phone = _get_call_context(raw_data)
            state = load_call_state(call_id)

            if not raw_address:
                result = SwaigFunctionResult("I didn't catch an address. Ask them again.")
                result.swml_change_step("address_collection")
                return result

            # Geocode to normalize the address
            geo = geocode_address(raw_address)
            if geo:
                normalized = geo["formatted_address"]
            else:
                normalized = raw_address  # Fall back to raw if geocode fails

            if not confirmed:
                # Cache the geocode result in state for when they confirm
                state["pending_address"] = normalized
                state["pending_address_raw"] = raw_address
                if geo:
                    state["pending_geocode"] = {
                        "lat": geo["lat"], "lng": geo["lng"],
                        "confidence": geo["confidence"],
                    }
                save_call_state(call_id, state)

                result = SwaigFunctionResult(
                    f"Read this back to the caller: '{normalized}'. "
                    "Ask if that's correct. Then call submit_address again with confirmed=true."
                )
                return result

            # Confirmed — store and route to validation
            state["collected_address"] = normalized
            state["address_source"] = "voice_collected"
            state["address_attempts"] = state.get("address_attempts", 0) + 1
            save_call_state(call_id, state)

            result = SwaigFunctionResult("Got it.")
            result.update_global_data({**global_data, "collected_address": normalized})
            result.swml_change_step("address_validation")
            logger.info(f"submit_address: confirmed '{normalized}' → address_validation")
            return result

        @self.tool(
            name="validate_address",
            description="Validate collected address via geocoding and USPS. Call this immediately.",
            wait_file="/sounds/typing.mp3",
            fillers={"en-US": ["Checking that address", "Running it through the system"]},
            parameters={"type": "object", "properties": {}},
        )
        def validate_address(args, raw_data):
            call_id, global_data, caller_phone = _get_call_context(raw_data)
            state = load_call_state(call_id)

            address = state.get("collected_address") or global_data.get("collected_address") or global_data.get("candidate_address")

            if not address:
                result = SwaigFunctionResult("No address to validate.")
                result.swml_change_step("address_collection")
                return result

            # Run full enrichment pipeline (geocode + Smarty)
            normalized, lat, lng, confidence, dpv = self._enrich_address(address)

            if normalized is None:
                # Geocode failed — accept the raw address and proceed
                state["address_validation_status"] = "geocode_error"
                save_call_state(call_id, state)

                if caller_phone:
                    upsert_caller(caller_phone,
                        candidate_address=address,
                        last_call_at=datetime.now(timezone.utc).isoformat(),
                    )

                result = SwaigFunctionResult("Address noted.")
                result.update_global_data({**global_data, "candidate_address": address})
                result.swml_change_step("wrap_up")
                logger.info(f"validate_address: geocode failed for '{address}', accepting → wrap_up")
                return result

            # Store enrichment results
            if caller_phone:
                upsert_caller(caller_phone,
                    candidate_address=address,
                    address_normalized=normalized,
                    geocode_lat=lat,
                    geocode_lng=lng,
                    geocode_confidence=confidence,
                    dpv_match_code=dpv,
                    last_call_at=datetime.now(timezone.utc).isoformat(),
                )

            # DPV check: Y = deliverable, S = secondary missing, D = drop
            if dpv in ("Y", "S", "D", None):
                # Valid or acceptable — proceed
                state["address_validation_status"] = "valid"
                save_call_state(call_id, state)

                result = SwaigFunctionResult("Address checks out.")
                result.update_global_data({
                    **global_data,
                    "candidate_address": normalized,
                    "collected_address": normalized,
                })
                result.swml_change_step("wrap_up")
                logger.info(f"validate_address: '{normalized}' dpv={dpv} → wrap_up")
                return result

            # DPV N or vacant — address didn't validate
            state["address_attempts"] = state.get("address_attempts", 0) + 1
            save_call_state(call_id, state)

            if state["address_attempts"] >= 2:
                state["follow_up_required"] = True
                state["follow_up_reason"] = "address_validation_failed"
                save_call_state(call_id, state)

                result = SwaigFunctionResult(
                    "Address couldn't be verified. Follow-up will be scheduled. "
                    "Tell the caller: 'I couldn't verify that one. "
                    "Don't worry — I'll follow up to get it sorted.'"
                )
                result.swml_change_step("wrap_up")
                logger.info(f"validate_address: dpv={dpv}, retries exhausted → wrap_up")
                return result

            # Retry — back to collection
            result = SwaigFunctionResult(
                "That address didn't check out. Tell the caller: "
                "'That one's not coming up in my system. Can you double-check it for me?'"
            )
            result.update_global_data({**global_data, "collected_address": None})
            result.swml_change_step("address_collection")
            logger.info(f"validate_address: dpv={dpv}, attempt {state['address_attempts']} → address_collection")
            return result

        # ── schedule_followup ────────────────────────────────────────

        @self.tool(
            name="schedule_followup",
            description="Schedule a follow-up call for unresolved items",
            wait_file="/sounds/typing.mp3",
            fillers={"en-US": ["Making a note for next time", "Flagging that for follow-up"]},
            parameters={
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "enum": [
                            "email_not_captured",
                            "email_validation_failed",
                            "address_not_captured",
                            "address_validation_failed",
                            "caller_refused",
                            "other",
                        ],
                        "description": "Reason for follow-up",
                    },
                },
                "required": ["reason"],
            },
        )
        def schedule_followup(args, raw_data):
            reason = args.get("reason", "unspecified")
            call_id, global_data, caller_phone = _get_call_context(raw_data)

            state = load_call_state(call_id)
            state["follow_up_required"] = True
            state["follow_up_reason"] = reason
            save_call_state(call_id, state)

            result = SwaigFunctionResult(f"Follow-up scheduled: {reason}")
            result.swml_change_step("wrap_up")
            logger.info(f"schedule_followup: reason={reason} → wrap_up")
            return result

    # ── SWML Debug Output ────────────────────────────────────────────

    def _render_swml(self, call_id=None, modifications=None):
        """Override to dump the generated SWML to stderr for debugging."""
        swml = super()._render_swml(call_id, modifications)
        try:
            parsed = json.loads(swml) if isinstance(swml, str) else swml
            print(json.dumps(parsed, indent=2, default=str), file=sys.stderr)
        except Exception:
            print(swml, file=sys.stderr)
        return swml

    # ── Call Summary + Persistence ───────────────────────────────────

    def on_summary(self, summary=None, raw_data=None):
        """Called when the post-prompt summary arrives after hangup.

        Saves full call data to calls/ and cleans up ephemeral state.
        """
        if summary:
            logger.info(f"Call summary: {summary}")

        if raw_data:
            calls_dir = Path(__file__).parent / "calls"
            calls_dir.mkdir(exist_ok=True)
            call_id = raw_data.get("call_id", "unknown")
            out_path = calls_dir / f"{call_id}.json"
            try:
                out_path.write_text(json.dumps(raw_data, indent=2, default=str))
                logger.info(f"Saved call data to {out_path}")
            except Exception as e:
                logger.error(f"Failed to save call data: {e}")

            # Clean up ephemeral SQLite state for this call
            delete_call_state(call_id)
            cleanup_stale_states(24)


# Default call state for initialization (avoids circular import)
DEFAULT_CALL_STATE_INIT = {
    "owner_name": None,
    "line_type": None,
    "sms_eligible": False,
    "candidate_email": None,
    "candidate_address": None,
    "record_source": "new",
    "identity_confirmed": False,
    "identity_mismatch": False,
    "working_email": None,
    "email_source": None,
    "email_attempts": 0,
    "spelling_attempts": 0,
    "zb_status": None,
    "zb_sub_status": None,
    "email_consent": None,
    "sms_consent": None,
    "follow_up_required": False,
    "follow_up_reason": None,
    "collected_address": None,
    "address_source": None,
    "address_attempts": 0,
    "address_validation_status": None,
}


if __name__ == "__main__":
    agent = VeronicaAgent()
    agent.run()
