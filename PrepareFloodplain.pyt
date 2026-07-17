"""
Prepare Floodplain — FEMA NFHL Floodplain Preprocessing
====================================================================
Automatically discovers and downloads the current FEMA National Flood
Hazard Layer (NFHL) statewide GDB for a user-specified state — no
hardcoded dates, no manual downloads. Extracts S_FLD_HAZ_AR (flood
hazard area polygons), classifies as In_Flood = 1 or 0 using the
SFHA_TF field, clips to a user-supplied boundary layer, dissolves by
flood class, and writes the output floodplain feature class — ready to
feed the Calculate Dev Indices tool's Floodplain input, where it takes
precedence over slope classification wherever the two overlap.

Data source: FEMA National Flood Hazard Layer (NFHL)
  File server: https://hazards.fema.gov/nfhlv2/output/State/
  Pattern: NFHL_{state FIPS}_{YYYYMMDD}.zip
  Layer: S_FLD_HAZ_AR

Classification: SFHA_TF = T -> In_Flood = 1 (A, AE, AH, AO, VE zones)
                 SFHA_TF = F -> In_Flood = 0 (X, D, AREA NOT INCLUDED)

Auto-download uses a resilient session with:
  - Corporate-network compatibility options (see _fema_session) for
    environments with a TLS-inspecting proxy — common on managed
    enterprise networks
  - A standard browser User-Agent to avoid bot-detection on FEMA's servers
  - Date-walking to find the current effective date automatically
  - Magic-byte validation before extraction (a misconfigured network
    intermediary can return an HTML error page with a 200 status,
    masquerading as a valid file)

The downloaded GDB is cached locally after first download; subsequent
runs skip the download entirely.
"""

import arcpy
import os
import datetime
import zipfile
import re
import warnings

# ── FEMA constants ────────────────────────────────────────────────────────────
FEMA_BASE_URL = "https://hazards.fema.gov/nfhlv2/output/State"
NFHL_LAYER = "S_FLD_HAZ_AR"
SFHA_FIELD = "SFHA_TF"
FLD_ZONE_FIELD = "FLD_ZONE"
MAX_PROBE_DAYS = 120
MIN_ZIP_MB = 50  # conservative floor; actual size varies widely by state

STANDARD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _count(fc):
    return int(arcpy.management.GetCount(fc).getOutput(0))


def _build_clip_boundary(boundary_fc, where_clause):
    tmp_sel = "_preflood_sel"
    tmp_diss = r"in_memory\preflood_clip"
    try:
        if arcpy.Exists(tmp_sel):
            arcpy.management.Delete(tmp_sel)
        arcpy.management.MakeFeatureLayer(boundary_fc, tmp_sel, where_clause or None)
        n = _count(tmp_sel)
        arcpy.AddMessage(f"  Boundary features selected: {n:,}")
        if arcpy.Exists(tmp_diss):
            arcpy.management.Delete(tmp_diss)
        arcpy.management.Dissolve(tmp_sel, tmp_diss, multi_part="SINGLE_PART")
        arcpy.management.Delete(tmp_sel)
        return tmp_diss
    except Exception as e:
        arcpy.AddWarning(f"  Could not build clip boundary: {e}")
        return None


def _fema_session(verify_ssl):
    """Create a requests session for downloading from FEMA.

    trust_env=False and verify_ssl=False are corporate-network
    compatibility settings — bypassing a system proxy and disabling
    certificate verification are common workarounds needed on managed
    enterprise networks that inspect TLS traffic. They're off by
    default; enable via the tool's parameters only if downloads fail
    with proxy or SSL errors on your network."""
    import requests
    if not verify_ssl:
        warnings.filterwarnings("ignore", message="Unverified HTTPS request")
    s = requests.Session()
    s.verify = verify_ssl
    s.headers.update({"User-Agent": STANDARD_UA})
    return s


def _is_valid_zip(path):
    """Check PK magic bytes and zipfile validity."""
    try:
        with open(path, "rb") as f:
            header = f.read(4)
        if header[:2] != b"PK":
            return False, f"Bad header: {header.hex()}"
        if not zipfile.is_zipfile(path):
            return False, "Not a valid zip (is_zipfile failed)"
        return True, "OK"
    except Exception as e:
        return False, str(e)


