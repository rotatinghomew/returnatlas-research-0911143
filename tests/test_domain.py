import unittest
from returnatlas.catalog import Catalog
from returnatlas.domain import returned_orders


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog("data")

    def test_contact_duplicates_do_not_create_returns(self):
        self.assertLess(len(returned_orders(self.catalog)), len(self.catalog.rows("contacts")))
        self.assertEqual(len(returned_orders(self.catalog)), len(set(returned_orders(self.catalog))))


    def test_uncompleted_delivery_is_not_return_exposure(self):
        catalog = FakeCatalog({"deliveries": [{"id": "A", "delivered": False}],
                               "contacts": [{"delivery_id": "A", "action": "退货"}]})
        self.assertEqual(returned_orders(catalog), [])

    def test_multiple_claims_same_order(self):
        catalog = FakeCatalog({"deliveries": [{"id": "A", "delivered": True}],
                               "contacts": [{"delivery_id": "A", "action": "退货", "claim_id": claim} for claim in ("one", "two")]})
        self.assertEqual(returned_orders(catalog), ["A"])

    def test_inquiry_is_not_return(self):
        catalog = FakeCatalog({"deliveries": [{"id": "A", "delivered": True}],
                               "contacts": [{"delivery_id": "A", "action": "咨询"}]})
        self.assertEqual(returned_orders(catalog), [])

    def test_no_contact(self):
        self.assertEqual(returned_orders(FakeCatalog({"deliveries": [], "contacts": []})), [])


class FakeCatalog:
    def __init__(self, tables):
        self.tables = tables

    def rows(self, name):
        return self.tables[name]
