"""Tests for DeliveryEscrow's pure helpers and its lifecycle (stubbed GenVM).

Run: python3 -m pytest tests/ -q
"""
import json

import pytest

from conftest import (
    CLIENT,
    FREELANCER,
    NOW,
    OWNER,
    PAYOUT,
    delivery_escrow,
    record_transfers,
    set_now,
    set_sender,
    stub_judge,
)

m = delivery_escrow  # the contract module
UserError = delivery_escrow.gl.vm.UserError
ONE_GEN = 10**18


# ---------------------------------------------------------------------------
# Payout tiers
# ---------------------------------------------------------------------------
class TestPayoutSplit:
    @pytest.mark.parametrize(
        "score,delivered,expected",
        [
            (100, True, (5 * ONE_GEN, 0)),
            (80, True, (5 * ONE_GEN, 0)),          # boundary: full payout
            (79, True, (int(5 * ONE_GEN * 0.70), 5 * ONE_GEN - int(5 * ONE_GEN * 0.70))),
            (65, True, (int(5 * ONE_GEN * 0.70), 5 * ONE_GEN - int(5 * ONE_GEN * 0.70))),
            (50, True, (int(5 * ONE_GEN * 0.70), 5 * ONE_GEN - int(5 * ONE_GEN * 0.70))),
            (49, True, (0, 5 * ONE_GEN)),          # boundary: full refund
            (0, True, (0, 5 * ONE_GEN)),
            (95, False, (0, 5 * ONE_GEN)),         # not delivered -> full refund
            (0, False, (0, 5 * ONE_GEN)),
        ],
    )
    def test_tiers(self, score, delivered, expected):
        assert m._payout_split(5 * ONE_GEN, score, delivered) == expected

    def test_no_dust_lost_on_odd_amounts(self):
        # 101 atto at 70%: freelancer 70, client 31 — the two halves sum exactly.
        freelancer, client = m._payout_split(101, 65, True)
        assert (freelancer, client) == (70, 31)
        assert freelancer + client == 101

    def test_split_sums_to_amount(self):
        for amount in (1, 7, 101, 10**18, 7 * 10**18 + 3):
            for score in (0, 49, 50, 65, 79, 80, 100):
                f, c = m._payout_split(amount, score, True)
                assert f + c == amount


# ---------------------------------------------------------------------------
# Verdict parsing
# ---------------------------------------------------------------------------
class TestParseVerdict:
    def test_valid_json(self):
        v = m._parse_verdict('{"delivered": true, "quality_score": 82, "reasoning": "ok"}')
        assert v == {
            "delivered": True,
            "quality_score": 82,
            "cannot_verify": False,
            "reasoning": "ok",
        }

    def test_alias_keys(self):
        assert m._parse_verdict('{"delivered": true, "score": 71}')["quality_score"] == 71
        assert m._parse_verdict('{"delivered": true, "rating": 64}')["quality_score"] == 64
        assert m._parse_verdict('{"delivered": true, "points": 55}')["quality_score"] == 55
        assert m._parse_verdict('{"delivered": true, "value": 90}')["quality_score"] == 90

    def test_string_and_float_scores(self):
        assert m._parse_verdict('{"delivered": true, "quality_score": "82"}')["quality_score"] == 82
        assert m._parse_verdict('{"delivered": true, "quality_score": " 77% "}')["quality_score"] == 77
        assert m._parse_verdict('{"delivered": true, "quality_score": 82.6}')["quality_score"] == 83

    def test_string_booleans(self):
        assert m._parse_verdict('{"delivered": "yes", "quality_score": 60}')["delivered"] is True
        assert m._parse_verdict('{"delivered": "no", "quality_score": 10}')["delivered"] is False

    def test_cannot_verify_normalizes(self):
        v = m._parse_verdict(
            '{"delivered": true, "quality_score": 88, "cannot_verify": true}'
        )
        assert v == {
            "delivered": False,
            "quality_score": 0,
            "cannot_verify": True,
            "reasoning": "",
        }

    @pytest.mark.parametrize(
        "raw",
        [
            "not json at all",
            "[1, 2, 3]",  # not an object
            '{"quality_score": 80}',  # missing delivered
            '{"delivered": true}',  # missing score
            '{"delivered": true, "quality_score": "high"}',  # non-numeric
            '{"delivered": true, "quality_score": true}',  # boolean is not a score
            '{"delivered": true, "quality_score": 101}',  # out of range
            '{"delivered": true, "quality_score": -1}',  # out of range
            '{"delivered": "maybe", "quality_score": 50}',  # bad boolean
        ],
    )
    def test_malformed_raises_llm_error(self, raw):
        with pytest.raises(ValueError) as excinfo:
            m._parse_verdict(raw)
        assert str(excinfo.value).startswith("[LLM_ERROR]")


