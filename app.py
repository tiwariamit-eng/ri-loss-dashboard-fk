#!/usr/bin/env python3
"""
RI Loss Analysis — Streamlit launcher for the exact HTML dashboard.
"""
import io
import json
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# TODO: replace with your RI Loss Drive folder ID
# (resealing folder: 1wfyMOkNiJGYYIcu9L2IYS-k6Yi-Z6MgL
#  refinishing folder: 1ZztjmzZ931IRGNwLRGwQbGeMigcsvIDu)
RI_LOSS_FOLDER_ID = "1nM_Hiqdm9VcsFF86LJw1qOqXrXVl5HSB"


def get_drive_service():
    creds = Credentials(
        token=None,
        refresh_token=st.secrets["GOOGLE_REFRESH_TOKEN"],
        client_id=st.secrets["GOOGLE_CLIENT_ID"],
        client_secret=st.secrets["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    return build("drive", "v3", credentials=creds)


def find_file_id(service, folder_id, filename_contains):
    query = f"'{folder_id}' in parents and trashed = false and name contains '{filename_contains}'"
    results = service.files().list(
        q=query, fields="files(id, name, modifiedTime)",
        orderBy="modifiedTime desc"
    ).execute()
    files = results.get("files", [])
    if not files:
        raise FileNotFoundError(
            f"No file matching '{filename_contains}' found in Drive folder {folder_id}"
        )
    return files[0]["id"], files[0]["name"]


def download_file_to_buffer(service, file_id):
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    return buf


@st.cache_data(show_spinner="Fetching RI Loss CSV from Google Drive...", ttl=600)
def fetch_csv_from_drive(folder_id, filename_contains="RI_Final"):
    service = get_drive_service()
    file_id, file_name = find_file_id(service, folder_id, filename_contains)
    buf = download_file_to_buffer(service, file_id)
    return buf, file_name


USE = ["warehouse_id","business_unit","zone","FC Zone","Month",
       "return_sub_reason","return_reason","final_bucket","product_detail_cms_vertical",
       "brand","Vertical x Brand","ekl_bf_last_recvd_dh_name",
       "resealing eligible","tag reprinting status","rvp_rto_status",
       "BGM Eligible brand","BGM Eligible Mapping",
       "final amount","quantity"]

@st.cache_data(show_spinner=True)
def load_data(source):
    try:
        df = pd.read_csv(source, usecols=lambda c: c in USE, low_memory=False)
    except TypeError:
        # older pandas versions don't support usecols as a callable
        df = pd.read_csv(source, low_memory=False)
        df = df[[c for c in df.columns if c in USE]]
    df["amt"] = pd.to_numeric(df["final amount"], errors="coerce").fillna(0.0)
    df["u"]   = pd.to_numeric(df["quantity"], errors="coerce").fillna(0).astype(int)
    df["fcz"] = df["FC Zone"].fillna("Unknown")
    df["z"]   = df["zone"].fillna("Unknown")
    df["wh"]  = df["warehouse_id"].astype(str)
    df["bu"]  = df["business_unit"].fillna("Unknown")
    df["fb"]  = df["final_bucket"].fillna("Unknown")
    df["m"]   = df["Month"].astype(str).str.strip()
    df["v"]   = df["product_detail_cms_vertical"].fillna("Unknown")
    df["sr"]  = df["return_sub_reason"].fillna("Unknown")
    df["br"]  = df["brand"].fillna("Unknown")
    df["vxb"] = df["Vertical x Brand"].fillna("Unknown")
    df["dh"]  = df["ekl_bf_last_recvd_dh_name"].fillna("Not Tagged").replace("", "Not Tagged")
    df["reason"] = df["return_reason"].fillna("Unknown")
    df["resel"]  = df["resealing eligible"].fillna("N/A").replace("", "N/A")
    df["tagr"]   = df["tag reprinting status"].fillna("N/A").replace("", "N/A")
    df["bgmb"]   = df["BGM Eligible brand"].fillna("N/A").replace("", "N/A")
    df["bgmm"]   = df["BGM Eligible Mapping"].fillna("N/A").replace("", "N/A")
    df["rr"]     = df["rvp_rto_status"].astype(str).str.upper().where(
                      df["rvp_rto_status"].notna(), "Unknown").replace({"": "Unknown", "NAN": "Unknown"})
    
    _topr = df.groupby("reason")["amt"].sum().sort_values(ascending=False).head(30).index
    df["reason_c"] = df["reason"].where(df["reason"].isin(_topr), "Other")

    BU_KEEP = ["BGM","Electronics","LifeStyle","Home","LargeAppliances","Mobile"]
    before = len(df)
    df = df[df["bu"].isin(BU_KEEP)].copy()
    df = df[df["m"].str.fullmatch(r"\d{6}")].copy()
    print(f"kept {len(df)} of {before} rows after BU/month scope filters",
          "| months:", sorted(df["m"].unique().tolist()))
    return df.reset_index(drop=True)


def build_payload(df):
    def cr(x): 
        return round(float(x) / 1e7, 4)

    payload = {}

    # ---------- meta ----------
    payload["meta"] = {
        "total_amt": round(float(df["amt"].sum()), 2),
        "total_cr":  cr(df["amt"].sum()),
        "units":     int(df["u"].sum()),
        "shipments": int(len(df)),
        "sites":     int(df["wh"].nunique()),
        "months":    sorted(df["m"].unique().tolist()),
        "zones":     sorted([z for z in df["fcz"].unique() if z != "Unknown"]),
        "bus":       df.groupby("bu")["amt"].sum().sort_values(ascending=False).index.tolist(),
        "buckets":   df.groupby("fb")["amt"].sum().sort_values(ascending=False).index.tolist(),
        "resel_opts": [x for x in df["resel"].value_counts().index.tolist()],
        "tagr_opts":  [x for x in df["tagr"].value_counts().index.tolist()],
        "bgmb_opts":  [x for x in df["bgmb"].value_counts().index.tolist()],
        "bgmm_opts":  [x for x in df["bgmm"].value_counts().index.tolist()],
    }

    # ---------- base fact (filterable client-side) ----------
    base = (df.groupby(["m","fcz","z","wh","bu","fb"])
              .agg(amt=("amt","sum"), u=("u","sum"), s=("amt","size"))
              .reset_index())
    payload["base"] = [
        {"m":r.m,"fcz":r.fcz,"z":r.z,"wh":r.wh,"bu":r.bu,"fb":r.fb,
         "cr":cr(r.amt),"u":int(r.u),"s":int(r.s)}
        for r in base.itertuples()
    ]

    # ---------- base_reason fact (with RTO/RVP so reasons can be split) ----------
    br = (df.groupby(["m","fcz","z","wh","bu","rr","reason_c"])
            .agg(amt=("amt","sum"), u=("u","sum"), s=("amt","size")).reset_index())
    payload["base_reason"] = [
        {"m":r.m,"fcz":r.fcz,"z":r.z,"wh":r.wh,"bu":r.bu,"rr":r.rr,"rsn":r.reason_c,
         "cr":cr(r.amt),"u":int(r.u),"s":int(r.s)}
        for r in br.itertuples()
    ]

    # ---------- base_rto fact ----------
    brt = (df.groupby(["m","fcz","z","wh","bu","rr"])
             .agg(amt=("amt","sum"), u=("u","sum"), s=("amt","size")).reset_index())
    payload["base_rto"] = [
        {"m":r.m,"fcz":r.fcz,"z":r.z,"wh":r.wh,"bu":r.bu,"rr":r.rr,
         "cr":cr(r.amt),"u":int(r.u),"s":int(r.s)}
        for r in brt.itertuples()
    ]

    # ---------- BU x Month x Vertical×Brand fact (vectorized, single-pass) ----------
    def _build_vxb(frame, top_n, scope=None):
        """scope=None -> {bu:[entries]}; scope='wh' -> {wh:{bu:[entries]}}.
        Uses a fixed handful of groupby passes regardless of #warehouses."""
        pre = [scope] if scope else []
        gtop = pre + ["bu", "vxb"]
        bv = frame.groupby(gtop)["amt"].sum()
        keep = set(bv.groupby(level=list(range(len(pre) + 1)), group_keys=False)
                     .nlargest(top_n).index)
        empty = ({} if scope else {bu: [] for bu in frame["bu"].unique()})
        if not keep:
            return empty
        midx = pd.MultiIndex.from_arrays([frame[c] for c in gtop])
        d = frame[midx.isin(keep)]

        K = pre + ["bu", "vxb", "m"]
        sp = (d.groupby(K + ["resel","tagr","bgmb","bgmm"], sort=False)
                .agg(amt=("amt","sum"), u=("u","sum")).reset_index())
        fb = d.groupby(K + ["fb"], sort=False)["amt"].sum().reset_index()
        rr = d.groupby(K + ["rr"], sort=False)["amt"].sum().reset_index()
        rn = d.groupby(K + ["reason_c"], sort=False)["amt"].sum().reset_index()

        sp["item"] = [{"r":a,"t":b,"gb":c,"gm":dd,"cr":cr(e),"u":int(f)}
                      for a,b,c,dd,e,f in zip(sp.resel,sp.tagr,sp.bgmb,sp.bgmm,sp.amt,sp.u)]
        sp_by = sp.groupby(K, sort=False)["item"].agg(list).to_dict()

        fb = fb.sort_values("amt", ascending=False)
        fb["item"] = [{"fb":a,"cr":cr(b)} for a,b in zip(fb.fb, fb.amt)]
        fb_by = fb.groupby(K, sort=False)["item"].agg(list).to_dict()
        tfb = fb.groupby(K, sort=False)["fb"].first().to_dict()

        rr = rr.sort_values("amt", ascending=False)
        rr["item"] = [{"rr":a,"cr":cr(b)} for a,b in zip(rr.rr, rr.amt)]
        rr_by = rr.groupby(K, sort=False)["item"].agg(list).to_dict()

        rn = rn.sort_values("amt", ascending=False)
        rn = rn[rn.groupby(K, sort=False).cumcount() < 10]
        rn["item"] = [{"rn":a,"cr":cr(b)} for a,b in zip(rn.reason_c, rn.amt)]
        rn_by = rn.groupby(K, sort=False)["item"].agg(list).to_dict()

        # reason split within RTO and within RVP (top 8 each)
        rnr = d.groupby(K + ["rr","reason_c"], sort=False)["amt"].sum().reset_index()
        rnr = rnr.sort_values("amt", ascending=False)
        rnr = rnr[rnr.groupby(K + ["rr"], sort=False).cumcount() < 8]
        rnr["item"] = [{"rr":a,"rn":b,"cr":cr(c)} for a,b,c in zip(rnr.rr, rnr.reason_c, rnr.amt)]
        rnr_by = rnr.groupby(K, sort=False)["item"].agg(list).to_dict()

        months_for = {}
        for key in sp_by:
            months_for.setdefault(key[:-1], []).append(key[-1])
        order = bv[bv.index.isin(keep)].sort_values(ascending=False)

        out = {}
        for full in order.index:
            base = full  # (bu,vxb) or (wh,bu,vxb)
            for m in sorted(months_for.get(base, [])):
                key = base + (m,)
                entry = {"vxb": key[-2], "m": m, "top_fb": tfb.get(key, "-"),
                         "sp": sp_by.get(key, []), "fb": fb_by.get(key, []),
                         "rrd": rr_by.get(key, []), "rnd": rn_by.get(key, []),
                         "rnr": rnr_by.get(key, [])}
                if scope:
                    out.setdefault(base[0], {}).setdefault(base[1], []).append(entry)
                else:
                    out.setdefault(base[0], []).append(entry)
        if scope:
            for wh in frame[scope].unique():
                out.setdefault(wh, {})
                for bu in frame["bu"].unique():
                    out[wh].setdefault(bu, [])
        else:
            for bu in frame["bu"].unique():
                out.setdefault(bu, [])
        return out

    payload["vxb_fact"] = _build_vxb(df, 120)
    payload["vxb_fact_wh"] = _build_vxb(df, 60, scope="wh")

    # ---------- vertical summary ----------
    vg = df.groupby("v").agg(amt=("amt","sum"), u=("u","sum"), s=("amt","size"))
    vg = vg.sort_values("amt", ascending=False).head(60)
    top_fb = df.groupby(["v","fb"])["amt"].sum().reset_index()
    top_bu = df.groupby(["v","bu"])["amt"].sum().reset_index()
    def top_of(frame, key, v):
        sub = frame[frame["v"] == v]
        return sub.loc[sub["amt"].idxmax(), key] if len(sub) else "-"
    payload["verticals"] = [
        {"v":v, "cr":cr(row.amt), "u":int(row.u), "s":int(row.s),
         "top_fb":top_of(top_fb,"fb",v), "top_bu":top_of(top_bu,"bu",v)}
        for v, row in vg.iterrows()
    ]

    # ---------- site-level findings (overall + per month) ----------
    def _findings(frame):
        out = {}
        for wh, wdf in frame.groupby("wh"):
            site_amt = wdf["amt"].sum()
            bus = []
            bu_rank = wdf.groupby("bu")["amt"].sum().sort_values(ascending=False)
            bu_groups = {b: bg for b, bg in wdf.groupby("bu")}
            for bu, bu_amt in bu_rank.items():
                bdf = bu_groups[bu]
                tv  = bdf.groupby("v")["amt"].sum().sort_values(ascending=False)
                tvb = bdf.groupby("vxb")["amt"].sum().sort_values(ascending=False)
                top5 = [{"v":i, "cr":cr(a)} for i, a in tv.head(5).items()]
                bus.append({
                    "bu": bu, "cr": cr(bu_amt),
                    "share": round(100*bu_amt/site_amt,1) if site_amt else 0,
                    "top_vertical": tv.index[0] if len(tv) else "-",
                    "top_vxb": tvb.index[0] if len(tvb) else "-",
                    "top5": top5,
                })
            out[wh] = {"site_cr": cr(site_amt), "fcz": wdf["fcz"].mode().iloc[0], "bus": bus}
        return out
    payload["site_findings"] = _findings(df)
    payload["site_findings_m"] = {m: _findings(mdf) for m, mdf in df.groupby("m")}

    # ---------- BU x vertical drill ----------
    def build_drill(frame, top_n=25):
        slc = frame.groupby(["bu","v"])["amt"].sum().nlargest(top_n)
        top_pairs = set(slc.index)
        midx = pd.MultiIndex.from_arrays([frame["bu"], frame["v"]])
        fsub = frame[midx.isin(top_pairs)]
        groups = {k: sub for k, sub in fsub.groupby(["bu","v"])}
        out = []
        for (bu, v), amt in slc.items():
            sdf = groups[(bu, v)]
            sr = (sdf.groupby("sr").agg(amt=("amt","sum"), u=("u","sum"))
                     .sort_values("amt", ascending=False).head(12))
            fb = (sdf.groupby("fb").agg(amt=("amt","sum"), u=("u","sum"))
                     .sort_values("amt", ascending=False))
            top_br = sdf.groupby("br")["amt"].sum().sort_values(ascending=False)
            out.append({
                "v": v, "bu": bu, "cr": cr(amt), "u": int(sdf["u"].sum()),
                "top_brand": top_br.index[0] if len(top_br) else "-",
                "subreasons": [{"sr":i,"cr":cr(r.amt),"u":int(r.u)} for i,r in sr.iterrows()],
                "buckets":    [{"fb":i,"cr":cr(r.amt),"u":int(r.u)} for i,r in fb.iterrows()],
            })
        return out

    payload["site_vertical_drill"] = {wh: build_drill(wdf, 25) for wh, wdf in df.groupby("wh")}
    payload["overall_drill"] = build_drill(df, 40)

    # ---------- brand metrics ----------
    def build_brand(frame, n=40):
        g = frame.groupby(["bu","br"]).agg(amt=("amt","sum"), u=("u","sum")).reset_index()
        g = g.sort_values("amt", ascending=False).head(n)
        return [{"br":r.br, "bu":r.bu, "cr":cr(r.amt), "u":int(r.u)} for r in g.itertuples()]
    payload["site_brand"]    = {wh: build_brand(wdf) for wh, wdf in df.groupby("wh")}
    payload["overall_brand"] = build_brand(df, 60)

    # ---------- DH-level (overall + per month) ----------
    def _dh(frame):
        out = {}
        for bu, bdf in frame.groupby("bu"):
            g = (bdf.groupby(["dh","z"]).agg(amt=("amt","sum"), u=("u","sum")).reset_index())
            g = g.sort_values("amt", ascending=False).head(30)
            fb_by_dh = (bdf.groupby(["dh","fb"])["amt"].sum().reset_index()
                        .sort_values("amt").drop_duplicates("dh", keep="last")
                        .set_index("dh")["fb"].to_dict())
            out[bu] = [{"dh":r.dh, "z":r.z, "cr":cr(r.amt), "u":int(r.u),
                        "top_fb": fb_by_dh.get(r.dh, "-")} for r in g.itertuples()]
        return out
    payload["dh_by_bu"] = _dh(df)
    payload["dh_by_bu_m"] = {m: _dh(mdf) for m, mdf in df.groupby("m")}
    payload["dh_by_bu_mz"] = {m: {z: _dh(zdf) for z, zdf in mdf.groupby("fcz")}
                              for m, mdf in df.groupby("m")}

    # ---------- zone summary ----------
    zms = {}
    for m, mdf in df.groupby("m"):
        zms[m] = {}
        for z, zdf in mdf.groupby("fcz"):
            ts = zdf.groupby("wh")["amt"].sum().sort_values(ascending=False)
            tv = zdf.groupby("v")["amt"].sum().sort_values(ascending=False)
            tb = zdf.groupby("bu")["amt"].sum().sort_values(ascending=False)
            zms[m][z] = {"cr": cr(zdf["amt"].sum()),
                         "site": ts.index[0] if len(ts) else "-",
                         "site_cr": cr(ts.iloc[0]) if len(ts) else 0,
                         "vert": tv.index[0] if len(tv) else "-",
                         "vert_cr": cr(tv.iloc[0]) if len(tv) else 0,
                         "bu": tb.index[0] if len(tb) else "-",
                         "bu_cr": cr(tb.iloc[0]) if len(tb) else 0,
                         "top_verts": [{"v": i, "cr": cr(a)} for i, a in tv.head(10).items()]}
    payload["zone_month_summary"] = zms
    return payload


TEMPLATE_HTML = r""" <meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RI Loss Analysis</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">

<style>
:root{
  --bg:#f6f7f9; --panel:#ffffff; --ink:#141b26; --muted:#6a7482; --faint:#98a1ad;
  --line:#e7eaef; --line2:#eef1f5; --accent:#2f5bd4;
  --north:#3b6fd4; --south:#0e9b8a; --east:#e0952a; --west:#8a4fd0; --unknown:#94a0af;
  --up:#d64550; --down:#1f9d6b; --heat:#2f5bd4;
  --shadow:0 1px 2px rgba(20,27,38,.04),0 2px 8px rgba(20,27,38,.05);
}
*{box-sizing:border-box}

/* Lock scrolling to dashboard layout inside the iframe window */
html, body {
  height: 100%;
  overflow: hidden;
}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:Inter,-apple-system,Segoe UI,Roboto,sans-serif;font-size:13px;line-height:1.45;
  display: flex; flex-direction: column;
}
h1,h2,h3{font-family:"Space Grotesk",Inter,sans-serif;margin:0;letter-spacing:-.01em}
.num{font-family:"Space Grotesk",Inter,sans-serif;font-variant-numeric:tabular-nums}

/* Main inner scroll layer */
.scroll-container {
  flex: 1;
  overflow-y: auto;
  padding: 0 20px 64px;
}
.wrap{max-width:1480px;margin:0 auto}
.split{display:grid;grid-template-columns:1.7fr 1fr;gap:14px}
.top5{margin-top:3px}
@media(max-width:1000px){.split{grid-template-columns:1fr}}

/* Fixed Header Sticky Top block */
.sticky-top-panel {
  position: sticky;
  top: 0;
  z-index: 100;
  background: var(--bg);
  border-bottom: 1px solid var(--line);
  padding: 0 20px;
}

/* header */
header{padding:18px 0 10px;}
.eyebrow{font-size:11px;font-weight:600;letter-spacing:.14em;text-transform:uppercase;color:var(--accent)}
header h1{font-size:24px;font-weight:700;margin-top:3px}
.sub{color:var(--muted);margin-top:4px;font-size:12.5px}

/* filter bar */
.filters{padding:10px 0 14px;}
.filters .row{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end}
.fg{display:flex;flex-direction:column;gap:5px;min-width:150px}
.fg label{font-size:10.5px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.chips{display:flex;gap:5px;flex-wrap:wrap;max-width:340px}
.chip{border:1px solid var(--line);background:var(--panel);border-radius:999px;
  padding:4px 10px;font-size:11.5px;cursor:pointer;user-select:none;transition:.12s;font-weight:500}
.chip:hover{border-color:var(--accent)}
.chip.on{background:var(--accent);border-color:var(--accent);color:#fff}
.chip[data-z]{padding-left:22px;position:relative}
.chip[data-z]::before{content:"";position:absolute;left:9px;top:50%;transform:translateY(-50%);
  width:7px;height:7px;border-radius:2px}
.chip[data-z="North"]::before{background:var(--north)} .chip[data-z="South"]::before{background:var(--south)}
.chip[data-z="East"]::before{background:var(--east)} .chip[data-z="West"]::before{background:var(--west)}
select{font-family:inherit;font-size:12.5px;padding:6px 9px;border:1px solid var(--line);
  border-radius:8px;background:var(--panel);color:var(--ink);min-width:150px}
.disabled{opacity:.45;pointer-events:none}
.btn{border:1px solid var(--line);background:var(--panel);border-radius:8px;padding:7px 13px;
  font-size:12px;font-weight:600;cursor:pointer}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.scope{font-size:12px;color:var(--muted);margin-left:auto}
.scope b{color:var(--ink);font-weight:600}

/* KPI */
.kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:26px;margin-top:16px}
.kpi{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 15px;box-shadow:var(--shadow)}
.kpi .k{font-size:10.5px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.kpi .v{font-size:23px;font-weight:700;margin-top:6px;font-family:"Space Grotesk";font-variant-numeric:tabular-nums}
.kpi .d{font-size:11.5px;margin-top:3px;color:var(--faint)}

/* sections */
section{margin-bottom:30px}
.shead{display:flex;align-items:baseline;gap:10px;margin-bottom:12px}
.shead .n{font-family:"Space Grotesk";font-weight:700;color:var(--accent);font-size:13px}
.shead h2{font-size:16px;font-weight:600}
.shead .h{font-size:12px;color:var(--muted);margin-left:6px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);overflow:hidden}
.pad{padding:16px 18px}

/* stat row */
.statrow{display:flex;gap:0;flex-wrap:wrap;border-bottom:1px solid var(--line2)}
.stat{padding:12px 18px;border-right:1px solid var(--line2)}
.stat .l{font-size:11px;color:var(--muted);display:flex;align-items:center;gap:6px;font-weight:500}
.stat .l .dot{width:8px;height:8px;border-radius:2px}
.stat .v{font-size:18px;font-weight:700;font-family:"Space Grotesk";font-variant-numeric:tabular-nums;margin-top:3px}
.stat.total{background:#f0f4ff}
.stat.total .l{color:var(--accent);font-weight:600}

/* tables */
.tscroll{max-height:440px;overflow:auto}
table{width:100%;border-collapse:separate;border-spacing:0;font-size:12.5px}
thead th{position:sticky;top:0;z-index:2;background:#f2f4f8;color:var(--muted);
  font-weight:600;text-align:right;padding:9px 12px;font-size:11px;letter-spacing:.03em;
  text-transform:uppercase;border-bottom:1px solid var(--line);white-space:nowrap}
thead th:first-child{text-align:left}
tbody td{padding:8px 12px;text-align:right;border-bottom:1px solid var(--line2);white-space:nowrap;
  font-variant-numeric:tabular-nums}
tbody td:first-child{text-align:left;font-weight:500}
tbody tr:hover{background:#f8fafc}
.zband{border-left:3px solid var(--unknown)}
.zband.North{border-left-color:var(--north)} .zband.South{border-left-color:var(--south)}
.zband.East{border-left-color:var(--east)} .zband.West{border-left-color:var(--west)}
.pill{display:inline-block;padding:2px 8px;border-radius:6px;font-size:11px;font-weight:600;background:#eef1f5;color:var(--muted)}
.muted{color:var(--muted)} .tag{color:var(--west);font-weight:600}
.controls{display:flex;gap:10px;align-items:center;padding:12px 18px;border-bottom:1px solid var(--line2);flex-wrap:wrap}
.controls input{font-family:inherit;font-size:12.5px;padding:6px 10px;border:1px solid var(--line);border-radius:8px;min-width:180px}
.linkbtn{background:none;border:none;color:var(--accent);font-weight:600;cursor:pointer;font-size:12px;font-family:inherit}

/* findings */
.finds{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.find{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:13px 15px;box-shadow:var(--shadow)}
.find .fh{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px}
.find .fh .w{font-family:"Space Grotesk";font-weight:700;font-size:14px}
.find .fh .c{font-weight:700;font-variant-numeric:tabular-nums}
.find .line{font-size:12px;padding:6px 0;border-top:1px solid var(--line2)}
.find .line .bu{font-weight:600}
.find .line .sh{color:var(--muted)}
.find .line .k{color:var(--muted);font-size:11px}

/* drill cards */
.dcards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;padding:16px 18px 4px}
.dc{border:1px solid var(--line);border-radius:10px;padding:12px 14px;background:#fafbfd}
.dc .l{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);font-weight:600}
.dc .v{font-size:15px;font-weight:700;margin-top:4px;font-family:"Space Grotesk"}
.two{display:grid;grid-template-columns:1fr 1fr;gap:0}
.two>div:first-child{border-right:1px solid var(--line2)}
.subhead{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);padding:12px 18px 4px}
.note{font-size:11.5px;color:var(--faint);padding:0 18px 14px}
.hbar{height:9px;border-radius:3px;background:var(--accent);display:inline-block;vertical-align:middle}

@media(max-width:1000px){.kpis{grid-template-columns:repeat(3,1fr)}.dcards,.two{grid-template-columns:1fr}}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
</style>

<div class="sticky-top-panel">
  <div class="wrap">
    <header>
      <div class="eyebrow">Reverse Chain · Returns Centre</div>
      <h1>RI Loss Analysis</h1>
      <div class="sub" id="subline"></div>
    </header>

    <div class="filters">
      <div class="row">
        <div class="fg"><label>Zone (FC / DH)</label><div class="chips" id="f-zone"></div></div>
        <div class="fg"><label>Month <span style="font-weight:400;text-transform:none;opacity:.6">· pick one, click again for all</span></label><div class="chips" id="f-month"></div></div>
        <div class="fg disabled"><label>Week</label><select disabled><option>— no week field —</option></select></div>
        <div class="fg"><label>Business Unit</label><div class="chips" id="f-bu"></div></div>
        <div class="fg"><label>Warehouse / Site</label><select id="f-wh" multiple size="1" style="min-height:34px"></select></div>
        <button class="btn" id="reset">Reset filters</button>
        <div class="scope" id="scope"></div>
      </div>
    </div>
  </div>
</div>

<div class="scroll-container">
  <div class="wrap">
    <div class="kpis" id="kpis"></div>

    <section>
      <div class="shead"><span class="n">02</span><h2>Zone-wise loss</h2><span class="h">month-on-month · FC Zone</span></div>
      <div class="card tscroll" style="margin-bottom:14px"><div class="subhead">Zone × month (₹ Cr) · reduction % across the period</div><table id="zone-trend"></table></div>
      <div class="card"><div class="subhead" id="zone-sum-head">Hotspots · worst site &amp; vertical by zone</div><div class="finds" id="zone-summary" style="padding:14px 16px"></div></div>
    </section>

    <section>
      <div class="shead"><span class="n">03</span><h2>Business-unit loss</h2><span class="h">month-on-month · table</span></div>
      <div class="card"><div class="tscroll"><table id="bu-trend"></table></div></div>
      <div class="note" style="margin-top:2px">Under each value: <b>RTO</b> <span style="display:inline-block;width:16px;height:5px;border-radius:3px;background:#2f5bd4;vertical-align:middle"></span> vs <b>RVP</b> <span style="display:inline-block;width:16px;height:5px;border-radius:3px;background:#9db8ef;vertical-align:middle"></span> split (% of that cell's loss; hover for ₹ Cr).</div>
    </section>

    <section>
      <div class="shead"><span class="n">04</span><h2>BU × Final bucket</h2><span class="h">where inside each BU the loss sits · with return-reason cut (₹ Cr)</span></div>
      <div class="split">
        <div class="card tscroll"><table id="matrix"></table></div>
        <div class="card tscroll"><div class="subhead">RTO vs RVP · in scope <span class="muted" style="font-weight:400;text-transform:none">— click a type to split reasons</span></div><table id="rto"></table>
          <div class="subhead" id="reason-head">Return reason · in scope</div><table id="reason"></table></div>
      </div>
    </section>

    <section>
      <div class="shead"><span class="n">05</span><h2>Vertical × Brand deep-dive</h2><span class="h" id="vscope">top 50 · pareto</span></div>
      <div class="split">
        <div class="card">
          <div class="controls">
            <span class="muted">Resealing</span><select id="v-resel"></select>
            <span class="muted">Tag reprinting</span><select id="v-tagr"></select>
            <span class="muted">BGM brand</span><select id="v-bgmb"></select>
            <span class="muted">BGM mapping</span><select id="v-bgmm"></select>
            <span class="note" style="padding:0" id="vpareto"></span>
          </div>
          <div class="tscroll"><table id="vtable"></table></div>
          <div class="note"><button class="linkbtn" id="vmore"></button> · Click any row for its bucket / site / RTO-RVP / reason splits → · Reacts to the top <b>Business Unit</b>, <b>Warehouse</b> and <b>Month</b> filters and the selectors above.</div>
        </div>
        <div class="card tscroll">
          <div class="subhead" id="vbk-head">Bucket split · click a row</div><table id="vbucket"></table>
          <div class="subhead" id="vsite-head" style="margin-top:18px">By site — where this is worst</div><table id="vsite"></table>
          <div class="subhead" id="vrto-head" style="margin-top:18px">RTO vs RVP</div><table id="vrto"></table>
          <div class="subhead" id="vrtorn-head" style="margin-top:18px">Return reason · RTO</div><table id="vrtorn"></table>
          <div class="subhead" id="vrvprn-head" style="margin-top:18px">Return reason · RVP</div><table id="vrvprn"></table>
        </div>
      </div>
    </section>

    <section>
      <div class="shead"><span class="n">06</span><h2>Site-wise loss &amp; key findings</h2><span class="h">per site, per BU — worst vertical &amp; brand</span></div>
      <div class="card" style="margin-bottom:14px"><div class="tscroll"><table id="sitetable"></table></div></div>
      <div class="finds" id="finds"></div>
    </section>

    <section>
      <div class="shead"><span class="n">07</span><h2>DH &amp; Zone analysis, per BU</h2><span class="h">Zone×BU on FC Zone · DH ranked on customer zone</span></div>
      <div class="card" style="margin-bottom:14px"><div class="subhead">Zone × BU matrix (₹ Cr · FC Zone)</div><div class="tscroll"><table id="zbu"></table></div></div>
      <div class="card">
        <div class="controls"><span class="muted">Business unit</span><select id="dh-bu"></select><span class="note" style="padding:0" id="dh-note2"></span></div>
        <div class="statrow" id="dh-stats"></div>
        <div class="tscroll"><table id="dhtable"></table></div>
      </div>
    </section>
  </div>
</div>

<script>
const DATA = /*__RI_DATA__*/;
const ZC = {North:'#3b6fd4',South:'#0e9b8a',East:'#e0952a',West:'#8a4fd0',Unknown:'#94a0af'};
const PAL = ['#2f5bd4','#0e9b8a','#e0952a','#8a4fd0','#d64550','#3b6fd4','#5a9e6f','#c76b3a','#6b7bd6','#b0559a','#4aa3a3','#9a8f2f'];

function ind(n){n=Math.round(n);let s=String(Math.abs(n)),r='';let last=s.slice(-3),rest=s.slice(0,-3);
  if(rest)r=rest.replace(/\B(?=(\d{2})+(?!\d))/g,',')+','+last;else r=last;return (n<0?'-':'')+r;}
const cr = v => '₹'+(+v).toFixed(2)+' Cr';
const rs = v => '₹'+ind(v);
const crToRs = c => c*1e7;

const M=DATA.meta;
const state={zone:new Set(),month:new Set([M.months[M.months.length-1]]),bu:new Set(),wh:new Set()};

function chip(txt,set,val,zone){const c=document.createElement('span');c.className='chip'+(set.has(val)?' on':'');
  c.textContent=txt;if(zone)c.dataset.z=val;c.onclick=()=>{set.has(val)?set.delete(val):set.add(val);render();};return c;}
function buildFilters(){
  const fz=document.getElementById('f-zone');M.zones.forEach(z=>fz.appendChild(chip(z,state.zone,z,true)));
  const fm=document.getElementById('f-month');
  M.months.forEach(m=>{const c=document.createElement('span');c.className='chip'+(state.month.has(m)?' on':'');
    c.textContent=m;
    c.onclick=()=>{ if(state.month.has(m)&&state.month.size===1){state.month=new Set();}
                    else {state.month=new Set([m]);} buildChips(); render(); };
    fm.appendChild(c);});
  const fb=document.getElementById('f-bu');
  const BU_ALLOW=['Electronics','BGM','LifeStyle','Home','LargeAppliances','Mobile'];
  M.bus.filter(b=>BU_ALLOW.includes(b)).forEach(b=>fb.appendChild(chip(b,state.bu,b)));
  const fw=document.getElementById('f-wh');[...new Set(DATA.base.map(r=>r.wh))].sort().forEach(w=>{
    const o=document.createElement('option');o.value=w;o.textContent=w;fw.appendChild(o);});
  fw.size=6;fw.onchange=()=>{state.wh=new Set([...fw.selectedOptions].map(o=>o.value));render();};
  document.getElementById('reset').onclick=()=>{state.zone.clear();state.bu.clear();state.wh.clear();
    state.month=new Set([M.months[M.months.length-1]]);[...fw.options].forEach(o=>o.selected=false);buildChips();render();};
}
function buildChips(){document.querySelectorAll('#f-zone .chip').forEach(c=>c.classList.toggle('on',state.zone.has(c.dataset.z)));
  document.querySelectorAll('#f-month .chip').forEach(c=>c.classList.toggle('on',state.month.has(c.textContent)));
  document.querySelectorAll('#f-bu .chip').forEach(c=>c.classList.toggle('on',state.bu.has(c.textContent)));}

function fbase(){return DATA.base.filter(r=>
  (state.zone.size===0||state.zone.has(r.fcz))&&
  (state.month.size===0||state.month.has(r.m))&&
  (state.bu.size===0||state.bu.has(r.bu))&&
  (state.wh.size===0||state.wh.has(r.wh)));}
function sum(rows,k){return rows.reduce((a,b)=>a+b[k],0);}
function roll(rows,key){const m={};rows.forEach(r=>{m[r[key]]=(m[r[key]]||0)+r.cr;});return m;}

function kpis(rows){
  const tcr=sum(rows,'cr'),u=sum(rows,'u'),s=sum(rows,'s');
  const avg=u?crToRs(tcr)/u:0;const sites=new Set(rows.map(r=>r.wh)).size;
  const fbk=roll(rows,'fb');const top=Object.entries(fbk).sort((a,b)=>b[1]-a[1])[0]||['—',0];
  const K=[['Total loss',cr(tcr),''],['Units',ind(u),''],['Shipments',ind(s),''],
    ['Avg loss / unit',rs(avg),''],['Sites in scope',sites,''],['Top bucket',top[0],cr(top[1])]];
  document.getElementById('kpis').innerHTML=K.map(k=>
    `<div class="kpi"><div class="k">${k[0]}</div><div class="v">${k[1]}</div><div class="d">${k[2]||'&nbsp;'}</div></div>`).join('');
}

let zChart,bChart;
function fbaseNoMonth(){return DATA.base.filter(r=>
  (state.zone.size===0||state.zone.has(r.fcz))&&
  (state.bu.size===0||state.bu.has(r.bu))&&
  (state.wh.size===0||state.wh.has(r.wh)));}
function latestMonth(){
  const sel=[...state.month].filter(m=>M.months.includes(m)).sort();
  return sel.length?sel[sel.length-1]:M.months[M.months.length-1];
}
function trendSection(key,statEl,canvasId,chartRef,palFn,tableId,splitRR){
  const rows=fbaseNoMonth();
  const months=(state.month.size>=2)?M.months.filter(m=>state.month.has(m)):M.months;
  const latest=latestMonth();
  const totAll={};rows.forEach(r=>{totAll[r[key]]=(totAll[r[key]]||0)+r.cr;});
  const keys=Object.keys(totAll).sort((a,b)=>totAll[b]-totAll[a]);
  if(tableId && document.getElementById(tableId)) trendTable(rows,key,keys,months,tableId,palFn,splitRR);
}
function trendTable(rows,key,keys,months,tableId,palFn,splitRR){
  const cell={};rows.forEach(r=>{cell[r[key]+'|'+r.m]=(cell[r[key]+'|'+r.m]||0)+r.cr;});
  // RTO/RVP split per (key,month) from base_rto (respects zone/bu/wh, all months)
  const rr={};
  if(splitRR){
    DATA.base_rto.filter(r=>(state.zone.size===0||state.zone.has(r.fcz))&&
      (state.bu.size===0||state.bu.has(r.bu))&&(state.wh.size===0||state.wh.has(r.wh)))
      .forEach(r=>{const k=r[key]+'|'+r.m;if(!rr[k])rr[k]={rto:0,rvp:0};
        if(r.rr==='RTO')rr[k].rto+=r.cr; else if(r.rr==='RVP')rr[k].rvp+=r.cr;});
  }
  const rrT={};  // per-month totals for the Total row
  const seg=(o)=>{if(!o||(o.rto+o.rvp)<=0)return '';const t=o.rto+o.rvp;const W=34;
    const rp=Math.round(o.rto/t*100), vp=100-rp;
    return `<div style="display:flex;flex-direction:column;gap:1px;margin-top:3px;font-size:10px;line-height:1.2">`+
      `<span style="display:flex;align-items:center;gap:4px;justify-content:flex-end" title="RTO ₹${o.rto.toFixed(2)} Cr">`+
        `<span style="height:5px;border-radius:3px;background:#2f5bd4;width:${o.rto>0?Math.max(3,o.rto/t*W):0}px"></span>`+
        `<span class="muted">RTO ${rp}%</span></span>`+
      `<span style="display:flex;align-items:center;gap:4px;justify-content:flex-end" title="RVP ₹${o.rvp.toFixed(2)} Cr">`+
        `<span style="height:5px;border-radius:3px;background:#9db8ef;width:${o.rvp>0?Math.max(3,o.rvp/t*W):0}px"></span>`+
        `<span class="muted">RVP ${vp}%</span></span></div>`;};
  const colT={};months.forEach(m=>colT[m]=0);
  const lbl=months.length>1?`${months[0]}→${months[months.length-1]} %`:'trend %';
  let h='<thead><tr><th>'+(key==='fcz'?'Zone':'BU')+'</th>'+months.map(m=>`<th>${m}</th>`).join('')+`<th>${lbl}</th></tr></thead><tbody>`;
  const delta=(vals)=>{const f=vals[0],l=vals[vals.length-1];const p=f?((l-f)/f*100):0;
    return `<td style="color:${p<0?'var(--down)':(p>0?'var(--up)':'var(--muted)')};font-weight:600">${months.length>1?(p<0?'▼':(p>0?'▲':'–'))+' '+Math.abs(p).toFixed(0)+'%':'–'}</td>`;};
  keys.forEach(k=>{const vals=months.map(m=>cell[k+'|'+m]||0);months.forEach((m,i)=>colT[m]+=vals[i]);
    const band=palFn?` class="zband ${k}"`:'';
    h+=`<tr${band}><td>${k}</td>`+months.map((m,i)=>{const o=rr[k+'|'+m];
      if(splitRR&&o){rrT[m]=rrT[m]||{rto:0,rvp:0};rrT[m].rto+=o.rto;rrT[m].rvp+=o.rvp;}
      return `<td><div>${vals[i]?vals[i].toFixed(2):'·'}</div>${splitRR?seg(o):''}</td>`;}).join('')+delta(vals)+'</tr>';});
  const tvals=months.map(m=>colT[m]);
  h+=`<tr style="background:#f2f4f8"><td><b>Total</b></td>`+months.map((m,i)=>
      `<td><b>${tvals[i].toFixed(2)}</b>${splitRR?seg(rrT[m]):''}</td>`).join('')+delta(tvals)+'</tr>';
  document.getElementById(tableId).innerHTML=h+'</tbody>';
}
function zoneSummary(){
  const lm=latestMonth();const Z=(DATA.zone_month_summary||{})[lm]||{};
  document.getElementById('zone-sum-head').innerHTML=`Hotspots · worst BU · site · vertical by zone · <b>${lm}</b>`;
  if(!window._whZone){window._whZone={};DATA.base.forEach(r=>{window._whZone[r.wh]=r.fcz;});}
  const allowZones=state.wh.size?new Set([...state.wh].map(w=>window._whZone[w])):null;
  const order=['North','South','East','West'].filter(z=>Z[z]).concat(Object.keys(Z).filter(z=>!['North','South','East','West'].includes(z)))
    .filter(z=>(state.zone.size===0||state.zone.has(z)) && (!allowZones||allowZones.has(z)));
  document.getElementById('zone-summary').innerHTML=order.map(z=>{const d=Z[z];
    const tv=(d.top_verts||[]).map(x=>`${x.v} <span class="muted">${x.cr.toFixed(2)}</span>`).join(' · ');
    return `<div class="find">
    <div class="fh"><span class="w">${z}</span><span class="c">${cr(d.cr)}</span></div>
    <div class="line"><span class="k">worst BU: <b>${d.bu}</b> <span class="muted">${cr(d.bu_cr)}</span></span></div>
    <div class="line"><span class="k">worst site: <b>${d.site}</b> <span class="muted">${cr(d.site_cr)}</span></span></div>
    <div class="line"><span class="k">worst vertical: <b>${d.vert}</b> <span class="muted">${cr(d.vert_cr)}</span></span></div>
    <div class="line" style="margin-top:4px"><span class="k" style="font-size:11px"><b>top 10 verticals:</b> ${tv}</span></div>
  </div>`;}).join('')||'<span class="muted" style="padding:14px">No data for this month.</span>';
}

function matrix(rows){
  const bus=M.bus.filter(b=>rows.some(r=>r.bu===b));
  const fbs=M.buckets.filter(f=>rows.some(r=>r.fb===f));
  const cell={};let cmax=0;
  rows.forEach(r=>{const k=r.bu+'|'+r.fb;cell[k]=(cell[k]||0)+r.cr;});
  bus.forEach(b=>fbs.forEach(f=>{cmax=Math.max(cmax,cell[b+'|'+f]||0);}));
  const colT={},rowT={};let gT=0;
  bus.forEach(b=>{rowT[b]=0;fbs.forEach(f=>{const v=cell[b+'|'+f]||0;rowT[b]+=v;colT[f]=(colT[f]||0)+v;gT+=v;});});
  let h='<thead><tr><th>BU ↓ / Bucket →</th>'+fbs.map(f=>`<th>${f}</th>`).join('')+'<th>Total</th></tr></thead><tbody>';
  bus.sort((a,b)=>rowT[b]-rowT[a]).forEach(b=>{
    h+=`<tr><td>${b}</td>`+fbs.map(f=>{const v=cell[b+'|'+f]||0;const a=cmax?v/cmax:0;
      const bg=v?`background:rgba(47,91,212,${(0.06+a*0.5).toFixed(2)});color:${a>0.6?'#fff':'inherit'}`:'color:#c3cad3';
      return `<td style="${bg}">${v?v.toFixed(2):'·'}</td>`;}).join('')+`<td><b>${rowT[b].toFixed(2)}</b></td></tr>`;});
  h+=`<tr style="background:#f2f4f8"><td><b>Total</b></td>`+fbs.map(f=>`<td><b>${(colT[f]||0).toFixed(2)}</b></td>`).join('')+`<td><b>${gT.toFixed(2)}</b></td></tr>`;
  document.getElementById('matrix').innerHTML=h+'</tbody>';
}

function fbaseR(){return DATA.base_reason.filter(r=>
  (state.zone.size===0||state.zone.has(r.fcz))&&
  (state.month.size===0||state.month.has(r.m))&&
  (state.bu.size===0||state.bu.has(r.bu))&&
  (state.wh.size===0||state.wh.has(r.wh)));}
let rsnRR='all';  // 'all' | 'RTO' | 'RVP'
function reasonCut(){
  let rows=fbaseR();
  if(rsnRR!=='all') rows=rows.filter(r=>r.rr===rsnRR);
  const g={};let tot=0;
  rows.forEach(r=>{g[r.rsn]=(g[r.rsn]||0)+r.cr;tot+=r.cr;});
  const list=Object.entries(g).sort((a,b)=>b[1]-a[1]);
  const mx=list.length?list[0][1]:1;
  const lbl=rsnRR==='all'?'in scope':rsnRR;
  document.getElementById('reason-head').innerHTML=`Return reason · <b>${lbl}</b>`;
  let h='<thead><tr><th>Return reason</th><th></th><th>₹ Cr</th><th>%</th></tr></thead><tbody>';
  h+=list.map(([k,v])=>`<tr><td>${k}</td>
    <td style="width:64px"><span class="hbar" style="width:${Math.max(3,v/mx*54)}px"></span></td>
    <td>${v.toFixed(2)}</td><td class="muted">${tot?(v/tot*100).toFixed(1):0}%</td></tr>`).join('')
    || '<tr><td colspan="4" class="muted">—</td></tr>';
  document.getElementById('reason').innerHTML=h+'</tbody>';
}

function fbaseRT(){return DATA.base_rto.filter(r=>
  (state.zone.size===0||state.zone.has(r.fcz))&&
  (state.month.size===0||state.month.has(r.m))&&
  (state.bu.size===0||state.bu.has(r.bu))&&
  (state.wh.size===0||state.wh.has(r.wh)));}
function rtoCut(){
  const rows=fbaseRT();const g={};let tot=0;
  rows.forEach(r=>{g[r.rr]=(g[r.rr]||0)+r.cr;tot+=r.cr;});
  const order=['RVP','RTO','Unknown'];
  const list=order.filter(k=>g[k]!==undefined).map(k=>[k,g[k]])
    .concat(Object.entries(g).filter(([k])=>!order.includes(k)));
  const mx=list.length?Math.max(...list.map(x=>x[1])):1;
  let h='<thead><tr><th>Type</th><th></th><th>₹ Cr</th><th>%</th></tr></thead><tbody>';
  h+=list.map(([k,v])=>{const sel=(rsnRR===k)?' style="background:#eef2ff"':'';
    const clk=(k==='RTO'||k==='RVP')?` data-rr="${k}" style="cursor:pointer"`:'';
    return `<tr${clk||sel}><td>${k}${(k==='RTO'||k==='RVP')?' <span class="muted" style="font-size:10px">▸ click</span>':''}</td><td style="width:64px"><span class="hbar" style="width:${Math.max(3,v/mx*54)}px"></span></td>
    <td>${v.toFixed(2)}</td><td class="muted">${tot?(v/tot*100).toFixed(1):0}%</td></tr>`;}).join('');
  const el=document.getElementById('rto');el.innerHTML=h+'</tbody>';
  el.onclick=e=>{const tr=e.target.closest&&e.target.closest('tr[data-rr]');
    if(tr){const k=tr.getAttribute('data-rr');rsnRR=(rsnRR===k)?'all':k;rtoCut();reasonCut();}};
}

let vShowAll=false, VXB_LIST=[];
function _pctTable(elId, headEl, headHtml, rows, keyName, keyProp){
  headEl.innerHTML=headHtml;
  const tot=rows.reduce((a,b)=>a+b.cr,0); const mx=rows.length?Math.max(...rows.map(r=>r.cr)):1;
  let h=`<thead><tr><th>${keyName}</th><th></th><th>₹ Cr</th><th>%</th></tr></thead><tbody>`;
  h+=rows.map(r=>`<tr><td>${r[keyProp]}</td><td style="width:60px"><span class="hbar" style="width:${Math.max(3,r.cr/mx*50)}px"></span></td>
    <td>${r.cr.toFixed(2)}</td><td class="muted">${tot?(r.cr/tot*100).toFixed(1):0}%</td></tr>`).join('')
    || `<tr><td colspan="4" class="muted">—</td></tr>`;
  document.getElementById(elId).innerHTML=h+'</tbody>';
}
function showVBucket(item){
  const H=id=>document.getElementById(id);
  if(!item){
    ['vbucket','vsite','vrto','vrtorn','vrvprn'].forEach(x=>H(x).innerHTML='');
    H('vbk-head').textContent='Bucket split · click a row';
    return;
  }
  const lbl=item.vxb, amt=cr(item.cr);
  // bucket split
  const brows=Object.entries(item.fbd||{}).map(([fb,cr])=>({fb,cr})).sort((a,b)=>b.cr-a.cr);
  _pctTable('vbucket', H('vbk-head'), `Bucket split · <b>${lbl}</b> · ${amt}`, brows, 'Final bucket', 'fb');
  // by site — where this vxb is worst
  _pctTable('vsite', H('vsite-head'), `By site · where <b>${lbl}</b> is worst`, (item.sites||[]).slice(0,15), 'Site', 'wh');
  // RTO vs RVP
  _pctTable('vrto', H('vrto-head'), `RTO vs RVP · <b>${lbl}</b>`, (item.rrd||[]).slice().sort((a,b)=>b.cr-a.cr), 'Type', 'rr');
  // reason split, RTO and RVP separately
  _pctTable('vrtorn', H('vrtorn-head'), `Return reason · RTO · <b>${lbl}</b>`, (item.rtoRn||[]), 'Reason', 'rn');
  _pctTable('vrvprn', H('vrvprn-head'), `Return reason · RVP · <b>${lbl}</b>`, (item.rvpRn||[]), 'Reason', 'rn');
}
function initVFilters(){
  const mk=(id,opts)=>{const s=document.getElementById(id);s.add(new Option('All','__all'));
    opts.forEach(o=>s.add(new Option(o,o)));s.onchange=vtable;};
  mk('v-resel',M.resel_opts);mk('v-tagr',M.tagr_opts);
  mk('v-bgmb',M.bgmb_opts||[]);mk('v-bgmm',M.bgmm_opts||[]);
  document.getElementById('vmore').onclick=()=>{vShowAll=!vShowAll;vtable();};
  document.getElementById('vtable').onclick=e=>{const tr=e.target.closest&&e.target.closest('tr[data-i]');
    if(tr)showVBucket(VXB_LIST[+tr.getAttribute('data-i')]);};
}

/* Vertical×Brand aggregation — follows top BU, Warehouse and Month filters + section selectors */
function vtable(){
  const rf=document.getElementById('v-resel').value, tf=document.getElementById('v-tagr').value,
        gbf=document.getElementById('v-bgmb').value, gmf=document.getElementById('v-bgmm').value;
  const activeBU=state.bu, activeMonths=state.month, activeWh=state.wh;
  const pass=s=>((rf==='__all'||s.r===rf)&&(tf==='__all'||s.t===tf)&&(gbf==='__all'||s.gb===gbf)&&(gmf==='__all'||s.gm===gmf));
  const agg={};
  // effective warehouse scope: explicit site filter, else zone filter (wh->zone from base), else all
  if(!window._whZone){window._whZone={};DATA.base.forEach(r=>{window._whZone[r.wh]=r.fcz;});}
  let whList=null;
  if(activeWh.size) whList=[...activeWh];
  else if(state.zone.size) whList=Object.keys(window._whZone).filter(w=>state.zone.has(window._whZone[w]));
  function addEntry(e, wh){
    if(activeMonths.size && !activeMonths.has(e.m)) return;
    let c=0,u=0; e.sp.forEach(s=>{ if(pass(s)){ c+=s.cr; u+=s.u; } });
    if(c<=0) return;
    const o=agg[e.vxb]||(agg[e.vxb]={cr:0,u:0,tfb:{},fbd:{},rrd:{},rnd:{},sites:{},rtoRn:{},rvpRn:{}});
    o.cr+=c; o.u+=u; o.tfb[e.top_fb]=(o.tfb[e.top_fb]||0)+c;
    (e.fb||[]).forEach(b=>{o.fbd[b.fb]=(o.fbd[b.fb]||0)+b.cr;});
    (e.rrd||[]).forEach(r=>{o.rrd[r.rr]=(o.rrd[r.rr]||0)+r.cr;});
    (e.rnd||[]).forEach(r=>{o.rnd[r.rn]=(o.rnd[r.rn]||0)+r.cr;});
    (e.rnr||[]).forEach(r=>{const t=(r.rr==='RVP')?o.rvpRn:(r.rr==='RTO'?o.rtoRn:null);if(t)t[r.rn]=(t[r.rn]||0)+r.cr;});
    if(wh) o.sites[wh]=(o.sites[wh]||0)+c;
  }
  if(whList){
    whList.forEach(wh=>{const bud=(DATA.vxb_fact_wh||{})[wh]||{};
      Object.entries(bud).forEach(([bu,arr])=>{ if(activeBU.size&&!activeBU.has(bu))return; arr.forEach(e=>addEntry(e,wh)); });});
  } else {
    // no site filter: use overall for totals, and vxb_fact_wh to attribute per-site
    Object.entries(DATA.vxb_fact).forEach(([bu,arr])=>{ if(activeBU.size&&!activeBU.has(bu))return; arr.forEach(e=>addEntry(e,null)); });
    Object.entries(DATA.vxb_fact_wh||{}).forEach(([wh,bud])=>{
      Object.entries(bud).forEach(([bu,arr])=>{ if(activeBU.size&&!activeBU.has(bu))return;
        arr.forEach(e=>{ if(activeMonths.size&&!activeMonths.has(e.m))return; let c=0; e.sp.forEach(s=>{if(pass(s))c+=s.cr;}); if(c>0&&agg[e.vxb])agg[e.vxb].sites[wh]=(agg[e.vxb].sites[wh]||0)+c; }); });});
  }
  let list=Object.entries(agg).map(([vxb,o])=>({vxb,cr:o.cr,u:o.u,fbd:o.fbd,
    rrd:Object.entries(o.rrd).map(([rr,cr])=>({rr,cr})),
    rnd:Object.entries(o.rnd).map(([rn,cr])=>({rn,cr})),
    sites:Object.entries(o.sites).map(([wh,cr])=>({wh,cr})).sort((a,b)=>b.cr-a.cr),
    rtoRn:Object.entries(o.rtoRn).map(([rn,cr])=>({rn,cr})).sort((a,b)=>b.cr-a.cr),
    rvpRn:Object.entries(o.rvpRn).map(([rn,cr])=>({rn,cr})).sort((a,b)=>b.cr-a.cr),
    top_fb:Object.entries(o.tfb).sort((a,b)=>b[1]-a[1])[0][0]})).sort((a,b)=>b.cr-a.cr).slice(0,50);
  VXB_LIST=list;
  const grand=list.reduce((a,b)=>a+b.cr,0);
  let cum=0;list.forEach(x=>{cum+=x.cr;x.cumpct=grand?cum/grand*100:0;});
  const p80=list.findIndex(x=>x.cumpct>=80);
  const vmax=list.length?list[0].cr:1;
  const show=vShowAll?list:list.slice(0,20);
  let h='<thead><tr><th>Vertical × Brand</th><th></th><th>₹ Cr</th><th>Units</th><th>₹/unit</th><th>Cum %</th><th>Top bucket</th></tr></thead><tbody>';
  h+=show.map((x,i)=>`<tr data-i="${i}" style="cursor:pointer"><td>${x.vxb}</td>
    <td style="width:80px"><span class="hbar" style="width:${Math.max(4,x.cr/vmax*70)}px"></span></td>
    <td>${x.cr.toFixed(2)}</td><td>${ind(x.u)}</td><td>${x.u?ind(crToRs(x.cr)/x.u):'—'}</td>
    <td class="muted">${x.cumpct.toFixed(0)}%</td><td><span class="pill">${x.top_fb}</span></td></tr>`).join('');
  h+=(list.length?'':'<tr><td colspan="7" class="muted">No Vertical × Brand match this filter.</td></tr>');
  document.getElementById('vtable').innerHTML=h+'</tbody>';
  const scope=activeBU.size?[...activeBU].join(', '):'all BUs';
  const ztxt=state.zone.size?` · ${[...state.zone].join('/')}`:'';
  const whtxt=state.wh.size?` · ${state.wh.size} site(s)`:'';
  const mtxt=state.month.size?` · ${[...state.month].sort().join(', ')}`:'';
  document.getElementById('vscope').textContent='top 50 · pareto · '+scope+ztxt+whtxt+mtxt;
  document.getElementById('vpareto').innerHTML=p80>=0
    ? `Pareto — top <b>${p80+1}</b> of ${list.length} reach <b>80%</b> of ₹${grand.toFixed(2)} Cr in scope`
    : `${list.length} in scope · ₹${grand.toFixed(2)} Cr`;
  document.getElementById('vmore').textContent=vShowAll?'Show top 20':`Show all ${list.length}`;
  showVBucket(list[0]);
}

function sitetable(rows){
  const months=[...state.month].length?[...state.month].sort():M.months;
  const whs=[...new Set(rows.map(r=>r.wh))];
  const t={};whs.forEach(w=>{t[w]={fcz:rows.find(r=>r.wh===w).fcz};months.forEach(m=>
    t[w][m]=rows.filter(r=>r.wh===w&&r.m===m).reduce((a,b)=>a+b.cr,0));t[w].tot=months.reduce((a,m)=>a+t[w][m],0);});
  whs.sort((a,b)=>t[b].tot-t[a].tot);
  let h='<thead><tr><th>Site</th><th>Zone</th>'+months.map(m=>`<th>${m}</th>`).join('')+(months.length>1?'<th>MoM %</th>':'')+'<th>Total ₹ Cr</th></tr></thead><tbody>';
  whs.forEach(w=>{let mom='';if(months.length>1){const a=t[w][months[months.length-2]],b=t[w][months[months.length-1]];
      const d=a?((b-a)/a*100):0;mom=`<td style="color:${d>0?'var(--up)':'var(--down)'}">${d>0?'▲':'▼'} ${Math.abs(d).toFixed(0)}%</td>`;}
    h+=`<tr class="zband ${t[w].fcz}"><td>${w}</td><td class="muted">${t[w].fcz}</td>`+
      months.map(m=>`<td>${t[w][m].toFixed(2)}</td>`).join('')+mom+`<td><b>${t[w].tot.toFixed(2)}</b></td></tr>`;});
  document.getElementById('sitetable').innerHTML=h+'</tbody>';
}
function findings(rows){
  const inscope=new Set(rows.map(r=>r.wh));
  const msel=[...state.month];
  const F=(msel.length===1 && DATA.site_findings_m && DATA.site_findings_m[msel[0]])
            ? DATA.site_findings_m[msel[0]] : DATA.site_findings;
  const el=document.getElementById('finds');const sel=state.bu;
  const items=Object.keys(F).filter(w=>inscope.has(w)).map(w=>{const f=F[w];
    const bus=sel.size?f.bus.filter(b=>sel.has(b.bu)):f.bus.slice(0,2);
    const head=sel.size?bus.reduce((a,b)=>a+b.cr,0):f.site_cr;
    return {w,f,bus,head};}).filter(x=>x.bus.length).sort((a,b)=>b.head-a.head);
  el.innerHTML=items.map(({w,f,bus,head})=>`<div class="find">
    <div class="fh"><span class="w">${w}<span class="muted" style="font-size:11px;font-weight:500"> · ${f.fcz}</span></span><span class="c">${cr(head)}</span></div>
    ${bus.map(b=>`<div class="line"><span class="bu">${b.bu}</span> <span class="sh">${cr(b.cr)} · ${b.share}% of site</span>
      <div class="k">worst vertical: <b>${b.top_vertical}</b> · worst brand: <b>${b.top_vxb}</b></div>
      ${b.top5&&b.top5.length?`<div class="k top5">top 5 verticals: ${b.top5.map(t=>`${t.v} <span class="muted">${t.cr.toFixed(2)}</span>`).join(' · ')}</div>`:''}</div>`).join('')}
  </div>`).join('');
}

function zbu(rows){
  const zones=['North','South','East','West'].filter(z=>rows.some(r=>r.fcz===z));
  const bus=M.bus.filter(b=>rows.some(r=>r.bu===b));
  const cell={},colT={},rowT={};let gT=0;
  rows.forEach(r=>{const k=r.fcz+'|'+r.bu;cell[k]=(cell[k]||0)+r.cr;});
  zones.forEach(z=>{rowT[z]=0;bus.forEach(b=>{const v=cell[z+'|'+b]||0;rowT[z]+=v;colT[b]=(colT[b]||0)+v;gT+=v;});});
  let h='<thead><tr><th>Zone ↓ / BU →</th>'+bus.map(b=>`<th>${b}</th>`).join('')+'<th>Total</th></tr></thead><tbody>';
  zones.forEach(z=>{h+=`<tr class="zband ${z}"><td>${z}</td>`+bus.map(b=>{const v=cell[z+'|'+b]||0;
    return `<td>${v?v.toFixed(2):'·'}</td>`;}).join('')+`<td><b>${rowT[z].toFixed(2)}</b></td></tr>`;});
  h+=`<tr style="background:#f2f4f8"><td><b>Total</b></td>`+bus.map(b=>`<td><b>${(colT[b]||0).toFixed(2)}</b></td>`).join('')+`<td><b>${gT.toFixed(2)}</b></td></tr>`;
  document.getElementById('zbu').innerHTML=h+'</tbody>';
}
function initDH(){const sel=document.getElementById('dh-bu');
  Object.keys(DATA.dh_by_bu).filter(b=>b!=='Unknown').forEach(b=>sel.add(new Option(b,b)));
  sel.onchange=drawDH;drawDH();}
function _dhMerge(acc, r){const k=r.dh+'|'+r.z; if(!acc[k])acc[k]={dh:r.dh,z:r.z,cr:0,u:0,fb:{}};
  acc[k].cr+=r.cr; acc[k].u+=r.u; acc[k].fb[r.top_fb]=(acc[k].fb[r.top_fb]||0)+r.cr;}
function _dhTop(acc){return Object.values(acc).map(o=>({dh:o.dh,z:o.z,cr:o.cr,u:o.u,
  top_fb:Object.entries(o.fb).sort((a,b)=>b[1]-a[1])[0][0]})).sort((a,b)=>b.cr-a.cr).slice(0,30);}
function dhRows(bu){
  const msel=[...state.month], zsel=[...state.zone];
  if(!zsel.length){
    if(msel.length===1) return ((DATA.dh_by_bu_m[msel[0]]||{})[bu])||[];
    if(msel.length===0) return DATA.dh_by_bu[bu]||[];
    const acc={}; msel.forEach(m=>(((DATA.dh_by_bu_m[m]||{})[bu])||[]).forEach(r=>_dhMerge(acc,r))); return _dhTop(acc);
  }
  const acc={}; const months=msel.length?msel:M.months;
  months.forEach(m=>zsel.forEach(z=>((((DATA.dh_by_bu_mz[m]||{})[z]||{})[bu])||[]).forEach(r=>_dhMerge(acc,r))));
  return _dhTop(acc);
}
function drawDH(){
  const bu=document.getElementById('dh-bu').value;
  const rows=dhRows(bu);
  // totals from filtered base — exact, respects Month / Zone / Warehouse / BU
  const bt=DATA.base.filter(r=>r.bu===bu &&
    (state.month.size===0||state.month.has(r.m)) &&
    (state.zone.size===0||state.zone.has(r.fcz)) &&
    (state.wh.size===0||state.wh.has(r.wh)));
  const buTot=bt.reduce((a,b)=>a+b.cr,0);
  const zTot={};bt.forEach(r=>{zTot[r.z]=(zTot[r.z]||0)+r.cr;});
  const bits=[]; if(state.zone.size)bits.push([...state.zone].join('/'));
  if([...state.month].length)bits.push([...state.month].sort().join(', '));
  const mlbl=bits.length?' · '+bits.join(' · '):'';
  let sh=`<div class="stat total"><div class="l">${bu} · overall${mlbl}</div><div class="v">${cr(buTot)}</div></div>`;
  sh+=Object.entries(zTot).sort((a,b)=>b[1]-a[1]).map(([z,v])=>
    `<div class="stat"><div class="l">${z}</div><div class="v">${cr(v)}</div></div>`).join('');
  document.getElementById('dh-stats').innerHTML=sh;
  const shown=rows.reduce((a,b)=>a+b.cr,0);
  let h='<thead><tr><th>DH (last received hub)</th><th>Zone</th><th>BU</th><th>₹ Cr</th><th>Units</th><th>% of BU</th><th>Top bucket</th></tr></thead><tbody>';
  h+=rows.map(r=>`<tr><td class="${r.dh==='Not Tagged'?'tag':''}">${r.dh}</td><td class="muted">${r.z}</td>
    <td class="muted">${bu}</td><td>${r.cr.toFixed(2)}</td><td>${ind(r.u)}</td>
    <td class="muted">${buTot?(r.cr/buTot*100).toFixed(1):0}%</td><td><span class="pill">${r.top_fb}</span></td></tr>`).join('');
  document.getElementById('dhtable').innerHTML=h+'</tbody>';
  document.getElementById('dh-note2').innerHTML=`Ranked by loss · grouped by customer zone · untagged as “Not Tagged”${mlbl} · top ${rows.length} DHs = <b>${buTot?(shown/buTot*100).toFixed(0):0}%</b> of ${bu} loss`;
}

function render(){
  buildChips();
  const rows=fbase();
  const zsel=state.zone.size?[...state.zone].join(', '):'all zones';
  const sTot=sum(rows,'s');
  document.getElementById('scope').innerHTML = sTot===0
    ? `<b style="color:var(--up)">In scope: 0 shipments</b> — no returns match this filter combination. If you selected a warehouse, it may have no returns in the selected month; try clearing or changing the <b>Month</b> filter (or click <b>Reset filters</b>).`
    : `In scope: <b>${ind(sTot)}</b> shipments · <b>${cr(sum(rows,'cr'))}</b> · ${zsel}`;
  kpis(rows);
  trendSection('fcz',null,null,{get c(){return zChart},set c(v){zChart=v}},k=>ZC[k]||ZC.Unknown,'zone-trend');
  trendSection('bu',null,null,{get c(){return bChart},set c(v){bChart=v}},null,'bu-trend',true);
  zoneSummary();
  matrix(rows);reasonCut();rtoCut();sitetable(rows);findings(rows);zbu(rows);vtable();
  if(state.bu.size===1){const b=[...state.bu][0];const s=document.getElementById('dh-bu');
    if([...s.options].some(o=>o.value===b))s.value=b;}
  drawDH();
}
document.getElementById('subline').innerHTML=
  `${M.shipments.toLocaleString('en-IN')} shipments · ${cr(M.total_cr)} total loss · ${M.sites} sites · ${M.months.length} month${M.months.length>1?'s':''} (${M.months.join(', ')})`;
buildFilters();initVFilters();initDH();render();
</script>
 """


def main():
    st.set_page_config(page_title="RI Loss Analysis", layout="wide")
    st.markdown(
        "<style>.block-container{padding:0rem 0.5rem;max-width:100%}"
        "header[data-testid='stHeader']{height:0; display:none;}"
        "[data-testid='stAppViewBlockContainer']{overflow:hidden;}"
        "iframe{border:none !important; height:95vh !important;}</style>", 
        unsafe_allow_html=True)
        
    height = st.sidebar.slider("Viewport height (px)", 600, 2000, 920, 40)

    if st.sidebar.button("🔄 Refresh data from Drive"):
        fetch_csv_from_drive.clear()

    try:
        buf, file_name = fetch_csv_from_drive(RI_LOSS_FOLDER_ID, filename_contains="RI_Final")
        st.sidebar.caption(f"Loaded: {file_name}")
    except Exception as e:
        st.error(f"Could not fetch RI Loss CSV from Google Drive: {e}")
        st.stop()

    try:
        df = load_data(buf)
    except Exception as e:
        st.error(f"Could not parse the RI Loss CSV: {e}")
        st.stop()
        
    payload = build_payload(df)
    data_str = json.dumps(payload, separators=(",", ":"))
    html = TEMPLATE_HTML.replace("/*__RI_DATA__*/", data_str)
    components.html(html, height=height, scrolling=False) # Scrolling false maps outer panel to window bounds


if __name__ == "__main__":
    main()