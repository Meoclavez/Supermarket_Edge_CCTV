"""Push store_map.json to an Edge CCTV dashboard through the layout API.

  python apply_map.py --base http://100.78.122.93:8000 --user <name> --pw-file .dash_pw [--check | --replace]

--check     sign in, save the current layout to layout_before_<ts>.json, print a summary, sign out.
--replace   additionally delete the existing zones and structures (cameras are kept; recorded
            visits are kept) and create the map. Without existing geometry --replace is not needed.
The password file is shredded right after sign-in.
"""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--base", required=True)
ap.add_argument("--user", required=True)
ap.add_argument("--pw-file", required=True)
ap.add_argument("--map", default="store_map.json")
ap.add_argument("--check", action="store_true")
ap.add_argument("--replace", action="store_true")
ap.add_argument("--keep-pw", action="store_true", help="do not shred the password file (local tests)")
a = ap.parse_args()
TOKEN = None


def call(method, path, body=None):
    req = urllib.request.Request(a.base + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          **({"Authorization": f"Bearer {TOKEN}"} if TOKEN else {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, (e.read() or b"")[:400].decode(errors="replace")


pw = open(a.pw_file).read().rstrip("\n")
st, res = call("POST", "/api/v1/auth/login", {"username": a.user, "password": pw})
pw = None
if not a.keep_pw:
    subprocess.run(["shred", "-u", a.pw_file], check=False)
if st != 200:
    sys.exit(f"sign-in failed: HTTP {st} {res}")
TOKEN = res["access_token"]
print("signed in")
try:
    st, layout = call("GET", "/api/v1/layout")
    assert st == 200, (st, layout)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    json.dump(layout, open(f"layout_before_{stamp}.json", "w"), indent=1)
    print(f"current: {layout['name']} {layout['width_m']}x{layout['height_m']} m, "
          f"{len(layout['zones'])} zones, {len(layout['structures'])} structures, "
          f"{len(layout.get('cameras', []))} cameras, setup={layout.get('setup')}  (saved layout_before_{stamp}.json)")
    if a.check:
        sys.exit(0)
    if (layout["zones"] or layout["structures"]) and not a.replace:
        sys.exit("layout already has geometry; re-run with --replace to swap it for the map")

    m = json.load(open(a.map))
    for z in layout["zones"]:
        st, r = call("DELETE", f"/api/v1/layout/zones/{z['id']}")
        assert st == 200, (st, r)
    for s in layout["structures"]:
        st, r = call("DELETE", f"/api/v1/layout/structures/{s['id']}")
        assert st == 200, (st, r)
    st, r = call("PUT", "/api/v1/layout", m["layout"])
    assert st == 200, (st, r)
    for s in m["structures"]:
        st, r = call("POST", "/api/v1/layout/structures", s)
        assert st == 201, (st, r, s["name"])
    for z in m["zones"]:
        st, r = call("POST", "/api/v1/layout/zones", z)
        assert st == 201, (st, r, z["name"])
        if z.get("products") and r.get("products") != z["products"]:
            sys.exit(f"server did not keep products for {z['name']} (old build?): {r.get('products')}")
    st, after = call("GET", "/api/v1/layout")
    print(f"applied: {after['name']} {after['width_m']}x{after['height_m']} m, {len(after['zones'])} zones, "
          f"{len(after['structures'])} structures, setup={after['setup']}")
finally:
    st, _ = call("POST", "/api/v1/auth/logout", {})
    print(f"signed out (HTTP {st})")
