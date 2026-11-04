import unittest
from guardrail import StreamRedactor, redact_token, luhn

class RedactionTests(unittest.TestCase):
    def run_chunks(self, text, size):
        r=StreamRedactor()
        return ''.join(r.feed(text[i:i+size]) for i in range(0,len(text),size))+r.finish()

    def test_email(self):
        self.assertEqual(self.run_chunks('Email jane.doe@example.com now',1),'Email [REDACTED] now')
    def test_ssn(self):
        self.assertEqual(self.run_chunks('SSN 123-45-6789.',2),'SSN [REDACTED].')
    def test_card(self):
        self.assertEqual(self.run_chunks('Card 4111-1111-1111-1111!',1),'Card [REDACTED]!')
    def test_invalid_card(self):
        self.assertEqual(self.run_chunks('Number 4111-1111-1111-1112!',3),'Number 4111-1111-1111-1112!')
    def test_plain(self):
        self.assertEqual(self.run_chunks('Hello world!',1),'Hello world!')
    def test_adjacent(self):
        self.assertEqual(self.run_chunks('a@b.co,123-45-6789',1),'[REDACTED],[REDACTED]')
    def test_whitespace(self):
        self.assertEqual(self.run_chunks('A  B\nC',2),'A  B\nC')
    def test_overflow_fail_closed(self):
        self.assertEqual(self.run_chunks('a'*500+' end',7),'[REDACTED] end')
    def test_long_plain(self):
        self.assertEqual(self.run_chunks(('Hello '*2000),11),'Hello '*2000)
    def test_bounded(self):
        r=StreamRedactor()
        for i in range(20000):
            r.feed('x')
        self.assertLessEqual(len(r.pending),320)
        self.assertEqual(r.finish(),'[REDACTED]')
    def test_card_valid(self):
        self.assertTrue(luhn('4111111111111111'))
    def test_card_invalid(self):
        self.assertFalse(luhn('4111111111111112'))
    def test_email_split_every_boundary(self):
        t='Contact abc.xyz+tag@example.org.'
        for size in range(1,25):
            self.assertEqual(self.run_chunks(t,size),'Contact [REDACTED].')

if __name__=='__main__':
    unittest.main(verbosity=2)
