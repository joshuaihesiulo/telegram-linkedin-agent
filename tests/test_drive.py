"""Drive file-ID parsing across all common URL formats."""
from app.drive import parse_drive_file_id


def test_file_d_format():
    assert parse_drive_file_id(
        "https://drive.google.com/file/d/1AbC2dEfGhIjKlMnOp/view?usp=sharing"
    ) == "1AbC2dEfGhIjKlMnOp"


def test_open_id_format():
    assert parse_drive_file_id(
        "https://drive.google.com/open?id=1AbC2dEfGhIjKlMnOp"
    ) == "1AbC2dEfGhIjKlMnOp"


def test_uc_id_format():
    assert parse_drive_file_id(
        "https://drive.google.com/uc?id=1AbC2dEfGhIjKlMnOp&export=download"
    ) == "1AbC2dEfGhIjKlMnOp"


def test_bare_id():
    assert parse_drive_file_id("1AbC2dEfGhIjKlMnOp") == "1AbC2dEfGhIjKlMnOp"


def test_invalid():
    assert parse_drive_file_id("https://example.com/not-a-drive-link") is None
    assert parse_drive_file_id("") is None
