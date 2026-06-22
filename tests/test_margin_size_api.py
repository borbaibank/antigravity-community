import copy
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import server


class SizingEnvOnlyApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.orig_state = copy.deepcopy(server.state)

    def tearDown(self):
        server.state = self.orig_state

    async def test_notional_size_api_rejected(self):
        resp = await server.api_notional_size({"value": 10})
        self.assertEqual(resp.status_code, 403)
        body = json.loads(resp.body)
        self.assertEqual(body["status"], "error")
        self.assertIn(".env", body["msg"])

    async def test_leverage_api_rejected(self):
        resp = await server.api_leverage({"value": 5})
        self.assertEqual(resp.status_code, 403)

    async def test_margin_size_api_rejected(self):
        resp = await server.api_margin_size({"value": 2})
        self.assertEqual(resp.status_code, 403)


if __name__ == "__main__":
    unittest.main()
