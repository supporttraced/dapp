#!/usr/bin/env python3
"""
Seed two demo companies in a Traced backend — one textile, one battery — each with
its own login, demo products and DPPs. Pure Python 3 standard library; copy this one
file to the server and run it there.

    python3 seed_demo.py                 # create what is missing, report the rest
    python3 seed_demo.py --dry-run       # show what would happen, change nothing
    python3 seed_demo.py --dpps 10       # DPPs per new product (default 5)

Settings, from flags or the environment (flags win):

    TRACED_BASE_URL          backend URL        default http://localhost:8080
    SUPER_ADMIN_EMAIL        platform admin     required: creates and approves the demo tenants
    SUPER_ADMIN_PASSWORD
    DEMO_TEXTILE_LOGIN       demo textile login default demo-textile@tracedsystems.com
    DEMO_TEXTILE_PASSWORD                       generated if unset
    DEMO_TEXTILE_CONTACT     tenant contact     default demo-textile-owner@tracedsystems.com
    DEMO_BATTERY_LOGIN / DEMO_BATTERY_PASSWORD / DEMO_BATTERY_CONTACT   (same, battery)

Safe to re-run. Tenants are found by company name, logins by email, garments by SKU and
battery modules by module ID; anything that exists is reused, never duplicated, and DPPs
are minted only for products that have none. Generated passwords are saved to
demo-credentials.json (mode 0600) next to this file, so a re-run logs in with the same ones.

Email: approving a tenant makes the backend email the contact a generated admin password,
and creating each demo login emails a welcome. Use addresses you control.

All demo data is fictional: the brands, certificate numbers, verifiers and file URLs
(example.com) are demo values, not real records.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import ssl
import string
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(HERE, "demo-credentials.json")
USER_AGENT = "traced-demo-seed/1.0"   # Cloudflare in front of the public URL bans Python's default one
BULK_MAX = 500                          # per-request limit of the bulk mint endpoints


class ApiError(RuntimeError):
    pass


# ----------------------------------------------------------------------------- http


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats().get("x509_ca"):
        return ctx
    for bundle in ("/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/cert.pem", "/etc/pki/tls/certs/ca-bundle.crt"):
        if os.path.exists(bundle):
            return ssl.create_default_context(cafile=bundle)
    return ctx


SSL = _ssl_context()


class Api:
    def __init__(self, base: str, token: str | None = None):
        self.base = base.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, body=None):
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=120, context=SSL if self.base.startswith("https") else None) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            raise ApiError(f"{method} {path} -> {e.code}: {e.read().decode(errors='replace')[:300]}") from None
        except urllib.error.URLError as e:
            raise ApiError(f"{method} {path} -> {e.reason}") from None

    def login(self, email: str, password: str) -> dict:
        session = self.call("POST", "/api/account/login", {"email": email, "password": password})
        self.token = session["token"]
        return session


# ----------------------------------------------------------------------------- verticals


class Textile:
    vertical, noun = "TEXTILE", "garment"

    @staticmethod
    def key(data):
        return data["identification"]["sku"]

    @staticmethod
    def existing(api):
        """sku -> (product id, version millis) for the tenant's products."""
        found = {}
        for p in api.call("GET", "/api/textile/garments/tenant"):
            if p.get("sku"):
                found[p["sku"]] = (p["id"], api.call("GET", f"/api/textile/garments/{p['id']}")["createdAt"])
        return found

    @staticmethod
    def create(api, data):
        pid = api.call("POST", "/api/textile/garments", {"data": data})["id"]
        return pid, api.call("GET", f"/api/textile/garments/{pid}")["createdAt"]

    @staticmethod
    def dpp_counts(api):
        counts = {}
        for d in api.call("GET", "/api/textile/dpp/tenant"):
            counts[d["productId"]] = counts.get(d["productId"], 0) + 1
        return counts

    @staticmethod
    def mint(api, pid, version, count):
        return api.call("POST", "/api/textile/dpp/bulk",
                        {"productId": pid, "productCreatedAt": version, "count": count})["dpps"]

    @staticmethod
    def public_path(dpp_id):
        return f"/api/textile/dpp/{dpp_id}/public"


class Battery:
    vertical, noun = "BATTERY", "module"

    @staticmethod
    def key(data):
        return data["moduleId"]

    @staticmethod
    def existing(api):
        return {m["moduleId"]: (m["id"], m["createdAtMillis"])
                for m in api.call("GET", "/api/battery/modules/tenant") if m.get("moduleId")}

    @staticmethod
    def create(api, data):
        mid = api.call("POST", "/api/battery/modules", {"data": data})["id"]
        return mid, api.call("GET", f"/api/battery/modules/{mid}")["createdAtMillis"]

    @staticmethod
    def dpp_counts(api):
        counts = {}
        for d in api.call("GET", "/api/battery/dpp/tenant"):
            counts[d["moduleId"]] = counts.get(d["moduleId"], 0) + 1
        return counts

    @staticmethod
    def mint(api, mid, version, count):
        return api.call("POST", "/api/battery/dpp/bulk",
                        {"moduleId": mid, "moduleCreatedAt": version, "count": count})["dpps"]

    @staticmethod
    def public_path(dpp_id):
        return f"/api/battery/dpp/{dpp_id}/public"


DEMOS = [
    {"key": "textile", "kind": Textile, "company": "Nordic Threads (Demo)", "country": "Sweden",
     "contact_name": "Nordic Threads Owner", "login_name": "Nordic Threads Demo"},
    {"key": "battery", "kind": Battery, "company": "VoltCell Energy (Demo)", "country": "Germany",
     "contact_name": "VoltCell Owner", "login_name": "VoltCell Demo"},
]


# ----------------------------------------------------------------------------- setup


def settings_for(demo, args, saved):
    k = demo["key"].upper()
    login = getattr(args, f"{demo['key']}_login") or os.environ.get(f"DEMO_{k}_LOGIN") or f"demo-{demo['key']}@tracedsystems.com"
    contact = os.environ.get(f"DEMO_{k}_CONTACT") or f"demo-{demo['key']}-owner@tracedsystems.com"
    password = (getattr(args, f"{demo['key']}_password") or os.environ.get(f"DEMO_{k}_PASSWORD")
                or saved.get(login))
    generated = False
    if not password:
        alphabet = string.ascii_letters + string.digits
        password = "Demo-" + "".join(secrets.choice(alphabet) for _ in range(16)) + "!"
        generated = True
    return login, contact, password, generated


