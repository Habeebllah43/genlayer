# v0.2.16
# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }

# Always put above lines as first in the contract file.
# (GenLayer Studio / GenVM require this runner header; without it schema
# loading fails with "invalid contract absent_runner_comment".)
"""
DeliveryEscrow — an AI-judged freelance delivery escrow.

A reusable GenLayer Intelligent Contract primitive: a client locks GEN in
escrow for a job, a freelancer submits a delivery URL, GenLayer's AI
validators fetch the *real* delivery, judge it against the written brief and
acceptance criteria, agree on a structured verdict through consensus, and the
contract settles deterministically from that verdict — pay the freelancer,
split the payment, or refund the client.

Why this needs GenLayer (and why a normal smart contract cannot do it):
a normal contract cannot read a web page, cannot tell whether delivered work
matches a written brief, and cannot resolve a subjective question such as
"was the work actually done?". Here the validators do that reading and
judging, and Optimistic Democracy turns their agreement into an on-chain
verdict the contract can settle from. The money logic itself — tiers,
splits, refunds — is fully deterministic and runs on the stored verdict
only; the AI never touches money directly.

State machine (status):
    funded -> delivered -> judged -> settled
    funded -> refunded          (deadline passed, no delivery)
    delivered -> unverifiable -> refunded   (3 failed judgment attempts)

Money is u256 atto-GEN (1 GEN = 10**18 atto). No floats are ever used for
money. Payout tiers, from the stored verdict:
    quality >= 80            -> 100% to the freelancer
    50 <= quality < 80       -> 70% freelancer / 30% refund to the client
    quality < 50 or not done -> 100% refund to the client

Fail closed: if the delivery cannot be fetched, or the validators cannot
verify it, nobody is paid. The escrow stays open for re-judgment (max 3
attempts) and then refunds the client.

Payouts are direct transfers of the GEN locked in the contract by the
payable create_escrow call. They go out through the faucet-pattern EVM
interface below (_PayoutTarget): the @gl.evm.contract_interface decorator
provides emit_transfer, which takes only value=, and the destination is
the address passed to the constructor. No Arbiter or setup call is needed.
"""
import json
from dataclasses import dataclass

from genlayer import *

# ---------------------------------------------------------------------------
# Error taxonomy for consensus (see CONSENSUS.md for the full design)
#
# The leader's nondeterministic work can fail in four different ways, and
# the validators must agree on *what kind* of failure it was — not on the
# exact wording of an error message, which differs between runs.
# ---------------------------------------------------------------------------
ERROR_EXPECTED = "[EXPECTED]"      # business-logic failure, exact match
ERROR_EXTERNAL = "[EXTERNAL]"      # bad deliverable / 4xx, exact match
ERROR_TRANSIENT = "[TRANSIENT]"     # network / 5xx, agree if both transient
ERROR_LLM = "[LLM_ERROR]"          # malformed LLM output, always disagree

MAX_JUDGMENT_ATTEMPTS = 3
SCORE_TOLERANCE = 10               # validators must agree within +/-10
PAY_TIER_FULL = 80                 # quality >= 80  -> 100% to freelancer
PAY_TIER_PARTIAL = 50              # quality >= 50  -> 70/30 split
PARTIAL_FREELANCER_PCT = 70        # freelancer's share of a partial payout
MAX_JUDGMENT_CONTENT_CHARS = 12000  # cap on fetched content sent to the LLM


# ---------------------------------------------------------------------------
# Pure helpers (module level, so they are unit-testable without GenVM)
# ---------------------------------------------------------------------------
def _coerce_score(value):
    """Coerce an LLM score value to an int in 0..100.

    Accepts ints, floats, and numeric strings (with whitespace and a
    trailing "%" tolerated). Raises ValueError(ERROR_LLM ...) on anything
    missing, non-numeric, boolean, or out of range.
    """
    if isinstance(value, bool):
        raise ValueError(ERROR_LLM + " quality_score must be a number, got a boolean")
    if isinstance(value, (int, float)):
        num = float(value)
    elif isinstance(value, str):
        text = value.strip().rstrip("%").strip()
        try:
            num = float(text)
        except ValueError:
            raise ValueError(ERROR_LLM + " quality_score is not numeric: %r" % (value,))
    else:
        raise ValueError(ERROR_LLM + " quality_score has an unsupported type")
    score = int(round(num))
    if score < 0 or score > 100:
        raise ValueError(ERROR_LLM + " quality_score out of range 0-100: %r" % (value,))
    return score


