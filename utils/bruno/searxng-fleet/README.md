# searxng-fleet (Bruno collection)

## Setup

Paste the fleet's shared secret into `environments/local.bru`
(`secret:`) -- `BROWSER_CRAWL_SECRET` from the deployment's gitignored
`.env`. The gate (`webapp.require_endpoint_secret`) demands it on every
route but `/healthz`, as the `X-Crawl-Secret` header or HTTP basic auth
(any username, the secret as the password). These requests use basic
auth with an empty username -- the same credential Onyx sends by
carrying the secret as URL userinfo. `baseUrl` points at the VM's LAN
address; retarget freely, the gate travels with the instance.

## Running

GUI: open this folder as a Bruno collection, pick the `local`
environment, run.

CLI (sequential, like Onyx's paced client -- do not parallelize, every
request shares the six browser lanes):

    npm install -g @usebruno/cli
    cd utils/bruno/searxng-fleet
    bru run --env local

Fast subset, no lane usage (health probe + the 401 gate checks):

    bru run health gate-checks --env local

## Expectations

- **Searches are slow by design**: max-stealth means a lane types the
  query into the provider's UI with real input -- 20-90 s cold,
  instant for 24 h from `serp_cache`. A challenged search may burn the
  engine budget while the lane keeps solving in the background.
- `unresponsive_engines` in a search response lists engines that gave
  up (challenge, timeout); the web-search test tolerates it, the
  console line surfaces it.
- `mode=bytes` metadata rides the `X-Final-URL` / `X-Crawl-Status` /
  `X-Crawl-Challenge` / `X-Crawl-Lane` headers, not the body.
