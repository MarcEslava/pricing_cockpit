# Serves the cockpit page AND /api/products (reads ClickHouse). Same origin -> no CORS.
import http.server, socketserver, json, urllib.request, os
DIR = os.path.dirname(os.path.abspath(__file__))
CH = os.environ.get("CH_URL", "http://127.0.0.1:8123"); USER = os.environ.get("CH_USER", "price_monitor"); PW = os.environ.get("CH_PASS", "changeme_pm")   # 127.0.0.1: VPN mangles "localhost"
COLS = "name,lab,cat,ean,units,pvl,cost,cat_offset AS offset,reco,price_real AS real,margin,n1,n2,n3,comps"
CUR_MONTH = "2026-08-01"          # current pricing cycle; history = the months leading up to it

def ch(sql):
    req = urllib.request.Request(CH + "/", data=sql.encode("utf-8"),
            headers={"X-ClickHouse-User": USER, "X-ClickHouse-Key": PW})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8")

def history_map():
    # ean -> chronological list of monthly prices BEFORE the current cycle (one per month)
    q = ("SELECT ean, arrayMap(x -> x.2, arraySort(x -> x.1, groupArray((toString(month), price)))) "
         "FROM (SELECT ean, month, anyLast(price) AS price FROM price_monitor.pricing_history "
         f"WHERE ean != '' AND month < '{CUR_MONTH}' GROUP BY ean, month) "
         "GROUP BY ean FORMAT JSONEachRow")
    m = {}
    for line in ch(q).splitlines():
        if line.strip():
            r = json.loads(line)
            m[r["ean"]] = r[next(k for k in r if k != "ean")]
    return m

def rows_json(sql):
    return [json.loads(l) for l in ch(sql + " FORMAT JSONEachRow").splitlines() if l.strip()]

def trends():
    H = "price_monitor.pricing_history"
    # sellout = Vendes Total (Uds x PVP); margin_tot = Margen B. Total (con coste corregido).
    # NB: no median-price alias here -- 'AS price' would shadow the column and nest aggregates.
    monthly = rows_json(
        f"SELECT toString(month) AS m, count() AS n, "
        f"round(quantileExact(0.5)(margin),4) AS margin, "
        f"round(sum(units*price),0) AS sellout, "
        f"round(sum(units*(price-cost)),0) AS margin_tot "
        f"FROM {H} WHERE units>0 GROUP BY m ORDER BY m")
    years = rows_json(
        f"SELECT toString(toYear(month)) AS y, count() AS n, uniqExact(ean) AS eans, "
        f"round(quantileExact(0.5)(price),2) AS price, round(quantileExact(0.5)(margin),4) AS margin "
        f"FROM {H} GROUP BY y ORDER BY y")
    by_cat = rows_json(
        f"SELECT cat, count() AS n, round(quantileExact(0.5)(price),2) AS price, "
        f"round(quantileExact(0.5)(margin),4) AS margin "
        f"FROM {H} WHERE month >= (SELECT max(month) FROM {H}) AND cat != '' "
        f"GROUP BY cat ORDER BY n DESC LIMIT 14")
    # last-vs-previous price move per product (across its whole history)
    move = rows_json(
        f"SELECT countIf(chg>0.005) AS up, countIf(chg<-0.005) AS down, "
        f"countIf(abs(chg)<=0.005) AS flat, round(quantileExact(0.5)(chg),4) AS med "
        f"FROM (SELECT ean, arraySort(x->x.1, groupArray((month,price))) AS a, "
        f"a[length(a)].2 / a[length(a)-1].2 - 1 AS chg FROM {H} WHERE ean!='' "
        f"GROUP BY ean HAVING length(a)>=2 AND a[length(a)-1].2 > 0)")
    tot = rows_json(f"SELECT uniqExact(ean) AS eans, count() AS rows FROM {H} WHERE ean!=''")
    return {"monthly": monthly, "years": years, "by_cat": by_cat,
            "movement": (move[0] if move else {}), "total": (tot[0] if tot else {})}

# Rx / non-parafarmacia (precio regulado + fórmulas) — excluded from the inventory view.
RX_EXCL = ("ESPECIALIDAD", "EFG", "ESPEC. CARAS", "VACUNAS", "FORMULAS",
           "FORMULAS PRIVADAS", "-SIN SUPERFAM-ESPEC")

