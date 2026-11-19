# Consensus and equivalence design

This note explains how DeliveryEscrow reaches agreement on a subjective
question — "was the work delivered, and how good was it?" — and what
happens when validators disagree. It is the design behind
`contracts/delivery_escrow.py`; function names below refer to that file.

## The problem with LLM output and consensus

GenLayer's validators must agree on the outcome of a nondeterministic
call. A language model never returns byte-identical text twice: two
honest validators judging the same delivery will write different
reasoning, different word order, sometimes a different but equivalent
score ("82" vs "82.0"). Comparing raw strings therefore fails consensus
on honest work. That is why this contract **never uses `strict_eq`** on
LLM output.

## The leader / validator split

`request_judgment` runs one call:

```python
gl.vm.run_nondet_unsafe(leader_fn, validator_fn)
```

- **Leader** (`_leader_judgment`): fetches the delivery with
  `gl.nondet.web.render(url, mode="text")`, builds the judge prompt
  (`_build_judgment_prompt`), calls `gl.nondet.exec_prompt(prompt,
  response_format="json")`, parses the reply with `_parse_verdict`, and
  returns the normalized verdict as a sorted JSON string.
- **Validator** (`_validate_judgment`): re-runs the leader's work
  itself, parses both verdicts, and compares them with `_verdicts_agree`.
  It returns a boolean — agree or disagree. It never trusts the leader's
  text.

All web fetches and LLM calls happen inside these two functions. Nothing
outside `run_nondet_unsafe` is nondeterministic, and `settle` makes no
LLM call at all: it settles from the stored, already-agreed verdict.

## The comparison rules (`_verdicts_agree`)

The verdict is a small structured object:

```json
{"delivered": true, "quality_score": 82, "reasoning": "...", "cannot_verify": false}
```

Agreement requires:

1. **`cannot_verify` must match.** If either side says the delivery
   cannot be verified, both must say so. A verdict of "I can't tell"
   from one validator and "it was delivered, 85" from another is a real
   disagreement, not a wording difference.
2. **`delivered` must match exactly.** There is no tolerance on the
   yes/no question.
3. **`quality_score` must agree within ±10.** Scoring is genuinely
   subjective; two honest judges may give 78 and 85 for the same work.
   A gap of 11 or more is treated as disagreement. (The boundary is
   tested: diff 10 agrees, diff 11 does not.)
4. **Reasoning is never compared.** It is evidence for humans, not part
   of the consensus value.

## Defensive parsing (`_parse_verdict`, `_coerce_score`)

LLM output is hostile input until proven otherwise. The parser:

- requires a JSON object (a list, a bare string, or invalid JSON is
  rejected);
- accepts `delivered` as a boolean or as "true"/"false"/"yes"/"no";
- accepts the score under aliases (`quality_score`, `score`, `rating`,
  `points`, `value`) and coerces it aggressively — ints, floats,
  `"82"`, `" 77% "` all work;
- rejects booleans-as-scores, non-numeric strings, and scores outside
  0–100;
- normalizes `cannot_verify: true` to `delivered: false,
  quality_score: 0`, so a failed verification can never smuggle a
  payout score with it.

Every rejection raises `ValueError("[LLM_ERROR] ...")` — see the error
taxonomy below.

## Error taxonomy (`_handle_leader_error`, `_errors_agree`)

The leader's work can fail in four different ways, and the validators
must agree on *what kind* of failure it was — not on the exact error
text, which varies between runs and networks.

| Prefix | Meaning | Agreement rule |
|---|---|---|
| `[EXPECTED]` | Business-logic failure | Exact string match |
| `[EXTERNAL]` | Bad deliverable / HTTP 4xx (404, 403, …) | Exact string match |
| `[TRANSIENT]` | Network failure / HTTP 5xx / timeout | Agree if both are transient, wording ignored |
| `[LLM_ERROR]` | Malformed LLM output | **Always disagree** — forces leader rotation |

Classification (`_handle_leader_error`): an already-prefixed error
passes through unchanged. Otherwise the message is inspected — 4xx /
"not found" / "forbidden" become `[EXTERNAL]`; 5xx / timeout /
connection problems become `[TRANSIENT]`; anything unrecognized is
treated as `[TRANSIENT]` (retryable) rather than failing the escrow
permanently. A `ValueError` from `_parse_verdict` already carries
`[LLM_ERROR]`, so it passes through as such.

Why LLM errors always disagree: a malformed verdict is a property of
the leader's run, not of the world. If validators agreed "the leader's
output was garbage", a bad leader could stall every judgment. Forcing
disagreement rotates the leader, and the next leader's output is judged
fresh.

## What happens on each outcome

| Situation | On-chain result |
|---|---|
| Validators agree on a verdict | Verdict + score stored, status `judged`; `settle` can now run |
| Verdict is `cannot_verify` | Attempt counted; status stays `delivered` (attempts < 3) or becomes `unverifiable` (3rd attempt); nobody paid |
| Leader fails, validators agree it is transient/external | Attempt counted; status stays `delivered` (or `unverifiable` on 3rd); nobody paid; the caller may retry |
| Validators disagree (scores > 10 apart, `delivered` differs, LLM error) | **No consensus.** The judgment does not finalize; the escrow stays `delivered` with its attempt count unchanged, and the call can be retried. In the stubbed tests this surfaces as a failed `run_nondet_unsafe` call, which `request_judgment` records as an attempt — on a live network a true consensus failure means the transaction does not finalize, so no attempt is consumed. Confirm the exact behaviour in Studio testing (see "Open questions"). |
| 3 failed attempts | Status `unverifiable`; the client may `claim_refund` the full amount |

In every failure path the money stays locked. The contract fails closed:
there is no path from a failed or unverifiable judgment to a payout.

## What this design deliberately does not do

- **No appeal inside the contract.** A settled verdict is final as far
  as this contract is concerned. Re-judgment of a *settled* escrow is
  not possible — that would let a loser re-roll until they win. Appeal
  of a consensus outcome is GenLayer's own mechanism (validator
  rotation, and ultimately the network's dispute process), not something
  a single contract should re-implement.
- **No reputation or staking for judges.** Validators are GenLayer's;
  this contract does not add its own validator set or slashing.
- **No partial re-judgment.** A judgment is one verdict for the whole
  delivery. If the work is revised, the freelancer submits a new
  delivery — but only while the escrow is `funded`, i.e. before any
  judgment has been requested. (A future version could add an explicit
  "revise and resubmit" step while `delivered`.)

## Open questions (confirm in Studio testing)

1. **Error propagation shape.** The design assumes the leader's
   normalized error string is what the validator compares when the
   leader's nondeterministic call raises. Confirm in GenLayer Studio
   how a raised leader error reaches the validator, and adjust
   `_validate_judgment` if the VM hands validators an error object
   rather than the leader's return value.
2. **Validator argument.** The design assumes `validator_fn` receives
   the leader's return value as its single argument. Confirm against
   the `run_nondet_unsafe` signature in the deployed GenVM version.
3. **Payout interface.** Payouts use `_PayoutTarget.emit_transfer`
   (the faucet pattern from Studio's bundled `faucet.py`); confirm
   the payout wiring in Studio testing (see README).
4. **Block timestamp.** Deadline checks use `gl.block.timestamp`;
   confirm it is available in the deployed GenVM version.
