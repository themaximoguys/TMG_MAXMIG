"""MxLoader engine — a Python port of MxLoader's VBA load/query architecture.

MxLoader (Bruno Portaluri's Excel add-in, bportaluri.com/mxloader) is the de
facto Maximo bulk-load tool. This package reproduces its *behaviour* — not its
code or data, which are licensed for non-commercial use only — module by
module, so a sheet that works in MxLoader works here and vice versa:

| MxLoader VBA                      | here                     |
|-----------------------------------|--------------------------|
| MxSheet.cls (row 1/2 layout)      | `maxmig.mxl.sheet`       |
| ConvSheetToNode / ConvNodeToSheet | `maxmig.mxl.convert`     |
| MxNode.cls / MxNodeAttr.cls       | `maxmig.mxl.node`        |
| MxUtils.AdjustVal + ToJson typing | `maxmig.mxl.values`      |
| MxAttrCfg sheet (MAXATTRIBUTE)    | `maxmig.mxl.datadict`    |
| MxMifService.cls (REST/BULK)      | `maxmig.mxl.service`     |
| Main.MxRunSync / MxRunQuery       | `maxmig.mxl.runner`      |
| Main.FillData + Excel formulas    | `maxmig.mxl.fill` / `maxmig.mxl.formula` |
| Main.MxAddSheetBatch / MxRunBatch | `maxmig.mxl.batch`       |
| user VBA macros / worksheet UDFs  | `maxmig.mxl.scripting`   |
| (no equivalent — MaxMig YAML)     | `maxmig.mxl.bridge`      |

Deliberate differences from MxLoader, each called out where it's implemented:
sibling child relations nest correctly (MxLoader only supports one linear
chain per row), writes are dry-run unless `execute=True`, results are written
to every sheet row of a multi-row object (not just its first), and only the
REST service is supported (no legacy MIF OS/SOAP XML path).
"""