def inventory(idende=None):
    I = "price_monitor.inventory"
    snap = f"(SELECT max(snapshot_at) FROM {I})"
    # Restrict to the products we actually price (the cockpit CNs). inventory.cn is a
    # fixed-width CHAR from BIFarma -> trimBoth to match the cockpit's clean CN.
    pricing = "trimBoth(cn) IN (SELECT cn FROM price_monitor.pricing_cockpit WHERE cn != '')"
    where = f"snapshot_at={snap} AND {pricing}"
    if idende is not None:
        where += f" AND idende={int(idende)}"
    # cogs = ventas_uds * coste_ud (annual); meses de stock = valor_inv / (cogs/12)  [todo a coste]
    totals = rows_json(
        f"SELECT round(sum(valor_inv),0) AS valor, round(sum(ventas_eur_12m),0) AS ventas12m, "
        f"uniqExact(cn) AS prods, round(sum(valor_inv)/nullIf(sum(ventas_uds_12m*coste_ud)/12,0),2) AS meses "
        f"FROM {I} WHERE {where}")
    by_cat = rows_json(
        f"WITH t AS (SELECT * FROM {I} WHERE {where}) "
        f"SELECT cat, round(sum(valor_inv),0) AS valor, "
        f"round(100*sum(valor_inv)/nullIf((SELECT sum(valor_inv) FROM t),0),2) AS inv_pct, "
        f"round(100*sum(ventas_eur_12m)/nullIf((SELECT sum(ventas_eur_12m) FROM t),0),2) AS sales_pct, "
        f"round(sum(valor_inv)/nullIf(sum(ventas_uds_12m*coste_ud)/12,0),2) AS meses "
        f"FROM t GROUP BY cat ORDER BY valor DESC")
    farmacias = rows_json(
        f"SELECT idende, any(delegacion) AS delegacion, round(sum(valor_inv),0) AS valor "
        f"FROM {I} WHERE snapshot_at={snap} AND {pricing} "
        f"GROUP BY idende ORDER BY valor DESC")
    return {"totals": (totals[0] if totals else {}), "by_cat": by_cat,
            "farmacias": farmacias, "coverage": pricing_coverage()}

def pricing_coverage():
    # Pricing utility: what share of ALL parafarmacia (excl. Rx) do the priced CNs cover?
    # Network-level (all pharmacies), independent of the farmacia filter.
    I = "price_monitor.inventory"
    snap = f"(SELECT max(snapshot_at) FROM {I})"
    rx = ",".join("'" + c.replace("'", "''") + "'" for c in RX_EXCL)
    priced = "trimBoth(cn) IN (SELECT cn FROM price_monitor.pricing_cockpit WHERE cn != '')"
    base = (f"SELECT cat, ventas_eur_12m, valor_inv, {priced} AS priced "
            f"FROM {I} WHERE snapshot_at={snap} AND cat NOT IN ({rx})")
    glob = rows_json(
        f"SELECT round(100*sumIf(ventas_eur_12m,priced)/nullIf(sum(ventas_eur_12m),0),1) AS sales_pct, "
        f"round(100*sumIf(valor_inv,priced)/nullIf(sum(valor_inv),0),1) AS inv_pct "
        f"FROM ({base})")
    by_cat = rows_json(
        f"SELECT cat, round(sum(ventas_eur_12m),0) AS ventas, "
        f"round(100*sumIf(ventas_eur_12m,priced)/nullIf(sum(ventas_eur_12m),0),1) AS cobertura "
        f"FROM ({base}) GROUP BY cat ORDER BY ventas DESC")
    g = glob[0] if glob else {}
    return {"sales_pct": g.get("sales_pct"), "inv_pct": g.get("inv_pct"), "by_cat": by_cat}

# ---- Metabase: listar dashboards por persona para el menú del front ----
MB_URL = os.environ.get("MB_URL", "http://127.0.0.1:3000")          # server -> metabase
MB_BROWSER = os.environ.get("MB_BROWSER", "http://localhost:3000")  # navegador -> metabase (iframe)
MB_USER = os.environ.get("MB_USER", "admin@ecoceutics.local"); MB_PASS = os.environ.get("MB_PASS", "Ecoceutics-2026!")
_MB = {"session": None}

def mb_login():
    req = urllib.request.Request(MB_URL + "/api/session",
        data=json.dumps({"username": MB_USER, "password": MB_PASS}).encode(),
        headers={"Content-Type": "application/json"})
    _MB["session"] = json.loads(urllib.request.urlopen(req, timeout=20).read().decode())["id"]

