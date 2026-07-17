# Terrain Classification Toolbox Suite

Four ArcGIS Pro Python toolboxes that process public federal terrain and
infrastructure data into a per-zone development suitability
classification. Works with any zone layer (traffic analysis zones,
census tracts, parcels, planning districts), any state, any scale.

```
WF   WL   WM   WS       Water-served:      Floodplain / Level / Moderate / Steep
NWF  NWL  NWM  NWS       Not water-served:  Floodplain / Level / Moderate / Steep
```

## Contents

| File | Tool(s) | Purpose |
|---|---|---|
| `PrepareSlope.pyt` | Prepare Slope | Downloads SSURGO soil data, classifies percent slope |
| `PrepareWaterService.pyt` | Prepare Water Service | Downloads EPA water service area data, clips and dissolves |
| `PrepareFloodplain.pyt` | Prepare Floodplain | Downloads FEMA NFHL data, classifies floodplain |
| `DevelopmentIndicesToolbox.pyt` | Validate Inputs, Calculate Dev Indices | Validates prepared inputs, produces the classification output table |

See `Toolbox_Suite_Documentation.md` for full architecture, design notes,
and validation methodology.

## Requirements

ArcGIS Pro with the `arcgispro-py3` Python environment. No additional
packages required. An Advanced license is optional — `Calculate Dev
Indices` detects and uses parallelized geoprocessing tools automatically
when available, and falls back to standard tools otherwise.

## Installation

1. Download or clone this repository.
2. In ArcGIS Pro's **Catalog** pane, right-click **Toolboxes** → **Add
   Toolbox**, and add each of the four `.pyt` files.
3. Tools appear under their respective toolbox nodes in the Catalog pane.

## Usage

Recommended order:

1. Run **Prepare Slope**, **Prepare Water Service**, and **Prepare
   Floodplain** (any order, or in parallel) against your project
   geodatabase. Each writes a fixed-name output feature class
   (`Terrain_Slope`, `Terrain_Water`, `Terrain_Flood`).
2. Run **Validate Inputs** against the resulting layers plus your zone
   layer. Review any warnings before proceeding.
3. Run **Calculate Dev Indices**, supplying the zone layer, the three
   prepared terrain layers, and the slope classification breakpoints.

Output is a table with one row per zone: eight category acreages, a
`TotalAcres` sum, an `UnclassifiedAcres` column, an independently
computed `ZoneGeodesicAcres` column, and an `AcreageDiff` validation
column that should be ~0 for every zone.

## Data sources

- USDA Soil Data Access / SSURGO
- EPA Community Water System Service Area Boundaries (via GitHub:
  `USEPA/ORD_SAB_Model`)
- FEMA National Flood Hazard Layer

All three are public federal datasets, downloaded automatically by their
respective tools.

## Validation

Run against a full statewide dataset (several thousand zones, ~1.25
million input slope polygons): maximum discrepancy between independently
computed zone area and the sum of the eight category acreages was under
0.13 acres on zones up to tens of thousands of acres — a relative error
on the order of 0.0004%. Details in `Toolbox_Suite_Documentation.md`.

## License

MIT — see `LICENSE`.
