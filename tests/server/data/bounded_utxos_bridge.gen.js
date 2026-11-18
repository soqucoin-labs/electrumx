// Largest value first; equal values by outpoint, so two reads of an unchanged set agree.
function byValueDesc(a, b) {
  if (a.value !== b.value) return b.value - a.value;
  if (a.tx_hash !== b.tx_hash) return a.tx_hash < b.tx_hash ? -1 : 1;
  return a.tx_pos - b.tx_pos;
}

// At most `limit` entries of an ElectrumX listunspent result, the list as given when it fits.
// The wallet spends confirmed outputs only (height above 0), so confirmed outputs come before
// pending ones, each largest first. A transaction's outputs are kept together while they fit, so a
// sum per transaction over the kept list equals the sum over the whole list (the wallet's activity
// feed sums them); slots no whole transaction fits are filled with the largest outputs left, so a
// transaction paying the address more outputs than the bound still offers its largest to a send.
// The kept set is ordered largest first.
function largestUtxos(utxos, limit) {
  if (utxos.length <= limit) return utxos;
  const ordered = [
    ...utxos.filter((u) => u.height > 0).sort(byValueDesc),
    ...utxos.filter((u) => !(u.height > 0)).sort(byValueDesc),
  ];
  const byTx = new Map();
  for (const u of ordered) {
    const outs = byTx.get(u.tx_hash);
    if (outs) outs.push(u); else byTx.set(u.tx_hash, [u]);
  }
  const kept = new Set();
  for (const u of ordered) {
    if (kept.size === limit) break;
    const outs = byTx.get(u.tx_hash);
    if (!kept.has(u) && kept.size + outs.length <= limit) outs.forEach((o) => kept.add(o));
  }
  for (const u of ordered) {
    if (kept.size === limit) break;
    kept.add(u);
  }
  return [...kept].sort(byValueDesc);
}


// Fixtures for the indexer's bounded_utxos(): random sets run through the bridge's own
// largestUtxos() (soqucoin-ops infrastructure/soqushield-api/server.js at 4fc527b8).
let seed = 20261118;
const rnd = () => { seed = (seed * 1103515245 + 12345) % 2147483648; return seed / 2147483648; };
const hex = (n) => n.toString(16).padStart(12, "0");
const cases = [];
for (let c = 0; c < 150; c++) {
  const limit = [1, 2, 3, 5, 10, 20, 50][Math.floor(rnd() * 7)];
  const ntx = 1 + Math.floor(rnd() * 25);
  const rows = [];
  for (let t = 0; t < ntx; t++) {
    const txh = hex(Math.floor(rnd() * 1e9));
    const nout = rnd() < 0.2 ? 1 + Math.floor(rnd() * 40) : 1 + Math.floor(rnd() * 4);
    const height = rnd() < 0.25 ? (rnd() < 0.5 ? 0 : -1) : 1 + Math.floor(rnd() * 50000);
    for (let p = 0; p < nout; p++) {
      const value = rnd() < 0.4 ? 1000000 : Math.floor(rnd() * 5) * 1000000 + Math.floor(rnd() * 3);
      rows.push({ tx_hash: txh, tx_pos: p, height, value, n_visibility: 0, n_asset_type: 0 });
    }
  }
  // the order ElectrumX would give is irrelevant past the bound; shuffle it
  for (let i = rows.length - 1; i > 0; i--) { const j = Math.floor(rnd() * (i + 1)); [rows[i], rows[j]] = [rows[j], rows[i]]; }
  const kept = largestUtxos(rows, limit).map((u) => [u.tx_hash, u.tx_pos]);
  cases.push({ limit, rows: rows.map((u) => [u.tx_hash, u.tx_pos, u.height, u.value]), kept });
}
process.stdout.write(JSON.stringify({ source: 'soqucoin-ops 4fc527b8 infrastructure/soqushield-api/server.js largestUtxos', cases }));
