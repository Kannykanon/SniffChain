# Arc Safety Scanner

Free token risk checks for Arc mainnet (and Base, used for validation). Paste a token address and get a
buy-then-sell simulation, real round-trip cost in dollars, and plain risk signals before you trade.
Nothing is broadcast and no wallet is needed.

**Live on Arc mainnet (chain 5042):**
[HoneypotSimulator `0x59dFDB2c3c15529CBD0E999Cca42357C103E5E10`](https://explorer.arc.io/address/0x59dFDB2c3c15529CBD0E999Cca42357C103E5E10)
(deploy tx [`0xa43cba3a…`](https://explorer.arc.io/tx/0xa43cba3ae4a102a240335169525df47485cbea5307cb68c5ecd7902b6d0cbd32),
block 22,507,996). Its on-chain bytecode matches `build/HoneypotSimulator.json`. Anyone can use it: call
`simulate(poolManager, poolKey, token, usdcAmount)` through `eth_call` with a balance override on the
contract's address, and you get the buy and sell legs back without spending anything.

## How it works

1. **Round-trip simulation.** `contracts/HoneypotSimulator.sol` buys through the token's Uniswap v4
   pool or V2 pair and immediately sells what it received, at three sizes ($10/$100/$1000 on Arc). It runs inside `eth_call`
   with state overrides (its code and a USDC balance are injected), so no funds move.
   On Arc a native-balance override also funds the USDC ERC-20 at `0x3600…0000`; elsewhere the
   scanner finds the quote token's balance storage slot and overrides that. A failed sell shows the
   token's own revert reason (`Sell blocked`, `Honeypot: You cannot sell!`).
2. **Real cost in dollars.** Arc pays gas in USDC, so fees, taxes and gas all come out in the same
   unit. The report shows what $X becomes after a round trip.
3. **Static checks.** EIP-1167 clone detection (launchpad tokens share one implementation),
   EIP-1967 upgradeable proxies, owner/renounced, and risky functions found in the bytecode.
4. **v4 hook + pool checks.** Hook permissions are decoded from the hook address (can it block swaps
   or take a cut?), and every USDC pool for the token is listed, including LP-fee traps (v4 allows
   fees up to 100%, and we've seen 77–99.999% pools on Arc).
5. **Deterministic verdict.** HIGH / MEDIUM / LOW with reasons.
6. **Plain-English explanation** (`--explain`). An LLM rewords the verdict; it can't change it. Token
   names and symbols are never sent (attacker-written), revert text is cleaned and truncated, and the
   answer is rejected in favour of a template unless it states the fixed verdict, names no other risk
   level and contains no links. Provider is set by `LLM_PROVIDER` (same design as ArcGuard).

## Run

```sh
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # Windows path
python -m scanner.indexer              # one-time: index every v4 pool on Arc (~30 min on the public RPC)
python -m scanner 0x32b966b9f8b792fd30e051f8940900a66a858527
python -m scanner <token> --json
python -m scanner <token> --explain                # plain-English explanation via LLM_PROVIDER
python -m scanner --chain base <token>             # Base: Uniswap V2 pairs vs WETH/USDC
python scripts/probe_rpc.py            # re-check the RPC features the scanner relies on
python tests/test_simulator.py         # end-to-end: synthetic honeypots on live mainnet state
python tests/test_explain.py --live    # explanation checks + prompt injection against the real LLM
python scripts/validate_base.py 60     # compare with honeypot.is on 60 recent Base pairs (resumable)
```

`tests/test_simulator.py` needs no deployment. Inside one `eth_call` it creates a token, opens a v4
pool against real USDC, adds liquidity and runs the simulator. It checks a normal token (passes, 0.6%
loss = fees), a full honeypot (sell caught, token's own revert reason shown), and a token that only
blocks large sells (caught).

## Findings on Arc mainnet (2026-09-24)

- **15,574 of 205,685 Uniswap v4 pools (7.6%) charge LP fees above 10%**, up to 99.999%.
- Many of these trap pools are placed next to legitimate launchpad tokens. One Argus token had 11,
  several of them with liquidity. Through the trap pools, a $10 round trip lost 71–99%.
- In a sweep of 400 recent normal-fee pools, all 307 with liquidity could be bought and sold. The
  risk on Arc today is mostly fee traps and taxes, not blocked sells.

## Web app

`api/app.py` (FastAPI) serves the API and the page in `web/index.html` from one process:

```sh
pip install -r requirements.txt
uvicorn api.app:app --port 8000     # open http://localhost:8000
```

- `GET /scan?token=0x…&explain=true`: the report as display-ready JSON, cached for 5 minutes.
- `GET /health`: status, including how far the background pool index has synced.

The site scans Arc only. A background thread backfills the v4 pool index and then follows the chain
head; until the first backfill finishes on a fresh disk, scans use the slower per-token log scan. At
most 3 scans run at once to stay inside the RPC's rate limit. The page renders every on-chain string
(token names, revert reasons) as text, never HTML.

### Deploying

1. **Contract:** `python scripts/deploy.py` (dry run), then `python scripts/deploy.py --send`. Uses
   `DEPLOYER_PRIVATE_KEY` or `ATTESTER_PRIVATE_KEY` from `.env`; costs about 0.02 USDC.
2. **Web app on Render:** New > Blueprint > this repo. `render.yaml` defines one free web service.
   Set `GROQ_API_KEY`, and preferably `ARC_RPC_URL` to a provider endpoint (Alchemy, QuickNode, dRPC):
   the public RPC rate-limits, and a free instance rebuilds the pool index after every restart.
3. To host the page elsewhere (e.g. Vercel, like ArcGuard), set `<meta name="api-base">` in
   `web/index.html` to the API's URL and `CORS_ORIGINS` on the API to the page's origin.

## Validation against honeypot.is on Base (2026-09-24)

Arc has almost no live sell-blocking honeypots, so the sell-blocked path was checked on Base: the 60
most recent Uniswap V2 WETH pairs with liquidity, with both tools pointed at the same pair.

| | count |
| --- | --- |
| Compared (both tools returned a result) | 58 of 60 |
| Both flag a honeypot | 7 |
| Only we flag | 0 |
| Only honeypot.is flags | 0 |
| Neither | 51 |

Agreement was 100% on the 58 pairs both tools could check. In 3 of the 7 honeypots, honeypot.is
reported a generic router error (`TRANSFER_FROM_FAILED`); the scanner reported the token's own reason.
With one 0.001 WETH trade instead of the scanner's three sizes, agreement was 96.7%: a 100%-sell-tax
token only failed at larger sizes. That's why the scanner always tests several sizes. Caveats: 7
honeypots is a small sample, and honeypot.is is a reference, not ground truth. Raw data:
`data/validation_base_scanner.csv`.

The scanner also gives MEDIUM on 16 of the 60 for things honeypot.is doesn't look at: upgradeable
proxies, active `mint()`, owner fee exemptions.

## Arc RPC facts this depends on (checked 2026-09-23)

| Feature | Public RPC |
| --- | --- |
| `eth_call` state overrides (code, balance) | yes |
| Native balance override visible in USDC `balanceOf` | yes |
| Archive state | yes |
| `debug_traceCall` | **no**, which is why simulation runs in a helper contract |
| `eth_getLogs` | max 100k blocks plus a result-size cap; 429s under load |

## Known limits

- Arc: Uniswap v4 only for now. v3 and V2 forks together carry about 3% of Arc DEX volume.
- Arc's public RPC has no `eth_simulateV1`, so simulation can't run from a plain wallet there. Base's
  does, and a spot check there showed the same result from a wallet as from the simulator contract.
- The simulator swaps from its own address. A token or hook that treats contracts and the
  Universal Router differently could make the result differ from a real wallet's trade.
- Transfer-taxed tokens can't settle in v4 at all. That shows up as a failed sell, which is also
  what a real user would hit.
