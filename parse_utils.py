import pandas as pd
from pdfminer.high_level import extract_text as pdf_extract_text
from docx import Document as DocxDocument

async def parse_file(file_path: str, file_name: str) -> str:
    n = file_name.lower()
    if n.endswith(".pdf"):
        return pdf_extract_text(file_path)[:20000]
    if n.endswith(".docx"):
        d = DocxDocument(file_path)
        return "\n".join([p.text for p in d.paragraphs])[:20000]
    if n.endswith(".csv"):
        df = pd.read_csv(file_path)
        return df.to_markdown()[:20000]
    if n.endswith(".xlsx") or n.endswith(".xls"):
        df = pd.read_excel(file_path)
        return df.to_markdown()[:20000]
    with open(file_path, "r", errors="ignore") as f:
        return f.read()[:20000]