def ensure_tenant(admin, demo, contact, dry):
    tenants = [t for t in admin.call("GET", "/api/tenant") if t.get("companyName") == demo["company"]]
    if tenants:
        t = tenants[0]
        if t.get("status") in ("pending", "under_review"):
            print(f"  tenant exists but is {t['status']} — approving")
            if not dry:
                admin.call("POST", "/api/tenant/approve", {"tenantId": t["id"]})
        else:
            print(f"  tenant   {t['id']}  (exists, {t.get('status')})")
        return t["id"]
    print(f"  tenant   creating \"{demo['company']}\" ({demo['kind'].vertical}); approval email -> {contact}")
    if dry:
        return None
    t = admin.call("POST", "/api/tenant/request-access", {
        "companyName": demo["company"], "vertical": demo["kind"].vertical, "country": demo["country"],
        "fullName": demo["contact_name"], "businessEmail": contact, "phoneNumber": "+46000000000",
        "jobTitle": "Demo owner", "intendedUseCase": "Demo account", "requestedPlan": "demo"})
    admin.call("POST", "/api/tenant/approve", {"tenantId": t["id"]})
    print(f"           {t['id']}  (created, approved)")
    return t["id"]


def ensure_login(admin, demo, tenant_id, login, password, dry):
    if tenant_id:
        for a in admin.call("GET", f"/api/account/tenant/{tenant_id}"):
            if str(a.get("email", "")).lower() == login.lower():
                print(f"  login    {login}  (exists)")
                return False
    print(f"  login    creating {login}; welcome email -> {login}")
    if not dry:
        admin.call("POST", "/api/account", {"tenantId": tenant_id, "name": demo["login_name"], "email": login,
                                            "username": login, "password": password, "roles": ["TENANT_ADMIN"]})
    return True


def seed_products(api, kind, catalogue, dpps, dry):
    existing = {} if dry and api.token is None else kind.existing(api)
    counts = {} if dry and api.token is None else kind.dpp_counts(api)
    for data in catalogue:
        key = kind.key(data)
        if key in existing:
            pid, version = existing[key]
            state = "exists"
        else:
            if dry:
                print(f"  {kind.noun:<8} {key:<14} would create" + (f" + {dpps} DPPs" if dpps else ""))
                continue
            pid, version = kind.create(api, data)
            state = "created"
        have = counts.get(pid, 0)
        if have or not dpps:
            print(f"  {kind.noun:<8} {key:<14} {state:<8} DPPs: {have}")
            continue
        if dry:
            print(f"  {kind.noun:<8} {key:<14} {state:<8} would mint {dpps} DPPs")
            continue
        minted, left = [], dpps
        while left:
            batch = kind.mint(api, pid, version, min(left, BULK_MAX))
            minted += batch
            left -= len(batch)
            if not batch:
                break
        print(f"  {kind.noun:<8} {key:<14} {state:<8} DPPs: minted {len(minted)}  "
              f"({minted[0]['serialNumber']} .. {minted[-1]['serialNumber']})")


# ----------------------------------------------------------------------------- main


def main() -> int:
    p = argparse.ArgumentParser(description="Seed textile and battery demo accounts in a Traced backend.")
    p.add_argument("--base-url", default=os.environ.get("TRACED_BASE_URL", "http://localhost:8080"))
    p.add_argument("--super-admin-email", default=os.environ.get("SUPER_ADMIN_EMAIL"))
    p.add_argument("--super-admin-password", default=os.environ.get("SUPER_ADMIN_PASSWORD"))
    p.add_argument("--textile-login"); p.add_argument("--textile-password")
    p.add_argument("--battery-login"); p.add_argument("--battery-password")
    p.add_argument("--dpps", type=int, default=5, help="DPPs to mint per product that has none (default 5; 0 = none)")
    p.add_argument("--only", choices=("textile", "battery"), help="seed just one of the two")
    p.add_argument("--dry-run", action="store_true", help="report what would change, change nothing")
    args = p.parse_args()

    if not args.super_admin_email or not args.super_admin_password:
        print("SUPER_ADMIN_EMAIL and SUPER_ADMIN_PASSWORD are required (env or flags)", file=sys.stderr)
        return 2

    saved = {}
    if os.path.exists(CREDENTIALS_FILE):
        with open(CREDENTIALS_FILE) as f:
            saved = json.load(f)

    admin = Api(args.base_url)
    try:
        admin.login(args.super_admin_email, args.super_admin_password)
    except ApiError as e:
        print(f"super-admin login failed: {e}", file=sys.stderr)
        return 1
    print(f"backend {args.base_url}{'   (dry run: nothing is changed)' if args.dry_run else ''}")

    catalogues = {"textile": DEMO_TEXTILE, "battery": DEMO_BATTERY}
    summary = []
    for demo in DEMOS:
        if args.only and demo["key"] != args.only:
            continue
        kind = demo["kind"]
        login, contact, password, generated = settings_for(demo, args, saved)
        print(f"\n== {demo['company']} — {kind.vertical.lower()} ==")
        try:
            tenant_id = ensure_tenant(admin, demo, contact, args.dry_run)
            created = ensure_login(admin, demo, tenant_id, login, password, args.dry_run)
            if generated and not created and login not in saved:
                print(f"  ! {login} already exists but its password is not in DEMO_{demo['key'].upper()}_PASSWORD "
                      f"or {os.path.basename(CREDENTIALS_FILE)}; set it to seed products", file=sys.stderr)
                summary.append((demo, login, None, "password unknown"))
                continue
            if not args.dry_run:
                saved[login] = password
                with open(CREDENTIALS_FILE, "w") as f:
                    json.dump(saved, f, indent=2)
                os.chmod(CREDENTIALS_FILE, 0o600)
            user = Api(args.base_url)
            if not args.dry_run:
                session = user.login(login, password)
                if session.get("vertical") != kind.vertical:
                    raise ApiError(f"{login} logged in with vertical {session.get('vertical')}, expected {kind.vertical}")
            seed_products(user, kind, catalogues[demo["key"]], args.dpps, args.dry_run)
            # A dry run saves nothing, so a generated password is not the one a real run will use.
            summary.append((demo, login, "(generated on the real run)" if args.dry_run and generated else password, "ok"))
        except ApiError as e:
            print(f"  ERROR {e}", file=sys.stderr)
            summary.append((demo, login, None, f"failed: {e}"))

    print("\n== demo logins ==")
    for demo, login, password, state in summary:
        shown = password if password else "(unchanged)"
        print(f"  {demo['kind'].vertical:<8} {login:<36} {shown:<28} {state}")
    if not args.dry_run:
        print(f"\npasswords are kept in {CREDENTIALS_FILE} (mode 0600)")
    return 0 if all(s[3] == "ok" for s in summary) else 1


# ----------------------------------------------------------------------------- demo data (fictional)

