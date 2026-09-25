"""
sva_structural.py — ANTLR-backed structural extractor for SVA assertions.

Responsibility
--------------
Parse the STRUCTURE of a single   name : assert property ( ... ) ;
statement and return the components that the CNF converter needs:

  SVAExtractionResult
    .name     – assertion label
    .delay    – sum of ##N + 1 for |=>
    .body     – normalised boolean body (|-> replaced by ->, ##N stripped,
                whitespace and 'and'/'or' keywords normalised) ready for
                the existing  tokenize() / convert_to_cnf()  pipeline
    .success  – False when the ANTLR parse produced errors
    .error    – error message when success is False

The extractor intentionally does NOT parse boolean logic — that stays in
the existing SVAParser.tokenize() / convert_to_cnf() / flatten_to_cnf().
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import List

from antlr4 import CommonTokenStream, InputStream
from antlr4.error.ErrorListener import ErrorListener

from sva_grammar import SVAPropLexer, SVAPropParser


# ─────────────────────────────────────────────────────────────────────
# Public result type
# ─────────────────────────────────────────────────────────────────────

@dataclass
class SVAExtractionResult:
    name: str = ""
    delay: int = 0
    body: str = ""
    success: bool = False
    error: str = ""
    #: Portion of *delay* that belongs to the consequent. ``a |-> ##2 b`` and
    #: ``a ##2 b |-> c`` both sum to two; only the split tells them apart.
    consequent_delay: int = 0


# ─────────────────────────────────────────────────────────────────────
# Silent error listener (suppresses ANTLR's stderr output)
# ─────────────────────────────────────────────────────────────────────

class _CollectingErrorListener(ErrorListener):
    def __init__(self):
        super().__init__()
        self.errors: List[str] = []

    def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e):
        self.errors.append(f"line {line}:{column} {msg}")


# ─────────────────────────────────────────────────────────────────────
# Core extractor
# ─────────────────────────────────────────────────────────────────────

class SVAStructuralExtractor:
    """
    Convert a raw SVA assertion string into an SVAExtractionResult.

    Usage
    -----
        extractor = SVAStructuralExtractor()
        result = extractor.extract(sva_string)
        if result.success:
            # result.name / result.delay / result.body are ready
    """

    def extract(self, assertion_text: str) -> SVAExtractionResult:
        """Parse *assertion_text* and return its structural components."""
        # ── Set up ANTLR pipeline ─────────────────────────────────────
        input_stream = InputStream(assertion_text)
        lexer = SVAPropLexer(input_stream)
        lexer.removeErrorListeners()
        lex_err = _CollectingErrorListener()
        lexer.addErrorListener(lex_err)

        token_stream = CommonTokenStream(lexer)
        parser = SVAPropParser(token_stream)
        parser.removeErrorListeners()
        parse_err = _CollectingErrorListener()
        parser.addErrorListener(parse_err)

        tree = parser.assertion()

        all_errors = lex_err.errors + parse_err.errors
        if all_errors:
            return SVAExtractionResult(
                success=False,
                error="; ".join(all_errors),
            )

        # Fill token stream so hidden-channel (WS) tokens are accessible
        token_stream.fill()

        # ── Extract label ─────────────────────────────────────────────
        name = tree.label().ID().getText()

        # ── Navigate to propBody ──────────────────────────────────────
        prop_spec = tree.propSpec()
        prop_body = prop_spec.propBody()

        # ── Determine implication type via the labelled-alternative context ──
        # Labelled alternatives produce distinct Context subclasses:
        #   OverlapImplicationContext / NonOverlapImplicationContext / NoImplicationContext
        OverlapCtx    = SVAPropParser.OverlapImplicationContext
        NonOverlapCtx = SVAPropParser.NonOverlapImplicationContext

        has_overlap    = isinstance(prop_body, OverlapCtx)
        has_nonoverlap = isinstance(prop_body, NonOverlapCtx)
        extra_delay    = 1 if has_nonoverlap else 0

        # ── Extract antecedent / consequent expr contexts ─────────────
        # ANTLR generates different .expr() signatures per labelled alternative:
        #   Overlap/NonOverlap: .expr(i) returns the i-th ExprContext
        #   NoImplication:      .expr()  returns the single ExprContext directly
        if has_overlap or has_nonoverlap:
            ant_ctx = prop_body.expr(0)
            con_ctx = prop_body.expr(1)
        else:
            ant_ctx = None
            con_ctx = prop_body.expr()   # returns ExprContext directly

        # ── Build body string and collect delays ──────────────────────
        ant_text, ant_delay = self._expr_text_and_delay(ant_ctx, token_stream)
        con_text, con_delay = self._expr_text_and_delay(con_ctx, token_stream)

        total_delay = extra_delay + ant_delay + con_delay

        if ant_text:
            body = f"{ant_text}->{con_text}"
        else:
            body = con_text

        # ── Normalise for the existing tokenize() pipeline ────────────
        body = _normalise_body(body)

        return SVAExtractionResult(
            name=name,
            delay=total_delay,
            body=body,
            success=True,
            consequent_delay=extra_delay + con_delay,
        )

    # ── Private helpers ───────────────────────────────────────────────

    def _expr_text_and_delay(self, expr_ctx, token_stream) -> tuple[str, int]:
        """
        Walk the expr tree, collect ##N delays (summing them), and return
        (text_without_delays, total_delay).

        The text is reconstructed from the token stream (including hidden
        whitespace tokens) so the original spacing is preserved.
        """
        if expr_ctx is None:
            return "", 0

        total_delay = 0
        parts: List[str] = []

        for atom in expr_ctx.exprAtom():
            if atom.DELAY() is not None:
                amount = int(atom.DEC_NUMBER().getText())
                total_delay += amount
                if parts:
                    # An interior delay separates two operands of a sequence
                    # concatenation.  Dropping it glues them into a single
                    # indistinct atom, so 'a ##1 b' and 'a ##2 b' would only
                    # differ by a total that other shapes also produce.
                    parts.append(f'##{amount}')
            else:
                parts.append(self._raw_text(atom, token_stream))

        return "".join(parts), total_delay

    @staticmethod
    def _raw_text(ctx, token_stream) -> str:
        """
        Reconstruct the original source text for *ctx* (including whitespace)
        by reading all tokens — including those on the HIDDEN channel —
        between ctx.start.tokenIndex and ctx.stop.tokenIndex.
        """
        from antlr4 import Token
        start = ctx.start.tokenIndex
        stop = ctx.stop.tokenIndex
        return "".join(
            t.text
            for t in token_stream.tokens[start : stop + 1]
            if t.type != Token.EOF
        )


# ─────────────────────────────────────────────────────────────────────
# Body normalisation
# ─────────────────────────────────────────────────────────────────────

def _normalise_body(body: str) -> str:
    """
    Normalise the extracted body so the downstream tokenize() /
    convert_to_cnf() pipeline receives the same format it always expected:

      • SVA word operators  → symbolic equivalents
      • All whitespace stripped  (the char-by-char tokenizer handles spacing,
        but removing it here keeps atomic terms like 'a==b' intact, matching
        the behaviour of the old regex pre-processor)
    """
    # Word operators used in some SVA styles
    body = re.sub(r'\bor\b',  '||', body)
    body = re.sub(r'\band\b', '&&', body)
    body = re.sub(r'\bnot\b', '!',  body)
    # Strip all whitespace — must match old pre-processor's  re.sub(r"\s+", "", sva)
    body = re.sub(r'\s+', '', body)
    return body


# ─────────────────────────────────────────────────────────────────────
# Module-level convenience singleton
# ─────────────────────────────────────────────────────────────────────

_extractor = SVAStructuralExtractor()


def extract_sva_structure(assertion_text: str) -> SVAExtractionResult:
    """Module-level shortcut — avoids creating a new extractor each call."""
    return _extractor.extract(assertion_text)
