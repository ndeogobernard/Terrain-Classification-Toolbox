"""
Prepare Slope — SSURGO Terrain Slope Preprocessing
====================================================================
Downloads SSURGO soil survey data for a user-supplied list of soil
survey areas (SSURGO "area symbols," e.g. OH001, TX113, CA037 — these
follow USDA's national naming convention of state abbreviation +
3-digit county code, so this works for any state or territory in the
SSURGO system without modification).

For each area symbol, slope data is retrieved directly from the USDA
Soil Data Access (SDA) API (fast path). If the API is unavailable for
that area, the tool falls back to downloading and parsing the area's
full SSURGO zip package (muaggatt.txt) — same result, slower path.
Every polygon is classified into Level / Moderate / Steep using
user-configurable percent-slope breakpoints (default 12% / 25%,
matching the breakpoints used by the companion Calculate Dev Indices
tool), merged across all requested areas, and written to the output
geodatabase as Terrain_Slope.

This is a general-purpose data preparation tool — it doesn't assume
any particular state, agency, or downstream pipeline. Pair it with
Prepare Water Service and Prepare Floodplain to build the three
terrain constraint inputs the Development Suitability Indices toolbox
expects.

Data source: USDA Soil Data Access (SDA) / Web Soil Survey (WSS)
  SDA API : https://sdmdataaccess.sc.egov.usda.gov/Tabular/post.rest
  WSS zips: https://websoilsurvey.sc.egov.usda.gov/DSD/Download/Cache/SSA/
"""

import arcpy
import os
import csv
import json
import zipfile
import datetime
import re

# ── Constants ─────────────────────────────────────────────────────────────────
SLOPE_LEVEL          = 12.0
SLOPE_MODERATE       = 25.0
MUAGGATT_MUKEY_COL   = 39
MUAGGATT_SLOPE_COL   = 4
SDA_URL              = "https://sdmdataaccess.sc.egov.usda.gov/Tabular/post.rest"
WSS_BASE_URL         = "https://websoilsurvey.sc.egov.usda.gov/DSD/Download/Cache/SSA/"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_area_symbols(raw_text):
    """Parse a comma/newline/space-separated string of SSURGO area
    symbols (e.g. 'OH001, OH003, OH005') into a clean list."""
    if not raw_text:
        return []
    parts = re.split(r"[,\s]+", raw_text.strip())
    return [p.upper() for p in parts if re.match(r"^[A-Z]{2}\d{3}$", p.upper())]


def _count(fc):
    return int(arcpy.management.GetCount(fc).getOutput(0))


def _classify_slope(val):
    if val is None:
        return "NoData"
    if val <= SLOPE_LEVEL:
        return "Level"
    if val <= SLOPE_MODERATE:
        return "Moderate"
    return "Steep"


def _find_spatial_shp(extract_dir):
    for dirpath, _, files in os.walk(extract_dir):
        for fname in files:
            if fname.lower().startswith("soilmu_a_") and fname.lower().endswith(".shp"):
                return os.path.join(dirpath, fname)
    return None


