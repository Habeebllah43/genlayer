# DeliveryEscrow — AI-judged freelance delivery escrow

A standalone **GenLayer Intelligent Contract** (Python, GenVM). It is a
reusable primitive — escrow + AI judgment + deterministic settlement — not
a demo app and not a hello-world contract. There is no frontend in this
repo: the contract *is* the submission.

## The problem

Freelance work is paid on trust. A client pays upfront and hopes the work
arrives; a freelancer works first and hopes to be paid. Escrow fixes the
"who goes first" problem, but a normal smart contract cannot answer the
question that actually matters: **was the work delivered, and was it any
good?** That question needs someone to read the delivery and compare it
with the brief — something a deterministic contract cannot do at all.

## What this contract does

1. **Client locks GEN in escrow** for a job (`create_escrow`, payable),
   with a written brief, acceptance criteria, a deadline, and the
   freelancer's payout wallet.
2. **Freelancer submits a delivery URL** (`submit_delivery`) before the
   deadline.
3. **Anyone calls `request_judgment`**. GenLayer's AI validators fetch the
   *real* delivery from the URL, judge it against the brief and criteria,
   and agree on a structured verdict through consensus. The verdict and
   quality score are stored on-chain.
4. **`settle` pays out deterministically from the stored verdict** — no
   LLM call at settlement time:
   - quality **≥ 80** → 100% to the freelancer
   - quality **50–79** → 70% freelancer / 30% refund to the client
   - quality **< 50**, or not delivered → 100% refund to the client
5. **Refunds are fail-closed.** If the delivery cannot be fetched or the
   validators cannot verify it, nobody is paid. The escrow stays open for
   re-judgment (max 3 attempts), then becomes `unverifiable` and the
   client can claim a full refund (`claim_refund`). A funded escrow whose
   deadline passes with no delivery is also refundable.

## Why it needs GenLayer consensus

| Question | Normal contract | DeliveryEscrow |
|---|---|---|
| Read a web page? | No | Validators fetch the real delivery URL |
| Judge work against a brief? | No | Validators score it against the criteria |
| Resolve "was it delivered?" | No | Consensus on a structured verdict |
| Move the money? | Yes | Deterministic tiers on the stored verdict |

The AI never touches money directly: it only produces a verdict. The
money logic is pure integer arithmetic on that verdict, so settlement is
reproducible and auditable.

## State design

`contracts/delivery_escrow.py` — one contract class, `DeliveryEscrow`.

- **Storage** (class-level annotations only): `owner`,
  `escrows: TreeMap[u256, Escrow]`, `escrow_order: DynArray[u256]`,
  `counters: TreeMap[str, u256]`, `total_paid_out_atto`,
  `total_refunded_atto`. No Python dict/list in storage; money is `u256`
  atto-GEN (1 GEN = 10¹⁸ atto), never floats; status is `str`, never Enum.
- **Escrow record** (`@allow_storage @dataclass`): id, client, freelancer,
  payout_wallet, atto_amount, brief, criteria, deliverable_url,
  deliverable_notes, deadline, status, verdict (stored JSON), quality_score,
  attempts, created_at.
- **Status machine:** `funded → delivered → judged → settled`;
  `funded → refunded` (deadline, no delivery);
  `delivered → unverifiable → refunded` (3 failed judgments).
- **Access control** via `gl.message.sender_address` (an `Address`,
  compared directly against the stored client/freelancer), raising
  `gl.UserError` for business errors (wrong caller, wrong status, past
  deadline, zero amount, empty brief).
- **Payouts** are direct transfers of the GEN locked in the contract
  by the payable `create_escrow` call. They go out through
  `_PayoutTarget.emit_transfer` on an `@gl.evm.contract_interface`
  (the faucet pattern: `emit_transfer` takes only `value=`, and the
  destination is the address passed to the constructor). No Arbiter
  and no setup call are needed — `settle` and `claim_refund` pay out
  as soon as they run. See `DEPLOY.md`.

## How consensus and equivalence work here

All web fetches and LLM calls happen inside a leader function passed to
`gl.vm.run_nondet_unsafe(leader_fn, validator_fn)`. The leader fetches
the delivery with `gl.nondet.web.render(url, mode="text")` and judges it
with `gl.nondet.exec_prompt(prompt, response_format="json")`.

The validator is **custom, not `strict_eq`** — strict equality always
fails consensus on LLM output, because two honest validators word the
same conclusion differently. Instead the validator re-runs the leader's
work and compares the *structured* verdict. It receives the leader's
outcome as a `gl.vm.Result`: on `gl.vm.Return` the verdict JSON is on
`.calldata`; on any other Result the leader failed, and the validator
re-runs the leader's work and compares the failure under the error
taxonomy below:

- `delivered` must match exactly;
- `quality_score` must agree within **±10**;
- if either side says `cannot_verify`, both must agree on it;
- reasoning text is never compared.

Leader-side failures are classified with an error taxonomy
(`[EXPECTED]` business logic, `[EXTERNAL]` 4xx, `[TRANSIENT]` network/5xx,
`[LLM_ERROR]` malformed LLM output). Transient failures agree with each
other, expected/external failures must match exactly, and LLM errors
always disagree so the leader is rotated. Full design: `CONSENSUS.md`.

## Worked example

**Brief** (client locks 5 GEN, deadline +1 day):

> Build a landing page for a Lagos bakery: menu, opening hours, WhatsApp
> order button. Single self-contained index.html, mobile-friendly.