def _parse_verdict(raw):
    """Parse and validate a judge's verdict into a normalized dict.

    Accepts a JSON string or an already-parsed dict. The judge must return
    an object with:
        delivered:      bool (or "true"/"false"/"yes"/"no")
        quality_score:  number 0..100 (aliases: score, rating, points, value)
        reasoning:      string (optional)
        cannot_verify:  bool (optional, default false)

    A verdict of cannot_verify=True is normalized to delivered=False,
    quality_score=0. Anything malformed raises ValueError(ERROR_LLM ...),
    which the consensus error taxonomy treats as "always disagree" so the
    leader is rotated instead of a bad verdict being settled on.
    """
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw)
        except Exception:
            raise ValueError(ERROR_LLM + " verdict is not valid JSON")
    if not isinstance(data, dict):
        raise ValueError(ERROR_LLM + " verdict is not a JSON object")

    cannot_verify = data.get("cannot_verify", False)
    if isinstance(cannot_verify, str):
        cannot_verify = cannot_verify.strip().lower() in ("true", "yes", "1")
    cannot_verify = bool(cannot_verify)
    if cannot_verify:
        reasoning = data.get("reasoning", "")
        return {
            "delivered": False,
            "quality_score": 0,
            "cannot_verify": True,
            "reasoning": reasoning if isinstance(reasoning, str) else str(reasoning),
        }

    if "delivered" not in data:
        raise ValueError(ERROR_LLM + " verdict is missing 'delivered'")
    delivered = data["delivered"]
    if isinstance(delivered, str):
        lowered = delivered.strip().lower()
        if lowered in ("true", "yes", "delivered", "1"):
            delivered = True
        elif lowered in ("false", "no", "not delivered", "not_delivered", "0"):
            delivered = False
        else:
            raise ValueError(ERROR_LLM + " 'delivered' is not a boolean")
    elif not isinstance(delivered, bool):
        raise ValueError(ERROR_LLM + " 'delivered' is not a boolean")

    score = None
    for key in ("quality_score", "score", "rating", "points", "value"):
        if key in data and data[key] is not None:
            score = _coerce_score(data[key])
            break
    if score is None:
        raise ValueError(ERROR_LLM + " verdict is missing a numeric quality score")

    reasoning = data.get("reasoning", "")
    if not isinstance(reasoning, str):
        reasoning = str(reasoning)
    return {
        "delivered": delivered,
        "quality_score": score,
        "cannot_verify": False,
        "reasoning": reasoning,
    }


def _verdicts_agree(a, b):
    """Equivalence check for two structured verdicts (the consensus rule).

    - If either side says cannot_verify, both must agree on it.
    - Otherwise `delivered` must match exactly.
    - `quality_score` must agree within +/- SCORE_TOLERANCE.
    Reasoning text is never compared: two validators may word the same
    conclusion differently, and that is not a disagreement.
    """
    if a.get("cannot_verify") or b.get("cannot_verify"):
        return bool(a.get("cannot_verify")) == bool(b.get("cannot_verify"))
    if a.get("delivered") != b.get("delivered"):
        return False
    return abs(int(a.get("quality_score", 0)) - int(b.get("quality_score", 0))) <= SCORE_TOLERANCE


def _payout_split(atto_amount, quality_score, delivered):
    """Deterministic payout tiers. Returns (freelancer_atto, client_atto).

    Integer atto math only — no floats, no dust lost: the freelancer's share
    is computed first and the client gets the remainder.
    """
    if not delivered or quality_score < PAY_TIER_PARTIAL:
        return (0, atto_amount)
    if quality_score >= PAY_TIER_FULL:
        return (atto_amount, 0)
    freelancer = (atto_amount * PARTIAL_FREELANCER_PCT) // 100
    return (freelancer, atto_amount - freelancer)


