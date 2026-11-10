#!/usr/bin/env python3
"""Pytest suite for InvoiceSweep x402 reference agent."""

from __future__ import annotations

import base64
import json
import pathlib
from decimal import Decimal

import pytest

from invoicesweep_x402_agent import (
    DeterministicMockFacilitatorTransport,
    HERMES_BASE_URL,
    IncomingInvoice,
    InvoiceSweepAgent,
    InvoiceSweepError,
    KNOWN_SPL_MINTS,
    USDC_SOLANA_MINT,
    WSOL_SOLANA_MINT,
    ZeroDepositHermesHttpTransport,
    ZeroTrustSameOriginRedirectHandler,
    atomic_to_usdc,
    build_tx_request_body,
    compare_unit_economics,
    compute_min_out_atomic,
    parse_hermes_402_challenge,
    parse_retry_after_seconds,
    usdc_to_atomic,
    verify_x_payment_response,
)

DEMO_WALLET = "Dpd5z1quMeBwPBkKCQNHnjdU6taSLAAcetpon83vgQVe"
CAPTURE_PATH = pathlib.Path(__file__).resolve().parent / "hermes_live_402_capture.json"


def test_decimal_usdc_atomic_conversion_no_float_drift() -> None:
    # Classic IEEE-754 float trap: int(float("19.99") * 1e6) == 19989999 (CM-024)
    assert usdc_to_atomic("19.99") == 19_990_000
    assert usdc_to_atomic("0.001") == 1_000
    assert usdc_to_atomic("0.003007") == 3_007
    assert atomic_to_usdc(1_000) == Decimal("0.001")
    assert atomic_to_usdc("3007") == Decimal("0.003007")
    with pytest.raises(ValueError):
        usdc_to_atomic("0.0000001")


def test_multiply_before_divide_slippage_math() -> None:
    # 100_001 atomic output at 50 bps: (100_001 * 9950) // 10_000 == 99_500 (not (100_001//10_000)*9950 == 99_500 vs 99_999)
    assert compute_min_out_atomic(199_999, 50) == 198_999
    assert compute_min_out_atomic(1_000_000, 50) == 995_000
    with pytest.raises(ValueError):
        compute_min_out_atomic(1_000, 10_000)


def test_live_captured_hermes_402_payload_parses_cleanly() -> None:
    assert CAPTURE_PATH.exists(), "hermes_live_402_capture.json must exist"
    capture = json.loads(CAPTURE_PATH.read_text(encoding="utf-8"))
    assert capture["status_code"] == 402
    terms = parse_hermes_402_challenge(
        capture["raw_body"], max_call_usdc_cap=Decimal("0.010000")
    )
    assert terms.x402_version == 1
    assert terms.scheme == "exact"
    assert terms.network == "solana"
    assert terms.asset == USDC_SOLANA_MINT
    assert terms.resource == f"{HERMES_BASE_URL}/v1/swap/quote"
    assert terms.max_amount_required_atomic > 0
    assert terms.fee_payer != ""


def test_parse_402_enforces_atomic_cap_and_solana_usdc_mint() -> None:
    overpriced_body = json.dumps(
        {
            "x402Version": 1,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "solana",
                    "maxAmountRequired": "25000",  # $0.025 > $0.010 cap
                    "resource": f"{HERMES_BASE_URL}/v1/swap/quote",
                    "payTo": "BPz2u6eoWLervxEqfpsMCVmKk7h6fEVsd7L5rvEibMfD",
                    "asset": USDC_SOLANA_MINT,
                }
            ],
        }
    )
    with pytest.raises(InvoiceSweepError, match="exceeds agent safety cap"):
        parse_hermes_402_challenge(overpriced_body, max_call_usdc_cap=Decimal("0.010000"))

    wrong_mint_body = json.dumps(
        {
            "x402Version": 1,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "solana",
                    "maxAmountRequired": "1000",
                    "resource": f"{HERMES_BASE_URL}/v1/swap/quote",
                    "payTo": "BPz2u6eoWLervxEqfpsMCVmKk7h6fEVsd7L5rvEibMfD",
                    "asset": WSOL_SOLANA_MINT,
                }
            ],
        }
    )
    with pytest.raises(InvoiceSweepError, match="Unexpected settlement asset"):
        parse_hermes_402_challenge(wrong_mint_body)


