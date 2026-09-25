/**
 * SVAProp.g4 — Focused ANTLR4 grammar for SVA "assert property" statements.
 *
 * Design goals:
 *   - Parse the STRUCTURE of an assertion: label, disable iff, clock event,
 *     implication operator (|-> / |=>), antecedent, consequent, ##N delays.
 *   - Treat the boolean logic INSIDE antecedent/consequent as opaque text
 *     (it is handled downstream by the existing CNF converter).
 *   - |-> and |=> are only recognised as structural tokens at the outermost
 *     level of propBody; inside any parenthesised/bracketed/braced group they
 *     are absorbed as regular inner content.
 *
 * Generated files (Python target):
 *   SVAPropLexer.py  SVAPropParser.py  SVAPropVisitor.py  SVAPropListener.py
 */

grammar SVAProp;

// ─────────────────────────── PARSER RULES ───────────────────────────

/** One complete   name : assert property ( ... ) ; */
assertion
    : label COLON KW_ASSERT KW_PROPERTY LPAREN propSpec RPAREN SEMI? EOF
    ;

label
    : ID
    ;

/** Optional clock event and/or disable iff preceding the property body. */
propSpec
    : clockEvent? disableIff? propBody
    ;

/** @(posedge clk) — strip entirely, content is ignored. */
clockEvent
    : AT LPAREN innerContent RPAREN
    ;

/** disable iff (reset_expr) — strip entirely, content is ignored. */
disableIff
    : KW_DISABLE KW_IFF LPAREN innerContent RPAREN
    ;

/**
 * The property body.
 * Three labelled alternatives so the visitor can pattern-match cleanly:
 *   - overlapImplication:    antecedent |->  consequent
 *   - nonOverlapImplication: antecedent |=>  consequent  (implies ##1 delay)
 *   - noImplication:         bare expression (no implication)
 */
propBody
    : expr OVERLAP_IMP    expr  # overlapImplication
    | expr NONOVERLAP_IMP expr  # nonOverlapImplication
    | expr                      # noImplication
    ;

/**
 * An expression at the outer (un-grouped) level.
 * Cannot directly contain OVERLAP_IMP or NONOVERLAP_IMP — those are only
 * visible inside group (see innerContent).
 */
expr
    : exprAtom+
    ;

/**
 * Atom at the outer level: a balanced group, a ##N delay pair, or any
 * single non-structural token.
 */
exprAtom
    : group
    | DELAY DEC_NUMBER     // ##N consumed as a unit; visitor extracts the N
    | outerToken
    ;

/** Balanced parentheses, square brackets, or curly braces. */
group
    : LPAREN   innerContent RPAREN
    | LBRACKET innerContent RBRACKET
    | LBRACE   innerContent RBRACE
    ;

/**
 * Content inside a balanced group.
 * May contain ANYTHING — including |-> and |=> (they are not structural
 * separators when nested).
 */
innerContent
    : innerAtom*
    ;

innerAtom
    : group
    | DELAY DEC_NUMBER
    | innerToken
    ;

/**
 * Every token that is legal at the outer (non-grouped) expression level.
 * Deliberately excludes OVERLAP_IMP, NONOVERLAP_IMP, LPAREN, RPAREN,
 * LBRACKET, RBRACKET, LBRACE, RBRACE, and SEMI.
 */
outerToken
    : ID | DEC_NUMBER
    | LOGIC_NOT | LOGIC_AND | LOGIC_OR | LOGIC_IMPL
    | BIT_AND | BIT_OR | BIT_NOT | BIT_XOR | BIT_XNOR | BIT_XNOR2
    | CASE_EQ | CASE_NEQ | WEQ | WNEQ | EQ | NEQ
    | GEQ | LEQ | ARSHIFT | RSHIFT | LSHIFT_A | LSHIFT
    | LT | GT | PLUS | MINUS | STAR | DIV | PERCENT | POWER
    | DOT | COMMA | QUESTION | COLON | TICK | AT | HASH
    ;

/** innerToken: everything outerToken allows, plus the SVA implication ops. */
innerToken
    : outerToken
    | OVERLAP_IMP
    | NONOVERLAP_IMP
    ;


// ─────────────────────────── LEXER RULES ────────────────────────────
// Ordering is significant in ANTLR: earlier rules win on equal-length
// matches; max-munch (longest match) takes priority between rules.

// ── Keywords (MUST appear before ID) ──
KW_ASSERT   : 'assert'   ;
KW_PROPERTY : 'property' ;
KW_DISABLE  : 'disable'  ;
KW_IFF      : 'iff'      ;

// ── SVA implication operators (MUST appear before BIT_OR '|') ──
OVERLAP_IMP     : '|->' ;
NONOVERLAP_IMP  : '|=>' ;

// ── Temporal delay (MUST appear before single '#') ──
DELAY : '##' ;

// ── Multi-character operators (MUST appear before their single-char prefixes) ──
LOGIC_AND  : '&&'  ;
LOGIC_OR   : '||'  ;
LOGIC_IMPL : '->'  ;
CASE_EQ    : '===' ;
CASE_NEQ   : '!==' ;
WEQ        : '==?' ;
WNEQ       : '!=?' ;
EQ         : '=='  ;
NEQ        : '!='  ;
GEQ        : '>='  ;
LEQ        : '<='  ;
ARSHIFT    : '>>>' ;
RSHIFT     : '>>'  ;
LSHIFT_A   : '<<<' ;
LSHIFT     : '<<'  ;
POWER      : '**'  ;
BIT_XNOR   : '~^'  ;
BIT_XNOR2  : '^~'  ;

// ── Single-character operators ──
LOGIC_NOT : '!' ;
BIT_AND   : '&' ;
BIT_OR    : '|' ;
BIT_NOT   : '~' ;
BIT_XOR   : '^' ;
LT        : '<' ;
GT        : '>' ;
PLUS      : '+' ;
MINUS     : '-' ;
STAR      : '*' ;
DIV       : '/' ;
PERCENT   : '%' ;
DOT       : '.' ;
COMMA     : ',' ;
QUESTION  : '?' ;
AT        : '@' ;
TICK      : '\'' ;
HASH      : '#' ;

// ── Structural delimiters ──
LPAREN   : '(' ;
RPAREN   : ')' ;
LBRACKET : '[' ;
RBRACKET : ']' ;
LBRACE   : '{' ;
RBRACE   : '}' ;
COLON    : ':' ;
SEMI     : ';' ;

// ── Identifiers (MUST appear after all keywords) ──
ID         : [a-zA-Z_$][a-zA-Z0-9_$]* ;
DEC_NUMBER : [0-9]+                    ;

// ── Whitespace and comments go to the HIDDEN channel so that
//    the original source text can be reconstructed from the token stream. ──
WS         : [ \t\r\n]+    -> channel(HIDDEN) ;
SL_COMMENT : '//' ~[\r\n]* -> channel(HIDDEN) ;
ML_COMMENT : '/*' .*? '*/' -> channel(HIDDEN) ;
