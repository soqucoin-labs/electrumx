'''listunspent_bounded and the cache behind it and get_multi_balance.

The first tests are the attack the cache must not allow: offering a wallet an output that is
already spent.  The session methods run against a fake database and mempool; the real
Notifications object delivers the mempool's touched sets, with its deferral in force.
'''
import asyncio
import json
import os

import pytest
from aiorpcx import RPCError

from electrumx.lib.hash import hash_to_hex_str
from electrumx.server.controller import Notifications
from electrumx.server.db import UTXO
from electrumx.server.session import ElectrumX
from electrumx.server.utxo_cache import CACHE_MIN_UTXOS, UTXOCache, bounded_utxos

H = b'\x11' * 11          # the address under test, a hashX
OTHER = b'\x22' * 11
COIN = 100_000_000


def txh(n):
    return n.to_bytes(32, 'little')


class FakeDB:
    '''all_utxos() over a dict, utxo_seq moved by flush() as DB.flush_dbs() moves it.'''

    def __init__(self, rows):
        self.rows = {H: list(rows), OTHER: []}
        self.utxo_seq = 0
        self.reads = 0
        self.hold = None      # an asyncio.Event: all_utxos() waits on it after taking its copy

    async def all_utxos(self, hashX):
        self.reads += 1
        rows = list(self.rows.get(hashX, ()))
        if self.hold is not None:
            hold, self.hold = self.hold, None
            await hold.wait()
        return rows

    def flush(self, rows):
        '''A block (or a reorg's backup) committed, as DB.flush_dbs() does it.'''
        self.commit_start()
        self.rows[H] = list(rows)
        self.commit_end()

    def commit_start(self):
        self.utxo_seq += 1

    def commit_end(self):
        self.utxo_seq += 1


class FakeMempool:
    def __init__(self):
        self.spends = set()       # (tx_hash, tx_pos) spent by mempool transactions
        self.outputs = []         # UTXOs paying H from mempool transactions
        self.delta = 0
        self.hold = None          # potential_spends() waits on it after taking its copy

    async def unordered_UTXOs(self, hashX):
        return list(self.outputs) if hashX == H else []

    async def potential_spends(self, hashX):
        spends = set(self.spends) if hashX == H else set()
        if self.hold is not None:
            hold, self.hold = self.hold, None
            await hold.wait()
        return spends

    async def balance_delta(self, hashX):
        return self.delta if hashX == H else 0


class FakeManager:
    def __init__(self, db):
        self.utxo_cache = UTXOCache(lambda: db.utxo_seq)


def make_session(rows):
    db, mempool = FakeDB(rows), FakeMempool()
    session = ElectrumX.__new__(ElectrumX)
    session.db = db
    session.mempool = mempool
    session.session_mgr = FakeManager(db)
    session.costs = []
    session.bump_cost = session.costs.append
    return session, db, mempool