def _handle_leader_error(err):
    """Normalize a leader-side failure to one of the error-taxonomy strings.

    Already-prefixed errors pass through unchanged. HTTP 4xx / "not found" /
    "forbidden" become [EXTERNAL]; network and 5xx failures become
    [TRANSIENT]; anything unrecognized is treated as [TRANSIENT] (retryable)
    rather than failing the escrow permanently.
    """
    text = err if isinstance(err, str) else str(err)
    for prefix in (ERROR_EXPECTED, ERROR_EXTERNAL, ERROR_TRANSIENT, ERROR_LLM):
        if text.startswith(prefix):
            return text
    lowered = text.lower()
    if any(code in lowered for code in ("404", "403", "400", "401", "410", "not found", "forbidden", "gone")):
        return ERROR_EXTERNAL + " " + text
    if any(code in lowered for code in ("500", "502", "503", "504", "timeout", "timed out", "connection", "network", "temporarily", "unreachable")):
        return ERROR_TRANSIENT + " " + text
    return ERROR_TRANSIENT + " " + text


def _error_prefix(normalized):
    """Return the "[...]" prefix token of a normalized error string."""
    if "]" in normalized:
        return normalized[: normalized.index("]") + 1]
    return normalized


def _errors_agree(a, b):
    """Do two leader-side failures agree under the error taxonomy?

    - [LLM_ERROR] always disagrees (forces leader rotation).
    - [TRANSIENT] agrees with [TRANSIENT], whatever the wording.
    - [EXPECTED] / [EXTERNAL] must match exactly.
    """
    na = _handle_leader_error(a)
    nb = _handle_leader_error(b)
    pa = _error_prefix(na)
    pb = _error_prefix(nb)
    if pa == ERROR_LLM or pb == ERROR_LLM:
        return False
    if pa != pb:
        return False
    if pa == ERROR_TRANSIENT:
        return True
    return na == nb


def _validate_create(brief, criteria, deadline, amount, now):
    """Business-rule checks for create_escrow. Raises gl.UserError."""
    if amount <= 0:
        raise gl.UserError("escrow amount must be greater than zero")
    if not brief or not brief.strip():
        raise gl.UserError("brief must not be empty")
    if criteria is None or len(criteria) == 0:
        raise gl.UserError("at least one acceptance criterion is required")
    for criterion in criteria:
        if criterion is None or not str(criterion).strip():
            raise gl.UserError("acceptance criteria must not be empty")
    if deadline <= now:
        raise gl.UserError("deadline must be in the future")


def _require_status(escrow, *allowed):
    if escrow.status not in allowed:
        raise gl.UserError(
            "escrow %d is '%s', expected one of %s"
            % (escrow.id, escrow.status, ", ".join(allowed))
        )


def _build_judgment_prompt(escrow, content):
    """The judge prompt: brief + criteria + fetched delivery, strict JSON out.

    The prompt is deliberately narrow — it asks for one JSON object and
    nothing else, so the verdict can be parsed and compared deterministically.
    """
    criteria_lines = "\n".join(
        "%d. %s" % (i + 1, criterion) for i, criterion in enumerate(escrow.criteria)
    )
    trimmed = content[:MAX_JUDGMENT_CONTENT_CHARS]
    return (
        "You are an impartial judge of freelance work. A client posted a job "
        "brief with acceptance criteria. A freelancer submitted a delivery "
        "at a URL. Read the delivered content below and judge it.\n\n"
        "BRIEF:\n%s\n\n"
        "ACCEPTANCE CRITERIA:\n%s\n\n"
        "DELIVERED CONTENT (fetched from %s):\n%s\n\n"
        "Return ONLY a JSON object with exactly these fields, nothing else:\n"
        '{"delivered": true or false, '
        '"quality_score": an integer 0-100, '
        '"reasoning": a short explanation, '
        '"cannot_verify": true or false}\n\n'
        "Rules: delivered is true only if the delivery genuinely addresses "
        "the brief. quality_score is how much of the criteria it meets "
        "(100 = every criterion fully met). If the content is unreadable, "
        "unrelated to the brief, or there is not enough evidence to judge, "
        "set cannot_verify to true, delivered to false and quality_score to 0."
        % (escrow.brief, criteria_lines, escrow.deliverable_url, trimmed)
    )


# ---------------------------------------------------------------------------
# Storage records
# ---------------------------------------------------------------------------
@allow_storage
@dataclass
class Escrow:
    id: u256
    client: Address
    freelancer: Address
    payout_wallet: Address
    atto_amount: u256
    brief: str
    criteria: DynArray[str]
    deliverable_url: str
    deliverable_notes: str
    deadline: u256
    status: str
    verdict: str
    quality_score: u256
    attempts: u256
    created_at: str


