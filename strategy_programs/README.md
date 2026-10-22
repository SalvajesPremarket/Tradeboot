# Trusted Python strategy programs

The four original strategy sources in this directory are version-controlled, reviewed modules. Their database text is **not** imported or executed dynamically. The allowlist and original strategy IDs live in `strategy_program_registry.py`.

## Simulation endpoint

`POST /strategies/simulate-program` requires the existing strategy-admin token header (`X-Strategy-Admin-Token`). It is replay-only and never submits orders.

Example momentum request:

```json
{
  "strategyId": "292a064d-149c-4187-a50a-1d75a3f7a4cd",
  "symbol": "AAPL",
  "candles": [
    {"open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1, "volume": 100000}
  ],
  "context": {},
  "ticks": [
    {"price": 10.1, "timestamp": "2026-10-21T14:30:01Z", "dayVolume": 100000}
  ]
}
```

For a Pullback program, supply `capital`, `dailyPct`, and `context` as an array of precomputed context dictionaries for the chosen daily/weekly setup. Historical candles and context must come from the caller; this endpoint does not fabricate market history.

## Not yet live execution

The existing continuous scanner currently receives quote snapshots, not the full one-minute tick/candle history required by these strategies. The replay endpoint is a safe integration step, **not** a claim that the four programs are running continuously. A persistent runtime must ingest candles/ticks, build the Pullback daily/weekly context, preserve each strategy's state between ticks, and be tested before enabling any strategy. Demo orders remain disabled.
