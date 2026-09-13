#!/usr/bin/env python3
"""
purge_sandbox.py — archive every contact and company in the HubSpot DEVELOPER TEST portal
so a demo run starts from a clean CRM. Refuses to run unless --confirm is given and the
portal's accountType is DEVELOPER_TEST (never a production portal). Writes a receipt.

Usage: HUBSPOT_TOKEN=… python3 purge_sandbox.py --confirm [--receipt PATH] [--max 2000]
"""
import argparse, json, os, sys, time, urllib.request, urllib.error
API = "https://api.hubapi.com"

def call(method, path, token, body=None):
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read(); return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode(errors="replace")[:300]}

def list_ids(obj, token, cap):
    ids, after = [], None
    while len(ids) < cap:
        q = f"/crm/v3/objects/{obj}?limit=100&properties=hs_object_id" + (f"&after={after}" if after else "")
        st, d = call("GET", q, token)
        if st >= 300: sys.exit(f"FAIL list {obj} {st}: {d}")
        ids += [r["id"] for r in d.get("results", [])]
        after = (d.get("paging") or {}).get("next", {}).get("after")
        if not after: break
    return ids

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--receipt", default=None); ap.add_argument("--max", type=int, default=2000); a = ap.parse_args()
    token = os.environ.get("HUBSPOT_TOKEN") or sys.exit("FAIL: HUBSPOT_TOKEN not set")
    st, info = call("GET", "/account-info/v3/details", token)
    if st >= 300 or info.get("accountType") != "DEVELOPER_TEST":
        sys.exit(f"REFUSED: portal accountType={info.get('accountType')!r} (only DEVELOPER_TEST may be purged)")
    receipt = {"portal_id": info.get("portalId"), "account_type": info["accountType"], "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "objects": {}}
    for obj in ("contacts", "companies"):
        ids = list_ids(obj, token, a.max); before = len(ids)
        if before >= a.max: sys.exit(f"REFUSED: {obj} count {before} hit --max {a.max}; not a demo-sized portal")
        print(f"[{obj}] {before} found")
        if not a.confirm: receipt["objects"][obj] = {"before": before, "archived": 0, "dry_run": True}; continue
        archived, errors = 0, []
        for i in range(0, len(ids), 100):
            st, d = call("POST", f"/crm/v3/objects/{obj}/batch/archive", token, {"inputs": [{"id": x} for x in ids[i:i+100]]})
            if st in (204, 200): archived += len(ids[i:i+100])
            else: errors.append({"status": st, "detail": d})
        after = len(list_ids(obj, token, a.max))
        receipt["objects"][obj] = {"before": before, "archived": archived, "after": after, "errors": errors}
        print(f"[{obj}] archived {archived}, remaining {after}, errors {len(errors)}")
    if a.receipt:
        os.makedirs(os.path.dirname(a.receipt) or ".", exist_ok=True); open(a.receipt, "w").write(json.dumps(receipt, indent=2)); print("receipt:", a.receipt)
    if not a.confirm: print("dry run — pass --confirm to archive")

if __name__ == "__main__": main()
