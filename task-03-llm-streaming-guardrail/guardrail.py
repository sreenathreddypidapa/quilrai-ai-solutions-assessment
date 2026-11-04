"""Bounded-memory, incremental PII redaction for streaming text.

Delays candidate tokens until a delimiter is seen, preventing disclosure of
PII split across arbitrary upstream deltas. Very long tokens are fail-closed.
"""
import re

REDACTED = '[REDACTED]'
MAX_CANDIDATE = 320
EMAIL = re.compile(r'(?i)(?<![\w.+-])[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}(?![\w-])')
SSN = re.compile(r'(?<!\d)\d{3}[- ]?\d{2}[- ]?\d{4}(?!\d)')
CARD = re.compile(r'(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)')
# A candidate may contain a PII sequence, even when embedded in prose punctuation.
# Preserve only safe separators as boundaries; commas, brackets etc. are boundaries.
TOKEN_CHAR = re.compile(r'[A-Za-z0-9@._%+\-]')

def luhn(digits: str) -> bool:
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0

def redact_token(token: str) -> str:
    spans = [(m.start(), m.end()) for pattern in (EMAIL, SSN)
             for m in pattern.finditer(token)]
    for m in CARD.finditer(token):
        digits = re.sub(r'\D', '', m.group())
        if luhn(digits):
            spans.append((m.start(), m.end()))
    if not spans:
        return token
    spans.sort()
    out = []
    end = 0
    for start, stop in spans:
        if start < end:
            end = max(end, stop)
            continue
        out.extend((token[end:start], REDACTED))
        end = stop
    out.append(token[end:])
    return ''.join(out)

class StreamRedactor:
    def __init__(self):
        self.pending = ''
        self.overflow = False

    def feed(self, text: str) -> str:
        out = []
        for ch in text:
            if TOKEN_CHAR.fullmatch(ch):
                if self.overflow:
                    continue
                self.pending += ch
                if len(self.pending) > MAX_CANDIDATE:
                    self.pending = ''
                    self.overflow = True
            else:
                if self.overflow:
                    out.append(REDACTED)
                elif self.pending:
                    out.append(redact_token(self.pending))
                self.pending = ''
                self.overflow = False
                out.append(ch)
        return ''.join(out)

    def finish(self) -> str:
        if self.overflow:
            result = REDACTED
        else:
            result = redact_token(self.pending)
        self.pending = ''
        self.overflow = False
        return result
