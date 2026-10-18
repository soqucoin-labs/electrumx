# Copyright (c) 2026 Soqucoin Labs Inc.
#
# Reorg undo round trip for the Soqucoin UTXO record.
#
# WHY THIS EXISTS. The Soqucoin changes store two bytes after every output's
# value (visibility and asset type), so the value advance_txs keeps for a UTXO
# is HASHX_LEN + TXNUM_LEN + 8 + 2 bytes, and the undo record it writes for a
# spent input is that whole value. backup_txs, the reorg path, reads the undo
# record back in fixed-width steps. The width it stepped stayed at upstream's
# HASHX_LEN + TXNUM_LEN + 8, so the first reorg that undid a block holding a
# spend (mainnet height 7,434, 2026-10-18) raised `assert n == 0` and every
# mainnet indexer restarted in a loop until the width was corrected.
#
# The property under test: a block advanced through advance_txs and then
# backed out through backup_txs restores the spent UTXO's cache entry byte for
# byte, with no daemon and no database. The processor is built without its
# constructor so the test needs nothing beyond the block processor module and
# the coin class.

from electrumx.lib.coins import Soqucoin
from electrumx.lib.hash import HASHX_LEN
from electrumx.lib.script import is_unspendable_legacy
from electrumx.lib.tx import Tx, TxInput, TxOutput, ZERO, MINUS_1
from electrumx.lib.util import pack_le_uint32
from electrumx.server.block_processor import BlockProcessor
from electrumx.server.history import TXNUM_LEN

UTXO_VALUE_LEN = HASHX_LEN + TXNUM_LEN + 8 + 2


class _History:
    def add_unflushed(self, hashXs_by_tx, first_tx_num):
        pass


class _FakeDB:
    """The two things the write and undo paths ask of the database."""

    def __init__(self):
        self.history = _History()
        self.tx_counts = []
        self.undo = {}

    def read_undo_info(self, height):
        return self.undo.get(height)


def _processor():
    bp = object.__new__(BlockProcessor)
    bp.coin = Soqucoin
    bp.db = _FakeDB()
    bp.utxo_cache = {}
    bp.db_deletes = []
    bp.touched = set()
    bp.tx_hashes = []
    bp.tx_count = 0
    bp.height = 0
    return bp


def _witness_v1_script(seed: int) -> bytes:
    # OP_1 PUSH32 <32 bytes>: a standard Soqucoin output script.
    return bytes([0x51, 0x20]) + bytes([seed]) * 32


def _usdsoq_script(seed: int) -> bytes:
    # OP_7 PUSH32 <32 bytes>: the witness version the asset byte follows.
    return bytes([0x57, 0x20]) + bytes([seed]) * 32


def _coinbase(txid: bytes, outputs):
    return Tx(
        version=1,
        inputs=[TxInput(prev_hash=ZERO, prev_idx=MINUS_1, script=b'', sequence=0xffffffff)],
        outputs=outputs,
        locktime=0,
        txid=txid,
        wtxid=txid,
    )


def _spend(txid: bytes, prevouts, outputs):
    return Tx(
        version=1,
        inputs=[TxInput(prev_hash=h, prev_idx=i, script=b'', sequence=0xffffffff)
                for h, i in prevouts],
        outputs=outputs,
        locktime=0,
        txid=txid,
        wtxid=txid,
    )


def test_undo_entry_is_the_whole_stored_value():
    bp = _processor()
    funding = _coinbase(b'\x11' * 32, [TxOutput(value=100_000_00000000, pk_script=_witness_v1_script(1)),
                                       TxOutput(value=5_00000000, pk_script=_usdsoq_script(2))])
    bp.advance_txs([funding], is_unspendable_legacy)
    bp.height = 1

    for idx in (0, 1):
        stored = bp.utxo_cache[funding.txid + pack_le_uint32(idx)]
        assert len(stored) == UTXO_VALUE_LEN, (
            f"advance_txs stores {len(stored)} bytes per UTXO; the undo path must step the same width")

    spend = _spend(b'\x22' * 32, [(funding.txid, 0), (funding.txid, 1)],
                   [TxOutput(value=99_999_00000000, pk_script=_witness_v1_script(3))])
    undo_info = bp.advance_txs([spend], is_unspendable_legacy)
    bp.height = 2

    assert len(undo_info) == 2
    assert all(len(entry) == UTXO_VALUE_LEN for entry in undo_info), (
        "every undo entry is the whole stored value, two bytes wider than upstream's")


def test_backup_txs_restores_the_spent_utxos_byte_for_byte():
    bp = _processor()
    funding = _coinbase(b'\x11' * 32, [TxOutput(value=100_000_00000000, pk_script=_witness_v1_script(1)),
                                       TxOutput(value=5_00000000, pk_script=_usdsoq_script(2))])
    bp.advance_txs([funding], is_unspendable_legacy)
    bp.height = 1
    before = {k: v for k, v in bp.utxo_cache.items()}

    # Eight inputs, as the orphaned mainnet block at 7,434 carried: six of them
    # spend outputs of a second coinbase so the undo record holds more than one
    # kind of value.
    second = _coinbase(b'\x33' * 32, [TxOutput(value=1_00000000 * (i + 1), pk_script=_witness_v1_script(10 + i))
                                      for i in range(6)])
    bp.advance_txs([second], is_unspendable_legacy)
    bp.height = 2
    before.update({k: v for k, v in bp.utxo_cache.items()})

    prevouts = [(funding.txid, 0), (funding.txid, 1)] + [(second.txid, i) for i in range(6)]
    spend = _spend(b'\x22' * 32, prevouts,
                   [TxOutput(value=99_999_00000000, pk_script=_witness_v1_script(3)),
                    TxOutput(value=0, pk_script=b'\x6a\x04test')])
    undo_info = bp.advance_txs([spend], is_unspendable_legacy)
    bp.height = 3
    bp.db.undo[3] = b''.join(undo_info)
    assert spend.txid + pack_le_uint32(0) in bp.utxo_cache
    assert spend.txid + pack_le_uint32(1) not in bp.utxo_cache  # OP_RETURN never stored
    tx_count_after_advance = bp.tx_count

    # The reorg: back the spending block out. On the unfixed width this raised
    # AssertionError (n == 16 with 8 inputs).
    bp.backup_txs([spend], is_unspendable_legacy)

    assert spend.txid + pack_le_uint32(0) not in bp.utxo_cache, "the block's own output is gone"
    for key, value in before.items():
        assert bp.utxo_cache.get(key) == value, f"UTXO {key.hex()} not restored byte for byte"
    assert bp.tx_count == tx_count_after_advance - 1
