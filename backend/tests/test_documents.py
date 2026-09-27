import asyncio
import importlib.util
import uuid
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from permetheus.auth import require_session
from permetheus.db import make_engine, make_sessionmaker
from permetheus.errors import install
from permetheus.models import Base, Company, Evidence, FinancialObservation, Job, JobState, ReviewStatus
from permetheus.documents import (
    Document, _extract_ixbrl, _extract_ixbrl_detailed, _extract_pdf, _pdf_pages,
    _validate_pdf_finding, process_document, router,
)


XHTML = b'''<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"
 xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
 xmlns:xbrli="http://www.xbrl.org/2003/instance"
 xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
 xmlns:ifrs="http://example.test/ifrs"
 xmlns:iso4217="http://www.xbrl.org/2003/iso4217">
 <body><xbrli:xbrl>
  <xbrli:context id="duration"><xbrli:entity><xbrli:identifier scheme="id">123</xbrli:identifier></xbrli:entity>
   <xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate><xbrli:endDate>2024-12-31</xbrli:endDate></xbrli:period></xbrli:context>
  <xbrli:context id="instant"><xbrli:entity><xbrli:identifier scheme="id">123</xbrli:identifier></xbrli:entity>
   <xbrli:period><xbrli:instant>2024-12-31</xbrli:instant></xbrli:period></xbrli:context>
  <xbrli:unit id="eur"><xbrli:measure>iso4217:EUR</xbrli:measure></xbrli:unit>
  <xbrli:unit id="pure"><xbrli:measure>xbrli:pure</xbrli:measure></xbrli:unit>
  <ix:nonFraction name="ifrs:Revenue" contextRef="duration" unitRef="eur" format="ixt:num-dot-decimal" scale="3" sign="-" decimals="0">1,234</ix:nonFraction>
  <ix:nonFraction name="ifrs:Revenue" contextRef="duration" unitRef="eur" format="ixt:num-dot-decimal" scale="3" sign="-" decimals="0">1,234</ix:nonFraction>
  <ix:nonFraction name="ifrs:CashAndCashEquivalentsAtCarryingValue" contextRef="instant" unitRef="eur" scale="0">25</ix:nonFraction>
 </xbrli:xbrl></body></html>'''


class FakeLLM:
    configured = True

    async def extract(self, text, instruction):
        return {"financials": [
            {"metric": "revenue", "amount": "123.45", "currency": "EUR",
             "period_start": "2024-01-01", "period_end": "2024-12-31", "scope": "consolidated",
             "quote": "Revenue was EUR 123.45.", "page": 1},
            {"metric": "revenue", "amount": "999", "currency": "EUR",
             "period_start": "2024-01-01", "period_end": "2024-12-31", "scope": "consolidated",
             "quote": "invented amount", "page": 1},
        ]}


class DocumentTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = make_engine("sqlite://")
        self.sessions = make_sessionmaker(self.engine)
        Base.metadata.create_all(self.engine)
        with self.sessions() as db:
            self.company = Company(name="Example Oy", name_normalized="example oy")
            db.add(self.company)
            db.commit()
            self.company_id = self.company.id
        app = FastAPI()
        app.state.settings = SimpleNamespace(data_dir=self.root)
        app.state.sessionmaker = self.sessions
        app.include_router(router)
        install(app)
        app.dependency_overrides[require_session] = lambda: object()
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()
        self.temp.cleanup()

    def test_ixbrl_signed_scaled_contexts_are_proposed_with_provenance(self):
        values = _extract_ixbrl(XHTML)
        self.assertEqual(len(values), 2)  # duplicate fact in same context is deduplicated
        revenue = next(v for v in values if v["metric"] == "revenue")
        self.assertEqual(revenue["amount"], "-1234000")
        self.assertEqual(revenue["currency"], "EUR")
        self.assertEqual(revenue["period_start"], "2024-01-01")
        self.assertEqual(revenue["locator"]["context_id"], "duration")
        self.assertEqual(revenue["scope"], "unknown")
        response = self.client.post(
            f"/api/companies/{self.company_id}/documents",
            files={"document": ("annual.xhtml", XHTML, "application/xhtml+xml")},
        )
        self.assertEqual(response.status_code, 202, response.text)
        document = response.json()
        self.assertEqual(document["status"], "queued")
        self.assertTrue(document["job_id"])
        self.assertTrue((self.root / "documents" / str(self.company_id) / document["id"] / "source.xhtml").is_file())
        with self.sessions() as db:
            job = db.query(Job).one()
            self.assertEqual((job.kind, job.state), ("document.extract", JobState.queued))
        asyncio.run(process_document(uuid.UUID(document["id"]), self.sessions, None, self.root))
        saved = self.client.get(f"/api/documents/{document['id']}").json()
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(len(saved["financials"]), 2)
        self.assertTrue(all(x["review_status"] == ReviewStatus.proposed.value for x in saved["financials"]))
        with self.sessions() as db:
            observations = db.query(FinancialObservation).all()
            evidence = db.query(Evidence).all()
            self.assertEqual(len(observations), 2)
            self.assertEqual(len(evidence), 2)
            self.assertTrue(all(e.excerpt and e.locator["context_id"] for e in evidence))
            observations[0].review_status = ReviewStatus.accepted
            evidence[0].review_status = ReviewStatus.rejected
            db.get(Document, uuid.UUID(document["id"])).status = "failed"
            db.commit()
        asyncio.run(process_document(uuid.UUID(document["id"]), self.sessions, None, self.root))
        with self.sessions() as db:
            observations = db.query(FinancialObservation).all()
            evidence = db.query(Evidence).all()
            self.assertEqual(len(observations), 2)
            self.assertEqual(len(evidence), 2)
            self.assertEqual(observations[0].review_status, ReviewStatus.accepted)
            self.assertEqual(evidence[0].review_status, ReviewStatus.rejected)

    def test_xml_dtd_and_external_entity_are_rejected(self):
        payload = b'<!DOCTYPE html [<!ENTITY x SYSTEM "file:///etc/passwd">]><html>&x;</html>'
        response = self.client.post(
            f"/api/companies/{self.company_id}/documents",
            files={"document": ("bad.xhtml", payload, "application/xhtml+xml")},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "unsafe_xml")

    def test_utf16_dtd_is_rejected_before_xml_parse(self):
        payload = '<!DOCTYPE html [<!ENTITY x SYSTEM "file:///etc/passwd">]><html>&x;</html>'.encode("utf-16-le")
        response = self.client.post(
            f"/api/companies/{self.company_id}/documents",
            files={"document": ("bad.xhtml", payload, "application/xhtml+xml")},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "unsafe_xml")

    def test_exact_concepts_transforms_units_and_dimensions(self):
        payload = XHTML.replace(
            b'<xbrli:context id="instant">',
            b'''<xbrli:context id="geo"><xbrli:entity><xbrli:identifier scheme="id">123</xbrli:identifier>
             <xbrli:segment><xbrldi:explicitMember dimension="ifrs:GeographicalAreasAxis">ifrs:Finland</xbrldi:explicitMember></xbrli:segment></xbrli:entity>
             <xbrli:period><xbrli:startDate>2024-01-01</xbrli:startDate><xbrli:endDate>2024-12-31</xbrli:endDate></xbrli:period></xbrli:context>
             <xbrli:context id="instant">''',
        ).replace(b'xmlns:ifrs=', b'xmlns:ixt="http://www.xbrl.org/inlineXBRL/transformation/2022-02-16" xmlns:ifrs=')
        payload = payload.replace(
            b'  <ix:nonFraction name="ifrs:CashAndCashEquivalentsAtCarryingValue"',
            b'''  <ix:nonFraction name="ifrs:Revenue" contextRef="duration" unitRef="eur" format="ixt:num-comma-decimal" decimals="2">1.234,56</ix:nonFraction>
  <ix:nonFraction name="ifrs:IncomeTaxExpenseProfitLoss" contextRef="duration" unitRef="eur" format="ixt:num-dot-decimal" decimals="0">99</ix:nonFraction>
  <ix:nonFraction name="ifrs:Revenue" contextRef="geo" unitRef="eur" format="ixt:num-dot-decimal" decimals="0">100</ix:nonFraction>
  <ix:nonFraction name="ifrs:Revenue" contextRef="duration" unitRef="eur" format="ixt:num-unit-decimal" decimals="0">1,000 euro</ix:nonFraction>
  <ix:nonFraction name="ifrs:Revenue" contextRef="duration" unitRef="pure" format="ixt:num-dot-decimal" decimals="0">100</ix:nonFraction>
  <ix:nonFraction name="ifrs:NumberOfEmployees" contextRef="duration" unitRef="pure" format="ixt:num-dot-decimal" decimals="0">12</ix:nonFraction>
  <ix:fraction name="ifrs:Revenue" contextRef="duration" unitRef="eur"><ix:numerator>1</ix:numerator><ix:denominator>2</ix:denominator></ix:fraction>
  <ix:nonFraction name="ifrs:CashAndCashEquivalentsAtCarryingValue"''',
        )
        facts, warning = _extract_ixbrl_detailed(payload)
        comma = next(row for row in facts if row["locator"].get("numeric_format") == "ixt:num-comma-decimal")
        self.assertEqual(comma["amount"], "1234.56")
        self.assertEqual(comma["scope"], "unknown")
        self.assertNotIn("IncomeTaxExpenseProfitLoss", [row["locator"].get("concept") for row in facts])
        self.assertNotIn("geo", [row["locator"].get("context_id") for row in facts])
        employees = next(row for row in facts if row["metric"] == "employees")
        self.assertIsNone(employees["currency"])
        self.assertIn("fraction", warning)
        self.assertIn("numeric format", warning)
        self.assertIn("dimensional segment", warning)

    def test_money_and_employee_pdf_citations_require_matching_units(self):
        pages = {1: "5 employees and EUR 100 revenue."}
        base = {"amount": "5", "currency": "EUR", "period_start": "2024-01-01",
                "period_end": "2024-12-31", "scope": "unknown", "quote": "5 employees", "page": 1}
        self.assertIsNone(_validate_pdf_finding({**base, "metric": "employees"}, pages))
        self.assertIsNone(_validate_pdf_finding({**base, "metric": "revenue", "currency": None,
                                                 "quote": "EUR 100 revenue."}, pages))
        accepted = _validate_pdf_finding({**base, "metric": "revenue", "quote": "EUR 100 revenue."}, pages)
        self.assertEqual(accepted["scope"], "unknown")

    def test_pdf_quote_validation_and_page_provenance(self):
        pages = ["Revenue was EUR 123.45.", "Net income was EUR 9."]
        findings, warning = asyncio.run(_extract_pdf(pages, FakeLLM()))
        self.assertIsNone(warning)
        self.assertEqual(len(findings), 1)  # the hallucinated quote is removed
        self.assertEqual(findings[0]["locator"]["page"], 1)

    def test_pdf_batches_warn_when_only_part_of_document_was_extracted(self):
        class PartialLLM(FakeLLM):
            async def extract(self, text, instruction):
                if "[PAGE 2]" in text:
                    raise RuntimeError("model unavailable")
                return {"financials": []}

        pages = ["Revenue statement " + ("A" * 20_000), "Income statement " + ("B" * 20_000)]
        findings, warning = asyncio.run(_extract_pdf(pages, PartialLLM()))
        self.assertEqual(findings, [])
        self.assertIn("1 of 2", warning)

    @unittest.skipIf(importlib.util.find_spec("pypdf") is None, "pypdf dependency is added by the parent project")
    def test_pdf_text_parser_reads_page_text(self):
        # Minimal single-page PDF with literal text; pypdf extracts the exact page citation.
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
            b"<< /Length 57 >>\nstream\nBT /F1 12 Tf 72 700 Td (Revenue was EUR 123.45.) Tj ET\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
        pdf = bytearray(b"%PDF-1.4\n")
        offsets = [0]
        for index, obj in enumerate(objects, 1):
            offsets.append(len(pdf))
            pdf.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
        xref = len(pdf)
        pdf.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
        for offset in offsets[1:]:
            pdf.extend(f"{offset:010d} 00000 n \n".encode())
        pdf.extend(f"trailer << /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
        source = self.root / "source.pdf"
        source.write_bytes(pdf)
        pages = _pdf_pages(source)
        self.assertEqual(len(pages), 1)
        self.assertIn("Revenue was EUR 123.45.", pages[0])
        from permetheus.acquisition import _web_pdf_text
        self.assertEqual(_web_pdf_text(bytes(pdf), 2000), "[PDF page 1] Revenue was EUR 123.45.")
        findings, _ = asyncio.run(_extract_pdf(pages, FakeLLM()))
        self.assertEqual(findings[0]["quote"], "Revenue was EUR 123.45.")


if __name__ == "__main__":
    unittest.main()
