"""Pytest fixtures: a minimal fake `genlayer` module so the contract file
imports and runs without GenVM installed.

The stub mirrors the parts of the real API the contract uses:
- `gl` namespace: public.write / public.write.payable / public.view,
  evm.contract_interface, nondet.web.render, nondet.exec_prompt,
  vm.run_nondet_unsafe, eq_principle, message, block, Contract, UserError.
- Top-level types: u256, Address, TreeMap, DynArray, allow_storage.

`run_nondet_unsafe` here models the real flow in miniature: run the
leader, hand its result to the validator, and raise if the validator
disagrees. Tests override `gl.nondet.web.render` / `exec_prompt` with
canned responses, and patch `IEscrowArbiter.emit_transfer` to record
payouts.
"""
import sys
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Build the fake genlayer module
# ---------------------------------------------------------------------------

class _Deco:
    """A no-op decorator that is also usable as `.payable`.

    Plain functions stored as class attributes would get `self` bound when
    accessed through an instance; this object avoids that.
    """

    def __call__(self, fn):
        return fn

    @property
    def payable(self):
        return self


class _InterfaceDeco:
    """Decorator for @gl.evm.contract_interface classes.

    In real GenVM, an interface class is instantiated with the target
    contract's address: ``IEscrowArbiter(arbiter_address).emit_transfer(...)``.
    The stub emulates that by giving the class an address-taking constructor.
    """

    def __call__(self, cls):
        class Wrapped(cls):
            def __init__(self, address=None):
                self._address = address

        Wrapped.__name__ = cls.__name__
        Wrapped.__qualname__ = cls.__qualname__
        return Wrapped


class _Public:
    write = _Deco()
    view = _Deco()


class _Evm:
    contract_interface = _InterfaceDeco()


class _Web:
    @staticmethod
    def render(url, mode="text"):
        raise RuntimeError("nondet web.render not stubbed for this test")


class _Nondet:
    web = _Web()

    @staticmethod
    def exec_prompt(prompt, response_format="json"):
        raise RuntimeError("nondet exec_prompt not stubbed for this test")


class _Vm:
    @staticmethod
    def run_nondet_unsafe(leader_fn, validator_fn):
        result = leader_fn()
        if not validator_fn(result):
            raise RuntimeError("consensus failed: validator disagreed with leader")
        return result


class _EqPrinciple:
    @staticmethod
    def strict_eq(a, b):
        return a == b

    @staticmethod
    def custom_eq(a, b, fn):
        return fn(a, b)


class _Message:
    sender_account = "0x0000000000000000000000000000000000000000"
    value = 0


class _Block:
    timestamp = 0


class _Gl:
    public = _Public()
    evm = _Evm()
    nondet = _Nondet()
    vm = _Vm()
    eq_principle = _EqPrinciple()
    message = _Message()
    block = _Block()
    contract = _Deco()

    class Contract:
        pass

    class UserError(Exception):
        pass


class TreeMap(dict):
    def __class_getitem__(cls, params):
        return cls


class DynArray(list):
    def __class_getitem__(cls, params):
        return cls


genlayer = types.ModuleType("genlayer")
genlayer.gl = _Gl()
genlayer.u256 = int
genlayer.Address = str
genlayer.TreeMap = TreeMap
genlayer.DynArray = DynArray
genlayer.allow_storage = _Deco()
genlayer.__all__ = ["gl", "u256", "Address", "TreeMap", "DynArray", "allow_storage"]
sys.modules["genlayer"] = genlayer

# ---------------------------------------------------------------------------
# Import the contract file by path (no package needed)
# ---------------------------------------------------------------------------
CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contracts" / "delivery_escrow.py"

import importlib.util

_spec = importlib.util.spec_from_file_location("delivery_escrow", CONTRACT_PATH)
delivery_escrow = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(delivery_escrow)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

OWNER = "0x1111111111111111111111111111111111111111"
CLIENT = "0x2222222222222222222222222222222222222222"
FREELANCER = "0x3333333333333333333333333333333333333333"
PAYOUT = "0x4444444444444444444444444444444444444444"
ARBITER = "0x5555555555555555555555555555555555555555"
NOW = 1_800_000_000  # fixed "now" for deterministic deadline tests


@pytest.fixture
def gl():
    return genlayer.gl


@pytest.fixture
def contract(gl):
    """A freshly deployed contract with sender=owner and a fixed clock.

    Storage maps are created by hand here: real GenVM auto-creates them,
    but the stub needs them to exist before __init__ runs.
    """
    gl.message.sender_account = OWNER
    gl.message.value = 0
    gl.block.timestamp = NOW
    c = object.__new__(delivery_escrow.DeliveryEscrow)
    c.escrows = {}
    c.escrow_order = []
    c.counters = {}
    c.__init__(OWNER)
    return c


@pytest.fixture
def funded(contract, gl):
    """An escrow funded by CLIENT for FREELANCER: 5 GEN, deadline +1 day."""
    gl.message.sender_account = CLIENT
    gl.message.value = 5 * 10**18
    escrow_id = contract.create_escrow(
        FREELANCER,
        PAYOUT,
        "Build a landing page for a Lagos bakery: menu, opening hours, "
        "WhatsApp order button. Single self-contained index.html, mobile-friendly.",
        [
            "Single self-contained index.html",
            "Menu with at least 6 items and prices",
            "Opening hours section",
            "WhatsApp order button linking to wa.me",
        ],
        NOW + 86_400,
    )["escrow_id"]
    gl.message.value = 0
    return escrow_id


@pytest.fixture
def delivered(contract, gl, funded):
    """`funded` escrow with a delivery submitted by the freelancer."""
    gl.message.sender_account = FREELANCER
    contract.submit_delivery(
        funded, "https://example.com/bakery/index.html", "Done — all 4 criteria covered."
    )
    return funded


def set_sender(gl, account, value=0):
    gl.message.sender_account = account
    gl.message.value = value


def stub_judge(gl, verdict_json):
    """Make the stub's nondet calls return a fixed verdict for everyone."""
    gl.nondet.web.render = staticmethod(
        lambda url, mode="text": "<html><body>Bakery menu, hours, WhatsApp button</body></html>"
    )
    gl.nondet.exec_prompt = staticmethod(lambda prompt, response_format="json": verdict_json)


def record_transfers(monkeypatch):
    """Patch IEscrowArbiter.emit_transfer to record (to, amount) calls."""
    calls = []
    monkeypatch.setattr(
        delivery_escrow.IEscrowArbiter,
        "emit_transfer",
        lambda self, to, amount: calls.append((to, amount)),
    )
    return calls