# ---------------------------------------------------------------------------
# Equivalence: verdict agreement
# ---------------------------------------------------------------------------
class TestVerdictsAgree:
    def _v(self, delivered, score, cannot_verify=False):
        return {
            "delivered": delivered,
            "quality_score": score,
            "cannot_verify": cannot_verify,
            "reasoning": "x",
        }

    def test_identical(self):
        assert m._verdicts_agree(self._v(True, 82), self._v(True, 82))

    def test_reasoning_ignored(self):
        a = self._v(True, 82)
        b = self._v(True, 82)
        a["reasoning"] = "totally different words, same conclusion"
        assert m._verdicts_agree(a, b)

    @pytest.mark.parametrize("diff", [0, 1, 9, 10])
    def test_within_tolerance(self, diff):
        assert m._verdicts_agree(self._v(True, 80), self._v(True, 80 + diff))

    @pytest.mark.parametrize("diff", [11, 20, 50])
    def test_outside_tolerance(self, diff):
        assert not m._verdicts_agree(self._v(True, 80), self._v(True, 80 + diff))

    def test_delivered_must_match(self):
        assert not m._verdicts_agree(self._v(True, 90), self._v(False, 90))

    def test_cannot_verify_must_agree(self):
        assert m._verdicts_agree(self._v(False, 0, True), self._v(False, 0, True))
        assert not m._verdicts_agree(self._v(False, 0, True), self._v(True, 80))
        assert not m._verdicts_agree(self._v(True, 80), self._v(False, 0, True))


# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------
class TestErrorTaxonomy:
    def test_handle_leader_error_passthrough(self):
        assert m._handle_leader_error("[LLM_ERROR] bad json").startswith("[LLM_ERROR]")
        assert m._handle_leader_error("[TRANSIENT] boom").startswith("[TRANSIENT]")

    def test_handle_leader_error_classifies(self):
        assert m._handle_leader_error("HTTP 404 Not Found").startswith("[EXTERNAL]")
        assert m._handle_leader_error("HTTP 403 forbidden").startswith("[EXTERNAL]")
        assert m._handle_leader_error("connection timeout").startswith("[TRANSIENT]")
        assert m._handle_leader_error("HTTP 503 Service Unavailable").startswith("[TRANSIENT]")
        assert m._handle_leader_error("some weird failure").startswith("[TRANSIENT]")

    def test_errors_agree(self):
        # both transient, different wording -> agree
        assert m._errors_agree("connection timeout", "HTTP 503 boom")
        # LLM errors always disagree, even identical
        assert not m._errors_agree("[LLM_ERROR] bad", "[LLM_ERROR] bad")
        # expected must match exactly
        assert m._errors_agree("[EXPECTED] empty", "[EXPECTED] empty")
        assert not m._errors_agree("[EXPECTED] empty", "[EXPECTED] different")
        # external must match exactly
        assert m._errors_agree("[EXTERNAL] 404", "[EXTERNAL] 404")
        assert not m._errors_agree("[EXTERNAL] 404", "[EXTERNAL] 403")
        # different families disagree
        assert not m._errors_agree("[TRANSIENT] x", "[EXTERNAL] 404")


# ---------------------------------------------------------------------------
# Creation validation
# ---------------------------------------------------------------------------
class TestValidateCreate:
    def test_valid(self):
        m._validate_create("brief", ["criterion"], NOW + 100, ONE_GEN, NOW)

    @pytest.mark.parametrize(
        "brief,criteria,deadline,amount",
        [
            ("", ["a"], NOW + 100, ONE_GEN),        # empty brief
            ("   ", ["a"], NOW + 100, ONE_GEN),     # blank brief
            ("brief", [], NOW + 100, ONE_GEN),      # no criteria
            ("brief", [""], NOW + 100, ONE_GEN),    # empty criterion
            ("brief", ["a"], NOW, ONE_GEN),         # deadline not in future
            ("brief", ["a"], NOW - 1, ONE_GEN),     # past deadline
            ("brief", ["a"], NOW + 100, 0),         # zero amount
        ],
    )
    def test_invalid(self, brief, criteria, deadline, amount):
        with pytest.raises(UserError):
            m._validate_create(brief, criteria, deadline, amount, NOW)


