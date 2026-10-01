# Demo seed

`seed_demo.py` creates two demo companies in a Traced backend, each with its own login:

| Company | Vertical | Login | Demo data |
| --- | --- | --- | --- |
| Nordic Threads (Demo) | TEXTILE | `demo-textile@tracedsystems.com` | 5 garments with full passport data |
| VoltCell Energy (Demo) | BATTERY | `demo-battery@tracedsystems.com` | 4 battery modules: EV, home storage, e-bike, forklift |

Each product gets 5 DPPs. It is one file, Python 3 standard library only.

## Run it on the server

```bash
cd ~/dapp-dw && git pull          # this repo, already cloned on the server
SUPER_ADMIN_EMAIL=support@tracedsystems.com SUPER_ADMIN_PASSWORD='<super admin password>' \
  python3 scripts/seed_demo.py --dry-run          # shows what it would do, changes nothing
SUPER_ADMIN_EMAIL=support@tracedsystems.com SUPER_ADMIN_PASSWORD='<super admin password>' \
  python3 scripts/seed_demo.py
```

It talks to the backend at `http://localhost:8080` — the backend container's port on that
server. Point it elsewhere with `--base-url` (the public URL works too).

At the end it prints both logins and their passwords. Generated passwords are saved to
`demo-credentials.json` (mode 0600) next to the script. Keep that file: a re-run reads it to log
in again. To choose the passwords yourself, set `DEMO_TEXTILE_PASSWORD` / `DEMO_BATTERY_PASSWORD`.

## Safe to re-run

Tenants are found by company name, logins by email, garments by SKU, battery modules by module
ID. Anything that exists is reused, never duplicated, and DPPs are minted only for products that
have none. `--dpps N` sets how many (0 for none). `--only textile` or `--only battery` seeds one.

## Emails

Approving a tenant makes the backend email its contact (`demo-textile-owner@…`,
`demo-battery-owner@…`) a generated admin password, and creating each demo login emails a
welcome. On the server these are real emails. Override the addresses with
`DEMO_TEXTILE_CONTACT`, `DEMO_TEXTILE_LOGIN` and the `DEMO_BATTERY_*` equivalents.

## The data is fictional

Brands, certificate numbers, verifiers and file URLs (`example.com`) are demo values. Standard
names such as GOTS or OEKO-TEX appear as claims, with certificate IDs marked `DEMO-`.

## Anchoring

The DPPs are anchored by whatever the backend is configured for. With
`DPP_CHAIN_PROVIDER=dpp-api` they anchor through dapp-dw. On the Chromia default, garment DPPs
end up `failed` — the deployed Chromia dapp lacks the garment operation — but they are still
created and viewable.
