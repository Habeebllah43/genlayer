"""Pytest fixtures: a minimal fake `genlayer` module so the contract file
imports and runs without GenVM installed.

The stub mirrors the parts of the real API the contract uses:
- `gl` namespace: public.write / public.write.payable / public.view,
  evm.contract_interface, nondet.web.render, nondet.exec_prompt,
  vm.run_nondet_unsafe (with gl.vm.Return / gl.vm.UserError Result
  types), eq_principle, message, Contract, UserError.
- Top-level types: u256, Address, TreeMap, DynArray, allow_storage.

There is deliberately NO `gl.block`: GenLayer exposes no block number,
hash or timestamp (docs: Transaction Context). Time comes from the
stdlib clock, which GenVM wires to the transaction datetime — the stub
pins the contract module's `datetime.now(timezone.utc)` to the test's
NOW via _FixedDateTime (see set_now).

`run_nondet_unsafe` here models the real flow in miniature: run the
leader, wrap its outcome in a gl.vm.Result (Return with the value on
.calldata, or UserError carrying the leader's message), hand that
Result to the validator, and raise if the validator disagrees. If the
leader errored and the validator agrees it failed, the leader's error
is re-raised, as errors flow from nondet to deterministic code. Tests
override `gl.nondet.web.render` / `exec_prompt` with canned responses,
and patch `_PayoutTarget.emit_transfer` to record payouts.
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

    In real GenVM, an interface class is instantiated with the payout
    destination's address: ``_PayoutTarget(recipient).emit_transfer(value=v)``.
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
    class Return:
        """A successful leader outcome; the value is on .calldata."""

        def __init__(self, calldata):
            self.calldata = calldata

    class UserError(Exception):
        """A leader application error, delivered to the validator as a
        gl.vm.Result carrying the leader's message on .message."""

        def __init__(self, message):
            super().__init__(message)
            self.message = message

    @staticmethod
    def run_nondet_unsafe(leader_fn, validator_fn):
        try:
            result = leader_fn()
        except Exception as e:
            leaders_res = _Vm.UserError(str(e))
            if not validator_fn(leaders_res):
                raise RuntimeError("consensus failed: validator disagreed with leader")
            raise  # validators agreed the leader failed — propagate its error
        leaders_res = _Vm.Return(result)
        if not validator_fn(leaders_res):
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
    sender_address = "0x0000000000000000000000000000000000000000"
    value = 0


class _Gl:
    public = _Public()
    evm = _Evm()
    nondet = _Nondet()
    vm = _Vm()
    eq_principle = _EqPrinciple()
    message = _Message()

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
NOW = 1_800_000_000  # fixed "now" for deterministic deadline tests

from datetime import datetime as _RealDateTime, timezone as _RealTimezone


class _FixedDateTime:
    """Stand-in for the contract module's `datetime` name.

    Real GenVM wires the stdlib clock to the transaction datetime, so
    `datetime.now(timezone.utc)` returns the same value on every
    validator (docs: Transaction Context). The stub pins it to NOW;
    set_now() moves it, which is how tests simulate time passing.
    Only `now()` is modeled — the contract uses nothing else from
    `datetime`.
    """

    _now = _RealDateTime.fromtimestamp(NOW, tz=_RealTimezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls._now

    @classmethod
    def set_now(cls, ts):
        cls._now = _RealDateTime.fromtimestamp(ts, tz=_RealTimezone.utc)


delivery_escrow.datetime = _FixedDateTime


def set_now(ts):
    """Move the stub's pinned transaction datetime to Unix seconds `ts`."""
    _FixedDateTime.set_now(ts)


@pytest.fixture
def gl():
    return genlayer.gl


@pytest.fixture
def contract(gl):
    """A freshly deployed contract with sender=owner and a fixed clock.

    Storage maps are created by hand here: real GenVM auto-creates them,
    but the stub needs them to exist before __init__ runs.
    """
    gl.message.sender_address = OWNER
    gl.message.value = 0
    set_now(NOW)
    c = object.__new__(delivery_escrow.DeliveryEscrow)
    c.escrows = {}
    c.escrow_order = []
    c.counters = {}
    c.__init__(OWNER)
    return c


@pytest.fixture
def funded(contract, gl):
    """An escrow funded by CLIENT for FREELANCER: 5 GEN, deadline +1 day."""
    gl.message.sender_address = CLIENT
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
    gl.message.sender_address = FREELANCER
    contract.submit_delivery(
        funded, "https://example.com/bakery/index.html", "Done — all 4 criteria covered."
    )
    return funded


def set_sender(gl, account, value=0):
    gl.message.sender_address = account
    gl.message.value = value


def stub_judge(gl, verdict_json):
    """Make the stub's nondet calls return a fixed verdict for everyone."""
    gl.nondet.web.render = staticmethod(
        lambda url, mode="text": "<html><body>Bakery menu, hours, WhatsApp button</body></html>"
    )
    gl.nondet.exec_prompt = staticmethod(lambda prompt, response_format="json": verdict_json)


def record_transfers(monkeypatch):
    """Patch _PayoutTarget.emit_transfer to record (destination, value) calls.

    emit_transfer takes only value=; the destination is the address the
    interface was constructed with, stored on the instance as _address.
    """
    calls = []
    monkeypatch.setattr(
        delivery_escrow._PayoutTarget,
        "emit_transfer",
        lambda self, value: calls.append((self._address, value)),
        raising=False,
    )
    return calls