# ---------------------------------------------------------------------------
# Lifecycle (stubbed GenVM)
# ---------------------------------------------------------------------------
class TestLifecycle:
    def test_create_happy_path(self, contract, gl):
        set_sender(gl, CLIENT, 5 * ONE_GEN)
        res = contract.create_escrow(
            FREELANCER, PAYOUT, "brief", ["c1"], NOW + 3600
        )
        assert res["escrow_id"] == 1
        assert res["status"] == "funded"
        escrow = contract.get_escrow(1)
        assert escrow["client"] == CLIENT
        assert escrow["freelancer"] == FREELANCER
        assert escrow["atto_amount"] == 5 * ONE_GEN
        assert escrow["status"] == "funded"
        assert contract.counters["escrow_count"] == 1

    def test_create_freelancer_must_differ_from_client(self, contract, gl):
        set_sender(gl, CLIENT, ONE_GEN)
        with pytest.raises(UserError):
            contract.create_escrow(CLIENT, PAYOUT, "brief", ["c1"], NOW + 3600)

    def test_get_escrow_not_found(self, contract):
        with pytest.raises(UserError):
            contract.get_escrow(99)

    def test_submit_delivery_wrong_caller(self, contract, gl, funded):
        set_sender(gl, CLIENT)
        with pytest.raises(UserError):
            contract.submit_delivery(funded, "https://example.com/x", "")

    def test_submit_delivery_bad_url(self, contract, gl, funded):
        set_sender(gl, FREELANCER)
        with pytest.raises(UserError):
            contract.submit_delivery(funded, "not-a-url", "")

    def test_submit_delivery_after_deadline(self, contract, gl, funded):
        set_now(NOW + 86_400)  # exactly at deadline
        set_sender(gl, FREELANCER)
        with pytest.raises(UserError):
            contract.submit_delivery(funded, "https://example.com/x", "")

    def test_submit_delivery_twice_rejected(self, contract, gl, delivered):
        set_sender(gl, FREELANCER)
        with pytest.raises(UserError):
            contract.submit_delivery(delivered, "https://example.com/y", "")

    def test_judgment_before_delivery_rejected(self, contract, funded):
        with pytest.raises(UserError):
            contract.request_judgment(funded)

    def test_full_flow_full_payout(self, contract, gl, delivered, monkeypatch):
        stub_judge(gl, json.dumps({"delivered": True, "quality_score": 85, "reasoning": "all criteria met"}))
        calls = record_transfers(monkeypatch)
        res = contract.request_judgment(delivered)
        assert res["status"] == "judged"
        assert res["quality_score"] == 85
        assert contract.get_escrow(delivered)["status"] == "judged"
        res = contract.settle(delivered)
        assert res["status"] == "settled"
        assert res["freelancer_atto"] == 5 * ONE_GEN
        assert res["client_atto"] == 0
        assert calls == [(PAYOUT, 5 * ONE_GEN)]
        assert contract.get_stats()["total_paid_out_atto"] == 5 * ONE_GEN

    def test_full_flow_partial_payout(self, contract, gl, delivered, monkeypatch):
        stub_judge(gl, json.dumps({"delivered": True, "quality_score": 65, "reasoning": "mostly done"}))
        calls = record_transfers(monkeypatch)
        contract.request_judgment(delivered)
        res = contract.settle(delivered)
        assert res["freelancer_atto"] == int(5 * ONE_GEN * 0.70)
        assert res["client_atto"] == 5 * ONE_GEN - int(5 * ONE_GEN * 0.70)
        assert calls == [
            (PAYOUT, int(5 * ONE_GEN * 0.70)),
            (CLIENT, 5 * ONE_GEN - int(5 * ONE_GEN * 0.70)),
        ]

    def test_full_flow_full_refund_low_score(self, contract, gl, delivered, monkeypatch):
        stub_judge(gl, json.dumps({"delivered": True, "quality_score": 30, "reasoning": "poor"}))
        calls = record_transfers(monkeypatch)
        contract.request_judgment(delivered)
        res = contract.settle(delivered)
        assert res == {
            "escrow_id": delivered,
            "status": "settled",
            "freelancer_atto": 0,
            "client_atto": 5 * ONE_GEN,
        }
        assert calls == [(CLIENT, 5 * ONE_GEN)]

    def test_settle_twice_rejected(self, contract, gl, delivered, monkeypatch):
        stub_judge(gl, json.dumps({"delivered": True, "quality_score": 90, "reasoning": "great"}))
        record_transfers(monkeypatch)
        contract.request_judgment(delivered)
        contract.settle(delivered)
        with pytest.raises(UserError):
            contract.settle(delivered)

    def test_settle_pays_directly_without_setup(self, contract, gl, delivered, monkeypatch):
        stub_judge(gl, json.dumps({"delivered": True, "quality_score": 90, "reasoning": "great"}))
        calls = record_transfers(monkeypatch)
        contract.request_judgment(delivered)
        res = contract.settle(delivered)
        assert res["status"] == "settled"
        assert res["freelancer_atto"] == 5 * ONE_GEN
        assert calls == [(PAYOUT, 5 * ONE_GEN)]

    def test_cannot_verify_three_times_then_refund(
        self, contract, gl, delivered, monkeypatch
    ):
        stub_judge(
            gl,
            json.dumps(
                {
                    "delivered": False,
                    "quality_score": 0,
                    "cannot_verify": True,
                    "reasoning": "page unreadable",
                }
            ),
        )
        calls = record_transfers(monkeypatch)
        for attempt in (1, 2):
            res = contract.request_judgment(delivered)
            assert res["status"] == "delivered"
            assert res["attempts"] == attempt
        res = contract.request_judgment(delivered)
        assert res["status"] == "unverifiable"
        assert res["attempts"] == 3
        assert calls == []  # nobody paid on an unverifiable judgment
        set_sender(gl, CLIENT)
        res = contract.claim_refund(delivered)
        assert res["status"] == "refunded"
        assert res["refunded_atto"] == 5 * ONE_GEN
        assert calls == [(CLIENT, 5 * ONE_GEN)]

    def test_refund_after_deadline_no_delivery(self, contract, gl, funded, monkeypatch):
        calls = record_transfers(monkeypatch)
        set_now(NOW + 86_400 + 1)
        set_sender(gl, CLIENT)
        res = contract.claim_refund(funded)
        assert res["status"] == "refunded"
        assert calls == [(CLIENT, 5 * ONE_GEN)]

    def test_refund_too_early_rejected(self, contract, gl, funded):
        set_sender(gl, CLIENT)
        with pytest.raises(UserError):
            contract.claim_refund(funded)

    def test_refund_wrong_caller(self, contract, gl, funded):
        set_now(NOW + 86_400 + 1)
        set_sender(gl, FREELANCER)
        with pytest.raises(UserError):
            contract.claim_refund(funded)

    def test_consensus_disagreement_counts_as_attempt(
        self, contract, gl, delivered, monkeypatch
    ):
        # Leader says 90, validator re-run says 50 -> >10 apart -> no consensus.
        answers = iter(
            [
                json.dumps({"delivered": True, "quality_score": 90, "reasoning": "a"}),
                json.dumps({"delivered": True, "quality_score": 50, "reasoning": "b"}),
            ]
        )
        gl.nondet.web.render = lambda url, mode="text": "content"
        gl.nondet.exec_prompt = lambda prompt, response_format="json": next(answers)
        res = contract.request_judgment(delivered)
        assert res["status"] == "delivered"  # still open, attempt consumed
        assert res["attempts"] == 1
        assert res["error"].startswith("[TRANSIENT]")

    def test_fetch_failure_counts_as_attempt(self, contract, gl, delivered):
        gl.nondet.web.render = lambda url, mode="text": (_ for _ in ()).throw(
            RuntimeError("HTTP 503 Service Unavailable")
        )
        res = contract.request_judgment(delivered)
        assert res["status"] == "delivered"
        assert res["attempts"] == 1
        assert res["error"].startswith("[TRANSIENT]")

    def test_list_and_stats(self, contract, gl, funded):
        set_sender(gl, CLIENT, 2 * ONE_GEN)
        contract.create_escrow(FREELANCER, PAYOUT, "second job", ["c1"], NOW + 3600)
        page = contract.list_escrows(0, 100)
        assert page["total"] == 2
        assert [e["id"] for e in page["escrows"]] == [1, 2]
        page = contract.list_escrows(1, 100)
        assert [e["id"] for e in page["escrows"]] == [2]
        stats = contract.get_stats()
        assert stats["total_escrows"] == 2
        assert stats["by_status"]["funded"] == 2
        assert stats["total_locked_atto"] == 7 * ONE_GEN

    def test_no_arbiter_setup_needed(self, contract):
        # The old Arbiter machinery is gone: no set_arbiter method and no
        # arbiter storage. Payouts go out directly on settle / claim_refund.
        assert not hasattr(contract, "set_arbiter")
        assert not hasattr(contract, "arbiter")
        assert not hasattr(contract, "arbiter_set")
