"""House electronic PTR (PDF with a text layer) -> trade rows.

The Transactions table has columns ID | Owner | Asset | Transaction Type | Date | Notification Date |
Amount | Cap. Gains > $200?. Words are placed into columns by the x-position of the header words on
each page (the header repeats on every page the table spans).

  - A row starts on a line with a transaction type, a date and a notification date in their columns.
  - The asset name and the amount can wrap onto the following lines.
  - After the asset come small-caps detail lines: Filing Status, Subholding Of, Location,
    Description, Comments. Their labels extract as e.g. "F\\x00\\x00 S\\x00\\x00:", and a
    description can wrap across every column, so detail text is never split into columns.
  - The table ends at the "* For the complete list of asset type abbreviations" footnote.
"""

import io
import re

import pdfplumber

from parse import normalize as norm
from parse.normalize import ParsedTrade

DATE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
TYPES = {"P", "S", "S (partial)", "E"}
DETAIL_LABEL = re.compile(r"^([A-Z][A-Z\x00 ]*?)\s*:\s*")
DETAIL_FIELDS = {"FS": "filing_status", "SO": "subholding", "L": "location", "D": "description", "C": "comments"}
CODE = re.compile(r"\[([A-Z]{2})\]\s*$")
FOOTNOTE = "* For the complete list"
COLUMNS = ("owner", "asset", "type", "date", "notification", "amount", "cap_gains")
HEADER_WORDS = ("Owner", "Asset", "Transaction", "Date", "Notification", "Amount", "Cap.")
LINE_TOLERANCE = 3  # points; words this close vertically are on the same line
COLUMN_SLACK = 4  # cell text can start a little left of its header word


def parse(pdf_bytes: bytes) -> list[ParsedTrade]:
    rows: list[dict] = []
    columns: list[float] | None = None
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            lines = _lines(page.extract_words(keep_blank_chars=True, x_tolerance=1.5))
            header = _header(lines)
            if header is not None:
                columns, header_bottom = header
                lines = [line for line in lines if line[0]["top"] > header_bottom]
            if columns is None:
                continue
            for line in lines:
                if _text(line).startswith(FOOTNOTE):
                    return [_trade(i, row) for i, row in enumerate(rows, start=1)]
                _consume(line, columns, rows)
    return [_trade(i, row) for i, row in enumerate(rows, start=1)]


def _lines(words: list[dict]) -> list[list[dict]]:
    lines: list[list[dict]] = []
    for word in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        if lines and abs(lines[-1][0]["top"] - word["top"]) <= LINE_TOLERANCE:
            lines[-1].append(word)
        else:
            lines.append([word])
    return [sorted(line, key=lambda w: w["x0"]) for line in lines]


def _text(line: list[dict]) -> str:
    return " ".join(w["text"] for w in line).strip()


def _header(lines: list[list[dict]]) -> tuple[list[float], float] | None:
    """Column left edges and the bottom of the header block, if this page has the table header."""
    for i, line in enumerate(lines):
        texts = [w["text"].strip() for w in line]
        if all(word in texts for word in HEADER_WORDS):
            edges = [line[texts.index(word)]["x0"] - COLUMN_SLACK for word in HEADER_WORDS]
            # The header wraps over three lines ("Type", "Date", "Gains >", "$200?"); skip them all.
            bottom = line[0]["top"]
            for following in lines[i + 1:i + 4]:
                if any(w["text"].strip() in ("Type", "Gains >", "Gains", "$200?") for w in following):
                    bottom = following[0]["top"]
            return edges, bottom
    return None


def _cells(line: list[dict], edges: list[float]) -> dict[str, str]:
    cells = dict.fromkeys(COLUMNS, "")
    for word in line:
        column = None
        for name, edge in zip(COLUMNS, edges, strict=True):
            if word["x0"] >= edge:
                column = name
        if column:
            cells[column] = (cells[column] + " " + word["text"]).strip()
    return cells


def _consume(line: list[dict], edges: list[float], rows: list[dict]) -> None:
    text = _text(line)
    cells = _cells(line, edges)
    if cells["type"] in TYPES and DATE.match(cells["date"]) and DATE.match(cells["notification"]):
        rows.append({"owner": cells["owner"], "asset": [cells["asset"]], "type": cells["type"],
                     "date": cells["date"], "amount": cells["amount"], "details": {}, "last": None})
        return
    if not rows:
        return
    row = rows[-1]
    label = DETAIL_LABEL.match(text)
    if label and "\x00" in label.group(1):
        key = DETAIL_FIELDS.get(re.sub(r"[\x00 ]", "", label.group(1)), "other")
        row["details"][key] = norm.clean(text[label.end():])
        row["last"] = key
    elif row["last"]:  # a detail that wraps onto the next line
        row["details"][row["last"]] = norm.clean(row["details"][row["last"]] + " " + text.replace("\x00", ""))
    else:  # the asset name (and maybe the amount) wrapping onto the next line
        if cells["asset"]:
            row["asset"].append(cells["asset"])
        if cells["amount"]:
            row["amount"] += " " + cells["amount"]


def _trade(line_no: int, row: dict) -> ParsedTrade:
    asset = norm.clean(" ".join(row["asset"]))
    code_match = CODE.search(asset)
    code = code_match.group(1) if code_match else None
    name = asset[:code_match.start()].strip() if code_match else asset
    low, high = norm.amount(row["amount"])
    details = row["details"]
    description = "; ".join(details[k] for k in ("description", "comments") if details.get(k)) or None
    return ParsedTrade(
        line_no=line_no,
        owner=norm.owner(row["owner"]),
        asset_name=name,
        ticker=norm.ticker_in_name(name),
        asset_code=code,
        asset_type=norm.asset_type(code),
        action=norm.action(row["type"]),
        tx_date=norm.iso_date(row["date"]),
        amount_min=low,
        amount_max=high,
        description=description,
    )
