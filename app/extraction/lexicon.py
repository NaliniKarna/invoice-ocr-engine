"""Field-label lexicon: maps the words printed next to a value ("Taxes", "VAT (15%)", "Total Due", "IVA 21%") to the
roles they usually play on an invoice.

This is DATA plus a small scorer, not a list of templates. A label is split into words, light-stemmed (plural, case,
punctuation, trailing numbers and percentages dropped), matched against word groups with a small fuzzy tolerance for OCR
noise, and scored for each role it could mean. The scorer returns SEVERAL roles with weights when a label is ambiguous
("Net total" is a subtotal in some countries and the grand total in others). The extractor settles the ambiguity with
arithmetic (subtotal - discount + tax + charges = total), not with more label rules.

Adding a language or a synonym means adding a word here. It never means writing a rule for one vendor.
"""
from __future__ import annotations

import difflib
import re
from functools import lru_cache

TAX_WORDS = frozenset({"tax", "vat", "gst", "cgst", "sgst", "igst", "utgst", "hst", "pst", "qst", "iva", "mwst", "ust", "tva", "btw",
                       "impuesto", "steuer", "duty", "levy", "cess", "taxe"})
TAX_COMPONENT_WORDS = frozenset({"cgst", "sgst", "igst", "utgst", "hst", "pst", "qst", "cess"})
_BEFORE_TAX = frozenset({"before", "excl", "excluding", "exclusive", "ex", "exc", "pretax", "untaxed", "net", "netto", "neto", "base", "imponible",
                         "taxable", "without"})
_AFTER_TAX = frozenset({"incl", "including", "inclusive", "inc", "after", "brutto", "with", "gross"})
_TOTAL_WORDS = frozenset({"total", "totale", "gesamt", "summe", "totaal"})
_DUE_WORDS = frozenset({"due", "payable", "balance", "outstanding", "owing", "pay"})
_PAID_WORDS = frozenset({"paid", "received", "advance", "deposit", "payment", "tendered", "cash"})
_DISCOUNT_WORDS = frozenset({"discount", "disc", "rebate", "less", "reduction", "descuento", "rabatt", "remise"})
_OTHER_WORDS = frozenset({"shipping", "freight", "delivery", "postage", "handling", "packing", "carriage", "courier", "envio"})
_SERVICE_WORDS = frozenset({"service", "tip", "gratuity", "servicio"})
_SECTION_STRONG = frozenset({"grand", "invoice", "amount", "final", "overall", "factura", "rechnung", "facture"})
_NOT_AMOUNT_LABEL = frozenset({"registration", "reg", "number", "no", "id", "gstin", "tin", "pan", "rate", "invoice date", "phone", "tel", "fax", "account", "iban"})

# words that name each header field. A label needs one of the "key" words to be taken seriously.
INVOICE_NUMBER_KEY = frozenset({"no", "number", "num", "nr", "nbr", "ref", "reference", "n", "nº", "n.º", "numero", "número", "nummer", "#", "id"})
INVOICE_WORDS = frozenset({"invoice", "inv", "bill", "receipt", "factura", "rechnung", "facture", "fattura", "voucher", "memo", "challan"})
DATE_WORDS = frozenset({"date", "dated", "issued", "issue", "fecha", "datum", "data", "miti", "when"})
DUE_DATE_WORDS = frozenset({"due", "pay", "payment", "deadline", "vencimiento", "fällig", "terms"})
NOT_INVOICE_NUMBER = frozenset({"order", "po", "purchase", "account", "customer", "client", "phone", "tel", "fax", "gstin", "vat", "tax", "pan",
                                "tin", "zip", "postal", "post", "iban", "swift", "bank", "sku", "hsn", "code", "page", "cheque", "cust", "acct"})

_VOCAB = sorted(TAX_WORDS | _BEFORE_TAX | _AFTER_TAX | _TOTAL_WORDS | _DUE_WORDS | _PAID_WORDS | _DISCOUNT_WORDS | _OTHER_WORDS |
                _SERVICE_WORDS | _SECTION_STRONG | {"subtotal", "sub", "round", "rounding", "off", "of", "which", "amount", "value"})
_WORD = re.compile(r"[^\W\d_]+(?:[.'-][^\W\d_]+)*", re.U)


def stem(w: str) -> str:
    w = w.lower().strip(".'-")
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("xes") or w.endswith("ses") and len(w) > 4:
        return w[:-2]
    if w.endswith("s") and len(w) > 3 and not w.endswith("ss"):
        return w[:-1]
    return w


