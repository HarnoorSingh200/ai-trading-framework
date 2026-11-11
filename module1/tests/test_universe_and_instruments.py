"""Reading the constituent list and the broker's instrument dump."""

from __future__ import annotations

from pathlib import Path

import pytest

from module1.instruments import parse_instruments
from module1.universe import UniverseError, load_universe, parse_universe

pytestmark = pytest.mark.unit

DUMP_HEADER = (
    "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,"
    "tick_size,lot_size,instrument_type,segment,exchange\n"
)


def test_the_checked_in_index_has_fifty_members() -> None:
    members = load_universe()

    symbols = [member.symbol for member in members]
    assert len(members) == 50
    assert len(set(symbols)) == 50
    assert symbols == [symbol.upper() for symbol in symbols]
    assert "RELIANCE" in symbols and "TCS" in symbols


def test_a_constituent_carries_the_name_a_person_can_read() -> None:
    members = {member.symbol: member for member in load_universe()}

    assert members["TMPV"].name == "Tata Motors Passenger Vehicles Ltd."
    assert members["RELIANCE"].industry == "Oil Gas & Consumable Fuels"


def test_blank_rows_are_ignored_and_duplicates_collapse() -> None:
    text = (
        "Symbol,Company Name,Industry\n"
        "INFY,Infosys Ltd.,Information Technology\n"
        "\n"
        ",,\n"
        "INFY,Infosys Ltd.,Information Technology\n"
        "sbin,State Bank of India,Financial Services\n"
    )

    members = parse_universe(text)

    assert [member.symbol for member in members] == ["INFY", "SBIN"]


def test_an_empty_constituent_list_is_an_error_not_an_empty_universe() -> None:
    with pytest.raises(UniverseError):
        parse_universe("Symbol,Company Name,Industry\n")


def test_only_nse_equities_are_kept_from_the_dump() -> None:
    text = DUMP_HEADER + (
        "738561,2885,RELIANCE,RELIANCE INDUSTRIES,1234.5,,,0.05,1,EQ,NSE,NSE\n"
        "1234,1234,NIFTY 50,NIFTY 50,,,,\"0\",1,EQ,INDICES,NSE\n"
        "99000,9000,RELIANCE,RELIANCE FUT,,2026-09-24,1400,0.05,250,FUT,NFO,NFO\n"
    )

    records = parse_instruments(text)

    assert set(records) == {"RELIANCE"}
    assert records["RELIANCE"].instrument_token == "738561"
    assert records["RELIANCE"].lot_size == 1


def test_the_dump_path_is_configurable(tmp_path: Path) -> None:
    from module1.instruments import InstrumentMaster

    master = InstrumentMaster(tmp_path)

    assert master.dump_path == tmp_path / "instruments_nse.csv"
    assert master.describe()["cached"] is False
