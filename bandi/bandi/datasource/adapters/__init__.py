from bandi.datasource.adapters.csv import CsvAdapter
from bandi.datasource.adapters.parquet import ParquetAdapter
from bandi.datasource.adapters.sqlite import SqliteAdapter
from bandi.datasource.adapters.xlsx import XlsxAdapter
from bandi.datasource.core import AdapterRegistry


def register_all(registry: type[AdapterRegistry] = AdapterRegistry) -> None:
    registry.register("sqlite", lambda c: SqliteAdapter(c))
    registry.register("csv", lambda c: CsvAdapter(c))
    registry.register("xlsx", lambda c: XlsxAdapter(c))
    registry.register("parquet", lambda c: ParquetAdapter(c))
