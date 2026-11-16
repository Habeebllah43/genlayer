# Deploying DeliveryEscrow

No wallet, account, or deployment has been created for you — these are
the steps to run yourself. Nothing here costs real money until mainnet,
and the first two stages are free.

## 0. What you need

- The repo in this folder (`contracts/delivery_escrow.py` and the docs).
- A browser. No local toolchain is needed for Studio; the testnet steps
  use the GenLayer CLI if you prefer it to Studio's UI.
- A GenLayer-compatible wallet (the Studio / portal will prompt you).
  Never paste a private key or seed phrase anywhere except your own
  wallet.

## 1. Lint first (before you deploy anywhere)

```sh
genvm-lint check contracts/delivery_escrow.py
```

> Note: `genvm-lint` could not be installed in the environment this
> project was written in (`pip install genvm-lint` and `pip install
> py-genlayer` find no such packages; the `genlayer` package on PyPI is
> an empty placeholder). Install it from GenLayer's official tooling
> instructions and run the check before deploying. Fix anything it
> flags — in particular confirm `gl.block.timestamp` and the
> `IEscrowArbiter.emit_transfer` wiring (see step 2).

Also run the unit tests once more, so you know the logic you are
deploying is the logic that passed:

```sh
pip install -r requirements.txt
python3 -m pytest tests/ -q
```

## 2. GenLayer Studio — deploy and play with 5 simulated validators

1. Open **https://studio.genlayer.com** and connect your wallet.
2. Create a new contract and paste in `contracts/delivery_escrow.py`
   (one contract per file — this file is the whole contract).
3. Deploy to **studionet**. Studio deploys with 5 simulated validators,
   so `request_judgment` actually runs consensus.
4. **Confirm the Arbiter.** In Studio, find the network's Arbiter
   settlement interface/address and check it matches
   `IEscrowArbiter.emit_transfer` in the contract. If Studio names the
   interface or address differently, update the contract and redeploy.
   Then, as the contract owner, call `set_arbiter(<arbiter address>)` —
   until you do, `settle` and `claim_refund` refuse to pay (by design).
5. Walk the full flow on studionet (amounts are play money):
   - **Create:** call `create_escrow` from the client account,
     attaching GEN (the `value` field), e.g. freelancer = a second
     wallet, payout wallet = that same wallet, brief = the bakery
     example in `README.md`, criteria = the four listed criteria,
     deadline = a Unix timestamp one day ahead.
   - **Deliver:** from the freelancer account, call `submit_delivery`
     with a real, publicly fetchable URL (a GitHub Pages page or a
     raw file URL works; a localhost URL does not — validators cannot
     reach your machine).
   - **Judge:** call `request_judgment` (any account may call it).
     Watch the validators fetch the URL and agree on a verdict; the
     escrow should become `judged` with a `quality_score`.
   - **Settle:** call `settle` and check the payout split matches the
     tier (≥ 80 full, 50–79 split 70/30, < 50 full refund).
   - **Try the failure paths too:** a 404 delivery URL (attempt
     counted, nobody paid), and a second escrow left undelivered past
     its deadline followed by `claim_refund`.
6. Note the deployed contract address and the transaction hashes — you
   will need them for the portal submission.

## 3. Bradbury testnet

1. In Studio, switch the network to **Bradbury** (GenLayer testnet).
2. Get testnet GEN from the faucet (linked in Studio / the GenLayer
   docs). Testnet funds have no real value.
3. Redeploy the contract, call `set_arbiter` with Bradbury's Arbiter
   address (confirm it in the Bradbury docs — do not reuse the
   studionet address), and repeat the full flow from step 2.5.
4. Record: contract address, a `create_escrow` transaction, a
   `request_judgment` transaction showing a stored verdict, and a
   `settle` transaction. These are your evidence for the portal.

## 4. Submit to the GenLayer portal

1. Open **https://portal.genlayer.foundation** and sign in.
2. Submit under the **Builders / standalone contracts** category with:
   - **Link:** this repository (or a public GitHub copy of it), and
     the deployed contract address(es) from Studio and Bradbury.
   - **Title:** "DeliveryEscrow — AI-judged freelance delivery escrow".
   - **Description:** the README's first two sections (problem + what
     the contract does), plus the worked example and its expected
     outcome.
   - **Evidence:** the transaction hashes from step 3.4 and a note
     that the 68 unit tests pass (`python3 -m pytest tests/ -q`).
   - **Docs:** point reviewers at `README.md` (purpose, state design,
     worked example) and `CONSENSUS.md` (equivalence design).
3. Keep the repository public and do not change the contract after
   submitting — reviewers check the submitted code against the deployed
   address.

## 5. Mainnet (only when you are ready)

- Confirm the Arbiter interface name and address for mainnet in the
  official GenLayer docs, redeploy, and call `set_arbiter` with the
  mainnet address.
- Re-run `genvm-lint` against the exact file you deploy.
- Start with small escrow amounts: the settlement tiers are
  deterministic, but the judgment quality depends on the live
  validators and the quality of the brief and criteria you write.
