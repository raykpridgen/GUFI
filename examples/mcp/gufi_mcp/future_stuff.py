
from pathlib import Path


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