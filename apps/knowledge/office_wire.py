"""Wire format shared by the app and its local, networkless Office converter."""

from struct import Struct

HEADER = Struct("!BI")
OPERATIONS = {"ping": 0, "doc": 1, "xls": 2, "ppt": 3, "pptx": 4, "pdf": 5}
MAX_INPUT = 200 * 1024 * 1024
MAX_OUTPUT = 64 * 1024 * 1024
JOB_TIMEOUT = 60
