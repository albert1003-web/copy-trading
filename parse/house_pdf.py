"""House electronic PTR (PDF with a text layer) -> trade rows.

The Transactions table has columns ID | Owner | Asset | Transaction Type | Date | Notification Date |
Amount | Cap. Gains > $200?. Words are placed into columns by the x-position of the header words on
each page (the header repeats on every page the table spans).

  - A row starts on a line with a transaction type, a date and a notification date in their columns.
  - The asset name and the amount can wrap onto the following lines.
  - After the asset come small-caps detail lines: Filing Status, Subholding Of, Location,
    Description, Comments. Their labels extract as e.g. "F\\x00\\x00 S\\x00\\x00:" (or, in older filings
    such as 2020's, as "FILINg STATUS:"), and a description can wrap across every column, so detail text
    is never split into columns.
  - Older filings (2020-23) use a font whose capitals often extract as lowercase: cells read "s (partial)",
    "[sT]", "(ROKu)", "(Dg)", and the header ("owner asset ...") or the detail labels ("FIlINg STATuS:") give
    the font away. Header words and types are matched case-insensitively, and in such a filing codes and
    tickers are uppercased.
  - The table ends at the "* For the complete list of asset type abbreviations" footnote.
"""

import io
import re

import pdfplumber

from parse import normalize as norm
from parse.normalize import ParsedTrade

DATE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
TYPES = {"P", "S", "S (PARTIAL)", "E"}  # compared uppercased
DETAIL_LABEL = re.compile(r"^([A-Z][A-Za-z\x00 ]*?)\s*:\s*")
DETAIL_FIELDS = {"FS": "filing_status", "SO": "subholding", "L": "location", "D": "description", "C": "comments"}
DETAIL_WORDS = {"FILINGSTATUS": "filing_status", "SUBHOLDINGOF": "subholding", "LOCATION": "location",
                "DESCRIPTION": "description", "COMMENTS": "comments"}  # older filings spell the label out
CODE = re.compile(r"\[([A-Za-z]{2})\]\s*$")
TICKER_ANY_CASE = re.compile(r"\(([A-Za-z][A-Za-z0-9.]{0,5})\)")  # old-font filings: "(gE)", "(ROKu)"
FOOTNOTE = "* For the complete list"
COLUMNS = ("owner", "asset", "type", "date", "notification", "amount", "cap_gains")
HEADER_WORDS = ("owner", "asset", "transaction", "date", "notification", "amount", "cap.")  # compared lowercased
LINE_TOLERANCE = 3  # points; words this close vertically are on the same line
COLUMN_SLACK = 4  # cell text can start a little left of its header word


def parse(pdf_bytes: bytes) -> list[ParsedTrade]:
    rows: list[dict] = []
    columns: list[float] | None = None
    mixed_case = False
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            lines = _lines(page.extract_words(keep_blank_chars=True, x_tolerance=1.5))
            header = _header(lines)
            if header is not None:
                columns, header_bottom, lowercase_header = header
                mixed_case = mixed_case or lowercase_header
                lines = [line for line in lines if line[0]["top"] > header_bottom]
            if columns is None:
                continue
            if _consume_page(lines, columns, rows):
                break  # the table's footnote: nothing after it is a transaction
    mixed_case = mixed_case or any(row["mixed_case"] for row in rows)
    return [_trade(i, row, mixed_case) for i, row in enumerate(rows, start=1)]


def _consume_page(lines: list[list[dict]], columns: list[float], rows: list[dict]) -> bool:
    """Adds the page's lines to rows. True once the table's footnote is reached."""
    for line in lines:
        if _text(line).startswith(FOOTNOTE):
            return True
        _consume(line, columns, rows)
    return False


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


def _header(lines: list[list[dict]]) -> tuple[list[float], float, bool] | None:
    """Column left edges, the bottom of the header block, and whether the font extracts capitals as lowercase
    (the old-font filings), if this page has the table header."""
    for i, line in enumerate(lines):
        texts = [w["text"].strip().lower() for w in line]
        mixed_case = any(w["text"].strip() == "owner" for w in line)
        if all(word in texts for word in HEADER_WORDS):
            edges = [line[texts.index(word)]["x0"] - COLUMN_SLACK for word in HEADER_WORDS]
            # The header wraps over three lines ("Type", "Date", "Gains >", "$200?"); skip them all.
            bottom = line[0]["top"]
            for following in lines[i + 1:i + 4]:
                if any(w["text"].strip().lower() in ("type", "gains >", "gains", "$200?") for w in following):
                    bottom = following[0]["top"]
            return edges, bottom, mixed_case
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
    if cells["type"].upper() in TYPES and DATE.match(cells["date"]) and DATE.match(cells["notification"]):
        rows.append({"owner": cells["owner"], "asset": [cells["asset"]], "type": cells["type"],
                     "date": cells["date"], "amount": cells["amount"], "details": {}, "last": None,
                     "mixed_case": False})
        return
    if not rows:
        return
    row = rows[-1]
    label = DETAIL_LABEL.match(text)
    key = _detail_key(label.group(1)) if label else None
    if key:
        row["details"][key] = norm.clean(text[label.end():])
        row["last"] = key
        row["mixed_case"] |= "\x00" not in label.group(1) and label.group(1) != label.group(1).upper()
    elif row["last"]:  # a detail that wraps onto the next line
        row["details"][row["last"]] = norm.clean(row["details"][row["last"]] + " " + text.replace("\x00", ""))
    else:  # the asset name (and maybe the amount) wrapping onto the next line
        if cells["asset"]:
            row["asset"].append(cells["asset"])
        if cells["amount"]:
            row["amount"] += " " + cells["amount"]


def _detail_key(label: str) -> str | None:
    """The detail field for a label: "F\\x00\\x00 S\\x00:" (newer filings) or "FILINg STATUS" (older ones).
    None if it isn't a label (e.g. an asset name that happens to contain a colon)."""
    if "\x00" in label:
        return DETAIL_FIELDS.get(re.sub(r"[\x00 ]", "", label), "other")
    return DETAIL_WORDS.get(re.sub(r"\s", "", label).upper())


def _trade(line_no: int, row: dict, mixed_case: bool = False) -> ParsedTrade:
    asset = norm.clean(" ".join(row["asset"]))
    code_match = CODE.search(asset)
    code = code_match.group(1).upper() if code_match else None
    name = asset[:code_match.start()].strip() if code_match else asset
    ticker = norm.ticker_in_name(name)
    if mixed_case and ticker is None and (found := TICKER_ANY_CASE.findall(name)):
        ticker = found[-1].upper()
    low, high = norm.amount(row["amount"])
    details = row["details"]
    description = "; ".join(details[k] for k in ("description", "comments") if details.get(k)) or None
    return ParsedTrade(
        line_no=line_no,
        owner=norm.owner(row["owner"]),
        asset_name=name,
        ticker=ticker,
        asset_code=code,
        asset_type=norm.asset_type(code),
        action=norm.action(row["type"]),
        tx_date=norm.iso_date(row["date"]),
        amount_min=low,
        amount_max=high,
        description=description,
    )
