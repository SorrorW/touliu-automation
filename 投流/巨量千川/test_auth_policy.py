# -*- coding: utf-8 -*-
import unittest

from .auth import auth_type_candidates, auth_mode_for_department

class AuthPolicyTests(unittest.TestCase):
    def test_matrix_is_self_operated_only(self):
        self.assertEqual(auth_mode_for_department("业务1部-1"), "matrix")
        self.assertEqual(auth_type_candidates("matrix"), ("自运营",))

    def test_business_upload_uses_cooperative_authorization(self):
        self.assertEqual(auth_mode_for_department("业务1部"), "business")
        self.assertEqual(auth_type_candidates("business"), ("合作达人",))

    def test_main_account_uses_cooperative_authorization(self):
        self.assertEqual(auth_mode_for_department("主号"), "business")
        self.assertEqual(auth_type_candidates(auth_mode_for_department("主号")), ("合作达人",))

if __name__ == "__main__":
    unittest.main()