def test_build_tx_requires_inner_provider_quote() -> None:
    bad_wrapper = {"provider": {"key": "jupiter"}, "outputAmount": "1000000"}
    with pytest.raises(InvoiceSweepError, match="providerQuote"):
        build_tx_request_body(bad_wrapper, DEMO_WALLET)

    good_quote = {
        "provider": {"key": "jupiter"},
        "providerQuote": {"routeId": "r1", "outAmount": "1000000"},
    }
    req = build_tx_request_body(good_quote, DEMO_WALLET)
    assert req["provider"] == "jupiter"
    assert req["quoteResponse"] == {"routeId": "r1", "outAmount": "1000000"}


def test_verify_x_payment_response_rejects_missing_or_unsettled() -> None:
    with pytest.raises(InvoiceSweepError, match="Missing X-PAYMENT-RESPONSE"):
        verify_x_payment_response(None)

    unsettled = base64.b64encode(json.dumps({"success": False}).encode("utf-8")).decode("ascii")
    with pytest.raises(InvoiceSweepError, match="unsettled"):
        verify_x_payment_response(unsettled)

    settled_payload = {
        "success": True,
        "network": "solana",
        "transaction": "MOCK_SETTLE_TX_0001",
        "payer": DEMO_WALLET,
    }
    settled_b64 = base64.b64encode(json.dumps(settled_payload).encode("utf-8")).decode("ascii")
    receipt = verify_x_payment_response(settled_b64)
    assert receipt.success is True
    assert receipt.transaction == "MOCK_SETTLE_TX_0001"


def test_mock_facilitator_end_to_end_sweep_and_usdc_skip() -> None:
    transport = DeterministicMockFacilitatorTransport(
        quote_price_atomic=1000, build_price_atomic=1000, payer_wallet=DEMO_WALLET
    )
    agent = InvoiceSweepAgent(
        user_address=DEMO_WALLET,
        transport=transport,
        max_call_usdc_cap=Decimal("0.010000"),
        allow_mock_payment_header=True,
    )

    # Non-USDC invoice triggers 4 calls: quote(402)->quote(200)->build-tx(402)->build-tx(200)
    jup_inv = IncomingInvoice.from_mapping(
        {
            "invoice_id": "INV-JUP-1",
            "client_label": "DAO Grant",
            "symbol": "JUP",
            "amount_atomic": "500000000",
            "slippage_bps": 50,
        }
    )
    plan = agent.simulate_invoice_sweep(jup_inv)
    assert plan.action == "SWEEP_TO_USDC_READY"
    assert plan.total_x402_fee_atomic == 2000
    assert plan.total_x402_fee_usdc == Decimal("0.002")
    assert plan.selected_provider == "jupiter"
    assert plan.unsigned_tx_base64 is not None
    assert len(transport.call_log) == 4

    # Already-USDC invoice makes 0 API calls and spends $0
    usdc_inv = IncomingInvoice(
        "INV-USDC-2", "Direct Client", "USDC", USDC_SOLANA_MINT, "250000000", 50
    )
    skip_plan = agent.simulate_invoice_sweep(usdc_inv)
    assert skip_plan.action == "SKIP_ALREADY_USDC"
    assert skip_plan.total_x402_fee_atomic == 0
    assert len(transport.call_log) == 4


def test_mass_assignment_and_redirect_guards() -> None:
    with pytest.raises(InvoiceSweepError, match="Disallowed invoice fields"):
        IncomingInvoice.from_mapping(
            {
                "invoice_id": "INV-BAD",
                "symbol": "BONK",
                "token_mint": KNOWN_SPL_MINTS["BONK"][0],
                "amount_atomic": "1000",
                "is_admin": True,
            }
        )

    import urllib.request

    handler = ZeroTrustSameOriginRedirectHandler({"hermes.swap.io"})
    req = urllib.request.Request("https://hermes.swap.io/v1/swap/quote")
    with pytest.raises(InvoiceSweepError, match="Blocked unsafe redirect"):
        handler.redirect_request(req, None, 302, "Found", {}, "https://evil.example.com/steal")


def test_unit_economics_comparison() -> None:
    econ = compare_unit_economics(
        swaps_per_month=8, quote_price_atomic=1000, build_price_atomic=1000
    )
    assert econ["per_swap_x402_usdc"] == "0.002000"
    assert econ["monthly_x402_usdc"] == "0.016000"
    assert econ["savings_percent"] == "99.98"
    assert econ["breakeven_swaps_per_month"] == "49500"