def _get_area_date(sym):
    """Query SDA API for the current publication date of a soil survey area."""
    from xml.etree import ElementTree as ET
    import urllib.request
    try:
        payload = json.dumps({
            "query": f"SELECT areasymbol, saverest FROM sacatalog WHERE areasymbol = '{sym}'"
        }).encode("utf-8")
        req = urllib.request.Request(
            SDA_URL, data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            root = ET.fromstring(resp.read())
        row = root.find('.//Table')
        if row is None:
            return None
        saverest = row.findtext('saverest')
        if not saverest:
            return None
        for fmt in ["%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y"]:
            try:
                return datetime.datetime.strptime(saverest.strip(), fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
        return None
    except Exception:
        return None


def _download_zip(sym, date_str, dest_path):
    """Download the SSURGO zip for one area using requests. Verifies PK
    header and minimum size before accepting the download.

    The state abbreviation embedded in the filename is derived directly
    from the area symbol's own 2-letter prefix (e.g. 'OH001' -> 'OH'),
    matching USDA's national naming convention — this works for any
    state without configuration."""
    import requests
    state_abbrev = sym[:2]
    filename = f"wss_SSA_{sym}_soildb_{state_abbrev}_2003_[{date_str}].zip"
    url = WSS_BASE_URL + filename
    arcpy.AddMessage(f"  Downloading: {filename}")
    try:
        r = requests.get(url, stream=True, timeout=120)
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
        if os.path.getsize(dest_path) < 1024 * 1024:
            arcpy.AddWarning(
                f"  File too small ({os.path.getsize(dest_path):,} bytes) "
                "— server may have returned an error page."
            )
            os.remove(dest_path)
            return False
        with open(dest_path, "rb") as f:
            if f.read(2) != b"PK":
                arcpy.AddWarning("  Invalid zip header — re-download required.")
                os.remove(dest_path)
                return False
        return True
    except Exception as e:
        arcpy.AddWarning(f"  Download failed: {e}")
        if os.path.exists(dest_path):
            os.remove(dest_path)
        return False


def _slope_from_api(sym):
    """Query SDA API for slope data. Returns {mukey: slope} or None."""
    from xml.etree import ElementTree as ET
    import urllib.request
    try:
        sql = (
            f"SELECT m.musym, m.mukey, ma.slopegradwta "
            f"FROM mapunit m "
            f"INNER JOIN muaggatt ma ON m.mukey = ma.mukey "
            f"INNER JOIN legend l ON m.lkey = l.lkey "
            f"WHERE l.areasymbol = '{sym}'"
        )
        payload = json.dumps({"query": sql}).encode("utf-8")
        req = urllib.request.Request(
            SDA_URL, data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
        if len(raw) < 50:
            return None
        root = ET.fromstring(raw)
        result = {}
        for row in root.findall('.//Table'):
            mukey = row.findtext('mukey')
            slope = row.findtext('slopegradwta')
            if mukey:
                try:
                    result[mukey.strip()] = float(slope) if slope and slope.strip() else None
                except ValueError:
                    result[mukey.strip()] = None
        return result if result else None
    except Exception:
        return None


def _slope_from_zip(extract_dir):
    """Read slope from muaggatt.txt. Fallback when API is unavailable."""
    tabular = next(
        (os.path.join(d, "tabular")
         for d, subs, _ in os.walk(extract_dir) if "tabular" in subs),
        None
    )
    if not tabular:
        return None
    data_file = os.path.join(tabular, "muaggatt.txt")
    if not os.path.exists(data_file):
        return None
    result = {}
    with open(data_file, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            parts = line.rstrip("\r\n").split("|")
            if len(parts) <= max(MUAGGATT_MUKEY_COL, MUAGGATT_SLOPE_COL):
                continue
            mukey = parts[MUAGGATT_MUKEY_COL].strip().replace('"', '')
            raw = parts[MUAGGATT_SLOPE_COL].strip().replace('"', '')
            if mukey:
                try:
                    result[mukey] = float(raw) if raw else None
                except ValueError:
                    result[mukey] = None
    return result if result else None


def _write_log(path, lines):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
    except PermissionError:
        arcpy.AddWarning("Could not write log — file may be open.")


# ── Toolbox ───────────────────────────────────────────────────────────────────

class Toolbox(object):
    def __init__(self):
        self.label = "Prepare Slope"
        self.alias = "PrepareSlope"
        self.tools = [SSURGOSlopePrep]


class SSURGOSlopePrep(object):

    def __init__(self):
        self.label = "Prepare Slope"
        self.description = (
            "Downloads SSURGO soil data for a user-supplied list of soil "
            "survey areas and calculates terrain slope classes. Retrieves "
            "slope values directly from the USDA SDA API (fast), falling "
            "back to the area's muaggatt.txt if the API is unavailable. "
            "Produces a QAQC report and a map-unit detail CSV."
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
            displayName="SSURGO Area Symbols", name="area_symbols",
            datatype="GPString", parameterType="Required", direction="Input")
        p2.description = (
            "Comma- or space-separated list of SSURGO soil survey area "
            "symbols to process, e.g. 'OH001, OH003, OH005'. Area symbols "
            "follow USDA's national convention of 2-letter state "
            "abbreviation + 3-digit county code, so any state's areas can "
            "be listed here."
        )

        p3 = arcpy.Parameter(
            displayName="Pre-Downloaded SSURGO Zip Folder (optional)",
            name="local_zips", datatype="DEFolder",
            parameterType="Optional", direction="Input")
        p3.description = (
            "Point to a folder of previously downloaded SSURGO zips to "
            "skip re-downloading. Leave blank to download from USDA. "
            "Downloads are cached in Source_Data/SSURGO next to the GDB."
        )

        p4 = arcpy.Parameter(
            displayName="Overwrite Existing Area Files",
            name="overwrite", datatype="GPBoolean",
            parameterType="Optional", direction="Input")
        p4.value = False
        p4.description = (
            "False (default): skip areas whose per-area shapefile already "
            "exists — acts as a checkpoint on re-run after failure. "
            "True: reprocess all areas."
        )

        p5 = arcpy.Parameter(
            displayName="Level / Moderate Slope Threshold (%)",
            name="slope_level_threshold", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        p5.value = 12.0

        p6 = arcpy.Parameter(
            displayName="Moderate / Steep Slope Threshold (%)",
            name="slope_moderate_threshold", datatype="GPDouble",
            parameterType="Optional", direction="Input")
        p6.value = 25.0

        return [p0, p1, p2, p3, p4, p5, p6]

    def execute(self, parameters, messages):
        gdb_path = parameters[0].valueAsText
        log_folder = parameters[1].valueAsText
        area_symbols = _parse_area_symbols(parameters[2].valueAsText)
        local_zips = parameters[3].valueAsText
        overwrite = parameters[4].value
        slope_level = float(parameters[5].value) if parameters[5].value else 12.0
        slope_moderate = float(parameters[6].value) if parameters[6].value else 25.0

        global SLOPE_LEVEL, SLOPE_MODERATE
        SLOPE_LEVEL = slope_level
        SLOPE_MODERATE = slope_moderate

        if not area_symbols:
            arcpy.AddError(
                "No valid SSURGO area symbols parsed from input. Expected "
                "format: 'OH001, OH003, OH005'."
            )
            return

        run_id = datetime.datetime.now().strftime("PS_%Y%m%d_%H%M%S")
        work_folder = os.path.join(os.path.dirname(gdb_path), "Source_Data", "SSURGO")
        os.makedirs(work_folder, exist_ok=True)
        log_dir = os.path.join(log_folder, run_id)
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, f"PrepareSlope_{run_id}.txt")
        log_base = os.path.splitext(log_file)[0]
        detail_csv = f"{log_base}_mapunits.csv"

        out_ds = gdb_path
        out_shp = os.path.join(work_folder, "Terrain_Slope_merged.shp")
        arcpy.env.overwriteOutput = True

        run_start = datetime.datetime.now()
        operator = os.environ.get("USERNAME", "unknown")

        arcpy.AddMessage(f"Processing {len(area_symbols)} SSURGO area(s)...")

        area_rows = []
        area_shps = []
        mapunit_rows = []

        for sym in area_symbols:
            row_start = datetime.datetime.now()
            arcpy.AddMessage(f"\n[{sym}]")
            area_shp = os.path.join(work_folder, f"{sym}_slope.shp")

            if not overwrite and os.path.exists(area_shp):
                arcpy.AddMessage(f"  Already processed, skipping (overwrite=False).")
                area_shps.append(area_shp)
                continue

            # Get slope from SDA API first (fast path)
            slope_map = _slope_from_api(sym)
            slope_source = "SDA API" if slope_map else None

            extract_dir = None
            if slope_map is None:
                arcpy.AddMessage("  SDA API unavailable, falling back to zip download...")
                date_str = _get_area_date(sym)
                if date_str is None:
                    arcpy.AddWarning(f"  Could not determine current date for {sym}, skipping.")
                    area_rows.append(_skipped_row(sym, run_id, row_start, "SDA API", "Could not determine publish date"))
                    continue

                if local_zips:
                    candidates = [f for f in os.listdir(local_zips) if sym in f and f.endswith(".zip")]
                    zip_path = os.path.join(local_zips, candidates[0]) if candidates else None
                else:
                    zip_path = os.path.join(work_folder, f"wss_SSA_{sym}.zip")
                    if not _download_zip(sym, date_str, zip_path):
                        arcpy.AddWarning(f"  Download failed for {sym}, skipping.")
                        area_rows.append(_failed_row(sym, run_id, row_start, "Download", "SSURGO zip download failed"))
                        continue

                if not zip_path or not os.path.exists(zip_path):
                    arcpy.AddWarning(f"  No zip available for {sym}, skipping.")
                    area_rows.append(_skipped_row(sym, run_id, row_start, "Local/Download", "No zip file available"))
                    continue

                extract_dir = os.path.join(work_folder, f"{sym}_extract")
                os.makedirs(extract_dir, exist_ok=True)
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(extract_dir)

                slope_map = _slope_from_zip(extract_dir)
                slope_source = "muaggatt.txt"

            if slope_map is None:
                arcpy.AddWarning(f"  Could not obtain slope data for {sym}, skipping.")
                area_rows.append(_failed_row(sym, run_id, row_start, "N/A", "No slope data from API or zip"))
                continue

            # Find the soil map unit polygon shapefile
            if extract_dir is None:
                # Need to download for the geometry even if slope came from API
                date_str = _get_area_date(sym)
                zip_path = os.path.join(work_folder, f"wss_SSA_{sym}.zip")
                if local_zips:
                    candidates = [f for f in os.listdir(local_zips) if sym in f and f.endswith(".zip")]
                    zip_path = os.path.join(local_zips, candidates[0]) if candidates else zip_path
                if not os.path.exists(zip_path):
                    if date_str is None or not _download_zip(sym, date_str, zip_path):
                        arcpy.AddWarning(f"  Could not download geometry for {sym}, skipping.")
                        area_rows.append(_failed_row(sym, run_id, row_start, "Download", "Geometry download failed"))
                        continue
                extract_dir = os.path.join(work_folder, f"{sym}_extract")
                os.makedirs(extract_dir, exist_ok=True)
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(extract_dir)

            shp_path = _find_spatial_shp(extract_dir)
            if shp_path is None:
                arcpy.AddWarning(f"  No soil polygon shapefile found for {sym}, skipping.")
                area_rows.append(_failed_row(sym, run_id, row_start, "N/A", "soilmu_a_*.shp not found"))
                continue

            arcpy.management.CopyFeatures(shp_path, area_shp)
            arcpy.management.AddField(area_shp, "SLOPE_PCT", "DOUBLE")
            arcpy.management.AddField(area_shp, "TERRAIN_CL", "TEXT", field_length=12)

            level_n = mod_n = steep_n = nodata_n = 0
            slope_vals = []
            with arcpy.da.UpdateCursor(area_shp, ["MUKEY", "SLOPE_PCT", "TERRAIN_CL"]) as cur:
                for row in cur:
                    mukey = row[0]
                    slope_val = slope_map.get(mukey)
                    cls = _classify_slope(slope_val)
                    row[1] = slope_val
                    row[2] = cls
                    cur.updateRow(row)
                    if cls == "Level":
                        level_n += 1
                    elif cls == "Moderate":
                        mod_n += 1
                    elif cls == "Steep":
                        steep_n += 1
                    else:
                        nodata_n += 1
                    if slope_val is not None:
                        slope_vals.append(slope_val)
                    mapunit_rows.append({
                        "AREASYMBOL": sym, "RUN_ID": run_id, "MUKEY": mukey,
                        "SLOPE_VALUE": slope_val, "TERRAIN_CLASS": cls,
                        "SLOPE_SOURCE": slope_source,
                    })

            total_n = level_n + mod_n + steep_n + nodata_n
            arcpy.AddMessage(
                f"  {total_n:,} polygons — Level: {level_n:,}, "
                f"Moderate: {mod_n:,}, Steep: {steep_n:,}, NoData: {nodata_n:,} "
                f"(source: {slope_source})"
            )

            area_rows.append({
                "AREASYMBOL": sym, "RUN_ID": run_id,
                "TOTAL_FEATURES": total_n, "LEVEL_COUNT": level_n,
                "MODERATE_COUNT": mod_n, "STEEP_COUNT": steep_n,
                "NODATA_COUNT": nodata_n,
                "MIN_SLOPE": min(slope_vals) if slope_vals else "",
                "MAX_SLOPE": max(slope_vals) if slope_vals else "",
                "AVG_SLOPE": round(sum(slope_vals) / len(slope_vals), 2) if slope_vals else "",
                "SLOPE_SOURCE": slope_source, "QA_FLAG": "PASS", "NOTES": "",
            })
            area_shps.append(area_shp)

        if not area_shps:
            arcpy.AddError("No areas processed successfully — nothing to merge.")
            return

        arcpy.AddMessage(f"\nMerging {len(area_shps)} area shapefile(s)...")
        arcpy.management.Merge(area_shps, out_shp)
        total_polys = _count(out_shp)
        arcpy.AddMessage(f"Output: {out_shp} ({total_polys:,} polygons)")

        out_fc_path = os.path.join(out_ds, "Terrain_Slope")
        if arcpy.Exists(out_fc_path):
            arcpy.management.Delete(out_fc_path)
        arcpy.conversion.FeatureClassToFeatureClass(out_shp, out_ds, "Terrain_Slope")
        arcpy.AddMessage(f"  Terrain_Slope written to GDB: {out_fc_path}")

        run_end = datetime.datetime.now()
        run_elapsed = round((run_end - run_start).total_seconds(), 1)

        good = [r for r in area_rows if r.get("QA_FLAG") not in ("SKIPPED", "FAILED")]
        total_all = sum(r.get("TOTAL_FEATURES", 0) for r in good)
        lvl_all = sum(r.get("LEVEL_COUNT", 0) for r in good)
        mod_all = sum(r.get("MODERATE_COUNT", 0) for r in good)
        steep_all = sum(r.get("STEEP_COUNT", 0) for r in good)
        nodata_all = sum(r.get("NODATA_COUNT", 0) for r in good)

        def spct(n):
            return round(n / total_all * 100, 1) if total_all else 0

        lines = [
            "=" * 70, "SSURGO SLOPE PREPROCESSING — QAQC REPORT", "=" * 70, "",
            "RUN INFORMATION", "-" * 70,
            f"Run ID          : {run_id}",
            f"Operator        : {operator}",
            f"Areas requested : {', '.join(area_symbols)}",
            f"Start Time      : {run_start.strftime('%Y-%m-%d %H:%M:%S')}",
            f"End Time        : {run_end.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Elapsed         : {run_elapsed}s ({round(run_elapsed/60,1)} min)",
            f"Output Shapefile: {out_shp}",
            f"Total Polygons  : {total_polys:,}", "",
            "TERRAIN CLASS DISTRIBUTION", "-" * 70,
            f"Total polygons : {total_all:,}",
            f"Level          : {lvl_all:,}  ({spct(lvl_all):.1f}%)",
            f"Moderate       : {mod_all:,}  ({spct(mod_all):.1f}%)",
            f"Steep          : {steep_all:,}  ({spct(steep_all):.1f}%)",
            f"NoData         : {nodata_all:,}  ({spct(nodata_all):.1f}%)", "",
            "=" * 70, f"END OF REPORT — Run ID: {run_id}", "=" * 70,
        ]
        _write_log(log_file, lines)
        arcpy.AddMessage(f"QAQC log   : {log_file}")

        try:
            mu_fields = ["AREASYMBOL", "RUN_ID", "MUKEY", "SLOPE_VALUE", "TERRAIN_CLASS", "SLOPE_SOURCE"]
            with open(detail_csv, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=mu_fields)
                w.writeheader()
                w.writerows(mapunit_rows)
            arcpy.AddMessage(f"Map unit CSV: {detail_csv} ({len(mapunit_rows):,} rows)")
        except PermissionError:
            arcpy.AddWarning("Could not write map unit CSV — file may be open.")

        arcpy.AddMessage("\nSUCCESS — Prepare Slope complete.")


def _skipped_row(sym, run_id, start, dl_src, note):
    return {
        "AREASYMBOL": sym, "RUN_ID": run_id, "TOTAL_FEATURES": 0,
        "LEVEL_COUNT": 0, "MODERATE_COUNT": 0, "STEEP_COUNT": 0, "NODATA_COUNT": 0,
        "SLOPE_SOURCE": dl_src, "QA_FLAG": "SKIPPED", "NOTES": note,
    }


def _failed_row(sym, run_id, start, dl_src, note):
    return {
        "AREASYMBOL": sym, "RUN_ID": run_id, "TOTAL_FEATURES": 0,
        "LEVEL_COUNT": 0, "MODERATE_COUNT": 0, "STEEP_COUNT": 0, "NODATA_COUNT": 0,
        "SLOPE_SOURCE": dl_src, "QA_FLAG": "FAILED", "NOTES": note,
    }
