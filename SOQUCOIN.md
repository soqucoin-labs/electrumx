# ElectrumX with Soqucoin support

This is a fork of [spesmilo/electrumx](https://github.com/spesmilo/electrumx) adding Soqucoin
(SOQ) support. Upstream does not ship a Soqucoin coin definition, so this fork is what you need in
order to run your own indexer.

**Upstream base:** `24865dc` (`readthedocs: update base image`)
**Branch:** `soqucoin`
**License:** unchanged from upstream — see [`LICENCE`](./LICENCE). Copyright remains with Neil
Booth and the Electrum developers. This fork adds the modifications listed below and asserts no
claim over the original work.

---

## Why an indexer is required

Soqucoin nodes run with `disablewallet=1`, so `soqucoind` exposes no address index. Anything that
needs to watch addresses — an exchange crediting deposits, a wallet, a mining pool — reads UTXOs
from ElectrumX rather than from the node.

If you are integrating Soqucoin, running your own instance of this fork is the recommended path.
It removes any dependency on infrastructure operated by Soqucoin Labs.

---

## What this fork changes

Five files, ~242 lines under `src/`. Everything else is upstream.

### 1. `src/electrumx/lib/coins.py` — the coin definitions

Adds two coin classes:

| Class | `NET` | Address prefix | Notes |
|-------|-------|----------------|-------|
| `Soqucoin` | `mainnet` | `S` (P2PKH verbyte `0x3f`) | genesis `0d828600…86a8` (ceremony 2026-09-02, soqucoin v2.3.0) |
| `SoqucoinStagenet` | `stagenet` | `s` (P2PKH verbyte `0x7d`) | the live test network |

Both override `header_hash` and `block_header` because Soqucoin uses **AuxPoW** (merge-mined
headers), so the header layout is not the plain Bitcoin one.

> `Soqucoin.GENESIS_HASH` is the ceremony genesis
> `0d828600816cbd7c23789660b53f90cb6ec7ff85540698e13845eb2d2f0486a8` (soqucoin v2.3.0,
> `chainparams.cpp`). Until 2026-09-03 it was a zero placeholder, so a mainnet instance built from an
> earlier revision refuses to sync: rebuild from this revision or later. The Soqucoin SDK's ElectrumX
> client checks `server.features` `genesis_hash` against the chain its addresses belong to and refuses
> a server that reports anything else.

### 2. `src/electrumx/lib/tx_soqucoin.py` — the AuxPoW deserializer

`DeserializerSoqucoinAuxPow`, referenced by both coin classes above. Outputs are standard Bitcoin
(value plus scriptPubKey), inherited unchanged from `DeserializerSegWit`, because the byte-less
`CTxOut` migration removed the extension bytes. The only Soqucoin-specific handling is
`read_auxpow()`, which walks the variable-length merge-mining header (parent coinbase, merkle
branches, parent header) so the cursor lands correctly for the rest of the block.

Both coin classes name this class in `DESERIALIZER`, so `coins.py` cannot be imported without it.

### 3. `src/electrumx/server/block_processor.py` — asset and visibility derivation

Soqucoin outputs carry an asset type and a visibility flag. Following the byte-less `CTxOut`
migration these are **derived from the witness version** rather than read from extra output bytes:

```
witness v7  (OP_7, 0x57)  ->  USDSOQ
witness v4  (OP_4, 0x54)  ->  confidential
everything else           ->  transparent native SOQ
```

The result is stored as a 2-byte `[visibility, assetType]` suffix on each UTXO database record, so
`db.py` and `session.py` keep a stable layout.

> ⚠️ **Verify this mapping against the node before relying on it.** The witness-version allocation
> has been revised at least once (a confidential variant moved off v6 after a collision with
> P2WSH-Dilithium). The authoritative allocation lives in `src/script/interpreter.cpp` in the
> Soqucoin node repository, never in documentation. If the indexer's mapping drifts from the
> node's, assets are mislabelled — balances would be wrong without any error being raised.

### 4. `src/electrumx/server/db.py` — UTXO records carry asset metadata

`UTXO` gains `n_visibility` and `n_asset_type`. The stored value field becomes 10 bytes (8-byte
value plus the 2 extra), and reads are length-checked so shorter legacy records still decode.

### 5. `src/electrumx/server/session.py` — protocol additions

- UTXO responses include `n_asset_type`.
- **New method `get_multi_balance(hashX)`** returns confirmed balances split by asset type, so a
  client can distinguish native SOQ from USDSOQ in one call.

> Note for integrators: `get_multi_balance` is a **Soqucoin extension**, not part of the standard
> Electrum protocol. A stock Electrum client will not call it, and you do not need it unless you
> want per-asset balances. Confirmed balances only — unconfirmed is deliberately not split, because
> mempool records lack the asset metadata.

---

## Verifying your checkout

One command, and it is worth running before you build anything on top:

```bash
git clone -b soqucoin https://github.com/soqucoin-labs/electrumx && cd electrumx
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

CI runs exactly this on every push, on Python 3.10 and 3.12, installing from a fresh checkout so
that only committed files can satisfy it. That gate exists because an earlier revision of this
branch was missing `tx_soqucoin.py` and could not be imported from a clean clone at all, while
looking complete from the outside.

---

## Configuration

```ini
COIN=Soqucoin
NET=stagenet          # or mainnet
DB_DIRECTORY=/path/to/db
DAEMON_URL=http://user:password@127.0.0.1:PORT/
SERVICES=tcp://:50001,rpc://
```

Point `DAEMON_URL` at a `soqucoind` instance. `disablewallet=1` on that node is expected and
supported — the indexer only needs block and transaction data.

---

## Keeping current with upstream

The `soqucoin` branch is deliberately based on a **pinned** upstream commit rather than tracking
`master`, so the Soqucoin delta stays reviewable:

```bash
git diff 24865dc..soqucoin -- src/
```

That command is the complete change set. To rebase onto a newer upstream, re-apply it against the
new base and re-run the mapping verification in section 2.

---

## Support

Issues with the Soqucoin-specific parts belong here. Issues reproducible on upstream ElectrumX
belong upstream.
