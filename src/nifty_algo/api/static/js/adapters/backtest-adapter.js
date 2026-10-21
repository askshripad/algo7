// static/js/adapters/backtest-adapter.js
export function mapBacktestResponse(resp) {
    const niftyByTs = new Map((resp.nifty_history || []).map(x => [normTs(x.timestamp), x]));
    const ceByTs = new Map((resp.ce_history || []).map(x => [normTs(x.timestamp), x]));
    const peByTs = new Map((resp.pe_history || []).map(x => [normTs(x.timestamp), x]));
  
    const points = (resp.strangle_premium_1m || []).map(p => {
      const ts = normTs(p.timestamp);
      const ce = ceByTs.get(ts);
      const pe = peByTs.get(ts);
      return {
        ts: new Date(ts),
        totalPremium: Number(p.total_premium),
        nifty: niftyByTs.get(ts)?.close ?? null,
        volumeDiff: ((ce?.volume_delta ?? ce?.volume ?? 0) - (pe?.volume_delta ?? pe?.volume ?? 0)),
      };
    });
  
    return points;
  }
  
  function normTs(ts) {
    return String(ts).replace("T", " ").slice(0, 19);
  }