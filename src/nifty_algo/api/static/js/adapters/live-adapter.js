// static/js/adapters/live-adapter.js
export function mapLiveHistory(history) {
    return (history || []).map(h => ({
      ts: new Date(h.timestamp),
      totalPremium: Number(h.total_premium ?? 0),
      nifty: h.spot_price ?? null,
      volumeDiff: Number(h.volume_diff ?? ((h.ce_volume || 0) - (h.pe_volume || 0))),
    }));
  }
  
  export function mapLiveSnapshot(s) {
    return {
      ts: new Date(s.timestamp),
      totalPremium: Number(s.total_premium ?? 0),
      nifty: s.spot_price ?? null,
      volumeDiff: Number(s.volume_diff ?? ((s.ce_volume || 0) - (s.pe_volume || 0))),
    };
  }