@lru_cache(maxsize=4096)
def _fix(w: str) -> str:
    """Small OCR tolerance: snap a long word to a vocabulary word when it is nearly identical ("Subtotai" -> "subtotal")."""
    if len(w) < 5 or w in _VOCAB:
        return w
    m = difflib.get_close_matches(w, _VOCAB + ["subtotal"], n=1, cutoff=0.85)
    return m[0] if m else w


def label_words(label: str) -> list[str]:
    """Words of a label, lower-case, stemmed, OCR-snapped. Numbers, percentages and punctuation are dropped."""
    out = []
    for raw in _WORD.findall(label.replace("_", " ")):
        for part in re.split(r"[.-]", raw) if raw.lower() in ("sub-total", "sub.total") else [raw]:
            out.append(_fix(stem(part)))
    # "sub total" / "sub-total" -> "subtotal"
    j = 0
    merged = []
    while j < len(out):
        if out[j] == "sub" and j + 1 < len(out) and out[j + 1] == "total":
            merged.append("subtotal"); j += 2
        else:
            merged.append(out[j]); j += 1
    return merged


def money_roles(label: str) -> dict[str, float]:
    """Roles an amount printed next to `label` could have: subtotal, taxable, tax, total, due, paid, discount, service, other.
    Empty when the label says nothing about an amount."""
    stripped = re.sub(r"\([^)]*[A-Za-z][^)]*\)", " ", label)      # "(prices include tax)" explains the label; "(15%)" is not a word anyway
    w = label_words(stripped) or label_words(label)
    if not w:
        return {}
    s = set(w)
    roles: dict[str, float] = {}
    if s & {"term", "method", "mode", "instruction", "bank", "account", "iban", "swift", "sort", "reference"}:
        return {}                                               # "Payment terms 30", "Bank account 1234": numbers, not amounts
    n_words = len(w)
    if s & {"subtotal"}:
        return {"subtotal": 3.0}
    if "taxable" in s and (s & {"value", "amount", "total", "sale", "supply"} or n_words == 1):
        return {"taxable": 2.6, "subtotal": 1.6}
    if s & TAX_WORDS:
        if s & {"registration", "reg", "number", "no", "id", "gstin", "tin", "pan", "invoice"} and not s & {"total", "amount"}:
            return {}                                             # a tax identifier or "tax invoice" title, not an amount
        if s & _TOTAL_WORDS and s & (_BEFORE_TAX - {"net"}):
            return {"subtotal": 3.0}                              # "Total excl. VAT", "Total amount before tax"
        if s & _TOTAL_WORDS and s & _AFTER_TAX:
            return {"total": 3.0}                                 # "Total incl. VAT", "Total amount after tax"
        return {"tax": 3.0 if not s & TAX_COMPONENT_WORDS else 2.4}
    if s & _TOTAL_WORDS:
        if s & _BEFORE_TAX:
            return {"subtotal": 2.2, "total": 1.4} if "net" in s and not s & {"payable", "due"} else {"subtotal": 3.0}
        if s & _AFTER_TAX:
            return {"total": 3.0}
        if s & _DUE_WORDS:
            return {"total": 3.0, "due": 1.0}
        if s & _SECTION_STRONG:
            return {"total": 3.0}
        if n_words == 1:
            return {"total": 2.6}
        return {"total": 1.3, "subtotal": 0.5}                    # "<section> total": part of something larger
    if s & _DUE_WORDS:
        roles = {"total": 1.6, "due": 2.8}
        if s & {"net"} or "amount" in s:
            roles["total"] = 2.0
        return roles
    if s & {"net"} and s & {"amount", "value"}:
        return {"total": 2.0, "subtotal": 1.6}
    if s & _PAID_WORDS:
        return {"paid": 2.5}
    if s & _DISCOUNT_WORDS:
        return {"discount": 2.5}
    if s & _OTHER_WORDS:
        return {"other": 2.5}
    if s & _SERVICE_WORDS and (s & {"charge", "fee", "tip", "gratuity", "service"}):
        return {"service": 2.5}
    if s & {"imponible", "base", "neto", "netto", "untaxed", "pretax"}:
        return {"subtotal": 2.5}
    if s & {"net"} and n_words <= 2:
        return {"subtotal": 1.5, "total": 1.2}
    if s & {"amount"} and n_words == 1:
        return {}
    return roles


def is_component_tax(label: str) -> bool:
    return bool(set(label_words(label)) & TAX_COMPONENT_WORDS)