**Criteria:** single self-contained `index.html`; menu with ≥ 6 items and
prices; opening-hours section; WhatsApp order button linking to `wa.me`.

**Delivery:** `https://example.com/bakery/index.html` — a real page with a
menu of 8 items, hours, and a working WhatsApp button.

**Expected verdict:** `{"delivered": true, "quality_score": 85, ...}` —
all four criteria met. **Settlement:** 85 ≥ 80, so the freelancer's
payout wallet receives the full **5 GEN**; the client gets 0 back.

Two more outcomes from the same setup:

- The page is missing the WhatsApp button and has only 4 menu items:
  verdict `delivered: true, quality_score: 65` → **3.5 GEN** to the
  freelancer, **1.5 GEN** refunded to the client.
- The URL returns 404: the fetch fails, the attempt is counted, nobody
  is paid. After 3 failed attempts the escrow is `unverifiable` and the
  client claims the full **5 GEN** back.

## Files

| File | What it is |
|---|---|
| `contracts/delivery_escrow.py` | The contract (675 lines, commented) |
| `tests/test_delivery_escrow.py` | 68 pytest tests (see below) |
| `tests/conftest.py` | Minimal fake `genlayer` module (stub) |
| `CONSENSUS.md` | Equivalence design in depth |
| `DEPLOY.md` | Studio → testnet → portal, step by step |
| `pyproject.toml` / `requirements.txt` | pytest only |

## Tests

The `genlayer` package is not installed in this environment (see Linting
below), so the contract is structured with module-level pure helpers
(`_payout_split`, `_parse_verdict`, `_verdicts_agree`,
`_validate_create`, `_handle_leader_error`, `_errors_agree`) and
`tests/conftest.py` stubs the `genlayer` module (a minimal fake `gl`
with Contract, TreeMap, DynArray, public decorators, UserError, message,
nondet, eq_principle, vm with `gl.vm.Return` / `gl.vm.UserError` Result
types, and the stdlib clock pinned to the transaction datetime) so the
contract file imports cleanly and the lifecycle runs end-to-end under
the stub — including the consensus flow (leader + validator
agreement/disagreement) and payout calls, which are recorded by
patching `_PayoutTarget.emit_transfer`.

```sh
pip install -r requirements.txt
python3 -m pytest tests/ -q
# 68 passed
```

Coverage: payout tiers (100 / 70–30 / 0 at scores 80/79/50/49, not
delivered → full refund, no dust lost on odd amounts), verdict parsing
(aliases, string/float scores, malformed JSON → `[LLM_ERROR]`),
agreement rules (±10 tolerance, pass at 9 and 10, fail at 11,
`cannot_verify` rules), error taxonomy, creation guards (zero amount,
empty brief, no criteria, past deadline), and the full lifecycle
(create → deliver → judge → settle, refunds, re-judgment attempts,
consensus disagreement, fetch failure, pagination, stats).

## Linting

`genvm-lint` is **not available in this environment** and the contract
could not be lint-checked here:

- `pip install genvm-lint` → no such package on PyPI.
- `pip install py-genlayer` → no such package on PyPI.
- `pip install genlayer` → installs version 0.0.1, which is an empty
  placeholder package ("A minimal empty PyPI package for genlayer") with
  no `gl` API at all.

The contract was written to follow the linted patterns from the official
write-contract guidance (single contract per file, storage annotations,
TreeMap/DynArray, no floats for money, all nondeterminism inside
`run_nondet_unsafe`, custom validator instead of `strict_eq`), and it
compiles cleanly with `python3 -m py_compile`. **Before submitting, run
`genvm-lint check contracts/delivery_escrow.py`** from the official
GenLayer tooling (GenLayer Studio runs the same checks on deploy) and
fix anything it flags — in particular confirm the transaction-datetime
deadline clock (`datetime.now(timezone.utc)`) and the
`_PayoutTarget.emit_transfer` payout wiring against the target network.

## Deploy

See `DEPLOY.md`: GenLayer Studio (studio.genlayer.com, 5 simulated
validators) → Bradbury testnet (faucet) → submit via the GenLayer portal
(portal.genlayer.foundation) under the Builders / standalone contracts
category.

## Honest limits

- **Settlement interface:** payouts use `_PayoutTarget.emit_transfer`
  (the faucet pattern, copied from GenLayer Studio's bundled
  `faucet.py`). `settle` and `claim_refund` pay out directly with no
  setup call — confirm the payout wiring in Studio testing before
  mainnet use.
- **Delivery verification is only as good as what validators can fetch.**
  The contract judges the content at the submitted URL; it cannot prove
  the freelancer authored it, and it cannot check a delivery that needs a
  login or is not publicly fetchable (that lands as `cannot_verify`).
- **No appeal flow in the contract itself.** Disagreement is handled by
  GenLayer's consensus: validators that disagree force re-execution /
  leader rotation. Appeal of a settled verdict is GenLayer's own
  mechanism, not something this contract re-implements.
- **Deadlines use the transaction datetime** —
  `int(datetime.now(timezone.utc).timestamp())` in Unix seconds. GenLayer
  exposes no block timestamp (no block number or hash in the transaction
  context); the stdlib clock is wired to the transaction datetime, so
  every validator sees the same value.
- **`get_stats` iterates all escrows.** Fine for a primitive's expected
  scale; a deployment expecting thousands of escrows should paginate or
  maintain running counters instead.
