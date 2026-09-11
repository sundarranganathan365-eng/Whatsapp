import os
import sys
import warnings
import logging
import re
import ssl
import urllib.request
from pathlib import Path
from dotenv import load_dotenv

warnings.filterwarnings("ignore")
logging.getLogger().setLevel(logging.ERROR)
logging.getLogger("google_genai").setLevel(logging.ERROR)

# Try importing required libraries
try:
    from google import genai
except ImportError:
    print("Error: 'google-genai' package is not installed. Run: pip install google-genai")
    sys.exit(1)

try:
    import pymupdf as fitz  # PyMuPDF
except ImportError:
    try:
        import fitz
    except ImportError:
        fitz = None

try:
    import docx
except ImportError:
    docx = None


def extract_text_from_pdf(pdf_path):
    """Extract text from a PDF file using PyMuPDF, preserving page numbers."""
    if not fitz:
        return ""
    doc = fitz.open(pdf_path)
    text_blocks = []
    
    for page_num in range(len(doc)):
        page = doc.load_page(page_num)
        text = page.get_text("text")
        if text.strip():
            text_blocks.append(f"[Page {page_num + 1}]\n{text.strip()}")
            
    doc.close()
    return "\n\n".join(text_blocks)


def extract_text_from_docx(docx_path):
    """Extract text from a DOCX file, including paragraphs and tables."""
    if not docx:
        return ""
    doc = docx.Document(docx_path)
    elements = []
    
    # Extract paragraphs
    for p in doc.paragraphs:
        if p.text.strip():
            elements.append(p.text.strip())
            
    # Extract tables (crucial for schedules, timetables, tasks)
    for t_idx, table in enumerate(doc.tables, 1):
        table_rows = []
        for row in table.rows:
            row_cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
            if row_cells:
                table_rows.append(" | ".join(row_cells))
        if table_rows:
            elements.append(f"[Table {t_idx}]\n" + "\n".join(table_rows))
            
    return "\n\n".join(elements)


def fetch_google_doc_text(url_or_id):
    """Fetch text from a Google Docs link or Document ID via export URL."""
    match = re.search(r'/document/d/([a-zA-Z0-9_-]+)', url_or_id)
    doc_id = match.group(1) if match else url_or_id.strip()
    
    export_url = f"https://docs.google.com/document/d/{doc_id}/export?format=txt"
    
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(export_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, context=ctx) as resp:
        text = resp.read().decode("utf-8", errors="ignore").strip()
        
    if not text:
        raise ValueError(f"Could not retrieve text from Google Doc ID: {doc_id}")

    return text


def load_document_text(file_path_or_url):
    """Load text content from Google Docs URL, PDF, or DOCX file."""
    str_path = str(file_path_or_url)
    if "docs.google.com" in str_path or re.search(r'/document/d/([a-zA-Z0-9_-]+)', str_path):
        return fetch_google_doc_text(str_path)
        
    path = Path(str_path)
    if not path.exists():
        if len(str_path) > 25 and "/" not in str_path:
            return fetch_google_doc_text(str_path)
        raise FileNotFoundError(f"File not found: {str_path}")
        
    ext = path.suffix.lower()
    if ext == ".pdf":
        return extract_text_from_pdf(str(path))
    elif ext == ".docx":
        return extract_text_from_docx(str(path))
    else:
        raise ValueError(f"Unsupported file format '{ext}'. Supported formats: Google Docs URL, .pdf, .docx")


def load_combined_document_context(sources):
    """Load text from multiple sources and combine into a single context string."""
    combined_texts = []
    for idx, src in enumerate(sources, 1):
        try:
            text = load_document_text(src)
            source_name = f"Source {idx} ({src})"
            combined_texts.append(f"=== {source_name} ===\n{text}")
        except Exception as e:
            print(f"Warning: Failed to load source '{src}': {e}")
            
    return "\n\n".join(combined_texts)


def answer_question(client, query, document_context, model="gemini-2.5-flash"):
    """Generate answer strictly from the provided document context using Gemini LLM."""
    if not document_context or not document_context.strip():
        return "The information is not available in the provided document."
        
    prompt = f"""You are a precise Document Question Answering assistant.
Your task is to answer the user's question using ONLY the provided document context below.

CRITICAL INSTRUCTIONS:
1. Base your answer strictly and exclusively on the facts, definitions, data, and details provided in the DOCUMENT CONTEXT.
2. Do NOT use outside knowledge, external assumptions, or random guesses.
3. If the user's question cannot be completely answered from the provided DOCUMENT CONTEXT, respond EXACTLY with:
   The information is not available in the provided document.
4. Keep your answer clear, direct, helpful, accurate, and concise.

DOCUMENT CONTEXT:
{document_context}

USER QUESTION:
{query}

ANSWER:"""

    candidate_models = [model, "gemini-2.0-flash", "gemini-1.5-flash", "gemini-3.6-flash"]
    last_err = None
    for m in candidate_models:
        try:
            response = client.models.generate_content(
                model=m,
                contents=prompt
            )
            if response and response.text:
                return response.text.strip()
        except Exception as e:
            last_err = e
            
    if last_err:
        raise last_err
    return "The information is not available in the provided document."


DEFAULT_SOURCES = [
    "https://docs.google.com/document/d/1VrLsVr9k0MhLceV1or7hgS6rzmCMrxZ9xpZ5kCyj5x0/edit?usp=drivesdk",
    "https://docs.google.com/document/d/1lautHKbDdubg-MY7T3zJGdUQSHXbwTKzSOZZ1vWhGnY/edit?usp=drivesdk",
    "https://docs.google.com/document/d/1Pt0UAIm54VWumFQ6oOy0Wyxwf4iSAkez/edit?usp=drivesdk",
]


def main():
    # Load environment variables from .env
    env_path = Path(__file__).parent / ".env"
    load_dotenv(dotenv_path=env_path)

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "your_api_key_here":
        print("\n========================================================")
        print("WARNING: GEMINI_API_KEY is missing or set to placeholder in .env!")
        print(f"Please update '{env_path}' with your actual Gemini API key.")
        print("========================================================\n")
        sys.exit(1)

    # Initialize Gemini Client
    client = genai.Client(api_key=api_key)

    sources = DEFAULT_SOURCES
    # Also check if local DOCX exists in parent or current dir
    local_docx = Path(__file__).parent / "3. Classification (Module 2).docx"
    if local_docx.exists():
        sources.append(str(local_docx))

    print("======================================================")
    print("  Document Question Answering System (Direct Gemini Context)")
    print("======================================================")
    print("Loading documents from configured links and files...")

    try:
        document_context = load_combined_document_context(sources)
        if not document_context.strip():
            raise ValueError("No text could be extracted from any provided document.")
        print(f"Successfully loaded document context ({len(document_context)} characters).")
    except Exception as e:
        print(f"Error loading documents: {e}")
        sys.exit(1)

    print("\nDocuments loaded! Ask questions about the content.")
    print("Type 'exit' or 'quit' to stop.\n")

    while True:
        try:
            question = input("Ask a question: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting. Goodbye!")
            break
        if not question:
            continue
        lower_q = question.lower()
        if lower_q in ["exit", "quit", "q"]:
            print("Goodbye!")
            break
        try:
            answer = answer_question(client, question, document_context)
            print(f"\nAnswer:\n{answer}\n")
            print("-" * 55)
        except Exception as e:
            print(f"\nError processing question: {e}\n")


if __name__ == "__main__":
    main()
