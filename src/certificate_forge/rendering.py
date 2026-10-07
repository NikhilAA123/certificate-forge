"""One certificate template, behind a small interface that tests can replace."""

from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import Protocol
from xml.sax.saxutils import escape

import reportlab
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph

from certificate_forge.domain import CertificateData

FONT_LOCK = Lock()


class CertificateRenderer(Protocol):
    def render(self, certificate: CertificateData) -> bytes:
        """Return a complete PDF, or raise an exception for this recipient only."""
        ...


class UnsupportedTextError(ValueError):
    """The bundled font cannot faithfully represent some supplied text."""


class PdfCertificateRenderer:
    def __init__(self) -> None:
        # ReportLab ships the licensed Bitstream Vera fonts; no OS-specific font path.
        fonts = Path(reportlab.__file__).parent / "fonts"
        with FONT_LOCK:
            if "CertificateVera" not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont("CertificateVera", str(fonts / "Vera.ttf")))
                pdfmetrics.registerFont(TTFont("CertificateVeraBold", str(fonts / "VeraBd.ttf")))

    def render(self, certificate: CertificateData) -> bytes:
        for text in (certificate.recipient_name, certificate.title, certificate.issuer):
            for font_name in ("CertificateVera", "CertificateVeraBold"):
                glyphs = pdfmetrics.getFont(font_name).face.charToGlyph
                if any(ord(character) not in glyphs for character in text):
                    raise UnsupportedTextError("The certificate font does not support this text.")
        buffer = BytesIO()
        canvas = Canvas(buffer, pagesize=(792, 612), pageCompression=1)
        canvas.setTitle(f"Certificate - {certificate.recipient_name}")
        canvas.setAuthor(certificate.issuer)
        canvas.setFillColor(colors.HexColor("#FCFAF5"))
        canvas.rect(0, 0, 792, 612, stroke=0, fill=1)
        canvas.setStrokeColor(colors.HexColor("#B79B64"))
        canvas.setLineWidth(1.5)
        canvas.rect(26, 26, 740, 560, stroke=1, fill=0)
        canvas.setFillColor(colors.HexColor("#17334B"))
        canvas.rect(26, 570, 740, 16, stroke=0, fill=1)
        self._text(canvas, "CERTIFICATE OF COMPLETION", 508, 22, bold=True)
        self._text(canvas, "This certificate is proudly presented to", 450, 12)
        self._paragraph(canvas, certificate.recipient_name, 417, 36, 92, bold=True)
        self._text(canvas, "for successfully completing", 300, 12)
        self._paragraph(canvas, certificate.title, 272, 22, 66)
        self._paragraph(canvas, certificate.issuer, 171, 14, 45, bold=True)
        self._text(canvas, f"Issued on {certificate.issued_on}", 103, 11)
        self._text(canvas, f"Certificate ID: {certificate.certificate_id}", 64, 8)
        canvas.showPage()
        canvas.save()
        return buffer.getvalue()

    @staticmethod
    def _text(canvas: Canvas, text: str, y: float, size: int, *, bold: bool = False) -> None:
        canvas.setFillColor(colors.HexColor("#17334B"))
        canvas.setFont("CertificateVeraBold" if bold else "CertificateVera", size)
        canvas.drawCentredString(396, y, text)

    @staticmethod
    def _paragraph(
        canvas: Canvas, text: str, top: float, size: int, height: int, *, bold: bool = False
    ) -> None:
        # Escape input because Paragraph uses XML-like formatting. Fit long names deliberately.
        while size >= 10:
            style = ParagraphStyle(
                "certificate",
                fontName="CertificateVeraBold" if bold else "CertificateVera",
                fontSize=size,
                leading=size * 1.3,
                textColor=colors.HexColor("#17334B"),
                alignment=TA_CENTER,
            )
            paragraph = Paragraph(escape(text), style)
            _, actual_height = paragraph.wrap(650, height)
            if actual_height <= height:
                paragraph.drawOn(canvas, 71, top - actual_height)
                return
            size -= 1
        raise ValueError("Text cannot fit the certificate template.")