# ---------------------------------------------------------------------------
# Payout interface (faucet pattern)
#
# Payouts are direct transfers of the GEN locked in the contract by the
# payable create_escrow call. The @gl.evm.contract_interface decorator
# provides emit_transfer: it takes only value=, and the destination is the
# address passed to the constructor. No methods are declared here — the
# nested View and Write classes are required by the decorator, and
# declaring emit_transfer (or any method) breaks schema generation in
# GenLayer Studio ("'IEscrowArbiter' has no attribute 'View'"). This
# contract never holds private keys and never pays out without a stored,
# consensus-reached verdict (or an explicit refund state).
# ---------------------------------------------------------------------------
@gl.evm.contract_interface
class _PayoutTarget:
    class View:
        pass

    class Write:
        pass


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------
class DeliveryEscrow(gl.Contract):
    owner: Address
    escrows: TreeMap[u256, Escrow]
    escrow_order: DynArray[u256]
    counters: TreeMap[str, u256]
    total_paid_out_atto: u256
    total_refunded_atto: u256

    def __init__(self, owner_address: str):
        # The owner is passed in as a constructor argument on purpose:
        # reading the sender inside __init__ breaks deployment on
        # GenLayer Studio. When deploying, pass your own wallet address
        # as a hex string — GenLayer constructors never take Address
        # parameters, so it is converted to Address here.
        self.owner = Address(owner_address)
        self.counters["escrow_count"] = 0
        self.counters["settled_count"] = 0
        self.counters["refunded_count"] = 0
        self.total_paid_out_atto = 0
        self.total_refunded_atto = 0

    # -- internal -----------------------------------------------------------
    def _now(self) -> u256:
        # Block timestamp, in seconds. Used for deadline checks only.
        return gl.block.timestamp

    def _get_escrow(self, escrow_id: u256) -> Escrow:
        if escrow_id <= 0 or escrow_id > self.counters["escrow_count"]:
            raise gl.UserError("escrow not found")
        return self.escrows[escrow_id]

    def _pay(self, to: Address, amount: u256) -> None:
        if amount <= 0:
            return
        _PayoutTarget(to).emit_transfer(value=amount)

    def _leader_judgment(self, escrow: Escrow) -> str:
        """Fetch the delivery and judge it. Runs on leader AND validators.

        Returns the normalized verdict as a sorted JSON string. Raises
        ValueError with an error-taxonomy prefix on any failure, so the
        validator side can classify and compare the failure.
        """
        try:
            content = gl.nondet.web.render(escrow.deliverable_url, mode="text")
        except Exception as e:
            raise ValueError(_handle_leader_error(e))
        if not content or not content.strip():
            raise ValueError(ERROR_EXTERNAL + " deliverable fetched but was empty")
        prompt = _build_judgment_prompt(escrow, content)
        try:
            raw = gl.nondet.exec_prompt(prompt, response_format="json")
        except Exception as e:
            raise ValueError(ERROR_LLM + " exec_prompt failed: " + str(e))
        verdict = _parse_verdict(raw)
        return json.dumps(verdict, sort_keys=True)

    def _validate_judgment(self, escrow: Escrow, leader_result) -> bool:
        """Custom equivalence validator for the judgment (see CONSENSUS.md).

        Re-runs the leader's work and compares the STRUCTURED verdict —
        never the raw text, which two honest validators will word
        differently. If the leader failed, its normalized error is compared
        against our own failure under the error taxonomy.
        """
        if isinstance(leader_result, str) and leader_result.startswith("["):
            try:
                self._leader_judgment(escrow)
            except Exception as e:
                return _errors_agree(leader_result, e)
            return False
        try:
            mine = self._leader_judgment(escrow)
        except Exception:
            return False
        try:
            theirs = _parse_verdict(leader_result)
            mine_v = _parse_verdict(mine)
        except Exception:
            return False
        return _verdicts_agree(theirs, mine_v)

    def _run_judgment(self, escrow: Escrow) -> str:
        """Run the leader/validator consensus for one judgment."""
        def leader_fn():
            return self._leader_judgment(escrow)

        def validator_fn(leader_result):
            return self._validate_judgment(escrow, leader_result)

        return gl.vm.run_nondet_unsafe(leader_fn, validator_fn)

    # -- escrow lifecycle ---------------------------------------------------
    @gl.public.write.payable
    def create_escrow(
        self,
        freelancer: str,
        payout_wallet: str,
        brief: str,
        criteria: DynArray[str],
        deadline: u256,
    ) -> dict:
        """Lock GEN in escrow for a job. The attached GEN is the escrow."""
        client = gl.message.sender_account
        amount = gl.message.value
        # GenLayer public methods never take Address parameters: addresses
        # arrive as hex strings and are converted inside the method.
        freelancer = Address(freelancer)
        payout_wallet = Address(payout_wallet)
        if freelancer == client:
            raise gl.UserError("freelancer must be different from the client")
        _validate_create(brief, criteria, deadline, amount, self._now())
        escrow_id = self.counters["escrow_count"] + 1
        self.counters["escrow_count"] = escrow_id
        escrow = Escrow(
            id=escrow_id,
            client=client,
            freelancer=freelancer,
            payout_wallet=payout_wallet,
            atto_amount=amount,
            brief=brief,
            criteria=criteria,
            deliverable_url="",
            deliverable_notes="",
            deadline=deadline,
            status="funded",
            verdict="",
            quality_score=0,
            attempts=0,
            created_at=str(self._now()),
        )
        self.escrows[escrow_id] = escrow
        self.escrow_order.append(escrow_id)
        return {"escrow_id": escrow_id, "status": "funded", "atto_amount": amount}

    @gl.public.write
    def submit_delivery(self, escrow_id: u256, url: str, notes: str) -> dict:
        """Freelancer submits the delivery URL. Only while funded, before deadline."""
        escrow = self._get_escrow(escrow_id)
        if gl.message.sender_account != escrow.freelancer:
            raise gl.UserError("only the named freelancer can submit a delivery")
        _require_status(escrow, "funded")
        if self._now() >= escrow.deadline:
            raise gl.UserError("deadline has passed; delivery is no longer accepted")
        if not url or not (url.startswith("http://") or url.startswith("https://")):
            raise gl.UserError("deliverable_url must be an http(s) URL")
        escrow.deliverable_url = url
        escrow.deliverable_notes = notes
        escrow.status = "delivered"
        self.escrows[escrow_id] = escrow
        return {"escrow_id": escrow_id, "status": "delivered"}

    @gl.public.write
    def request_judgment(self, escrow_id: u256) -> dict:
        """Anyone may call once a delivery exists. Runs validator consensus.

        On a reached verdict the escrow becomes "judged". On a
        cannot_verify verdict, or a leader-side failure the validators agree
        is transient/external, the attempt is counted and the escrow stays
        open ("delivered") until the 3rd failure, when it becomes
        "unverifiable" and the client may claim a refund. Nobody is paid on
        a failed or unverifiable judgment.
        """
        escrow = self._get_escrow(escrow_id)
        _require_status(escrow, "delivered")
        if escrow.attempts >= MAX_JUDGMENT_ATTEMPTS:
            raise gl.UserError("judgment attempts exhausted")
        try:
            verdict_json = self._run_judgment(escrow)
        except Exception as e:
            normalized = _handle_leader_error(e)
            escrow.attempts = escrow.attempts + 1
            if escrow.attempts >= MAX_JUDGMENT_ATTEMPTS:
                escrow.status = "unverifiable"
            self.escrows[escrow_id] = escrow
            return {
                "escrow_id": escrow_id,
                "status": escrow.status,
                "attempts": escrow.attempts,
                "error": normalized,
            }
        verdict = _parse_verdict(verdict_json)
        escrow.verdict = json.dumps(verdict, sort_keys=True)
        escrow.quality_score = verdict["quality_score"]
        if verdict["cannot_verify"]:
            escrow.attempts = escrow.attempts + 1
            if escrow.attempts >= MAX_JUDGMENT_ATTEMPTS:
                escrow.status = "unverifiable"
            else:
                escrow.status = "delivered"
            self.escrows[escrow_id] = escrow
            return {
                "escrow_id": escrow_id,
                "status": escrow.status,
                "attempts": escrow.attempts,
                "cannot_verify": True,
            }
        escrow.status = "judged"
        self.escrows[escrow_id] = escrow
        return {
            "escrow_id": escrow_id,
            "status": "judged",
            "delivered": verdict["delivered"],
            "quality_score": verdict["quality_score"],
        }

    @gl.public.write
    def settle(self, escrow_id: u256) -> dict:
        """Settle deterministically from the stored verdict. Once only.

        No LLM call happens here: the verdict was already agreed by
        consensus in request_judgment, so settlement is pure arithmetic.
        """
        escrow = self._get_escrow(escrow_id)
        _require_status(escrow, "judged")
        verdict = _parse_verdict(escrow.verdict)
        freelancer_amount, client_amount = _payout_split(
            escrow.atto_amount, verdict["quality_score"], verdict["delivered"]
        )
        if freelancer_amount > 0:
            self._pay(escrow.payout_wallet, freelancer_amount)
        if client_amount > 0:
            self._pay(escrow.client, client_amount)
        self.total_paid_out_atto = self.total_paid_out_atto + freelancer_amount
        self.total_refunded_atto = self.total_refunded_atto + client_amount
        self.counters["settled_count"] = self.counters["settled_count"] + 1
        escrow.status = "settled"
        self.escrows[escrow_id] = escrow
        return {
            "escrow_id": escrow_id,
            "status": "settled",
            "freelancer_atto": freelancer_amount,
            "client_atto": client_amount,
        }

    @gl.public.write
    def claim_refund(self, escrow_id: u256) -> dict:
        """Client-only refund: unverifiable escrow, or funded escrow whose
        deadline passed with no delivery. Pays the full locked amount back."""
        escrow = self._get_escrow(escrow_id)
        if gl.message.sender_account != escrow.client:
            raise gl.UserError("only the client can claim a refund")
        now = self._now()
        if escrow.status == "unverifiable":
            pass
        elif escrow.status == "funded" and now >= escrow.deadline:
            pass
        else:
            raise gl.UserError("refund is not available for this escrow in its current state")
        self._pay(escrow.client, escrow.atto_amount)
        self.total_refunded_atto = self.total_refunded_atto + escrow.atto_amount
        self.counters["refunded_count"] = self.counters["refunded_count"] + 1
        escrow.status = "refunded"
        self.escrows[escrow_id] = escrow
        return {
            "escrow_id": escrow_id,
            "status": "refunded",
            "refunded_atto": escrow.atto_amount,
        }

    # -- views --------------------------------------------------------------
    @gl.public.view
    def get_escrow(self, escrow_id: u256) -> dict:
        escrow = self._get_escrow(escrow_id)
        return _escrow_to_dict(escrow)

    @gl.public.view
    def list_escrows(self, offset: u256, limit: u256) -> dict:
        """Paginated list (max 100 per call), oldest first."""
        total = self.counters["escrow_count"]
        if limit > 100:
            limit = 100
        end = offset + limit
        if end > total:
            end = total
        out = []
        i = offset + 1
        while i <= end:
            out.append(_escrow_to_dict(self.escrows[i]))
            i = i + 1
        return {"total": total, "offset": offset, "escrows": out}

    @gl.public.view
    def get_stats(self) -> dict:
        """Counts by status, GEN currently locked, and GEN paid/refunded."""
        counts = {
            "funded": 0,
            "delivered": 0,
            "judged": 0,
            "settled": 0,
            "refunded": 0,
            "unverifiable": 0,
        }
        locked = 0
        for escrow_id in self.escrow_order:
            escrow = self.escrows[escrow_id]
            if escrow.status in counts:
                counts[escrow.status] = counts[escrow.status] + 1
            if escrow.status in ("funded", "delivered", "judged"):
                locked = locked + escrow.atto_amount
        return {
            "total_escrows": self.counters["escrow_count"],
            "by_status": counts,
            "total_locked_atto": locked,
            "total_paid_out_atto": self.total_paid_out_atto,
            "total_refunded_atto": self.total_refunded_atto,
        }


def _escrow_to_dict(escrow: Escrow) -> dict:
    """Public-safe serialization of an escrow (criteria as a plain list)."""
    return {
        "id": escrow.id,
        "client": escrow.client,
        "freelancer": escrow.freelancer,
        "payout_wallet": escrow.payout_wallet,
        "atto_amount": escrow.atto_amount,
        "brief": escrow.brief,
        "criteria": list(escrow.criteria),
        "deliverable_url": escrow.deliverable_url,
        "deliverable_notes": escrow.deliverable_notes,
        "deadline": escrow.deadline,
        "status": escrow.status,
        "verdict": escrow.verdict,
        "quality_score": escrow.quality_score,
        "attempts": escrow.attempts,
        "created_at": escrow.created_at,
    }