def many_rows(n=CACHE_MIN_UTXOS + 200, big=None):
    '''n confirmed outputs of 0.01 SOQ, one transaction each, plus `big` if given.'''
    rows = [UTXO(i, 0, txh(i), 1000 + i // 700, COIN // 100) for i in range(n)]
    if big is not None:
        rows.append(big)
    return rows


BIG = UTXO(10**6, 0, txh(10**6), 52_000, 1_500_000 * COIN)
BIG_HEX = hash_to_hex_str(BIG.tx_hash)


def outpoints(result):
    return {(o['tx_hash'], o['tx_pos']) for o in result['outputs']}


async def wired(session, height=100):
    '''A Notifications object started at `height` with the cache listening, as serve() wires it.'''
    notifications = Notifications()
    notified = []

    async def notify(h, touched):
        notified.append((h, set(touched)))
    notifications.add_touched_listener(session.session_mgr.utxo_cache.on_touched)
    await notifications.start(height, notify)
    return notifications, notified


# ── The attack: a spent output must never be offered from the cache ──────────────────────

@pytest.mark.asyncio
async def test_an_output_spent_in_the_mempool_is_gone_from_the_next_reads_before_any_block():
    session, db, mempool = make_session(many_rows(big=BIG))
    notifications, notified = await wired(session, height=100)

    first = await session.hashX_listunspent_bounded(H, 200, None)
    balance = await session.get_multi_balance(H)
    assert (BIG_HEX, 0) in outpoints(first)
    assert balance['soq']['unconfirmed'] == 0
    # both are now served from the cache
    reads = db.reads
    assert await session.hashX_listunspent_bounded(H, 200, None) is first
    assert await session.get_multi_balance(H) is balance
    assert db.reads == reads

    # A transaction spending BIG enters the mempool.  The daemon is already at 101 while the
    # block processor is at 100, so the client notification is deferred; the cache's is not.
    mempool.spends.add((BIG.tx_hash, BIG.tx_pos))
    mempool.delta = -BIG.value
    await notifications.on_mempool({H}, 101)
    assert notified == [(100, set())]

    after = await session.hashX_listunspent_bounded(H, 200, None)
    balance_after = await session.get_multi_balance(H)
    assert (BIG_HEX, 0) not in outpoints(after)
    assert after['total'] == first['total'] - 1
    assert after['total_value'] == first['total_value'] - BIG.value
    assert balance_after['soq']['unconfirmed'] == -BIG.value
    assert db.reads == reads + 2


@pytest.mark.asyncio
async def test_a_read_in_flight_when_the_mempool_changes_is_answered_but_not_stored():
    session, db, mempool = make_session(many_rows(big=BIG))
    notifications, _ = await wired(session)

    # The read takes the mempool's spends (none), then waits; meanwhile BIG is spent.
    hold = mempool.hold = asyncio.Event()
    task = asyncio.create_task(session.hashX_listunspent_bounded(H, 200, None))
    await asyncio.sleep(0)
    assert mempool.hold is None                 # the read has its copy and waits
    mempool.spends.add((BIG.tx_hash, BIG.tx_pos))
    await notifications.on_mempool({H}, 100)
    hold.set()
    stale = await task
    assert (BIG_HEX, 0) in outpoints(stale)     # this one answer predates the spend

    fresh = await session.hashX_listunspent_bounded(H, 200, None)
    assert (BIG_HEX, 0) not in outpoints(fresh)


@pytest.mark.asyncio
async def test_a_read_in_flight_across_a_block_flush_is_answered_but_not_stored():
    session, db, _ = make_session(many_rows(big=BIG))
    await wired(session)

    hold = db.hold = asyncio.Event()
    task = asyncio.create_task(session.hashX_listunspent_bounded(H, 200, None))
    await asyncio.sleep(0)
    assert db.hold is None                      # the read has its copy and waits
    db.flush(many_rows())                       # BIG spent in a block
    hold.set()
    stale = await task
    assert (BIG_HEX, 0) in outpoints(stale)

    fresh = await session.hashX_listunspent_bounded(H, 200, None)
    assert (BIG_HEX, 0) not in outpoints(fresh)


@pytest.mark.asyncio
async def test_a_block_that_spends_an_output_is_seen_with_no_notification_at_all():
    session, db, _ = make_session(many_rows(big=BIG))
    await wired(session)
    first = await session.hashX_listunspent_bounded(H, 200, None)
    balance = await session.get_multi_balance(H)
    assert (BIG_HEX, 0) in outpoints(first)

    db.flush(many_rows())                       # committed; on_block not yet called
    after = await session.hashX_listunspent_bounded(H, 200, None)
    assert (BIG_HEX, 0) not in outpoints(after)
    assert (await session.get_multi_balance(H))['soq']['confirmed'] == \
        balance['soq']['confirmed'] - BIG.value


@pytest.mark.asyncio
async def test_a_read_during_a_commit_is_not_stored_and_nothing_is_served_until_it_ends():
    session, db, _ = make_session(many_rows(big=BIG))
    await wired(session)
    await session.hashX_listunspent_bounded(H, 200, None)       # cached at an even sequence
    db.commit_start()                                            # the batch is being written
    during = await session.hashX_listunspent_bounded(H, 200, None)
    assert (BIG_HEX, 0) in outpoints(during)                     # read before the write landed
    db.rows[H] = many_rows()                                     # the write lands: BIG spent
    assert (BIG_HEX, 0) not in outpoints(await session.hashX_listunspent_bounded(H, 200, None))
    db.commit_end()
    assert (BIG_HEX, 0) not in outpoints(await session.hashX_listunspent_bounded(H, 200, None))


@pytest.mark.asyncio
async def test_a_block_that_adds_an_output_shows_it():
    session, db, _ = make_session(many_rows())
    await wired(session)
    first = await session.hashX_listunspent_bounded(H, 200, None)
    assert (BIG_HEX, 0) not in outpoints(first)
    db.flush(many_rows(big=BIG))
    after = await session.hashX_listunspent_bounded(H, 200, None)
    assert (BIG_HEX, 0) in outpoints(after)
    assert after['outputs'][0]['value'] == BIG.value


@pytest.mark.asyncio
async def test_a_reorg_that_undoes_a_block_restores_its_outputs():
    session, db, _ = make_session(many_rows(big=BIG))
    notifications, _ = await wired(session)
    db.flush(many_rows())                                 # the block spending BIG
    await notifications.on_block({H}, 101)
    assert (BIG_HEX, 0) not in outpoints(await session.hashX_listunspent_bounded(H, 200, None))

    db.flush(many_rows(big=BIG))                          # the backup flush of the reorg
    restored = await session.hashX_listunspent_bounded(H, 200, None)
    assert (BIG_HEX, 0) in outpoints(restored)


@pytest.mark.asyncio
async def test_a_change_to_another_address_keeps_the_entry():
    session, db, _ = make_session(many_rows(big=BIG))
    notifications, _ = await wired(session)
    first = await session.hashX_listunspent_bounded(H, 200, None)
    await notifications.on_mempool({OTHER}, 100)
    reads = db.reads
    assert await session.hashX_listunspent_bounded(H, 200, None) is first
    assert db.reads == reads


# ── The method ───────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_address_within_the_bound_gets_listunspent_unchanged_and_is_not_cached():
    rows = [UTXO(i, i % 3, txh(i), 900 + i, (i + 1) * COIN) for i in range(36)]
    session, db, mempool = make_session(rows)
    mempool.outputs = [UTXO(-1, 0, txh(5000), 0, 7 * COIN)]
    full = await session.hashX_listunspent(H)
    bounded = await session.hashX_listunspent_bounded(H, 200, None)
    assert bounded == {'outputs': full, 'total': 37, 'total_value': sum(o['value'] for o in full)}
    await session.hashX_listunspent_bounded(H, 200, None)
    assert db.reads == 3 and len(session.session_mgr.utxo_cache) == 0


@pytest.mark.asyncio
async def test_seventy_thousand_outputs_give_the_200_largest_with_the_full_count():
    rows = many_rows(70_001) + [UTXO(10**6 + i, 0, txh(10**6 + i), 52_000, (i + 2) * COIN * 1000)
                                for i in range(6)]
    session, _, _ = make_session(rows)
    result = await session.hashX_listunspent_bounded(H, 200, None)
    assert len(result['outputs']) == 200 and result['total'] == 70_007
    assert [o['value'] for o in result['outputs'][:6]] == sorted((r.value for r in rows[-6:]),
                                                                 reverse=True)
    assert result['total_value'] == sum(r.value for r in rows)


@pytest.mark.asyncio
async def test_asset_type_filters_as_listunspent_by_asset_does():
    rows = many_rows() + [UTXO(10**6 + i, 0, txh(10**6 + i), 52_000, COIN, 0, 1) for i in range(3)]
    session, _, _ = make_session(rows)
    usdsoq = await session.hashX_listunspent_bounded(H, 200, 1)
    assert usdsoq['total'] == 3
    assert usdsoq['outputs'] == await session.hashX_listunspent_by_asset(H, 1)
    soq = await session.hashX_listunspent_bounded(H, 200, 0)
    assert soq['total'] == len(rows) - 3
    assert (await session.hashX_listunspent_bounded(H, 200, None))['total'] == len(rows)


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', [0, -1, 1001, '200', 200.0, True, None])
async def test_a_limit_outside_1_to_1000_is_refused(limit):
    session, _, _ = make_session([])
    with pytest.raises(RPCError):
        await session.scripthash_listunspent_bounded('00' * 32, limit)


@pytest.mark.asyncio
@pytest.mark.parametrize('asset_type', [2, -1, '0', True, 0.0])
async def test_an_unknown_asset_type_is_refused(asset_type):
    session, _, _ = make_session([])
    with pytest.raises(RPCError):
        await session.scripthash_listunspent_bounded('00' * 32, 200, asset_type)


@pytest.mark.asyncio
async def test_a_cache_hit_costs_the_reply_not_the_read():
    session, _, _ = make_session(many_rows(70_000))
    await session.hashX_listunspent_bounded(H, 200, None)
    await session.hashX_listunspent_bounded(H, 200, None)
    assert session.costs == [1.0 + 70_000 / 50, 1.0 + 200 / 50]


# ── The cache ────────────────────────────────────────────────────────────────────────────

def test_the_cache_holds_no_more_than_its_bound_in_outputs():
    cache = UTXOCache(lambda: 0, maxsize=1000)
    for i in range(10):
        hashX = bytes([i]) * 11
        cache.end(hashX, cache.begin(hashX), 'k', {'outputs': [{}] * 199, 'n': i})
        assert cache._entries.currsize <= 1000
    assert len(cache) == 5
    assert cache.get(bytes([9]) * 11, 'k')['n'] == 9
    assert cache.get(bytes([0]) * 11, 'k') is None
    # a result larger than the whole cache is answered and not stored
    cache.end(H, cache.begin(H), 'k', {'outputs': [{}] * 1000})
    assert cache.get(H, 'k') is None and cache._entries.currsize <= 1000


@pytest.mark.asyncio
async def test_one_address_asked_for_every_limit_keeps_four_results():
    session, _, _ = make_session(many_rows(5000))
    for limit in range(1, 1001, 7):
        await session.hashX_listunspent_bounded(H, limit, None)
        await session.hashX_listunspent_bounded(H, limit, 1)
    seq, results, rows = session.session_mgr.utxo_cache._entries[H]
    assert len(results) == 4
    assert rows == sum(len(r['outputs']) + 1 for r in results.values())
    assert session.session_mgr.utxo_cache._entries.currsize == rows


def test_reads_in_flight_are_forgotten_once_they_end():
    cache = UTXOCache(lambda: 0)
    a, b = cache.begin(H), cache.begin(H)
    cache.on_touched({H})
    cache.end(H, a, 'k', {'stale': 1})
    cache.end(H, b, 'k', {'stale': 2})
    assert cache.get(H, 'k') is None
    assert not cache._reads


# ── The selection matches the bridge ─────────────────────────────────────────────────────

def test_the_selection_matches_the_bridges_on_its_own_fixtures():
    path = os.path.join(os.path.dirname(__file__), 'data', 'bounded_utxos_bridge.json')
    with open(path) as f:
        cases = json.load(f)['cases']
    assert len(cases) == 150
    for case in cases:
        rows = [{'tx_hash': h, 'tx_pos': p, 'height': height, 'value': v,
                 'n_visibility': 0, 'n_asset_type': 0} for h, p, height, v in case['rows']]
        kept = [[r['tx_hash'], r['tx_pos'], r['height']] for r in bounded_utxos(rows, case['limit'])]
        if len(rows) <= case['limit']:
            assert kept == [[h, p, height] for h, p, height, _ in case['rows']]
        assert kept == case['kept'], case['limit']


def row(tx, pos, height, value):
    return {'tx_hash': f'{tx:064x}', 'tx_pos': pos, 'height': height, 'value': value,
            'n_visibility': 0, 'n_asset_type': 0}


def test_confirmed_outputs_keep_their_places_ahead_of_larger_pending_ones():
    rows = [row(i, 0, 100, 10 + i) for i in range(5)] + [row(100 + i, 0, 0, 1000 + i) for i in range(5)]
    kept = bounded_utxos(rows, 7)
    assert sum(1 for r in kept if r['height'] > 0) == 5
    assert {r['value'] for r in kept if r['height'] <= 0} == {1004, 1003}


def test_a_transaction_keeps_its_outputs_together_while_they_fit():
    rows = [row(1, p, 100, 50) for p in range(3)] + [row(2, 0, 100, 60), row(3, 0, 100, 40)]
    kept = bounded_utxos(rows, 4)
    assert sorted((int(r['tx_hash'], 16), r['tx_pos']) for r in kept) == [(1, 0), (1, 1), (1, 2), (2, 0)]
    # a transaction with more outputs than the bound still offers its largest
    big = [row(9, p, 100, 1000 - p) for p in range(10)]
    assert [r['tx_pos'] for r in bounded_utxos(big, 3)] == [0, 1, 2]


# ── The real mempool: a refresh changes the maps across awaits ───────────────────────────

from electrumx.lib.coins import BitcoinCash
from electrumx.lib.tx import Tx, TxInput, TxOutput
from electrumx.server.mempool import MemPool, MemPoolAPI

BCH = BitcoinCash


def _tx(prevouts, pk_script, value):
    tx = Tx(version=2, inputs=[TxInput(prev_hash=h, prev_idx=i, script=b'', sequence=0xffffffff)
                               for h, i in prevouts],
            outputs=[TxOutput(value=value, pk_script=pk_script)], locktime=0, txid=None, wtxid=None)
    raw = tx.serialize()
    return BCH.DESERIALIZER.TX_HASH_FN(raw), raw


class SlowAPI(MemPoolAPI):
    '''A daemon whose transaction fetches wait on `gate` for every chunk not holding `first`.'''

    def __init__(self, notifications, db_utxos):
        self.n = notifications
        self.db_utxos = db_utxos       # prevout -> (hashX, value)
        self.raw = {}
        self.first = None
        self.gate = asyncio.Event()

    async def height(self):
        return 0

    def cached_height(self):
        return 0

    def db_height(self):
        return 0

    async def mempool_hashes(self):
        return [hash_to_hex_str(h) for h in self.raw]

    async def raw_transactions(self, hex_hashes):
        hashes = [bytes.fromhex(hh)[::-1] for hh in hex_hashes]
        if self.first not in hashes:
            await self.gate.wait()
        return [self.raw[h] for h in hashes]

    async def lookup_utxos(self, prevouts):
        return [self.db_utxos.get(p) for p in prevouts]

    async def on_mempool(self, touched, height):
        await self.n.on_mempool(touched, height)

    def on_mempool_change(self, touched):
        self.n.on_mempool_change(touched)


@pytest.mark.asyncio
async def test_a_spend_accepted_mid_refresh_is_seen_before_the_refresh_ends():
    x_script = BCH.hash160_to_P2PKH_script(b'\x01' * 20)
    x = BCH.hashX_from_script(x_script)
    other_script = BCH.hash160_to_P2PKH_script(b'\x02' * 20)
    other = BCH.hashX_from_script(other_script)
    rows = [UTXO(i, 0, txh(i), 1000, COIN // 100) for i in range(1200)] + [BIG]
    db = FakeDB([])
    db.rows = {x: rows}
    filler = {(txh(10**7 + i), 0): (other, COIN) for i in range(260)}
    notifications = Notifications()
    api = SlowAPI(notifications, {**filler, (BIG.tx_hash, BIG.tx_pos): (x, BIG.value)})
    mempool = MemPool(BCH, api)
    session = ElectrumX.__new__(ElectrumX)
    session.db, session.mempool, session.session_mgr = db, mempool, FakeManager(db)
    session.bump_cost = lambda c: None
    notifications.add_touched_listener(session.session_mgr.utxo_cache.on_touched)
    await notifications.start(0, lambda h, t: asyncio.sleep(0))

    first = await session.hashX_listunspent_bounded(x, 200, None)
    assert (BIG_HEX, 0) in outpoints(first)
    # 260 new transactions, two chunks of fetches; the spend of BIG is in the chunk that finishes
    spend, raw = _tx([(BIG.tx_hash, BIG.tx_pos)], other_script, BIG.value - 1000)
    api.raw[spend] = raw
    api.first = spend
    for prevout in filler:
        h, r = _tx([prevout], other_script, COIN - 1000)
        api.raw[h] = r
    touched = set()
    refresh = asyncio.create_task(mempool._process_mempool(set(api.raw), touched, 0))
    while spend not in mempool.txs:
        await asyncio.sleep(0)
    assert not refresh.done()                     # on_mempool has not been called

    during = await session.hashX_listunspent_bounded(x, 200, None)
    assert (BIG_HEX, 0) not in outpoints(during)
    assert during['total'] == first['total'] - 1
    assert (await session.get_multi_balance(x))['soq']['unconfirmed'] == -BIG.value

    api.gate.set()
    await refresh
    assert len(mempool.txs) == 261
    assert (BIG_HEX, 0) not in outpoints(await session.hashX_listunspent_bounded(x, 200, None))

    # The spend leaves the daemon's mempool with no block (evicted): BIG is unspent again, and
    # is offered before the refresh reports through on_mempool (which _process_mempool never calls).
    del api.raw[spend]
    await mempool._process_mempool(set(api.raw), set(), 0)
    back = await session.hashX_listunspent_bounded(x, 200, None)
    assert (BIG_HEX, 0) in outpoints(back)
    assert (await session.get_multi_balance(x))['soq']['unconfirmed'] == 0
