"""data-toolkit: declares docling and data/ML packages (plugin.yaml) and the skill that explains them."""
from pathlib import Path


def register(ctx):
    ctx.register_skill(
        "analysis", Path(__file__).with_name("SKILL.md"),
        description="Parse PDF/DOCX/XLSX/images to markdown or tables with docling; analyse data with "
                    "pandas/polars/numpy/scipy/scikit-learn; plot with matplotlib.",
    )
