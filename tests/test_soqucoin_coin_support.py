# Copyright (c) 2026 Soqucoin Labs Inc.
#
# Clean-install smoke test for Soqucoin coin support.
#
# WHY THIS EXISTS. The first published revision of this fork looked complete —
# clean tree, descriptive commit, a SOQUCOIN.md — and could not be imported at
# all. coins.py does `import electrumx.lib.tx_soqucoin`, and that module was
# never committed: it existed only as an untracked file in the working tree on
# the server the work was done on. Every check the author ran passed, because
# every check ran in the one environment where the file was present on disk. An
# integrator cloning the repo would have hit ImportError and concluded our coin
# support was broken.
#
# So the property under test is not "does coins.py parse" but "does a checkout
# containing ONLY tracked files satisfy every import Soqucoin support needs".
# The CI job installs the package from a fresh checkout before running this, so
# an untracked file cannot satisfy it. Keep it dependency-light and keep it
# asserting on things an integrator would actually hit first.

import importlib

import pytest


def test_tx_soqucoin_module_is_importable():
    """The exact import coins.py performs, isolated so the failure is legible."""
    mod = importlib.import_module("electrumx.lib.tx_soqucoin")
    assert hasattr(mod, "DeserializerSoqucoinAuxPow"), (
        "electrumx.lib.tx_soqucoin imported but does not define "
        "DeserializerSoqucoinAuxPow, which every Soqucoin coin class references"
    )


def test_coins_module_imports():
    """coins.py imports the whole server surface; this is the real gate."""
    importlib.import_module("electrumx.lib.coins")


@pytest.mark.parametrize("net", ["mainnet", "stagenet"])
def test_soqucoin_coin_class_resolves(net):
    """Resolve the way the server does, via Coin.lookup_coin_class."""
    from electrumx.lib.coins import Coin

    coin = Coin.lookup_coin_class("Soqucoin", net)
    assert coin.NET == net
    assert coin.SHORTNAME == "SOQ"


@pytest.mark.parametrize("net", ["mainnet", "stagenet"])
def test_soqucoin_deserializer_is_the_auxpow_one(net):
    """A wrong DESERIALIZER is silent until the first AuxPoW block is indexed."""
    from electrumx.lib.coins import Coin
    from electrumx.lib.tx_soqucoin import DeserializerSoqucoinAuxPow

    coin = Coin.lookup_coin_class("Soqucoin", net)
    assert coin.DESERIALIZER is DeserializerSoqucoinAuxPow
    assert coin.STATIC_BLOCK_HEADERS is False, (
        "Soqucoin headers are variable-length (80 bytes + AuxPoW proof); static "
        "headers would mis-slice every merge-mined block"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