def test_submission_cli_slot1_lock_and_dry_run(tmp_path: pathlib.Path) -> None:
    import sys

    scripts_dir = str(pathlib.Path(__file__).resolve().parents[2] / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import submit_superteam_swapio_x402 as sub_cli

    # Verify Slot-1 lock rejects non-Slot-1 credentials
    fake_cred = tmp_path / "wrong_slot.json"
    fake_cred.write_text(
        json.dumps(
            {
                "username": "antigravityforgevn-marvellous-41",
                "agentId": "b0f276f3-1122-4de9-a03d-71630af69435",
                "userId": "cdfcd47f-3184-40fa-b6f5-42cabe3c0e25",
                "apiKey": "sk_" + ("0" * 40),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(sub_cli.SubmissionGateError, match="Single-Entry Lock"):
        sub_cli.load_slot1_credentials(fake_cred)

    # Verify target URL is strictly superteam.fun (never earn.superteam.fun 308 trap)
    assert sub_cli.SUPERTEAM_SUBMIT_URL == "https://superteam.fun/api/agents/submissions/create"
    assert sub_cli.ALLOWED_SUPERTEAM_HOSTS == frozenset({"superteam.fun"})

    # Verify deliverable pack & dry-run exit cleanly without printing raw apiKey
    pack = sub_cli.verify_deliverable_pack()
    assert pack["ok"] is True
    assert pack["listing_id"] == "c4b303ec-66ad-417c-8ae4-b35300876387"
    assert pack["locked_slot1_agent"]["username"] == "antigravity-forge-vn-hilarious-18"
    assert "apiKey" not in pack["locked_slot1_agent"]
    assert sub_cli.main(["--dry-run"]) == 0

    # Verify atomic O_CREAT|O_EXCL single-entry lock & response secret scrubbing
    inflight = tmp_path / "inflight.lock"
    receipt = tmp_path / "receipt.json"
    sub_cli.acquire_atomic_single_entry_lock(inflight, receipt)
    with pytest.raises(sub_cli.SubmissionGateError, match="inflight lock exists"):
        sub_cli.acquire_atomic_single_entry_lock(inflight, receipt)

    mock_secret = "sk_" + ("9" * 30)
    sanitized = sub_cli.sanitize_submission_response(
        {"id": "sub-1", "status": "Pending", "echoedSecret": mock_secret, "notes": mock_secret},
        mock_secret,
    )
    assert sanitized == {"id": "sub-1", "status": "Pending"}

    # Verify earn.superteam.fun 308 and port-change redirects fail closed (CM-026)
    import urllib.request

    st_handler = sub_cli.ZeroTrustSameOriginRedirectHandler({"superteam.fun"})
    st_req = urllib.request.Request("https://superteam.fun/api/agents/submissions/create")
    with pytest.raises(sub_cli.SubmissionGateError, match="CM-026 redirect blocked"):
        st_handler.redirect_request(
            st_req, None, 308, "Permanent Redirect", {}, "https://earn.superteam.fun/api/agents"
        )
    with pytest.raises(sub_cli.SubmissionGateError, match="CM-026 redirect blocked"):
        st_handler.redirect_request(
            st_req, None, 307, "Temporary Redirect", {}, "https://superteam.fun:8443/api/agents"
        )


def test_r101_structural_mock_lock_and_rfc7231_retry_after() -> None:
    # R101: live transport cannot be paired with allow_mock_payment_header=True
    with pytest.raises(InvoiceSweepError, match="R101 structural zero-spend lock"):
        InvoiceSweepAgent(
            user_address=DEMO_WALLET,
            transport=ZeroDepositHermesHttpTransport(),
            allow_mock_payment_header=True,
        )

    # RFC 7231 numeric + HTTP-date Retry-After parsing
    assert parse_retry_after_seconds("4.5") == 4.5
    assert parse_retry_after_seconds("Wed, 21 Oct 2099 07:28:00 GMT") > 60.0

    # Strict canonical digit & mint consistency checks
    with pytest.raises(InvoiceSweepError, match="Mint mismatch"):
        IncomingInvoice.from_mapping(
            {
                "invoice_id": "INV-MINT-ERR",
                "symbol": "JUP",
                "token_mint": WSOL_SOLANA_MINT,
                "amount_atomic": "1000",
            }
        )
    with pytest.raises(ValueError, match="quote_price_atomic"):
        compare_unit_economics(swaps_per_month=5, quote_price_atomic=-1)


