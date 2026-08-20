# Copyright (c) 2026 Soqucoin Labs Inc.
# Custom transaction deserializer for Soqucoin
#
# CTxOut migration Phase 4: the nVisibility/nAssetType extension bytes were REMOVED from
# CTxOut. Soqucoin outputs are now STANDARD Bitcoin (nValue + scriptPubKey) — identical to
# the AuxPoW parent (Litecoin) coinbase format. So the dual-format split that used to be the
# whole reason for this module is gone: block txs AND parent txs deserialize the same way.
#
# What REMAINS Soqucoin-specific is only the AuxPoW header structure (variable-length header
# carrying the parent coinbase + merkle branches), so DeserializerSoqucoinAuxPow keeps the
# read_auxpow() override but uses the standard output deserialization for everything.
#
# (Was: each CTxOut had +2 bytes nVisibility/nAssetType after the scriptPubKey — SOQ-INFRA-018.
#  That is removed at the genesis reset; this file is the byte-less un-patch.)

from electrumx.lib.tx import (
    Deserializer, DeserializerSegWit, DeserializerAuxPow,
)


class DeserializerSoqucoinAuxPow(DeserializerSegWit, DeserializerAuxPow):
    """Soqucoin AuxPoW deserializer (Phase 4, byte-less CTxOut).

    Outputs are STANDARD (value + scriptPubKey, no extension bytes) — inherited unchanged
    from DeserializerSegWit. The only Soqucoin-specific handling is the AuxPoW header: blocks
    with the AuxPoW version bit carry a variable-length header (parent coinbase TX + merkle
    branches + parent block header) that read_auxpow() skips past.

    Asset/visibility/freeze are no longer in the wire format — they follow the witness version
    (USDSOQ ⟺ v7, confidential ⟺ v4) in the scriptPubKey, and freezing is the node's DB registry.
    Consumers that need asset type should read it from the node RPC (assetType, now version-aware).
    """

    # VERSION_AUXPOW inherited from DeserializerAuxPow = (1 << 8)

    def read_auxpow(self):
        """Read AuxPoW data and return the raw bytes spanned.

        Phase 4: the parent coinbase TX and our own TXs are both standard now, so read_tx
        parsing is uniform — but the AuxPoW HEADER structure is still Soqucoin/Doge-style and
        must be walked here so the cursor lands correctly for subsequent block parsing.
        """
        start = self.cursor

        # Parent chain coinbase TX (standard outputs — same as ours now).
        Deserializer.read_tx(self)

        self.cursor += 32                 # Parent block hash
        merkle_size = self._read_varint()
        self.cursor += 32 * merkle_size   # Merkle branch
        self.cursor += 4                  # Index
        merkle_size = self._read_varint()
        self.cursor += 32 * merkle_size   # Chain merkle branch
        self.cursor += 4                  # Chain index
        self.cursor += 80                 # Parent block header

        end = self.cursor
        self.cursor = start
        return self._read_nbytes(end - start)
