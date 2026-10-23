import unittest

from ascendforge.agent.change_audit import audit_candidate, claim_check, strip_cpp_comments


class ChangeAuditTests(unittest.TestCase):
    def test_comment_only_change_is_not_effective(self):
        parent = {"kernel.asc": "int f() { return 1; }\n"}
        candidate = {"kernel.asc": "// performance claim only\nint f() { return 1; }\n"}
        audit = audit_candidate(parent, candidate)
        self.assertFalse(audit["effective"])
        self.assertEqual(audit["kind"], "comments_only")
        self.assertTrue(audit["raw_changed"])

    def test_code_change_is_effective(self):
        parent = {"kernel.asc": "int f() { return 1; }\n"}
        candidate = {"kernel.asc": "int f() { return 2; }\n"}
        audit = audit_candidate(parent, candidate)
        self.assertTrue(audit["effective"])
        self.assertEqual(audit["kind"], "code")
        self.assertGreater(audit["changed_lines"], 0)

    def test_claim_mismatch_when_no_vector_api(self):
        files = {"kernel.asc": "// vector math\nint f() { return 1; }\n"}
        claim = claim_check("use vector instructions on LocalTensor", files)
        self.assertTrue(claim["claims_vector"])
        self.assertFalse(claim["has_vector_api"])
        self.assertTrue(claim["mismatch"])

    def test_comment_stripper_preserves_strings(self):
        text = 'const char* s = "http://x"; // comment\n/* block */ int x = 1;\n'
        stripped = strip_cpp_comments(text)
        self.assertIn("http://x", stripped)
        self.assertNotIn("comment", stripped)
        self.assertIn("int x = 1", stripped)
