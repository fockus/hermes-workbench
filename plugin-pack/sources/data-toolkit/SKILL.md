---
name: analysis
description: "Parse documents (PDF, DOCX, PPTX, XLSX, HTML, images with OCR) into markdown or tables with docling, and analyse data with pandas, polars, numpy, scipy, scikit-learn, pyarrow, openpyxl, matplotlib. Use when the user sends a document to read or summarise, asks for tables out of a PDF, or wants numbers crunched, a model fitted or a chart drawn."
---

# Documents and data analysis

All packages below live in Hermes's own Python, so `execute_code` imports them directly. Do not
`pip install` them, and do not call `terminal()` with another python for them.

Installed: docling, pandas, numpy, polars, pyarrow, matplotlib, scikit-learn (`sklearn`), scipy,
openpyxl, plus torch/transformers that docling uses for its layout, table and OCR models.

## Documents → markdown / tables (docling)

```python
from docling.document_converter import DocumentConverter

result = DocumentConverter().convert("/path/to/file.pdf")   # also .docx .pptx .xlsx .html .png .jpg, or a URL
doc = result.document
md = doc.export_to_markdown()                                # whole document as markdown
tables = [t.export_to_dataframe(doc=doc) for t in doc.tables]  # each table as a pandas DataFrame
```

- The first conversion downloads docling's models (layout, TableFormer, OCR) into the Hugging Face
  cache; expect a slow first run, later runs are offline.
- A large PDF is slow on CPU: convert once, save the markdown next to the file, and work from it.
- Scanned PDFs and images are OCR'd automatically. For a born-digital PDF that converts slowly, skip OCR:

```python
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption

opts = PdfPipelineOptions(do_ocr=False)
converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
```

## Data

- Tables: `pandas` for everyday work and Excel (`pd.read_excel` / `to_excel` use openpyxl); `polars`
  for large files (lazy `pl.scan_csv` / `pl.scan_parquet`); `pyarrow` for Parquet/Arrow.
- Statistics: `scipy.stats`; models and metrics: `sklearn` (train/test split, cross-validation).
- Charts: matplotlib with a non-interactive backend, saved to a file you then send:

```python
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
fig, ax = plt.subplots()
ax.plot(x, y)
fig.savefig("chart.png", dpi=150, bbox_inches="tight")
```

Report the numbers you computed, not a guess, and say which file and which columns they came from.