def _find_cached_gdb(work_folder, state_fips):
    try:
        pattern = re.compile(rf"NFHL_{state_fips}_\d{{8}}\.gdb")
        matches = [
            os.path.join(work_folder, d)
            for d in os.listdir(work_folder)
            if pattern.match(d) and os.path.isdir(os.path.join(work_folder, d))
        ]
        return sorted(matches)[-1] if matches else None
    except Exception:
        return None


def _find_current_nfhl(session, state_fips):
    """Walk back up to MAX_PROBE_DAYS from today using HEAD requests to
    find the current effective NFHL date for the given state."""
    check = datetime.datetime.now()
    arcpy.AddMessage(
        f"  Probing hazards.fema.gov for current NFHL "
        f"(state FIPS {state_fips}, up to {MAX_PROBE_DAYS} days back)..."
    )
    for _ in range(MAX_PROBE_DAYS):
        date_str = check.strftime("%Y%m%d")
        product_id = f"NFHL_{state_fips}_{date_str}"
        url = f"{FEMA_BASE_URL}/{product_id}.zip"
        try:
            r = session.head(url, timeout=15)
            size_mb = int(r.headers.get("content-length", 0)) / 1024 / 1024
            if r.status_code == 200 and size_mb >= MIN_ZIP_MB:
                arcpy.AddMessage(f"  Found: {product_id} ({size_mb:.0f} MB)")
                return product_id, url
        except Exception:
            pass
        check -= datetime.timedelta(days=1)
    return None, None


def _download_nfhl(work_folder, state_fips, verify_ssl):
    """Auto-discover and download the current NFHL GDB for a state."""
    cached = _find_cached_gdb(work_folder, state_fips)
    if cached:
        arcpy.AddMessage(f"  Using cached GDB: {cached}")
        return cached, os.path.basename(cached).replace(".gdb", "")

    session = _fema_session(verify_ssl)
    product_id, url = _find_current_nfhl(session, state_fips)
    if product_id is None:
        arcpy.AddWarning(
            f"  Could not find a valid NFHL zip in the last {MAX_PROBE_DAYS} days.\n"
            "  Manual download: https://msc.fema.gov/portal/advanceSearch"
        )
        return None, None

    dest_zip = os.path.join(work_folder, f"{product_id}.zip")
    if os.path.exists(dest_zip):
        valid, reason = _is_valid_zip(dest_zip)
        if valid:
            arcpy.AddMessage(f"  Zip already downloaded: {os.path.basename(dest_zip)}")
        else:
            arcpy.AddWarning(f"  Existing zip invalid ({reason}) — re-downloading.")
            os.remove(dest_zip)

    if not os.path.exists(dest_zip):
        arcpy.AddMessage(f"  Downloading: {url}")
        try:
            r = session.get(url, stream=True, timeout=600)
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            downloaded = 0
            with open(dest_zip, "wb") as f:
                for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded / total * 100
                        arcpy.AddMessage(f"  {pct:.0f}%  ({downloaded/1024/1024:.0f} / {total/1024/1024:.0f} MB)")
            arcpy.AddMessage(f"  Download complete: {os.path.getsize(dest_zip)/1024/1024:.0f} MB")
        except Exception as e:
            arcpy.AddWarning(f"  Download failed: {e}")
            if os.path.exists(dest_zip):
                os.remove(dest_zip)
            return None, None

    valid, reason = _is_valid_zip(dest_zip)
    if not valid:
        arcpy.AddWarning(
            f"  Downloaded file is not a valid zip ({reason}).\n"
            "  A network intermediary (proxy/firewall) may have intercepted the download.\n"
            "  Manual download: https://msc.fema.gov/portal/advanceSearch"
        )
        os.remove(dest_zip)
        return None, None

    arcpy.AddMessage("  Zip validated (PK header OK, is_zipfile OK)")
    arcpy.AddMessage("  Extracting GDB...")
    try:
        with zipfile.ZipFile(dest_zip, "r") as zf:
            zf.extractall(work_folder)
        for item in os.listdir(work_folder):
            full = os.path.join(work_folder, item)
            if item.endswith(".gdb") and os.path.isdir(full):
                size_mb = sum(
                    os.path.getsize(os.path.join(r, f))
                    for r, d, fs in os.walk(full) for f in fs
                ) / 1024 / 1024
                arcpy.AddMessage(f"  Extracted: {item} ({size_mb:.0f} MB)")
                os.remove(dest_zip)
                return full, product_id
        arcpy.AddWarning("  GDB not found in extracted contents.")
        return None, None
    except Exception as e:
        arcpy.AddWarning(f"  Extraction failed: {e}")
        return None, None


