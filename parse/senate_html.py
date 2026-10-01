"""Senate electronic PTR (eFD HTML report page) -> trade rows.

The report has one transactions table with columns # | Transaction Date | Owner | Ticker | Asset Name |
Asset Type | Type | Amount | Comment. Columns are matched by header text, not position, so a reordered
or added column doesn't shift the values.
"""

from bs4 import BeautifulSoup

from parse import normalize as norm
from parse.normalize import ParsedTrade

HEADERS = {
    "#": "line_no",
    "transaction date": "date",
    "owner": "owner",
    "ticker": "ticker",
    "asset name": "asset",
    "asset type": "asset_type",
    "type": "type",
    "amount": "amount",
    "comment": "comment",
}
REQUIRED = {"date", "owner", "asset", "type", "amount"}


def parse(html: str) -> list[ParsedTrade]:
    table = _transactions_table(BeautifulSoup(html, "html.parser"))
    if table is None:
        return []
    header_row = table.find("tr")
    columns = [HEADERS.get(norm.clean(cell.get_text()).lower()) for cell in header_row.find_all(["th", "td"])]
    trades = []
    for position, tr in enumerate(table.find_all("tr")[1:], start=1):
        cells = tr.find_all("td")
        if len(cells) != len(columns):
            continue
        row = {name: norm.clean(cell.get_text(" ")) for name, cell in zip(columns, cells, strict=True) if name}
        trades.append(_trade(position, row))
    return sorted(trades, key=lambda t: t.line_no)


def _transactions_table(soup: BeautifulSoup):
    for table in soup.find_all("table"):
        header = table.find("tr")
        if header is None:
            continue
        names = {HEADERS.get(norm.clean(c.get_text()).lower()) for c in header.find_all(["th", "td"])}
        if REQUIRED <= names:
            return table
    return None


def _trade(position: int, row: dict[str, str]) -> ParsedTrade:
    code = row.get("asset_type") or None
    low, high = norm.amount(row.get("amount"))
    line_no = int(row["line_no"]) if row.get("line_no", "").isdigit() else position
    return ParsedTrade(
        line_no=line_no,
        owner=norm.owner(row.get("owner")),
        asset_name=row.get("asset", ""),
        # Some filers leave the Ticker column as "--" and type the ticker into the asset name instead.
        ticker=norm.ticker(row.get("ticker")) or norm.ticker_in_name(row.get("asset")),
        asset_code=code,
        asset_type=norm.asset_type(code),
        action=norm.action(row.get("type")),
        tx_date=norm.iso_date(row.get("date")),
        amount_min=low,
        amount_max=high,
        description=None if row.get("comment") in ("", "--") else row.get("comment"),
    )
