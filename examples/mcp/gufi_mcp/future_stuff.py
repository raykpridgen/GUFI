
from pathlib import Path


# Old dataclass stuff


    def parse_result(self, stdout: str, delimiter: str) -> None:
        lines = stdout.strip().splitlines()

        if not lines:
            return

        self.columns = lines[0].split(delimiter)
        self.rows = [line.split(delimiter) for line in lines[1:]]
        self.row_count = len(self.rows)

    def get_columns(self) -> list[str]:
        return self.columns

    def get_row_count(self) -> int:
        return self.row_count

    def get_rows(self, start, stop) -> list[list[Any]]:
        if stop > self.row_count or start < 0:
            return None

        return self.rows[start:stop]
        

''''''

    sql_specifiers: set[str] = field(
        default_factory=lambda: {"-I", "-T", "-S", "-E", "-J", "-K", "-G", "-F"}
    )

    config: set[str] = field(
        default_factory=lambda: {"-a"}
    )

    def __init_validate__(self):
        # Check that index exists
        if self.index not in get_gufi_indexes():
            raise RuntimeError("Failed to create query, index does not exist.")

    def add_option(self, tag: str, option: str):
        ''' Add an option to a query '''

        # Check for valid specifier
        if tag in self.sql_specifiers:
            # Check that SQL supplied is valid
            if not is_valid_sql_query(option, dialect="sqlite"):
                raise RuntimeError(f'Query: {option} is not a valid SQL query')

        # Check for config
        elif tag in self.config:
            # Invalid short circuit specifier
            if tag == "-a" and option not in ("0", "1", "2"):
                raise RuntimeError(f"Config for -a must be 0, 1, or 2")

        else:
            raise RuntimeError(f'Specifier: {tag} is not a valid option')

        self.options.append((tag,option))
        

    def validate_query(self) -> bool:

        # Check flag ordering

        # Check table existence

        # Check column existence

        # Pull out column names here to record in output

        return True

    def build_query_command(self) -> list[str]:

        # Add delimiter at the end to parse correctly
        self.options.append(("-d",self.delimiter))

        cmd = []
        cmd.append(GUFI_QUERY)
        for option in self.options:
            cmd.append(option[0])
            cmd.append(option[1])
        cmd.append(f"{GUFI_INDEX_ROOT}{self.index}")

        return cmd


# Object for general server management
class GufiMCPServer():
    index_root: Path
    indexes: list[str]

    def __init__(self, index_root):
        try:
            self.index_root = Path(index_root).resolve()
            if not self.index_root.is_dir():
                raise OSError("Error initializing server: GUFI indexes root is not a directory.")
        except FileNotFoundError:
            raise FileNotFoundError("Error initializing server: GUFI indexes root not found.")

        self.indexes = []
        self.discover_indexes()


    def discover_indexes(self):
        ''' Load in indexes once '''
        # Confirm path is a directory with other dirs inside
        for entry in self.index_root.iterdir():
            if not entry.is_dir():
                continue
            self.indexes.append(entry.name)