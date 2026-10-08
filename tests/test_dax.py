import unittest

from app.mcp_server.dax import DaxValidationError, validate_dax


class DaxTests(unittest.TestCase):
    def test_read_only_query_is_accepted(self):
        validate_dax('EVALUATE TOPN(10, \'Sales\')')

    def test_write_like_statement_is_rejected(self):
        with self.assertRaises(DaxValidationError):
            validate_dax('EVALUATE ROW("x", 1)\nDELETE something')

    def test_keywords_inside_identifiers_do_not_trigger_false_positive(self):
        validate_dax("EVALUATE SELECTCOLUMNS('Orders', \"Process Time\", 'Orders'[Drop Ship Flag])")