DEMO_TEXTILE = [{'product': {'productName': 'Field Jacket',
              'brandName': 'Nordic Threads',
              'productCategory': 'Jackets',
              'description': 'Cotton canvas field jacket with corozo buttons.',
              'intendedUse': 'Everyday casual wear',
              'seasonOrCollection': 'AW26 / SwePass',
              'gender': 'unisex',
              'ageGroup': 'adult',
              'sizeSystem': 'EU',
              'availableSizes': ['XS', 'S', 'M', 'L', 'XL', 'XXL'],
              'availableColors': ['Olive', 'Navy'],
              'images': ['https://example.com/demo/NT-1043-front.jpg']},
  'identification': {'sku': 'NT-1043-OLI',
                     'styleCode': 'NT-1043',
                     'modelNumber': 'NT-1043-MDL',
                     'gtin': '07350012340017',
                     'hsCode': '6201.40'},
  'fiberComposition': {'declaredLabelName': '70% Organic Cotton, 28% Polyester, 2% Elastane',
                       'components': [{'fiberName': 'Organic Cotton',
                                       'percentage': 70.0,
                                       'bioBasedContentPercent': 100.0,
                                       'originCountry': 'TR',
                                       'certified': True,
                                       'certificationReference': 'DEMO-GOTS-1043'},
                                      {'fiberName': 'Polyester',
                                       'percentage': 28.0,
                                       'bioBasedContentPercent': 0.0,
                                       'originCountry': 'TR',
                                       'certified': True,
                                       'certificationReference': 'DEMO-GOTS-1043'},
                                      {'fiberName': 'Elastane',
                                       'percentage': 2.0,
                                       'bioBasedContentPercent': 0.0,
                                       'originCountry': 'TR',
                                       'certified': True,
                                       'certificationReference': 'DEMO-GOTS-1043'}],
                       'totalRecycledContentPercent': 18.0,
                       'bioBasedContentPercent': 0.0},
  'textileTechnicalDetails': {'fabricConstruction': 'Plain weave canvas',
                              'fabricType': 'Cotton canvas',
                              'gsm': 320.0,
                              'pattern': 'Solid',
                              'finish': 'Enzyme washed',
                              'coatingOrLamination': 'PU-free water repellent'},
  'manufacturingModel': {'countryOfOrigin': 'PT',
                         'productionSteps': [{'stepName': 'Cut & Sew',
                                              'facilityName': 'Atlântico Confecções — Cut & Sew',
                                              'country': 'PT',
                                              'address': 'Rua da Indústria 214, 4760-563, Vila Nova de '
                                                         'Famalicão, Braga',
                                              'operatorName': 'Rui Mendes',
                                              'processDescription': 'Cut & Sew, Finishing'},
                                             {'stepName': 'Dyeing',
                                              'facilityName': 'Tinturaria do Ave',
                                              'country': 'PT',
                                              'address': 'Zona Industrial lote 9, 4795-321, Santo Tirso, '
                                                         'Porto',
                                              'operatorName': 'Rui Mendes',
                                              'processDescription': 'Dyeing'}]},
  'chemicalCompliance': {'reachCompliant': True,
                         'restrictedSubstancesChecked': True,
                         'svhcPresent': False,
                         'restrictedSubstancesListReference': 'Nordic Threads RSL, 2026 edition (demo)'},
  'environmentalPerformance': {'carbonFootprint': {'unit': 'kg CO2e',
                                                   'systemBoundary': 'cradle-to-gate',
                                                   'methodology': 'ISO 14067',
                                                   'verified': True,
                                                   'value': 12.4,
                                                   'verificationReference': 'Demo verifier, 2026'}},
  'useAndCare': {'washingInstructions': 'Machine wash 30°C, inside out, with similar colours.',
                 'dryingInstructions': 'Do not tumble dry. Dry flat in shade.',
                 'ironingInstructions': 'Iron on medium heat, reverse side.',
                 'bleachingInstructions': 'Do not bleach.',
                 'professionalCareInstructions': 'Dry clean with P solvent if needed.',
                 'careSymbols': ['wash_30',
                                 'no_bleach',
                                 'do_not_tumble_dry',
                                 'dry_flat',
                                 'iron_medium',
                                 'dry_clean_p',
                                 'dry_clean',
                                 'wash_inside_out']},
  'circularity': {'recyclabilityInformation': 'Fabric is suitable for mechanical fibre recovery once trims '
                                              'are removed.',
                  'endOfLifeInstructions': 'Hand the garment in dry and clean at a Human Bridge collection '
                                           'point.',
                  'materialSeparationGuidance': 'Remove metal trims, zippers and buttons before recycling '
                                                'the fabric.',
                  'takeBackProgramAvailable': True,
                  'takeBackProgramDetails': 'Return the garment to a Human Bridge collection point.',
                  'separateCollectionRequired': True,
                  'reusable': True,
                  'repairable': True,
                  'recyclable': True},
  'certifications': [{'name': 'GOTS',
                      'certificateId': 'DEMO-GOTS-1043-1',
                      'issuingBody': 'Demo Certification Body',
                      'validFrom': '2026-01-01',
                      'validTo': '2026-12-31',
                      'scope': 'Organic cotton, cut & sew',
                      'certificateUrl': 'https://example.com/demo/certificates/gots-nt-1043'},
                     {'name': 'OEKO-TEX Standard 100',
                      'certificateId': 'DEMO-OEKOTEXSTA-1043-2',
                      'issuingBody': 'Demo Certification Body',
                      'validFrom': '2026-02-15',
                      'validTo': '2027-02-14',
                      'scope': 'Finished garment',
                      'certificateUrl': 'https://example.com/demo/certificates/oekotexsta-nt-1043'}],
  'documents': [{'documentType': 'measurement_chart',
                 'title': 'NT-1043-measurement-chart.pdf',
                 'url': 'https://example.com/demo/NT-1043-measurement-chart.pdf',
                 'issuedBy': 'Nordic Threads'}]},
 {'product': {'productName': 'Merino Crew Knit',
              'brandName': 'Nordic Threads',
              'productCategory': 'Knitwear',
              'description': 'Fine-gauge merino wool crew neck.',
              'intendedUse': 'Everyday casual wear',
              'seasonOrCollection': 'AW26 / SwePass',
              'gender': 'unisex',
              'ageGroup': 'adult',
              'sizeSystem': 'EU',
              'availableSizes': ['S', 'M', 'L', 'XL'],
              'availableColors': ['Charcoal', 'Oat'],
              'images': ['https://example.com/demo/NT-1044-front.jpg']},
  'identification': {'sku': 'NT-1044-CHA',
                     'styleCode': 'NT-1044',
                     'modelNumber': 'NT-1044-MDL',
                     'gtin': '07350012340024',
                     'hsCode': '6110.11'},
  'fiberComposition': {'declaredLabelName': '100% Merino Wool',
                       'components': [{'fiberName': 'Merino Wool',
                                       'percentage': 100.0,
                                       'bioBasedContentPercent': 100.0,
                                       'originCountry': 'AU',
                                       'certified': True,
                                       'certificationReference': 'DEMO-RWS-1044'}],
                       'bioBasedContentPercent': 100.0},
  'textileTechnicalDetails': {'fabricConstruction': 'Jersey knit',
                              'fabricType': 'Merino jersey',
                              'gsm': 240.0,
                              'pattern': 'Solid',
                              'finish': 'Anti-pilling'},
  'manufacturingModel': {'countryOfOrigin': 'LT',
                         'productionSteps': [{'stepName': 'Knitting',
                                              'facilityName': 'Baltic Knitwear — Knitting',
                                              'country': 'LT',
                                              'address': 'Savanoriu pr. 180, 44151, Kaunas, Kauno',
                                              'operatorName': 'Egle Kazlauskas',
                                              'processDescription': 'Knitting, Linking'},
                                             {'stepName': 'Cut & Sew',
                                              'facilityName': 'Baltic Knitwear — Assembly',
                                              'country': 'LT',
                                              'address': 'Draugystes g. 19, 51230, Kaunas, Kauno',
                                              'operatorName': 'Egle Kazlauskas',
                                              'processDescription': 'Cut & Sew'}]},
  'chemicalCompliance': {'reachCompliant': True,
                         'restrictedSubstancesChecked': True,
                         'svhcPresent': False,
                         'restrictedSubstancesListReference': 'Nordic Threads RSL, 2026 edition (demo)'},
  'environmentalPerformance': {'carbonFootprint': {'unit': 'kg CO2e',
                                                   'systemBoundary': 'cradle-to-gate',
                                                   'methodology': 'ISO 14067',
                                                   'verified': False,
                                                   'value': 9.1}},
  'useAndCare': {'washingInstructions': 'Hand wash cold, or wool cycle 30°C.',
                 'dryingInstructions': 'Do not tumble dry. Dry flat in shade.',
                 'ironingInstructions': 'Iron on medium heat, reverse side.',
                 'bleachingInstructions': 'Do not bleach.',
                 'professionalCareInstructions': 'Dry clean with P solvent if needed.',
                 'careSymbols': ['wash_30',
                                 'hand_wash',
                                 'wool_cycle',
                                 'no_bleach',
                                 'do_not_tumble_dry',
                                 'dry_flat',
                                 'iron_medium',
                                 'dry_clean_p',
                                 'dry_clean']},
  'circularity': {'recyclabilityInformation': 'Fabric is suitable for mechanical fibre recovery once trims '
                                              'are removed.',
                  'endOfLifeInstructions': 'Hand the garment in dry and clean at a Human Bridge collection '
                                           'point.',
                  'materialSeparationGuidance': 'Remove metal trims, zippers and buttons before recycling '
                                                'the fabric.',
                  'takeBackProgramAvailable': True,
                  'takeBackProgramDetails': 'Return the garment to a Human Bridge collection point.',
                  'separateCollectionRequired': True,
                  'reusable': True,
                  'repairable': True,
                  'recyclable': True},
  'certifications': [{'name': 'Responsible Wool Standard',
                      'certificateId': 'DEMO-RESPONSIBL-1044-1',
                      'issuingBody': 'Demo Certification Body',
                      'validFrom': '2026-03-01',
                      'validTo': '2027-02-28',
                      'scope': 'Merino wool supply chain',
                      'certificateUrl': 'https://example.com/demo/certificates/responsibl-nt-1044'},
                     {'name': 'OEKO-TEX Standard 100'}],
  'documents': [{'documentType': 'measurement_chart',
                 'title': 'NT-1044-measurement-chart.pdf',
                 'url': 'https://example.com/demo/NT-1044-measurement-chart.pdf',
                 'issuedBy': 'Nordic Threads'}]},
 {'product': {'productName': 'Everyday Tee',
              'brandName': 'Nordic Threads',
              'productCategory': 'T-Shirts',
              'description': 'Heavyweight jersey T-shirt.',
              'intendedUse': 'Everyday casual wear',
              'seasonOrCollection': 'AW26 / SwePass',
              'gender': 'unisex',
              'ageGroup': 'adult',
              'sizeSystem': 'EU',
              'availableSizes': ['XS', 'S', 'M', 'L', 'XL', 'XXL'],
              'availableColors': ['White', 'Black', 'Sand'],
              'images': ['https://example.com/demo/NT-1045-front.jpg']},
  'identification': {'sku': 'NT-1045-WHI',
                     'styleCode': 'NT-1045',
                     'modelNumber': 'NT-1045-MDL',
                     'gtin': '07350012340031',
                     'hsCode': '6109.10'},
  'fiberComposition': {'declaredLabelName': '60% Organic Cotton, 40% Recycled Polyester',
                       'components': [{'fiberName': 'Organic Cotton',
                                       'percentage': 60.0,
                                       'bioBasedContentPercent': 100.0,
                                       'originCountry': 'IN',
                                       'certified': True,
                                       'certificationReference': 'DEMO-GOTS-1045'},
                                      {'fiberName': 'Recycled Polyester',
                                       'percentage': 40.0,
                                       'recycledContentPercent': 100.0,
                                       'bioBasedContentPercent': 0.0,
                                       'originCountry': 'IN',
                                       'certified': True,
                                       'certificationReference': 'DEMO-GOTS-1045'}],
                       'totalRecycledContentPercent': 40.0,
                       'bioBasedContentPercent': 0.0},
  'textileTechnicalDetails': {'fabricConstruction': 'Single jersey',
                              'fabricType': 'Single jersey',
                              'gsm': 210.0,
                              'pattern': 'Solid',
                              'finish': 'Bio-polished'},
  'manufacturingModel': {'countryOfOrigin': 'PT',
                         'productionSteps': [{'stepName': 'Cut & Sew',
                                              'facilityName': 'Nordic Garment — Confection',
                                              'country': 'SE',
                                              'address': 'Textilgatan 7, 50462, Borås, Västra Götaland',
                                              'operatorName': 'Lina Berg',
                                              'processDescription': 'Cut & Sew, Printing'}]},
  'chemicalCompliance': {'reachCompliant': True,
                         'restrictedSubstancesChecked': True,
                         'svhcPresent': False,
                         'restrictedSubstancesListReference': 'Nordic Threads RSL, 2026 edition (demo)'},
  'environmentalPerformance': {'carbonFootprint': {'unit': 'kg CO2e',
                                                   'systemBoundary': 'cradle-to-gate',
                                                   'methodology': 'ISO 14067',
                                                   'verified': False,
                                                   'value': 4.7}},
  'useAndCare': {'washingInstructions': 'Machine wash 40°C. Wash dark colours separately.',
                 'dryingInstructions': 'Do not tumble dry. Dry flat in shade.',
                 'ironingInstructions': 'Iron on medium heat, reverse side.',
                 'bleachingInstructions': 'Do not bleach.',
                 'professionalCareInstructions': 'Dry clean with P solvent if needed.',
                 'careSymbols': ['wash_40',
                                 'no_bleach',
                                 'do_not_tumble_dry',
                                 'dry_flat',
                                 'iron_medium',
                                 'dry_clean_p',
                                 'dry_clean',
                                 'wash_separately']},
  'circularity': {'recyclabilityInformation': 'Fabric is suitable for mechanical fibre recovery once trims '
                                              'are removed.',
                  'endOfLifeInstructions': 'Hand the garment in dry and clean at a Human Bridge collection '
                                           'point.',
                  'materialSeparationGuidance': 'Remove metal trims, zippers and buttons before recycling '
                                                'the fabric.',
                  'takeBackProgramAvailable': True,
                  'takeBackProgramDetails': 'Return the garment to a Human Bridge collection point.',
                  'separateCollectionRequired': True,
                  'reusable': True,
                  'repairable': True,
                  'recyclable': True},
  'certifications': [{'name': 'GOTS',
                      'certificateId': 'DEMO-GOTS-1045-1',
                      'issuingBody': 'Demo Certification Body',
                      'validFrom': '2026-01-01',
                      'validTo': '2026-12-31',
                      'scope': 'Organic cotton jersey',
                      'certificateUrl': 'https://example.com/demo/certificates/gots-nt-1045'},
                     {'name': 'Global Recycled Standard',
                      'certificateId': 'DEMO-GLOBALRECY-1045-2',
                      'issuingBody': 'Demo Certification Body',
                      'validFrom': '2026-01-01',
                      'validTo': '2026-12-31',
                      'scope': 'Recycled polyester content',
                      'certificateUrl': 'https://example.com/demo/certificates/globalrecy-nt-1045'}],
  'documents': [{'documentType': 'measurement_chart',
                 'title': 'NT-1045-measurement-chart.pdf',
                 'url': 'https://example.com/demo/NT-1045-measurement-chart.pdf',
                 'issuedBy': 'Nordic Threads'}]},
 {'product': {'productName': 'Work Trouser',
              'brandName': 'Nordic Threads',
              'productCategory': 'Trousers',
              'description': 'Straight-leg cotton twill trouser.',
              'intendedUse': 'Everyday casual wear',
              'seasonOrCollection': 'AW26 / SwePass',
              'gender': 'unisex',
              'ageGroup': 'adult',
              'sizeSystem': 'EU',
              'availableSizes': ['44', '46', '48', '50', '52', '54'],
              'availableColors': ['Stone', 'Black'],
              'images': ['https://example.com/demo/NT-1046-front.jpg']},
  'identification': {'sku': 'NT-1046-STO',
                     'styleCode': 'NT-1046',
                     'modelNumber': 'NT-1046-MDL',
                     'gtin': '07350012340048',
                     'hsCode': '6203.42'},
  'fiberComposition': {'declaredLabelName': '98% Cotton, 2% Elastane',
                       'components': [{'fiberName': 'Cotton',
                                       'percentage': 98.0,
                                       'bioBasedContentPercent': 100.0,
                                       'originCountry': 'PT'},
                                      {'fiberName': 'Elastane',
                                       'percentage': 2.0,
                                       'bioBasedContentPercent': 0.0,
                                       'originCountry': 'PT'}],
                       'bioBasedContentPercent': 0.0},
  'textileTechnicalDetails': {'fabricConstruction': '3/1 twill',
                              'fabricType': 'Cotton twill',
                              'gsm': 290.0,
                              'pattern': 'Solid',
                              'finish': 'Sanforized'},
  'manufacturingModel': {'countryOfOrigin': 'PT',
                         'productionSteps': [{'stepName': 'Cut & Sew',
                                              'facilityName': 'Atlântico Confecções — Cut & Sew',
                                              'country': 'PT',
                                              'address': 'Rua da Indústria 214, 4760-563, Vila Nova de '
                                                         'Famalicão, Braga',
                                              'operatorName': 'Rui Mendes',
                                              'processDescription': 'Cut & Sew, Finishing'},
                                             {'stepName': 'Dyeing',
                                              'facilityName': 'Tinturaria do Ave',
                                              'country': 'PT',
                                              'address': 'Zona Industrial lote 9, 4795-321, Santo Tirso, '
                                                         'Porto',
                                              'operatorName': 'Rui Mendes',
                                              'processDescription': 'Dyeing'}]},
  'chemicalCompliance': {'reachCompliant': True,
                         'restrictedSubstancesChecked': True,
                         'svhcPresent': False,
                         'restrictedSubstancesListReference': 'Nordic Threads RSL, 2026 edition (demo)'},
  'environmentalPerformance': {'carbonFootprint': {'unit': 'kg CO2e',
                                                   'systemBoundary': 'cradle-to-gate',
                                                   'methodology': 'ISO 14067',
                                                   'verified': False,
                                                   'value': 8.3}},
  'useAndCare': {'washingInstructions': 'Machine wash 40°C. Do not tumble dry.',
                 'dryingInstructions': 'Do not tumble dry. Dry flat in shade.',
                 'ironingInstructions': 'Iron on medium heat, reverse side.',
                 'bleachingInstructions': 'Do not bleach.',
                 'professionalCareInstructions': 'Dry clean with P solvent if needed.',
                 'careSymbols': ['wash_40',
                                 'no_bleach',
                                 'do_not_tumble_dry',
                                 'dry_flat',
                                 'iron_medium',
                                 'dry_clean_p',
                                 'dry_clean']},
  'circularity': {'recyclabilityInformation': 'Fabric is suitable for mechanical fibre recovery once trims '
                                              'are removed.',
                  'endOfLifeInstructions': 'Hand the garment in dry and clean at a Human Bridge collection '
                                           'point.',
                  'materialSeparationGuidance': 'Remove metal trims, zippers and buttons before recycling '
                                                'the fabric.',
                  'takeBackProgramAvailable': True,
                  'takeBackProgramDetails': 'Return the garment to a Human Bridge collection point.',
                  'separateCollectionRequired': True,
                  'reusable': True,
                  'repairable': True,
                  'recyclable': True},
  'certifications': [{'name': 'OEKO-TEX Standard 100',
                      'certificateId': 'DEMO-OEKOTEXSTA-1046-1',
                      'issuingBody': 'Demo Certification Body',
                      'validFrom': '2026-02-15',
                      'validTo': '2027-02-14',
                      'scope': 'Finished garment',
                      'certificateUrl': 'https://example.com/demo/certificates/oekotexsta-nt-1046'}],
  'documents': [{'documentType': 'measurement_chart',
                 'title': 'NT-1046-measurement-chart.pdf',
                 'url': 'https://example.com/demo/NT-1046-measurement-chart.pdf',
                 'issuedBy': 'Nordic Threads'}]},
 {'product': {'productName': 'Recycled Shell Parka',
              'brandName': 'Nordic Threads',
              'productCategory': 'Outerwear',
              'description': 'Water-repellent shell parka, PFAS-free finish.',
              'intendedUse': 'Everyday casual wear',
              'seasonOrCollection': 'AW26 / SwePass',
              'gender': 'unisex',
              'ageGroup': 'adult',
              'sizeSystem': 'EU',
              'availableSizes': ['S', 'M', 'L', 'XL'],
              'availableColors': ['Slate'],
              'images': ['https://example.com/demo/NT-1047-front.jpg']},
  'identification': {'sku': 'NT-1047-SLA',
                     'styleCode': 'NT-1047',
                     'modelNumber': 'NT-1047-MDL',
                     'gtin': '07350012340055',
                     'hsCode': '6201.30'},
  'fiberComposition': {'declaredLabelName': '85% Recycled Polyamide, 15% Polyurethane',
                       'components': [{'fiberName': 'Recycled Polyamide',
                                       'percentage': 85.0,
                                       'recycledContentPercent': 100.0,
                                       'bioBasedContentPercent': 0.0,
                                       'originCountry': 'TW',
                                       'certified': True,
                                       'certificationReference': 'DEMO-GRS-1047'},
                                      {'fiberName': 'Polyurethane',
                                       'percentage': 15.0,
                                       'bioBasedContentPercent': 0.0,
                                       'originCountry': 'TW',
                                       'certified': True,
                                       'certificationReference': 'DEMO-GRS-1047'}],
                       'totalRecycledContentPercent': 85.0,
                       'bioBasedContentPercent': 0.0},
  'textileTechnicalDetails': {'fabricConstruction': 'Ripstop',
                              'fabricType': 'Recycled ripstop',
                              'gsm': 180.0,
                              'pattern': 'Solid',
                              'finish': 'PFAS-free DWR',
                              'coatingOrLamination': 'Polyurethane membrane'},
  'manufacturingModel': {'countryOfOrigin': 'TR',
                         'productionSteps': [{'stepName': 'Knitting',
                                              'facilityName': 'Baltic Knitwear — Knitting',
                                              'country': 'LT',
                                              'address': 'Savanoriu pr. 180, 44151, Kaunas, Kauno',
                                              'operatorName': 'Egle Kazlauskas',
                                              'processDescription': 'Knitting, Linking'},
                                             {'stepName': 'Cut & Sew',
                                              'facilityName': 'Baltic Knitwear — Assembly',
                                              'country': 'LT',
                                              'address': 'Draugystes g. 19, 51230, Kaunas, Kauno',
                                              'operatorName': 'Egle Kazlauskas',
                                              'processDescription': 'Cut & Sew'}]},
  'chemicalCompliance': {'reachCompliant': True,
                         'restrictedSubstancesChecked': True,
                         'svhcPresent': True,
                         'restrictedSubstancesListReference': 'Nordic Threads RSL, 2026 edition (demo)',
                         'svhcDetails': [{'substanceName': 'Bis(2-ethylhexyl) phthalate (DEHP)',
                                          'casNumber': '117-81-7',
                                          'ecNumber': '204-211-0',
                                          'concentrationPercent': 0.03,
                                          'locationInProduct': 'Zipper pull coating',
                                          'safeUseInformation': 'No risk in normal use; do not chew or '
                                                                'ingest.'}]},
  'environmentalPerformance': {'carbonFootprint': {'unit': 'kg CO2e',
                                                   'systemBoundary': 'cradle-to-gate',
                                                   'methodology': 'ISO 14067',
                                                   'verified': False,
                                                   'value': 16.8}},
  'useAndCare': {'washingInstructions': 'Machine wash 30°C. Do not use fabric softener.',
                 'dryingInstructions': 'Do not tumble dry. Dry flat in shade.',
                 'ironingInstructions': 'Iron on medium heat, reverse side.',
                 'bleachingInstructions': 'Do not bleach.',
                 'professionalCareInstructions': 'Dry clean with P solvent if needed.',
                 'careSymbols': ['wash_30',
                                 'no_bleach',
                                 'do_not_tumble_dry',
                                 'dry_flat',
                                 'iron_medium',
                                 'dry_clean_p',
                                 'dry_clean',
                                 'do_not_use_softener']},
  'circularity': {'recyclabilityInformation': 'Fabric is suitable for mechanical fibre recovery once trims '
                                              'are removed.',
                  'endOfLifeInstructions': 'Hand the garment in dry and clean at a Human Bridge collection '
                                           'point.',
                  'materialSeparationGuidance': 'Remove metal trims, zippers and buttons before recycling '
                                                'the fabric.',
                  'takeBackProgramAvailable': True,
                  'takeBackProgramDetails': 'Return the garment to a Human Bridge collection point.',
                  'separateCollectionRequired': True,
                  'reusable': True,
                  'repairable': True,
                  'recyclable': True},
  'certifications': [{'name': 'Global Recycled Standard',
                      'certificateId': 'DEMO-GLOBALRECY-1047-1',
                      'issuingBody': 'Demo Certification Body',
                      'validFrom': '2026-01-01',
                      'validTo': '2026-12-31',
                      'scope': 'Recycled polyamide shell',
                      'certificateUrl': 'https://example.com/demo/certificates/globalrecy-nt-1047'},
                     {'name': 'bluesign',
                      'certificateId': 'DEMO-BLUESIGN-1047-2',
                      'issuingBody': 'Demo Certification Body',
                      'validFrom': '2026-04-01',
                      'validTo': '2027-03-31',
                      'scope': 'Shell fabric and DWR finish',
                      'certificateUrl': 'https://example.com/demo/certificates/bluesign-nt-1047'}],
  'documents': [{'documentType': 'measurement_chart',
                 'title': 'NT-1047-measurement-chart.pdf',
                 'url': 'https://example.com/demo/NT-1047-measurement-chart.pdf',
                 'issuedBy': 'Nordic Threads'}]}]

