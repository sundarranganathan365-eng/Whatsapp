import sys
import unittest
from unittest.mock import MagicMock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from rag import (
    load_document_text,
    load_combined_document_context,
    answer_question,
    extract_text_from_pdf,
    extract_text_from_docx
)


class TestDirectDocumentQA(unittest.TestCase):
    
    def setUp(self):
        self.sample_docx = Path(__file__).parent.parent / "3. Classification (Module 2).docx"
        self.temp_pdf = Path(__file__).parent / "temp_test.pdf"
        try:
            import pymupdf as fitz
            doc = fitz.open()
            page = doc.new_page()
            page.insert_text(fitz.Point(50, 50), "Temporary Test Document Page 1\nClassification overview.", fontsize=11)
            doc.save(str(self.temp_pdf))
            doc.close()
        except ImportError:
            pass
        
    def tearDown(self):
        if self.temp_pdf.exists():
            self.temp_pdf.unlink()
        
    def test_pdf_extraction(self):
        if self.temp_pdf.exists():
            text = load_document_text(self.temp_pdf)
            self.assertIn("Temporary Test Document", text)
            self.assertIn("Page 1", text)
        
    def test_docx_extraction(self):
        if self.sample_docx.exists():
            text = load_document_text(self.sample_docx)
            self.assertGreater(len(text), 0)

    def test_combined_context_loading(self):
        sources = []
        if self.temp_pdf.exists():
            sources.append(str(self.temp_pdf))
        if self.sample_docx.exists():
            sources.append(str(self.sample_docx))
            
        if sources:
            context = load_combined_document_context(sources)
            self.assertGreater(len(context), 0)
            self.assertIn("=== Source 1", context)

    def test_gemini_qa_answering(self):
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "Classification is a supervised learning method."
        mock_client.models.generate_content.return_value = mock_response

        context = "Classification is a supervised learning method."
        answer = answer_question(mock_client, "What is classification?", context)
        self.assertEqual(answer, "Classification is a supervised learning method.")
        self.assertTrue(mock_client.models.generate_content.called)

    def test_missing_information_response(self):
        mock_client = MagicMock()
        answer = answer_question(mock_client, "What is quantum gravity?", "")
        self.assertEqual(answer, "The information is not available in the provided document.")


if __name__ == "__main__":
    unittest.main()
