# ElectrumX with Soqucoin support

This repository is a fork of [spesmilo/electrumx](https://github.com/spesmilo/electrumx) that adds
Soqucoin (SOQ). Upstream ships no Soqucoin coin definition, so an indexer for Soqucoin is built
from this fork.

**Upstream base:** `24865dc` (`readthedocs: update base image`)
**Branch:** `soqucoin`
**License:** unchanged from upstream, see [`LICENCE`](./LICENCE). Copyright remains with Neil
Booth and the Electrum developers. This fork adds the modifications listed below and asserts no
claim over the original work.

---

## Releases

Tags name the revisions the project's own indexers run. Build from a tag.

| Tag | Date (UTC) | What it adds |
|-----|------------|--------------|
| `soqucoin-1.19.0-3` | 2026-11-22 | `blockchain.scripthash.listunspent_bounded` and a cache of address reads |
| `soqucoin-1.19.0-2` | 2026-10-18 | the reorganisation undo path reads the 26-byte undo entries this fork writes |

A checkout from before `soqucoin-1.19.0-2` stops at the first mainnet reorganisation, height 7,434.
It recovers on a restart at a tag, with no reindex.

---

## Why an indexer is required

`soqucoind` has no address index, and the integration path runs the node with `disablewallet=1`.
Anything that watches addresses (an exchange crediting deposits, a wallet, a mining pool) reads
unspent outputs from ElectrumX.

Running your own instance of this fork removes any dependency on infrastructure operated by
Soqucoin Labs.

---

## What this fork changes

Eight files under `src/`. One command prints the whole change:

```bash
git diff 24865dc..soqucoin -- src/
```

Every other file under `src/` is upstream's. CI checks that each file the command lists is named
below.

### 1. `src/electrumx/lib/coins.py`: the coin definitions

Adds two coin classes and one import. No upstream coin class is changed.

| Class | `NET` | Genesis | AuxPoW |
|-------|-------|---------|--------|
| `Soqucoin` | `mainnet` | `0d828600…86a8` | from genesis |
| `SoqucoinStagenet` | `stagenet` | `97df3ae7…1bb0` | from block 100 |

Both override `header_hash` and `block_header`. A Soqucoin header is merge-mined (AuxPoW): the
80-byte header is followed by a proof of variable length, and the block hash covers the first 80
bytes.

Both set `DEFAULT_MAX_SEND` to 10,000,000 bytes (see [Reply size](#reply-size)).

Mainnet addresses are bech32m and start with `sq1`. Clients query by script hash, so the base58
version bytes the classes also carry are unused by the methods of section 5.

> `Soqucoin.GENESIS_HASH` is the mainnet genesis
> `0d828600816cbd7c23789660b53f90cb6ec7ff85540698e13845eb2d2f0486a8` (soqucoin `chainparams.cpp`).
> Every tag carries it. The Soqucoin SDK's ElectrumX client checks `server.features` `genesis_hash`
> against the chain its addresses belong to and refuses a server that reports anything else.

### 2. `src/electrumx/lib/tx_soqucoin.py`: the AuxPoW deserializer

`DeserializerSoqucoinAuxPow`, named by both coin classes in `DESERIALIZER`. Outputs are standard
Bitcoin outputs (value and scriptPubKey), inherited unchanged from `DeserializerSegWit`. The one
Soqucoin-specific part is `read_auxpow()`, which walks the merge-mining proof (parent coinbase,
merkle branches, parent header) so the cursor lands at the block's transactions.

### 3. `src/electrumx/server/block_processor.py`: asset and visibility labels

Each indexed output gets two labels, read from its script:

```
34-byte script starting OP_7 (0x57)  ->  asset type 1 (USDSOQ)
34-byte script starting OP_4 (0x54)  ->  visibility 1 (confidential)
anything else                        ->  asset type 0, visibility 0 (transparent native SOQ)
```

The labels are stored as two bytes, `[visibility, asset type]`, after the value of each UTXO
record. An undo entry carries the same two bytes, so it is 26 bytes long, and `backup_txs` reads
entries of that length.

A label is the shape of the script and nothing more. The node decides what a witness version
means, in `src/script/interpreter.cpp` of the Soqucoin node repository. Check the mapping against
the node release you run before relying on a label. If the two differ, an asset is mislabelled and
no error is raised.

### 4. `src/electrumx/server/db.py`: UTXO records carry the labels

`UTXO` gains `n_visibility` and `n_asset_type`. A stored UTXO value is 10 bytes: 8 of value and
the 2 label bytes. Reads check the length, so a shorter record written by upstream code still
decodes.

`DB.utxo_seq` counts commits that change UTXO rows. It moves once before and once after each
commit (a block flushed, or backed up in a reorganisation) and is odd in between. The cache of
section 6 reads it to tell whether an answer is current.

### 5. `src/electrumx/server/session.py`: protocol additions

Entries of `blockchain.scripthash.listunspent` carry `n_visibility` and `n_asset_type`.

Three methods are added. They are Soqucoin extensions, and a stock Electrum client does not call
them.

| Method | Answer |
|--------|--------|
| `blockchain.scripthash.get_multi_balance(scripthash)` | `{"soq": {"confirmed", "unconfirmed"}, "usdsoq": {"confirmed", "unconfirmed"}}`. Confirmed balances are split by asset type. The mempool delta is reported whole under `soq` and `usdsoq.unconfirmed` is always 0, because mempool records carry no asset label. |
| `blockchain.scripthash.listunspent_by_asset(scripthash, asset_type)` | The `listunspent` entries of one asset: `0` for native SOQ (the default), `1` for USDSOQ. |
| `blockchain.scripthash.listunspent_bounded(scripthash, limit, asset_type)` | `{"outputs", "total", "total_value"}`: at most `limit` entries (1 to 1,000), with the count and the value of all the address's unspent outputs. `asset_type` omitted or `null` takes every output. |

When an address holds more outputs than `limit`, `listunspent_bounded` keeps confirmed outputs
ahead of pending ones and larger ahead of smaller, and keeps one transaction's outputs together
while they fit. It applies the mempool as `listunspent` does: pending outputs are listed at height
0 and outputs spent in the mempool are left out.

### 6. `src/electrumx/server/utxo_cache.py`: the bounded selection and the cache

`bounded_utxos()` is the selection rule of `listunspent_bounded`.

`UTXOCache` keeps answers of `listunspent_bounded` and `get_multi_balance` for addresses of 1,000
outputs or more. It holds at most 50,000 outputs, about 25 MB, and up to four answers for one
address. An answer is served only while it is current: until the next commit that changes UTXO
rows, and until a transaction that touches the address enters or leaves the mempool. A read in
flight when either happens is not stored. `electrumx_rpc getinfo` reports the cache as
`utxo cache`.

### 7. `src/electrumx/server/controller.py`: listeners on touched addresses

`Notifications.add_touched_listener()` registers a function that is called with each set of
touched addresses, synchronously, on every mempool and block update. The call comes before client
notifications are deferred. The cache registers `UTXOCache.on_touched`.

### 8. `src/electrumx/server/mempool.py`: a change is reported as it happens

The mempool calls `on_mempool_change` with the addresses of each transaction the moment it enters
or leaves, without waiting for the refresh to end. It logs a warning at start when its API has no
such method.

---

## Reply size

The server refuses a reply larger than `MAX_SEND`. The value defaults to 10,000,000 bytes for both
coin classes and can be set in the environment. An output takes about 160 bytes of a `listunspent`
reply, so a full list is refused above about 60,000 outputs, with an error whose message starts
`response too large`. `listunspent_bounded` answers an address of any size.

---

## Verifying your checkout

One command, worth running before you build anything on top:

```bash
git clone -b soqucoin-1.19.0-3 https://github.com/soqucoin-labs/electrumx && cd electrumx
pip install .
cd /tmp && python -c "
from electrumx.lib.coins import Coin
for net in ('mainnet', 'stagenet'):
    c = Coin.lookup_coin_class('Soqucoin', net)
    print(c.__name__, c.NET, c.DESERIALIZER.__name__)
"
```

Expected output:

```
Soqucoin mainnet DeserializerSoqucoinAuxPow
SoqucoinStagenet stagenet DeserializerSoqucoinAuxPow
```

CI runs this on every pull request and on every push to `soqucoin`, on Python 3.10 and 3.12. It
installs from a fresh checkout, so only committed files can satisfy it. The same job runs the
fork's tests and upstream's block tests, which parse a block sample of every mainnet coin class.
The Soqucoin sample is mainnet block 58,079.

---

## Configuration

```ini
COIN=Soqucoin
NET=mainnet
DB_DIRECTORY=/path/to/db
DAEMON_URL=http://user:password@127.0.0.1:33389/
SERVICES=tcp://:50001,rpc://
```

Start `electrumx_server` with these in its environment. `DAEMON_URL` points at a `soqucoind` that
runs with `txindex=1`, and 33389 is the mainnet RPC port. `disablewallet=1` on that node is
expected: the indexer reads block and transaction data only. For the test network set
`NET=stagenet` and the RPC port of a stagenet node.

On 2026-11-22 a clean Ubuntu 22.04 machine followed these steps at `soqucoin-1.19.0-3`. The
indexer caught up with a synced mainnet node in 54 seconds, and its database was 82 MB.

---

## Operating notes

ElectrumX stores its history flush counter in 16 bits (`src/electrumx/server/history.py`). A
caught-up server flushes after each block, so at one block a minute the counter reaches 65,535 in
about 45 days. At the limit every flush fails with
`struct.error: 'H' format requires 0 <= number <= 65535` and the index stops at one height. Watch
the `flush #N` line the server logs, and run `electrumx_compact_history` with the server stopped
well before N reaches 65,535. This is upstream behaviour, met sooner on a chain of one-minute
blocks.

---

## Keeping current with upstream

The `soqucoin` branch is based on a pinned upstream commit, so the Soqucoin change stays
reviewable with the command at the top of [What this fork changes](#what-this-fork-changes). To
move to a newer upstream, re-apply that change on the new base, check the mapping of section 3
against the node, and run the tests.

---

## Support

Issues with the Soqucoin-specific parts belong here. Issues reproducible on upstream ElectrumX
belong upstream.
