from agents.ingestion import discover_invoice_files, read_source_text


def test_read_source_text_plain_file(tmp_path):
    path = tmp_path / "invoice.txt"
    path.write_text("Invoice INV-1\nAmount: 100")
    assert read_source_text(path) == "Invoice INV-1\nAmount: 100"


def test_discover_invoice_files_filters_by_extension_and_sorts(tmp_path):
    (tmp_path / "b.txt").write_text("b")
    (tmp_path / "a.json").write_text("{}")
    (tmp_path / "c.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "ignored.docx").write_text("nope")
    (tmp_path / "subdir").mkdir()

    files = discover_invoice_files(tmp_path)
    assert [p.name for p in files] == ["a.json", "b.txt", "c.pdf"]


def test_discover_invoice_files_empty_dir(tmp_path):
    assert discover_invoice_files(tmp_path) == []
