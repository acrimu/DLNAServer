import unittest
from unittest.mock import patch
from pathlib import Path

from dlna_server import DLNAServer, Library, _ebml_value, didl_entry, format_npt, mime_for, parse_npt, scpd_content, soap_args


class LibraryTests(unittest.TestCase):
    def test_ids_round_trip_and_block_traversal(self):
        root = Path(__file__).parent.resolve()
        child = root / "README.md"
        library = Library(root)
        self.assertEqual(library.decode(library.encode(child)), child.resolve())
        with self.assertRaises(ValueError):
            library.decode("..%2Fsecret")

    def test_mime_override(self):
        self.assertEqual(mime_for(Path("video.mkv")), "video/x-matroska")

    def test_soap_arguments_ignore_namespaces(self):
        xml = b'<s:Envelope xmlns:s="soap"><s:Body><u:Browse xmlns:u="service"><ObjectID>0</ObjectID><BrowseFlag>BrowseDirectChildren</BrowseFlag></u:Browse></s:Body></s:Envelope>'
        self.assertEqual(soap_args(xml)["ObjectID"], "0")

    def test_disconnect_errors_are_not_file_errors(self):
        self.assertFalse(issubclass(ConnectionResetError, (ValueError, FileNotFoundError, PermissionError)))

    def test_server_suppresses_connection_reset_traceback(self):
        server = object.__new__(DLNAServer)
        with patch("sys.exc_info", return_value=(ConnectionResetError, ConnectionResetError(), None)):
            with patch("http.server.HTTPServer.handle_error") as parent:
                server.handle_error(None, ("127.0.0.1", 1234))
                parent.assert_not_called()

    def test_content_directory_declares_required_actions(self):
        xml = scpd_content()
        for action in ("Browse", "GetSearchCapabilities", "GetSortCapabilities", "GetSystemUpdateID"):
            self.assertIn(f"<name>{action}</name>", xml)

    def test_npt_time_conversion(self):
        self.assertEqual(format_npt(3723.125), "1:02:03.125")
        self.assertEqual(parse_npt("1:02:03.125"), 3723.125)

    def test_reads_ebml_element_payload(self):
        self.assertEqual(_ebml_value(b"before\x44\x89\x84data", b"\x44\x89"), b"data")

    def test_root_metadata_has_upnp_parent_id(self):
        root = Path(__file__).parent.resolve()
        server = object.__new__(DLNAServer)
        server.library = Library(root)
        server.advertised_ip = "127.0.0.1"
        server.server_address = ("127.0.0.1", 8200)
        self.assertIn('id="0" parentID="-1"', didl_entry(server, root))


if __name__ == "__main__":
    unittest.main()