def mb_api(path, data=None, method=None, retry=True):
    if not _MB["session"]:
        mb_login()
    headers = {"Content-Type": "application/json", "X-Metabase-Session": _MB["session"]}
    req = urllib.request.Request(MB_URL + path,
        data=(json.dumps(data).encode() if data is not None else None),
        headers=headers, method=method or ("POST" if data is not None else "GET"))
    try:
        return json.loads(urllib.request.urlopen(req, timeout=20).read().decode() or "{}")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403) and retry:
            _MB["session"] = None; return mb_api(path, data, method, retry=False)
        raise

def mb_dashboards():
    dl = mb_api("/api/dashboard")
    dl = dl.get("data", dl) if isinstance(dl, dict) else dl
    users = mb_api("/api/user")
    ul = users.get("data", users) if isinstance(users, dict) else users
    umap = {u["id"]: (u.get("common_name") or (u.get("first_name", "") + " " + u.get("last_name", "")).strip() or u.get("email", "")) for u in ul}
    # dashboards in the shared "Compartido" subtree count as "General", not under a person
    cols = mb_api("/api/collection")
    cols = cols.get("data", cols) if isinstance(cols, dict) else cols
    shared_ids = set()
    comp = next((c["id"] for c in cols if "Compartido" in (c.get("name") or "")), None)
    if comp is not None:
        shared_ids.add(comp)
        for c in cols:
            if f"/{comp}/" in (c.get("location") or ""):
                shared_ids.add(c["id"])
    out = []
    for d in dl:
        if d.get("archived"):
            continue
        did = d["id"]
        uuid = d.get("public_uuid")
        if not uuid:                                  # asegura un enlace público para embeber
            try:
                uuid = mb_api(f"/api/dashboard/{did}/public_link", {}).get("uuid")
            except Exception:
                uuid = mb_api(f"/api/dashboard/{did}").get("public_uuid")
        if not uuid:
            continue
        person = umap.get(d.get("creator_id"))
        group = "General" if (person is None or d.get("collection_id") in shared_ids) else person
        out.append({"id": did, "name": d.get("name", "(sin nombre)"),
                    "creator": group,
                    "url": f"{MB_BROWSER}/public/dashboard/{uuid}#theme=transparent&bordered=false&titled=false"})
    out.sort(key=lambda x: (x["creator"].lower(), x["name"].lower()))
    return out


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k): super().__init__(*a, directory=DIR, **k)
    def log_message(self, *a): pass
    def _send_json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if self.path.split("?")[0] == "/api/trends":
            try:
                self._send_json(trends())
            except Exception as e:
                b = ("api error: " + str(e)).encode("utf-8")
                self.send_response(500); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            return
        if self.path.split("?")[0] == "/api/inventory":
            try:
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(self.path).query)
                idende = qs.get("idende", [None])[0]
                self._send_json(inventory(idende if idende not in (None, "", "all") else None))
            except Exception as e:
                b = ("api error: " + str(e)).encode("utf-8")
                self.send_response(500); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            return
        if self.path.split("?")[0] == "/api/mb/dashboards":
            try:
                self._send_json(mb_dashboards())
            except Exception as e:
                b = ("api error: " + str(e)).encode("utf-8")
                self.send_response(500); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            return
        if self.path.split("?")[0] == "/api/products":
            try:
                hist = history_map()
                q = f"SELECT {COLS} FROM price_monitor.pricing_cockpit ORDER BY units DESC NULLS LAST FORMAT JSONEachRow"
                rows = []
                for line in ch(q).splitlines():
                    if not line.strip():
                        continue
                    r = json.loads(line)
                    r["comps"] = json.loads(r["comps"])
                    sheet = [r.pop("n3"), r.pop("n2"), r.pop("n1")]          # sheet PVP n-3..n-1 (fallback)
                    prev = hist.get(r.get("ean") or "", [])[-3:]             # real archive, last 3 months
                    if prev:
                        r["history"] = [None] * (3 - len(prev)) + prev       # left-pad -> oldest..newest
                        r["history_src"] = "archive"
                    else:
                        r["history"] = sheet
                        r["history_src"] = "sheet"
                    rows.append(r)
                body = json.dumps(rows, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)
            except Exception as e:
                b = ("api error: " + str(e)).encode("utf-8")
                self.send_response(500); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            return
        super().do_GET()

if __name__ == "__main__":
    socketserver.TCPServer.allow_reuse_address = True
    print("serving cockpit on http://localhost:8899  (page + /api/products + /api/trends)")
    socketserver.TCPServer(("127.0.0.1", 8899), H).serve_forever()