def _write_log(path, lines):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except PermissionError:
        arcpy.AddWarning("Could not write log — file may be open.")


# ── Toolbox ───────────────────────────────────────────────────────────────────

class Toolbox(object):
    def __init__(self):
        self.label = "Prepare Floodplain"
        self.alias = "PrepareFloodplain"
        self.tools = [FloodplainPrep]


class FloodplainPrep(object):

    def __init__(self):
        self.label = "Prepare Floodplain"
        self.description = (
            "Automatically discovers and downloads the current FEMA NFHL "
            "statewide GDB for a user-specified state. Classifies "
            "S_FLD_HAZ_AR using the SFHA_TF field (T=In_Flood=1, "
            "F=In_Flood=0), clips to a boundary layer, dissolves by flood "
            "class, and writes the output floodplain feature class."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        p0 = arcpy.Parameter(
            displayName="Project Geodatabase", name="project_gdb",
            datatype="DEWorkspace", parameterType="Required", direction="Input")
        p0.filter.list = ["Local Database"]

        p1 = arcpy.Parameter(
            displayName="Log Folder", name="log_folder",
            datatype="DEFolder", parameterType="Required", direction="Input")

        p2 = arcpy.Parameter(
            displayName="Boundary/Zone Layer", name="boundary_fc",
            datatype="GPFeatureLayer", parameterType="Required", direction="Input")
        p2.description = "Any polygon layer defining the area to clip to (e.g. zone/TAZ layer, county boundary, project extent)."

        p3 = arcpy.Parameter(
            displayName="Boundary Selection Query (optional)", name="boundary_query",
            datatype="GPSQLExpression", parameterType="Optional", direction="Input")
        p3.parameterDependencies = [p2.name]

        p4 = arcpy.Parameter(
            displayName="State FIPS Code", name="state_fips",
            datatype="GPString", parameterType="Required", direction="Input")
        p4.description = (
            "2-digit state FIPS code used in FEMA's NFHL file naming "
            "convention, e.g. '39' for Ohio, '48' for Texas, '06' for "
            "California."
        )

        p5 = arcpy.Parameter(
            displayName="Bypass System Proxy", name="bypass_proxy",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p5.value = False
        p5.description = "Enable only if downloads fail due to a system proxy interfering with the connection."

        p6 = arcpy.Parameter(
            displayName="Verify SSL Certificates", name="verify_ssl",
            datatype="GPBoolean", parameterType="Optional", direction="Input")
        p6.value = True
        p6.description = "Uncheck only if downloads fail with SSL errors on a corporate network that inspects TLS traffic."

        return [p0, p1, p2, p3, p4, p5, p6]

    def execute(self, parameters, messages):
        gdb_path = parameters[0].valueAsText
        log_folder = parameters[1].valueAsText
        boundary_fc = parameters[2].valueAsText
        boundary_query = parameters[3].valueAsText
        state_fips = (parameters[4].valueAsText or "").strip().zfill(2)
        bypass_proxy = parameters[5].value or False
        verify_ssl = parameters[6].value if parameters[6].value is not None else True

        if not state_fips or len(state_fips) != 2 or not state_fips.isdigit():
            arcpy.AddError("State FIPS Code must be a 2-digit code, e.g. '39' for Ohio.")
            return

        if bypass_proxy:
            os.environ["NO_PROXY"] = "*"

        run_id = datetime.datetime.now().strftime("PF_%Y%m%d_%H%M%S")
        work_folder = os.path.join(os.path.dirname(gdb_path), "Source_Data", "Floodplain")
        os.makedirs(work_folder, exist_ok=True)
        log_dir = os.path.join(log_folder, run_id)
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, f"PrepareFloodplain_{run_id}.txt")
        out_shp = os.path.join(work_folder, "Terrain_Flood_merged.shp")
        out_ds = gdb_path

        arcpy.env.overwriteOutput = True
        run_start = datetime.datetime.now()
        operator = os.environ.get("USERNAME", "unknown")

        # ── Step 1: Boundary ────────────────────────────────────────────────
        arcpy.AddMessage("Step 1: Building clip boundary...")
        tmp_clip = _build_clip_boundary(boundary_fc, boundary_query)
        if tmp_clip is None:
            arcpy.AddError("Could not build clip boundary from the boundary layer.")
            return

        # ── Step 2: Download/locate NFHL GDB ────────────────────────────────
        arcpy.AddMessage("Step 2: FEMA NFHL data...")
        gdb_dir, product_id = _download_nfhl(work_folder, state_fips, verify_ssl)
        if gdb_dir is None:
            arcpy.AddError("Could not obtain NFHL GDB — cannot proceed.")
            return
        fc_path = os.path.join(gdb_dir, NFHL_LAYER)
        if not arcpy.Exists(fc_path):
            arcpy.AddError(f"{NFHL_LAYER} not found in {gdb_dir}")
            return

        n_total = _count(fc_path)
        arcpy.AddMessage(f"  Total GDB features: {n_total:,}")

        zone_counts, sfha_counts = {}, {}
        with arcpy.da.SearchCursor(fc_path, [SFHA_FIELD, FLD_ZONE_FIELD]) as cur:
            for row in cur:
                sfha = str(row[0]).strip() if row[0] else "None"
                zone = str(row[1]).strip() if row[1] else "None"
                sfha_counts[sfha] = sfha_counts.get(sfha, 0) + 1
                zone_counts[zone] = zone_counts.get(zone, 0) + 1
        arcpy.AddMessage(f"  SFHA_TF=T (flood)    : {sfha_counts.get('T',0):,}")
        arcpy.AddMessage(f"  SFHA_TF=F (non-flood): {sfha_counts.get('F',0):,}")

        # ── Step 3: Classify ────────────────────────────────────────────────
        arcpy.AddMessage("Step 3: Classifying...")
        tmp_flood = r"in_memory\preflood_all"
        arcpy.management.CopyFeatures(fc_path, tmp_flood)
        arcpy.management.AddField(tmp_flood, "In_Flood", "SHORT")
        arcpy.management.CalculateField(
            tmp_flood, "In_Flood", f"1 if !{SFHA_FIELD}! == 'T' else 0", "PYTHON3")

        # ── Step 4: Clip ────────────────────────────────────────────────────
        arcpy.AddMessage("Step 4: Clipping to boundary...")
        tmp_clipped = r"in_memory\preflood_clipped"
        arcpy.analysis.Clip(tmp_flood, tmp_clip, tmp_clipped)
        n_clipped = _count(tmp_clipped)
        arcpy.AddMessage(f"  After clip: {n_clipped:,}")
        arcpy.management.Delete(tmp_flood)
        try:
            arcpy.management.Delete(tmp_clip)
        except Exception:
            pass

        clip_flood = clip_noflood = 0
        with arcpy.da.SearchCursor(tmp_clipped, ["In_Flood"]) as cur:
            for row in cur:
                if row[0] == 1:
                    clip_flood += 1
                else:
                    clip_noflood += 1
        arcpy.AddMessage(f"  In_Flood=1 : {clip_flood:,}")
        arcpy.AddMessage(f"  In_Flood=0 : {clip_noflood:,}")

        # ── Step 5: Dissolve by In_Flood ────────────────────────────────────
        arcpy.AddMessage("Step 5: Dissolving by In_Flood class...")
        tmp_dissolved = r"in_memory\preflood_dissolved"
        arcpy.management.Dissolve(tmp_clipped, tmp_dissolved, dissolve_field="In_Flood", multi_part="SINGLE_PART")
        n_dissolved = _count(tmp_dissolved)
        arcpy.AddMessage(f"  Dissolved: {n_clipped:,} -> {n_dissolved:,}")
        arcpy.management.Delete(tmp_clipped)

        # ── Step 6: Write output ────────────────────────────────────────────
        arcpy.management.CopyFeatures(tmp_dissolved, out_shp)
        arcpy.management.Delete(tmp_dissolved)
        n_out = _count(out_shp)

        out_flood = out_noflood = 0
        with arcpy.da.SearchCursor(out_shp, ["In_Flood"]) as cur:
            for row in cur:
                if row[0] == 1:
                    out_flood += 1
                else:
                    out_noflood += 1

        run_end = datetime.datetime.now()
        run_elapsed = round((run_end - run_start).total_seconds(), 1)

        arcpy.AddMessage(f"Output: {out_shp} ({n_out:,} polygons)")
        out_fc_path = os.path.join(out_ds, "Terrain_Flood")
        if arcpy.Exists(out_fc_path):
            arcpy.management.Delete(out_fc_path)
        arcpy.conversion.FeatureClassToFeatureClass(out_shp, out_ds, "Terrain_Flood")
        arcpy.AddMessage(f"  Terrain_Flood written to GDB: {out_fc_path}")
        arcpy.AddMessage(f"  In_Flood=1 : {out_flood:,}")
        arcpy.AddMessage(f"  In_Flood=0 : {out_noflood:,}")

        pct_flood = round(clip_flood / n_clipped * 100, 1) if n_clipped else 0
        qa_flags = []
        if n_clipped == 0:
            qa_flags.append("WARN: No features within boundary.")
        if clip_flood == 0:
            qa_flags.append("WARN: No floodplain features after clip.")
        if out_flood == 0:
            qa_flags.append("WARN: No In_Flood=1 polygons in output.")
        if pct_flood > 30:
            qa_flags.append(f"WARN: High floodplain coverage ({pct_flood}%). Review boundary/scope.")

        lines = [
            "=" * 70, "FLOODPLAIN PREPROCESSING — QAQC REPORT", "=" * 70, "",
            "RUN INFORMATION", "-" * 70,
            f"Run ID          : {run_id}",
            f"Operator        : {operator}",
            f"State FIPS      : {state_fips}",
            f"Start Time      : {run_start.strftime('%Y-%m-%d %H:%M:%S')}",
            f"End Time        : {run_end.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Elapsed         : {run_elapsed}s ({round(run_elapsed/60,1)} min)",
            f"Output          : {out_shp}", "",
            "DATA SOURCE", "-" * 70,
            f"GDB             : {gdb_dir}",
            f"Product ID      : {product_id}",
            f"Layer           : {NFHL_LAYER}",
            f"Classification  : SFHA_TF=T -> In_Flood=1, F -> In_Flood=0", "",
            "FEATURE STATISTICS", "-" * 70,
            f"Total in GDB        : {n_total:,}",
            f"SFHA_TF=T (flood)   : {sfha_counts.get('T', 0):,}",
            f"SFHA_TF=F (no flood): {sfha_counts.get('F', 0):,}", "",
            f"After clip          : {n_clipped:,}",
            f"  In_Flood=1        : {clip_flood:,} ({pct_flood:.1f}% of clipped)",
            f"  In_Flood=0        : {clip_noflood:,}", "",
            f"After dissolve      : {n_dissolved:,}",
            f"Output polygons     : {n_out:,}",
            f"  In_Flood=1        : {out_flood:,}",
            f"  In_Flood=0        : {out_noflood:,}", "",
            "FLOOD ZONE BREAKDOWN (full GDB)", "-" * 70,
        ]
        for zone, cnt in sorted(zone_counts.items()):
            lines.append(f"  {zone:<25} : {cnt:>8,}")
        lines += [
            "", "PROCESSING NOTES", "-" * 70,
            "Floodplain takes precedence over slope class downstream.",
            "In_Flood=1 -> F category. In_Flood=0 -> slope-based category (L/M/S).",
            "Dissolve resolves overlapping flood zone boundaries.", "",
            "IF AUTO-DOWNLOAD FAILS IN FUTURE:",
            "  1. Go to https://msc.fema.gov/portal/advanceSearch",
            "  2. Select your state, County=Any -> Search",
            "  3. Under 'NFHL Data-State' click Download",
            "  4. Extract zip — place the .gdb folder in the Work Folder",
            "  5. Re-run — tool detects cached GDB automatically", "",
        ]
        if qa_flags:
            lines += ["QA FLAGS RAISED", "-" * 70] + [f"  {f}" for f in qa_flags] + [""]
        else:
            lines += ["QA FLAGS : None raised.", ""]
        lines += ["=" * 70, f"END OF REPORT — Run ID: {run_id}", "=" * 70]

        _write_log(log_file, lines)
        arcpy.AddMessage(f"QAQC log : {log_file}")
        arcpy.AddMessage("\nSUCCESS — Prepare Floodplain complete.")
