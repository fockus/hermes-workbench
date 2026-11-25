"""Import every data-toolkit package and print its version; with a PDF path, also convert it with docling.

Run with Hermes's runtime python: `python check.py [file.pdf]`. Exits 1 if any import fails.
"""
import sys
from importlib import import_module
from importlib.metadata import version

PACKAGES = {  # import name -> distribution name
    "docling": "docling", "pandas": "pandas", "numpy": "numpy", "polars": "polars",
    "pyarrow": "pyarrow", "matplotlib": "matplotlib", "sklearn": "scikit-learn",
    "scipy": "scipy", "openpyxl": "openpyxl",
}

failed = []
for module, dist in PACKAGES.items():
    try:
        import_module(module)
        print(f"{dist:<14} {version(dist)}")
    except Exception as exc:
        failed.append(dist)
        print(f"{dist:<14} FAILED: {exc!r}")
print(f"python         {sys.version.split()[0]}  ({sys.executable})")

if len(sys.argv) > 1 and not failed:
    from docling.document_converter import DocumentConverter
    print(DocumentConverter().convert(sys.argv[1]).document.export_to_markdown())

sys.exit(1 if failed else 0)