DEMO_BATTERY = [{'moduleId': 'VC-EV-M72',
  'general': {'platform': 'EV traction module',
              'manufacturer': 'VoltCell Energy',
              'uniqueFacilityIdentifier': 'VCE-PLANT-DE-01',
              'location': 'Leipzig, Germany',
              'imageUrl': 'https://example.com/demo/vc-ev-m72.jpg'},
  'productSpecs': {'partNumbers': ['VC-EV-M72-A', 'VC-EV-M72-B'],
                   'batteryCategory': 'EV battery',
                   'batteryType': 'Li-ion NMC 811',
                   'taricCode': '8507.60',
                   'expectedLifetimeYears': 12,
                   'chargingDischargingEfficiencyPercent': 95.5,
                   'energyCapacityGrossKwh': 7.2,
                   'energyCapacityNetKwh': 6.9,
                   'capacityGrossAh': 180,
                   'capacityNetAh': 172,
                   'voltageV': 40.0,
                   'energyDensityGravimetricWhKg': 265,
                   'numberOfCells': 24,
                   'cellsConfiguration': '12s2p',
                   'lengthMm': 590,
                   'widthMm': 355,
                   'heightMm': 108,
                   'weightKg': 27.5,
                   'cRate': 3.0},
  'rawMaterials': {'batteryComponents': [{'componentType': 'Cathode', 'componentMaterial': 'NMC 811'},
                                         {'componentType': 'Anode',
                                          'componentMaterial': 'Synthetic graphite'},
                                         {'componentType': 'Electrolyte',
                                          'componentMaterial': 'LiPF6 in EC/DMC'},
                                         {'componentType': 'Separator',
                                          'componentMaterial': 'Ceramic-coated PE'},
                                         {'componentType': 'Housing',
                                          'componentMaterial': 'Aluminium alloy'}],
                   'rawMaterials': [{'material': 'Lithium', 'concentrationPercent': 1.9},
                                    {'material': 'Nickel', 'concentrationPercent': 12.4},
                                    {'material': 'Manganese', 'concentrationPercent': 1.6},
                                    {'material': 'Cobalt', 'concentrationPercent': 1.5},
                                    {'material': 'Graphite', 'concentrationPercent': 17.2}],
                   'criticalRawMaterials': ['Lithium', 'Nickel', 'Cobalt', 'Natural graphite']},
  'sustainability': {'euDeclarationOfConformity': 'https://example.com/demo/doc/VC-EV-M72-eu-doc.pdf',
                     'temperatureConditions': '-20 °C to 55 °C',
                     'carbonFootprint': '68 kg CO2e/kWh (demo)',
                     'environmentalFootprint': 'Cradle-to-gate LCA per PEFCR for rechargeable batteries '
                                               '(demo)',
                     'substancesOfConcern': [{'substance': 'Lithium hexafluorophosphate',
                                              'concentrationPercent': 0.9}],
                     'recycledContent': [{'material': 'Nickel', 'concentrationPercent': 8.0},
                                         {'material': 'Cobalt', 'concentrationPercent': 16.0},
                                         {'material': 'Lithium', 'concentrationPercent': 6.0}],
                     'renewableContent': [{'material': 'Aluminium', 'concentrationPercent': 35.0}]},
  'attachments': [{'fileName': 'VC-EV-M72-datasheet.pdf',
                   'fileUrl': 'https://example.com/demo/VC-EV-M72-datasheet.pdf',
                   'fileSizeBytes': 482113,
                   'uploadedAt': '2026-09-01T09:00:00Z'}]},
 {'moduleId': 'VC-HS-10',
  'general': {'platform': 'Home storage pack',
              'manufacturer': 'VoltCell Energy',
              'uniqueFacilityIdentifier': 'VCE-PLANT-DE-01',
              'location': 'Leipzig, Germany',
              'imageUrl': 'https://example.com/demo/vc-hs-10.jpg'},
  'productSpecs': {'partNumbers': ['VC-HS-10-A', 'VC-HS-10-B'],
                   'batteryCategory': 'Industrial battery',
                   'batteryType': 'Li-ion LFP',
                   'taricCode': '8507.60',
                   'expectedLifetimeYears': 15,
                   'chargingDischargingEfficiencyPercent': 96.0,
                   'energyCapacityGrossKwh': 10.2,
                   'energyCapacityNetKwh': 9.8,
                   'capacityGrossAh': 200,
                   'capacityNetAh': 192,
                   'voltageV': 51.2,
                   'energyDensityGravimetricWhKg': 165,
                   'numberOfCells': 16,
                   'cellsConfiguration': '16s1p',
                   'lengthMm': 650,
                   'widthMm': 420,
                   'heightMm': 190,
                   'weightKg': 98.0,
                   'cRate': 0.5},
  'rawMaterials': {'batteryComponents': [{'componentType': 'Cathode', 'componentMaterial': 'LiFePO4'},
                                         {'componentType': 'Anode', 'componentMaterial': 'Natural graphite'},
                                         {'componentType': 'Electrolyte',
                                          'componentMaterial': 'LiPF6 in EC/EMC'},
                                         {'componentType': 'Separator',
                                          'componentMaterial': 'PP/PE/PP trilayer'},
                                         {'componentType': 'Housing', 'componentMaterial': 'Steel'}],
                   'rawMaterials': [{'material': 'Lithium', 'concentrationPercent': 1.2},
                                    {'material': 'Iron', 'concentrationPercent': 9.8},
                                    {'material': 'Phosphorus', 'concentrationPercent': 5.5},
                                    {'material': 'Graphite', 'concentrationPercent': 14.0}],
                   'criticalRawMaterials': ['Lithium', 'Natural graphite']},
  'sustainability': {'euDeclarationOfConformity': 'https://example.com/demo/doc/VC-HS-10-eu-doc.pdf',
                     'temperatureConditions': '-10 °C to 50 °C',
                     'carbonFootprint': '52 kg CO2e/kWh (demo)',
                     'environmentalFootprint': 'Cradle-to-gate LCA per PEFCR for rechargeable batteries '
                                               '(demo)',
                     'substancesOfConcern': [{'substance': 'Lithium hexafluorophosphate',
                                              'concentrationPercent': 0.8}],
                     'recycledContent': [{'material': 'Lithium', 'concentrationPercent': 6.0}],
                     'renewableContent': [{'material': 'Steel', 'concentrationPercent': 20.0}]},
  'attachments': [{'fileName': 'VC-HS-10-datasheet.pdf',
                   'fileUrl': 'https://example.com/demo/VC-HS-10-datasheet.pdf',
                   'fileSizeBytes': 482113,
                   'uploadedAt': '2026-09-01T09:00:00Z'}]},
 {'moduleId': 'VC-EB-500',
  'general': {'platform': 'E-bike battery',
              'manufacturer': 'VoltCell Energy',
              'uniqueFacilityIdentifier': 'VCE-PLANT-DE-01',
              'location': 'Leipzig, Germany',
              'imageUrl': 'https://example.com/demo/vc-eb-500.jpg'},
  'productSpecs': {'partNumbers': ['VC-EB-500-A', 'VC-EB-500-B'],
                   'batteryCategory': 'LMT battery',
                   'batteryType': 'Li-ion NMC 622',
                   'taricCode': '8507.60',
                   'expectedLifetimeYears': 6,
                   'chargingDischargingEfficiencyPercent': 94.0,
                   'energyCapacityGrossKwh': 0.5,
                   'energyCapacityNetKwh': 0.48,
                   'capacityGrossAh': 13.8,
                   'capacityNetAh': 13.4,
                   'voltageV': 36.0,
                   'energyDensityGravimetricWhKg': 190,
                   'numberOfCells': 40,
                   'cellsConfiguration': '10s4p',
                   'lengthMm': 365,
                   'widthMm': 95,
                   'heightMm': 90,
                   'weightKg': 2.9,
                   'cRate': 1.0},
  'rawMaterials': {'batteryComponents': [{'componentType': 'Cathode', 'componentMaterial': 'NMC 622'},
                                         {'componentType': 'Anode',
                                          'componentMaterial': 'Synthetic graphite'},
                                         {'componentType': 'Electrolyte',
                                          'componentMaterial': 'LiPF6 in EC/DMC'},
                                         {'componentType': 'Housing', 'componentMaterial': 'Polycarbonate'}],
                   'rawMaterials': [{'material': 'Lithium', 'concentrationPercent': 1.7},
                                    {'material': 'Nickel', 'concentrationPercent': 9.1},
                                    {'material': 'Manganese', 'concentrationPercent': 2.8},
                                    {'material': 'Cobalt', 'concentrationPercent': 3.0},
                                    {'material': 'Graphite', 'concentrationPercent': 16.5}],
                   'criticalRawMaterials': ['Lithium', 'Nickel', 'Cobalt']},
  'sustainability': {'euDeclarationOfConformity': 'https://example.com/demo/doc/VC-EB-500-eu-doc.pdf',
                     'temperatureConditions': '0 °C to 45 °C',
                     'carbonFootprint': '85 kg CO2e/kWh (demo)',
                     'environmentalFootprint': 'Cradle-to-gate LCA per PEFCR for rechargeable batteries '
                                               '(demo)',
                     'substancesOfConcern': [{'substance': 'Lithium hexafluorophosphate',
                                              'concentrationPercent': 1.0}],
                     'recycledContent': [{'material': 'Cobalt', 'concentrationPercent': 16.0},
                                         {'material': 'Nickel', 'concentrationPercent': 6.0}],
                     'renewableContent': []},
  'attachments': [{'fileName': 'VC-EB-500-datasheet.pdf',
                   'fileUrl': 'https://example.com/demo/VC-EB-500-datasheet.pdf',
                   'fileSizeBytes': 482113,
                   'uploadedAt': '2026-09-01T09:00:00Z'}]},
 {'moduleId': 'VC-IND-48',
  'general': {'platform': 'Forklift traction pack',
              'manufacturer': 'VoltCell Energy',
              'uniqueFacilityIdentifier': 'VCE-PLANT-DE-01',
              'location': 'Leipzig, Germany',
              'imageUrl': 'https://example.com/demo/vc-ind-48.jpg'},
  'productSpecs': {'partNumbers': ['VC-IND-48-A', 'VC-IND-48-B'],
                   'batteryCategory': 'Industrial battery',
                   'batteryType': 'Li-ion LFP',
                   'taricCode': '8507.60',
                   'expectedLifetimeYears': 10,
                   'chargingDischargingEfficiencyPercent': 95.0,
                   'energyCapacityGrossKwh': 24.6,
                   'energyCapacityNetKwh': 23.9,
                   'capacityGrossAh': 480,
                   'capacityNetAh': 466,
                   'voltageV': 51.2,
                   'energyDensityGravimetricWhKg': 150,
                   'numberOfCells': 64,
                   'cellsConfiguration': '16s4p',
                   'lengthMm': 980,
                   'widthMm': 620,
                   'heightMm': 540,
                   'weightKg': 285.0,
                   'cRate': 1.0},
  'rawMaterials': {'batteryComponents': [{'componentType': 'Cathode', 'componentMaterial': 'LiFePO4'},
                                         {'componentType': 'Anode', 'componentMaterial': 'Natural graphite'},
                                         {'componentType': 'Electrolyte',
                                          'componentMaterial': 'LiPF6 in EC/EMC'},
                                         {'componentType': 'Separator',
                                          'componentMaterial': 'PP/PE/PP trilayer'},
                                         {'componentType': 'Housing', 'componentMaterial': 'Steel'}],
                   'rawMaterials': [{'material': 'Lithium', 'concentrationPercent': 1.1},
                                    {'material': 'Iron', 'concentrationPercent': 10.2},
                                    {'material': 'Phosphorus', 'concentrationPercent': 5.7},
                                    {'material': 'Graphite', 'concentrationPercent': 13.6}],
                   'criticalRawMaterials': ['Lithium', 'Natural graphite']},
  'sustainability': {'euDeclarationOfConformity': 'https://example.com/demo/doc/VC-IND-48-eu-doc.pdf',
                     'temperatureConditions': '-20 °C to 50 °C',
                     'carbonFootprint': '49 kg CO2e/kWh (demo)',
                     'environmentalFootprint': 'Cradle-to-gate LCA per PEFCR for rechargeable batteries '
                                               '(demo)',
                     'substancesOfConcern': [{'substance': 'Lithium hexafluorophosphate',
                                              'concentrationPercent': 0.8}],
                     'recycledContent': [{'material': 'Lithium', 'concentrationPercent': 6.0},
                                         {'material': 'Steel', 'concentrationPercent': 40.0}],
                     'renewableContent': []},
  'attachments': [{'fileName': 'VC-IND-48-datasheet.pdf',
                   'fileUrl': 'https://example.com/demo/VC-IND-48-datasheet.pdf',
                   'fileSizeBytes': 482113,
                   'uploadedAt': '2026-09-01T09:00:00Z'}]}]


if __name__ == "__main__":
    sys.exit(main())
