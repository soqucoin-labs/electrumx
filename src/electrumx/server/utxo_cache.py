# Copyright (c) 2026, Soqucoin Labs Inc.
#
# All rights reserved.
#
# See the file "LICENCE" for information about the copyright
# and warranty status of this software.

'''A bounded view of an address's unspent outputs, and a cache of results read from them.

An address can hold far more outputs than a client can use or a reply can carry (70,007 on
mainnet in November 2026, 16.8 MB as one listunspent).  bounded_utxos() keeps at most `limit`
of them by the rule the SoquShield wallet bridge applied before this method existed, so the
bridge and the indexer agree on every set.

UTXOCache holds results computed from all_utxos() and the mempool for addresses with many
outputs.  An entry is served only while it is current:

  * The database: each entry carries DB.utxo_seq as read before its computation began.
    utxo_seq moves after every commit that changes UTXO rows (a block flushed or backed up
    in a reorg), so every entry read before a commit stops being served after it, whatever
    the notifications do.
  * The mempool: the mempool calls Notifications.on_mempool() after each change to its state,
    with the addresses it touched; the listener drops their entries at once.  Client
    notifications can be deferred until the block processor catches up; the listener is not.
  * A computation in flight when either happens is not stored: each read records a
    generation per address, which every touched set moves.
'''

from electrumx.lib.lrucache import LRUCache


# A client may ask for at most this many outputs.
BOUND_MAX = 1000
# Results are cached for addresses holding at least this many outputs; below it a read is
# cheap and a cache would only spend memory.
CACHE_MIN_UTXOS = 1000
# Entries, one an address.  Each holds a few results of at most BOUND_MAX outputs.
CACHE_ENTRIES = 256


def _value_desc(row):
    '''Largest value first; equal values by outpoint, so two reads of an unchanged set agree.
    tx_hash is the hex string, compared as the bridge compares it.'''
    return (-row['value'], row['tx_hash'], row['tx_pos'])


def bounded_utxos(rows, limit):
    '''At most `limit` of `rows` (listunspent dicts), the list as given when it fits.

    Confirmed outputs (height above 0) come before pending ones, each largest first.  A
    transaction's outputs are kept together while they fit, so a sum per transaction over the
    kept list equals the sum over the whole list; slots no whole transaction fits are filled
    with the largest outputs left.  The kept set is ordered largest first.

    This is largestUtxos() of soqucoin-ops infrastructure/soqushield-api/server.js at
    4fc527b8; tests/server/test_utxo_cache.py checks the two agree.
    '''
    if len(rows) <= limit:
        return rows
    ordered = (sorted((r for r in rows if r['height'] > 0), key=_value_desc)
               + sorted((r for r in rows if not r['height'] > 0), key=_value_desc))
    by_tx = {}
    for row in ordered:
        by_tx.setdefault(row['tx_hash'], []).append(row)
    kept = {}
    for row in ordered:
        if len(kept) == limit:
            break
        outs = by_tx[row['tx_hash']]
        if (row['tx_hash'], row['tx_pos']) not in kept and len(kept) + len(outs) <= limit:
            for out in outs:
                kept[(out['tx_hash'], out['tx_pos'])] = out
    for row in ordered:
        if len(kept) == limit:
            break
        kept.setdefault((row['tx_hash'], row['tx_pos']), row)
    return sorted(kept.values(), key=_value_desc)


class UTXOCache:
    '''Results read from an address's outputs, served only while the outputs are unchanged.

    seq_fn returns the database's current utxo_seq.  A read goes:

        result = cache.get(hashX, key)
        if result is None:
            token = cache.begin(hashX)
            try:
                result = ...compute from all_utxos() and the mempool...
            finally:
                cache.end(hashX, token, key, result if worth_caching else None)
    '''

    def __init__(self, seq_fn, maxsize=CACHE_ENTRIES):
        self._seq = seq_fn
        # hashX -> (utxo_seq, {key: result})
        self._entries = LRUCache(maxsize=maxsize)
        # hashX -> [generation, reads in flight]; present only while a read is in flight
        self._reads = {}
        self.num_lookups = 0
        self.num_hits = 0

    def __len__(self):
        return len(self._entries)

    def get(self, hashX, key):
        self.num_lookups += 1
        entry = self._entries.get(hashX)
        if entry is None:
            return None
        seq, results = entry
        if seq != self._seq():
            del self._entries[hashX]
            return None
        result = results.get(key)
        if result is not None:
            self.num_hits += 1
        return result

    def begin(self, hashX):
        '''Call before the read touches the database or the mempool.'''
        read = self._reads.get(hashX)
        if read is None:
            read = self._reads[hashX] = [0, 0]
        read[1] += 1
        return (self._seq(), read[0])

    def end(self, hashX, token, key, result):
        '''Store `result` (None stores nothing) if nothing changed since begin().'''
        read = self._reads[hashX]
        read[1] -= 1
        current = token == (self._seq(), read[0])
        if not read[1]:
            del self._reads[hashX]
        if result is None or not current:
            return
        seq = token[0]
        entry = self._entries.get(hashX)
        if entry is not None and entry[0] == seq:
            entry[1][key] = result
        else:
            self._entries[hashX] = (seq, {key: result})

    def on_touched(self, touched):
        '''The addresses in `touched` changed: drop their entries and void their reads.'''
        for hashX in self._reads.keys() & touched:
            self._reads[hashX][0] += 1
        entries = self._entries
        if len(touched) < len(entries):
            gone = [hashX for hashX in touched if hashX in entries]
        else:
            gone = set(entries).intersection(touched)
        for hashX in gone:
            del entries[hashX]