def invoice_number_strength(label: str) -> float:
    """How strongly `label` announces an invoice/document number. 0 when it does not."""
    w = set(label_words(label)) | {t.lower() for t in re.findall(r"#|n\.?[º°o]", label, re.I)}
    if not w or w & NOT_INVOICE_NUMBER or w & DATE_WORDS or w & {"to", "bill to", "from"} and not w & INVOICE_WORDS:
        return 0.0
    has_inv, has_key = bool(w & INVOICE_WORDS), bool(w & INVOICE_NUMBER_KEY) or "#" in label or bool(re.search(r"n\.?[º°]", label, re.I))
    if has_inv and has_key:
        return 3.0
    if has_inv:
        return 1.5            # "Invoice" immediately followed by something shaped like a number
    if has_key and w & {"ref", "reference", "document", "doc"}:
        return 2.0
    if "document" in w or "doc" in w:
        return 1.0
    return 0.0


def date_strength(label: str) -> tuple[str, float]:
    """('date' | 'due_date', strength) for a label that announces a date, else ('', 0)."""
    w = set(label_words(label))
    if not w & DATE_WORDS and not w & {"due"}:
        return "", 0.0
    if w & {"due", "payment", "deadline", "vencimiento"} and not w & {"issue", "issued", "invoice"}:
        return "due_date", 3.0
    if w & {"invoice", "inv", "bill", "issue", "issued", "factura", "fecha"} or w & DATE_WORDS:
        return "date", 3.0 if w & {"invoice", "inv", "issue", "issued", "bill", "factura"} else 2.4
    return "", 0.0


# ---- table header vocabulary (column roles)
_HEADER_GROUPS: dict[str, frozenset[str]] = {
    "description": frozenset({"description", "item", "product", "particular", "service", "article", "detail", "name", "good", "descripcion", "beschreibung",
                              "designation", "work", "material", "narration"}),
    "quantity": frozenset({"hrs", "qty", "quantity", "unit", "hr", "hour", "no", "nos", "pc", "pcs", "count", "cantidad", "menge", "quantite", "day"}),
    "unit_price": frozenset({"rate", "price", "cost", "mrp", "precio", "preis", "prix"}),
    "amount": frozenset({"amount", "total", "value", "sum", "amt", "line", "importe", "betrag", "montant", "extended"}),
    "other": frozenset({"sku", "code", "hsn", "sac", "uom", "tax", "vat", "gst", "iva", "disc", "discount", "date", "s", "sl", "sr", "ref", "id", "#", "cgst", "sgst",
                        "igst", "hrs", "no.", "item#", "part", "barcode", "ean"}),
}


def header_role(text: str) -> tuple[str, float]:
    """Best column role for a table header cell, or ('', 0). Hint only: the extractor confirms roles with the numbers below."""
    w = [stem(x) for x in re.findall(r"[^\W\d_]+", text)]
    if not w and text.strip() in ("#", "№"):
        return "other", 0.5
    if not w:
        return "", 0.0
    s = set(w)
    if s & {"unit"} and s & {"price", "cost", "rate"}:
        return "unit_price", 3.0
    if s & {"tax", "vat", "gst", "iva", "disc", "discount"} and len(w) <= 3:
        return "other", 2.0
    if s & {"line", "extended"} and s & _HEADER_GROUPS["amount"]:
        return "amount", 3.0
    if s & _HEADER_GROUPS["amount"] and len(w) <= 3 and s <= (_HEADER_GROUPS["amount"] | {"price", "total", "net"}):
        return "amount", 2.5
    best, best_w = "", 0.0
    for role, words in _HEADER_GROUPS.items():
        hits = len(s & words)
        if hits and hits / len(s) >= 0.5 and 1.5 + hits > best_w:
            best, best_w = role, 1.5 + hits
    if not best:      # OCR tolerance on single words
        for x in w:
            for role, words in _HEADER_GROUPS.items():
                if len(x) >= 5 and difflib.get_close_matches(x, list(words), n=1, cutoff=0.86):
                    return role, 1.2
    return best, best_w


def title_like(text: str) -> bool:
    """Document titles and generic words that are never a supplier name."""
    w = set(label_words(text))
    return bool(w) and len(w) <= 5 and bool(w & ({"invoice", "receipt", "bill", "factura", "rechnung", "facture", "statement", "quotation", "estimate", "memo",
                                                  "original", "duplicate", "triplicate", "copy", "logo", "page", "tax", "credit", "note", "proforma", "welcome",
                                                  "thank", "customer", "due", "total"}))
