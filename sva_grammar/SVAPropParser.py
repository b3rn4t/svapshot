# Generated from SVAProp.g4 by ANTLR 4.13.2
# encoding: utf-8
from antlr4 import *
from io import StringIO
import sys
if sys.version_info[1] > 5:
	from typing import TextIO
else:
	from typing.io import TextIO

def serializedATN():
    return [
        4,1,56,115,2,0,7,0,2,1,7,1,2,2,7,2,2,3,7,3,2,4,7,4,2,5,7,5,2,6,7,
        6,2,7,7,7,2,8,7,8,2,9,7,9,2,10,7,10,2,11,7,11,2,12,7,12,1,0,1,0,
        1,0,1,0,1,0,1,0,1,0,1,0,3,0,35,8,0,1,0,1,0,1,1,1,1,1,2,3,2,42,8,
        2,1,2,3,2,45,8,2,1,2,1,2,1,3,1,3,1,3,1,3,1,3,1,4,1,4,1,4,1,4,1,4,
        1,4,1,5,1,5,1,5,1,5,1,5,1,5,1,5,1,5,1,5,3,5,69,8,5,1,6,4,6,72,8,
        6,11,6,12,6,73,1,7,1,7,1,7,1,7,3,7,80,8,7,1,8,1,8,1,8,1,8,1,8,1,
        8,1,8,1,8,1,8,1,8,1,8,1,8,3,8,94,8,8,1,9,5,9,97,8,9,10,9,12,9,100,
        9,9,1,10,1,10,1,10,1,10,3,10,106,8,10,1,11,1,11,1,12,1,12,1,12,3,
        12,113,8,12,1,12,0,0,13,0,2,4,6,8,10,12,14,16,18,20,22,24,0,1,3,
        0,8,43,50,50,52,53,116,0,26,1,0,0,0,2,38,1,0,0,0,4,41,1,0,0,0,6,
        48,1,0,0,0,8,53,1,0,0,0,10,68,1,0,0,0,12,71,1,0,0,0,14,79,1,0,0,
        0,16,93,1,0,0,0,18,98,1,0,0,0,20,105,1,0,0,0,22,107,1,0,0,0,24,112,
        1,0,0,0,26,27,3,2,1,0,27,28,5,50,0,0,28,29,5,1,0,0,29,30,5,2,0,0,
        30,31,5,44,0,0,31,32,3,4,2,0,32,34,5,45,0,0,33,35,5,51,0,0,34,33,
        1,0,0,0,34,35,1,0,0,0,35,36,1,0,0,0,36,37,5,0,0,1,37,1,1,0,0,0,38,
        39,5,52,0,0,39,3,1,0,0,0,40,42,3,6,3,0,41,40,1,0,0,0,41,42,1,0,0,
        0,42,44,1,0,0,0,43,45,3,8,4,0,44,43,1,0,0,0,44,45,1,0,0,0,45,46,
        1,0,0,0,46,47,3,10,5,0,47,5,1,0,0,0,48,49,5,41,0,0,49,50,5,44,0,
        0,50,51,3,18,9,0,51,52,5,45,0,0,52,7,1,0,0,0,53,54,5,3,0,0,54,55,
        5,4,0,0,55,56,5,44,0,0,56,57,3,18,9,0,57,58,5,45,0,0,58,9,1,0,0,
        0,59,60,3,12,6,0,60,61,5,5,0,0,61,62,3,12,6,0,62,69,1,0,0,0,63,64,
        3,12,6,0,64,65,5,6,0,0,65,66,3,12,6,0,66,69,1,0,0,0,67,69,3,12,6,
        0,68,59,1,0,0,0,68,63,1,0,0,0,68,67,1,0,0,0,69,11,1,0,0,0,70,72,
        3,14,7,0,71,70,1,0,0,0,72,73,1,0,0,0,73,71,1,0,0,0,73,74,1,0,0,0,
        74,13,1,0,0,0,75,80,3,16,8,0,76,77,5,7,0,0,77,80,5,53,0,0,78,80,
        3,22,11,0,79,75,1,0,0,0,79,76,1,0,0,0,79,78,1,0,0,0,80,15,1,0,0,
        0,81,82,5,44,0,0,82,83,3,18,9,0,83,84,5,45,0,0,84,94,1,0,0,0,85,
        86,5,46,0,0,86,87,3,18,9,0,87,88,5,47,0,0,88,94,1,0,0,0,89,90,5,
        48,0,0,90,91,3,18,9,0,91,92,5,49,0,0,92,94,1,0,0,0,93,81,1,0,0,0,
        93,85,1,0,0,0,93,89,1,0,0,0,94,17,1,0,0,0,95,97,3,20,10,0,96,95,
        1,0,0,0,97,100,1,0,0,0,98,96,1,0,0,0,98,99,1,0,0,0,99,19,1,0,0,0,
        100,98,1,0,0,0,101,106,3,16,8,0,102,103,5,7,0,0,103,106,5,53,0,0,
        104,106,3,24,12,0,105,101,1,0,0,0,105,102,1,0,0,0,105,104,1,0,0,
        0,106,21,1,0,0,0,107,108,7,0,0,0,108,23,1,0,0,0,109,113,3,22,11,
        0,110,113,5,5,0,0,111,113,5,6,0,0,112,109,1,0,0,0,112,110,1,0,0,
        0,112,111,1,0,0,0,113,25,1,0,0,0,10,34,41,44,68,73,79,93,98,105,
        112
    ]

class SVAPropParser ( Parser ):

    grammarFileName = "SVAProp.g4"

    atn = ATNDeserializer().deserialize(serializedATN())

    decisionsToDFA = [ DFA(ds, i) for i, ds in enumerate(atn.decisionToState) ]

    sharedContextCache = PredictionContextCache()

    literalNames = [ "<INVALID>", "'assert'", "'property'", "'disable'", 
                     "'iff'", "'|->'", "'|=>'", "'##'", "'&&'", "'||'", 
                     "'->'", "'==='", "'!=='", "'==?'", "'!=?'", "'=='", 
                     "'!='", "'>='", "'<='", "'>>>'", "'>>'", "'<<<'", "'<<'", 
                     "'**'", "'~^'", "'^~'", "'!'", "'&'", "'|'", "'~'", 
                     "'^'", "'<'", "'>'", "'+'", "'-'", "'*'", "'/'", "'%'", 
                     "'.'", "','", "'?'", "'@'", "'''", "'#'", "'('", "')'", 
                     "'['", "']'", "'{'", "'}'", "':'", "';'" ]

    symbolicNames = [ "<INVALID>", "KW_ASSERT", "KW_PROPERTY", "KW_DISABLE", 
                      "KW_IFF", "OVERLAP_IMP", "NONOVERLAP_IMP", "DELAY", 
                      "LOGIC_AND", "LOGIC_OR", "LOGIC_IMPL", "CASE_EQ", 
                      "CASE_NEQ", "WEQ", "WNEQ", "EQ", "NEQ", "GEQ", "LEQ", 
                      "ARSHIFT", "RSHIFT", "LSHIFT_A", "LSHIFT", "POWER", 
                      "BIT_XNOR", "BIT_XNOR2", "LOGIC_NOT", "BIT_AND", "BIT_OR", 
                      "BIT_NOT", "BIT_XOR", "LT", "GT", "PLUS", "MINUS", 
                      "STAR", "DIV", "PERCENT", "DOT", "COMMA", "QUESTION", 
                      "AT", "TICK", "HASH", "LPAREN", "RPAREN", "LBRACKET", 
                      "RBRACKET", "LBRACE", "RBRACE", "COLON", "SEMI", "ID", 
                      "DEC_NUMBER", "WS", "SL_COMMENT", "ML_COMMENT" ]

    RULE_assertion = 0
    RULE_label = 1
    RULE_propSpec = 2
    RULE_clockEvent = 3
    RULE_disableIff = 4
    RULE_propBody = 5
    RULE_expr = 6
    RULE_exprAtom = 7
    RULE_group = 8
    RULE_innerContent = 9
    RULE_innerAtom = 10
    RULE_outerToken = 11
    RULE_innerToken = 12

    ruleNames =  [ "assertion", "label", "propSpec", "clockEvent", "disableIff", 
                   "propBody", "expr", "exprAtom", "group", "innerContent", 
                   "innerAtom", "outerToken", "innerToken" ]

    EOF = Token.EOF
    KW_ASSERT=1
    KW_PROPERTY=2
    KW_DISABLE=3
    KW_IFF=4
    OVERLAP_IMP=5
    NONOVERLAP_IMP=6
    DELAY=7
    LOGIC_AND=8
    LOGIC_OR=9
    LOGIC_IMPL=10
    CASE_EQ=11
    CASE_NEQ=12
    WEQ=13
    WNEQ=14
    EQ=15
    NEQ=16
    GEQ=17
    LEQ=18
    ARSHIFT=19
    RSHIFT=20
    LSHIFT_A=21
    LSHIFT=22
    POWER=23
    BIT_XNOR=24
    BIT_XNOR2=25
    LOGIC_NOT=26
    BIT_AND=27
    BIT_OR=28
    BIT_NOT=29
    BIT_XOR=30
    LT=31
    GT=32
    PLUS=33
    MINUS=34
    STAR=35
    DIV=36
    PERCENT=37
    DOT=38
    COMMA=39
    QUESTION=40
    AT=41
    TICK=42
    HASH=43
    LPAREN=44
    RPAREN=45
    LBRACKET=46
    RBRACKET=47
    LBRACE=48
    RBRACE=49
    COLON=50
    SEMI=51
    ID=52
    DEC_NUMBER=53
    WS=54
    SL_COMMENT=55
    ML_COMMENT=56

    def __init__(self, input:TokenStream, output:TextIO = sys.stdout):
        super().__init__(input, output)
        self.checkVersion("4.13.2")
        self._interp = ParserATNSimulator(self, self.atn, self.decisionsToDFA, self.sharedContextCache)
        self._predicates = None




    class AssertionContext(ParserRuleContext):
        __slots__ = 'parser'

        def __init__(self, parser, parent:ParserRuleContext=None, invokingState:int=-1):
            super().__init__(parent, invokingState)
            self.parser = parser

        def label(self):
            return self.getTypedRuleContext(SVAPropParser.LabelContext,0)


        def COLON(self):
            return self.getToken(SVAPropParser.COLON, 0)

        def KW_ASSERT(self):
            return self.getToken(SVAPropParser.KW_ASSERT, 0)

        def KW_PROPERTY(self):
            return self.getToken(SVAPropParser.KW_PROPERTY, 0)

        def LPAREN(self):
            return self.getToken(SVAPropParser.LPAREN, 0)

        def propSpec(self):
            return self.getTypedRuleContext(SVAPropParser.PropSpecContext,0)


        def RPAREN(self):
            return self.getToken(SVAPropParser.RPAREN, 0)

        def EOF(self):
            return self.getToken(SVAPropParser.EOF, 0)

        def SEMI(self):
            return self.getToken(SVAPropParser.SEMI, 0)

        def getRuleIndex(self):
            return SVAPropParser.RULE_assertion

        def enterRule(self, listener:ParseTreeListener):
            if hasattr( listener, "enterAssertion" ):
                listener.enterAssertion(self)

        def exitRule(self, listener:ParseTreeListener):
            if hasattr( listener, "exitAssertion" ):
                listener.exitAssertion(self)

        def accept(self, visitor:ParseTreeVisitor):
            if hasattr( visitor, "visitAssertion" ):
                return visitor.visitAssertion(self)
            else:
                return visitor.visitChildren(self)




    def assertion(self):

        localctx = SVAPropParser.AssertionContext(self, self._ctx, self.state)
        self.enterRule(localctx, 0, self.RULE_assertion)
        self._la = 0 # Token type
        try:
            self.enterOuterAlt(localctx, 1)
            self.state = 26
            self.label()
            self.state = 27
            self.match(SVAPropParser.COLON)
            self.state = 28
            self.match(SVAPropParser.KW_ASSERT)
            self.state = 29
            self.match(SVAPropParser.KW_PROPERTY)
            self.state = 30
            self.match(SVAPropParser.LPAREN)
            self.state = 31
            self.propSpec()
            self.state = 32
            self.match(SVAPropParser.RPAREN)
            self.state = 34
            self._errHandler.sync(self)
            _la = self._input.LA(1)
            if _la==51:
                self.state = 33
                self.match(SVAPropParser.SEMI)


            self.state = 36
            self.match(SVAPropParser.EOF)
        except RecognitionException as re:
            localctx.exception = re
            self._errHandler.reportError(self, re)
            self._errHandler.recover(self, re)
        finally:
            self.exitRule()
        return localctx


    class LabelContext(ParserRuleContext):
        __slots__ = 'parser'

        def __init__(self, parser, parent:ParserRuleContext=None, invokingState:int=-1):
            super().__init__(parent, invokingState)
            self.parser = parser

        def ID(self):
            return self.getToken(SVAPropParser.ID, 0)

        def getRuleIndex(self):
            return SVAPropParser.RULE_label

        def enterRule(self, listener:ParseTreeListener):
            if hasattr( listener, "enterLabel" ):
                listener.enterLabel(self)

        def exitRule(self, listener:ParseTreeListener):
            if hasattr( listener, "exitLabel" ):
                listener.exitLabel(self)

        def accept(self, visitor:ParseTreeVisitor):
            if hasattr( visitor, "visitLabel" ):
                return visitor.visitLabel(self)
            else:
                return visitor.visitChildren(self)




    def label(self):

        localctx = SVAPropParser.LabelContext(self, self._ctx, self.state)
        self.enterRule(localctx, 2, self.RULE_label)
        try:
            self.enterOuterAlt(localctx, 1)
            self.state = 38
            self.match(SVAPropParser.ID)
        except RecognitionException as re:
            localctx.exception = re
            self._errHandler.reportError(self, re)
            self._errHandler.recover(self, re)
        finally:
            self.exitRule()
        return localctx


    class PropSpecContext(ParserRuleContext):
        __slots__ = 'parser'

        def __init__(self, parser, parent:ParserRuleContext=None, invokingState:int=-1):
            super().__init__(parent, invokingState)
            self.parser = parser

        def propBody(self):
            return self.getTypedRuleContext(SVAPropParser.PropBodyContext,0)


        def clockEvent(self):
            return self.getTypedRuleContext(SVAPropParser.ClockEventContext,0)


        def disableIff(self):
            return self.getTypedRuleContext(SVAPropParser.DisableIffContext,0)


        def getRuleIndex(self):
            return SVAPropParser.RULE_propSpec

        def enterRule(self, listener:ParseTreeListener):
            if hasattr( listener, "enterPropSpec" ):
                listener.enterPropSpec(self)

        def exitRule(self, listener:ParseTreeListener):
            if hasattr( listener, "exitPropSpec" ):
                listener.exitPropSpec(self)

        def accept(self, visitor:ParseTreeVisitor):
            if hasattr( visitor, "visitPropSpec" ):
                return visitor.visitPropSpec(self)
            else:
                return visitor.visitChildren(self)




    def propSpec(self):

        localctx = SVAPropParser.PropSpecContext(self, self._ctx, self.state)
        self.enterRule(localctx, 4, self.RULE_propSpec)
        self._la = 0 # Token type
        try:
            self.enterOuterAlt(localctx, 1)
            self.state = 41
            self._errHandler.sync(self)
            la_ = self._interp.adaptivePredict(self._input,1,self._ctx)
            if la_ == 1:
                self.state = 40
                self.clockEvent()


            self.state = 44
            self._errHandler.sync(self)
            _la = self._input.LA(1)
            if _la==3:
                self.state = 43
                self.disableIff()


            self.state = 46
            self.propBody()
        except RecognitionException as re:
            localctx.exception = re
            self._errHandler.reportError(self, re)
            self._errHandler.recover(self, re)
        finally:
            self.exitRule()
        return localctx


    class ClockEventContext(ParserRuleContext):
        __slots__ = 'parser'

        def __init__(self, parser, parent:ParserRuleContext=None, invokingState:int=-1):
            super().__init__(parent, invokingState)
            self.parser = parser

        def AT(self):
            return self.getToken(SVAPropParser.AT, 0)

        def LPAREN(self):
            return self.getToken(SVAPropParser.LPAREN, 0)

        def innerContent(self):
            return self.getTypedRuleContext(SVAPropParser.InnerContentContext,0)


        def RPAREN(self):
            return self.getToken(SVAPropParser.RPAREN, 0)

        def getRuleIndex(self):
            return SVAPropParser.RULE_clockEvent

        def enterRule(self, listener:ParseTreeListener):
            if hasattr( listener, "enterClockEvent" ):
                listener.enterClockEvent(self)

        def exitRule(self, listener:ParseTreeListener):
            if hasattr( listener, "exitClockEvent" ):
                listener.exitClockEvent(self)

        def accept(self, visitor:ParseTreeVisitor):
            if hasattr( visitor, "visitClockEvent" ):
                return visitor.visitClockEvent(self)
            else:
                return visitor.visitChildren(self)




    def clockEvent(self):

        localctx = SVAPropParser.ClockEventContext(self, self._ctx, self.state)
        self.enterRule(localctx, 6, self.RULE_clockEvent)
        try:
            self.enterOuterAlt(localctx, 1)
            self.state = 48
            self.match(SVAPropParser.AT)
            self.state = 49
            self.match(SVAPropParser.LPAREN)
            self.state = 50
            self.innerContent()
            self.state = 51
            self.match(SVAPropParser.RPAREN)
        except RecognitionException as re:
            localctx.exception = re
            self._errHandler.reportError(self, re)
            self._errHandler.recover(self, re)
        finally:
            self.exitRule()
        return localctx


    class DisableIffContext(ParserRuleContext):
        __slots__ = 'parser'

        def __init__(self, parser, parent:ParserRuleContext=None, invokingState:int=-1):
            super().__init__(parent, invokingState)
            self.parser = parser

        def KW_DISABLE(self):
            return self.getToken(SVAPropParser.KW_DISABLE, 0)

        def KW_IFF(self):
            return self.getToken(SVAPropParser.KW_IFF, 0)

        def LPAREN(self):
            return self.getToken(SVAPropParser.LPAREN, 0)

        def innerContent(self):
            return self.getTypedRuleContext(SVAPropParser.InnerContentContext,0)


        def RPAREN(self):
            return self.getToken(SVAPropParser.RPAREN, 0)

        def getRuleIndex(self):
            return SVAPropParser.RULE_disableIff

        def enterRule(self, listener:ParseTreeListener):
            if hasattr( listener, "enterDisableIff" ):
                listener.enterDisableIff(self)

        def exitRule(self, listener:ParseTreeListener):
            if hasattr( listener, "exitDisableIff" ):
                listener.exitDisableIff(self)

        def accept(self, visitor:ParseTreeVisitor):
            if hasattr( visitor, "visitDisableIff" ):
                return visitor.visitDisableIff(self)
            else:
                return visitor.visitChildren(self)




    def disableIff(self):

        localctx = SVAPropParser.DisableIffContext(self, self._ctx, self.state)
        self.enterRule(localctx, 8, self.RULE_disableIff)
        try:
            self.enterOuterAlt(localctx, 1)
            self.state = 53
            self.match(SVAPropParser.KW_DISABLE)
            self.state = 54
            self.match(SVAPropParser.KW_IFF)
            self.state = 55
            self.match(SVAPropParser.LPAREN)
            self.state = 56
            self.innerContent()
            self.state = 57
            self.match(SVAPropParser.RPAREN)
        except RecognitionException as re:
            localctx.exception = re
            self._errHandler.reportError(self, re)
            self._errHandler.recover(self, re)
        finally:
            self.exitRule()
        return localctx


    class PropBodyContext(ParserRuleContext):
        __slots__ = 'parser'

        def __init__(self, parser, parent:ParserRuleContext=None, invokingState:int=-1):
            super().__init__(parent, invokingState)
            self.parser = parser


        def getRuleIndex(self):
            return SVAPropParser.RULE_propBody

     
        def copyFrom(self, ctx:ParserRuleContext):
            super().copyFrom(ctx)



    class NoImplicationContext(PropBodyContext):

        def __init__(self, parser, ctx:ParserRuleContext): # actually a SVAPropParser.PropBodyContext
            super().__init__(parser)
            self.copyFrom(ctx)

        def expr(self):
            return self.getTypedRuleContext(SVAPropParser.ExprContext,0)


        def enterRule(self, listener:ParseTreeListener):
            if hasattr( listener, "enterNoImplication" ):
                listener.enterNoImplication(self)

        def exitRule(self, listener:ParseTreeListener):
            if hasattr( listener, "exitNoImplication" ):
                listener.exitNoImplication(self)

        def accept(self, visitor:ParseTreeVisitor):
            if hasattr( visitor, "visitNoImplication" ):
                return visitor.visitNoImplication(self)
            else:
                return visitor.visitChildren(self)


    class OverlapImplicationContext(PropBodyContext):

        def __init__(self, parser, ctx:ParserRuleContext): # actually a SVAPropParser.PropBodyContext
            super().__init__(parser)
            self.copyFrom(ctx)

        def expr(self, i:int=None):
            if i is None:
                return self.getTypedRuleContexts(SVAPropParser.ExprContext)
            else:
                return self.getTypedRuleContext(SVAPropParser.ExprContext,i)

        def OVERLAP_IMP(self):
            return self.getToken(SVAPropParser.OVERLAP_IMP, 0)

        def enterRule(self, listener:ParseTreeListener):
            if hasattr( listener, "enterOverlapImplication" ):
                listener.enterOverlapImplication(self)

        def exitRule(self, listener:ParseTreeListener):
            if hasattr( listener, "exitOverlapImplication" ):
                listener.exitOverlapImplication(self)

        def accept(self, visitor:ParseTreeVisitor):
            if hasattr( visitor, "visitOverlapImplication" ):
                return visitor.visitOverlapImplication(self)
            else:
                return visitor.visitChildren(self)


    class NonOverlapImplicationContext(PropBodyContext):

        def __init__(self, parser, ctx:ParserRuleContext): # actually a SVAPropParser.PropBodyContext
            super().__init__(parser)
            self.copyFrom(ctx)

        def expr(self, i:int=None):
            if i is None:
                return self.getTypedRuleContexts(SVAPropParser.ExprContext)
            else:
                return self.getTypedRuleContext(SVAPropParser.ExprContext,i)

        def NONOVERLAP_IMP(self):
            return self.getToken(SVAPropParser.NONOVERLAP_IMP, 0)

        def enterRule(self, listener:ParseTreeListener):
            if hasattr( listener, "enterNonOverlapImplication" ):
                listener.enterNonOverlapImplication(self)

        def exitRule(self, listener:ParseTreeListener):
            if hasattr( listener, "exitNonOverlapImplication" ):
                listener.exitNonOverlapImplication(self)

        def accept(self, visitor:ParseTreeVisitor):
            if hasattr( visitor, "visitNonOverlapImplication" ):
                return visitor.visitNonOverlapImplication(self)
            else:
                return visitor.visitChildren(self)



    def propBody(self):

        localctx = SVAPropParser.PropBodyContext(self, self._ctx, self.state)
        self.enterRule(localctx, 10, self.RULE_propBody)
        try:
            self.state = 68
            self._errHandler.sync(self)
            la_ = self._interp.adaptivePredict(self._input,3,self._ctx)
            if la_ == 1:
                localctx = SVAPropParser.OverlapImplicationContext(self, localctx)
                self.enterOuterAlt(localctx, 1)
                self.state = 59
                self.expr()
                self.state = 60
                self.match(SVAPropParser.OVERLAP_IMP)
                self.state = 61
                self.expr()
                pass

            elif la_ == 2:
                localctx = SVAPropParser.NonOverlapImplicationContext(self, localctx)
                self.enterOuterAlt(localctx, 2)
                self.state = 63
                self.expr()
                self.state = 64
                self.match(SVAPropParser.NONOVERLAP_IMP)
                self.state = 65
                self.expr()
                pass

            elif la_ == 3:
                localctx = SVAPropParser.NoImplicationContext(self, localctx)
                self.enterOuterAlt(localctx, 3)
                self.state = 67
                self.expr()
                pass


        except RecognitionException as re:
            localctx.exception = re
            self._errHandler.reportError(self, re)
            self._errHandler.recover(self, re)
        finally:
            self.exitRule()
        return localctx


    class ExprContext(ParserRuleContext):
        __slots__ = 'parser'

        def __init__(self, parser, parent:ParserRuleContext=None, invokingState:int=-1):
            super().__init__(parent, invokingState)
            self.parser = parser

        def exprAtom(self, i:int=None):
            if i is None:
                return self.getTypedRuleContexts(SVAPropParser.ExprAtomContext)
            else:
                return self.getTypedRuleContext(SVAPropParser.ExprAtomContext,i)


        def getRuleIndex(self):
            return SVAPropParser.RULE_expr

        def enterRule(self, listener:ParseTreeListener):
            if hasattr( listener, "enterExpr" ):
                listener.enterExpr(self)

        def exitRule(self, listener:ParseTreeListener):
            if hasattr( listener, "exitExpr" ):
                listener.exitExpr(self)

        def accept(self, visitor:ParseTreeVisitor):
            if hasattr( visitor, "visitExpr" ):
                return visitor.visitExpr(self)
            else:
                return visitor.visitChildren(self)




    def expr(self):

        localctx = SVAPropParser.ExprContext(self, self._ctx, self.state)
        self.enterRule(localctx, 12, self.RULE_expr)
        self._la = 0 # Token type
        try:
            self.enterOuterAlt(localctx, 1)
            self.state = 71 
            self._errHandler.sync(self)
            _la = self._input.LA(1)
            while True:
                self.state = 70
                self.exprAtom()
                self.state = 73 
                self._errHandler.sync(self)
                _la = self._input.LA(1)
                if not ((((_la) & ~0x3f) == 0 and ((1 << _la) & 15023726881931136) != 0)):
                    break

        except RecognitionException as re:
            localctx.exception = re
            self._errHandler.reportError(self, re)
            self._errHandler.recover(self, re)
        finally:
            self.exitRule()
        return localctx


    class ExprAtomContext(ParserRuleContext):
        __slots__ = 'parser'

        def __init__(self, parser, parent:ParserRuleContext=None, invokingState:int=-1):
            super().__init__(parent, invokingState)
            self.parser = parser

        def group(self):
            return self.getTypedRuleContext(SVAPropParser.GroupContext,0)


        def DELAY(self):
            return self.getToken(SVAPropParser.DELAY, 0)

        def DEC_NUMBER(self):
            return self.getToken(SVAPropParser.DEC_NUMBER, 0)

        def outerToken(self):
            return self.getTypedRuleContext(SVAPropParser.OuterTokenContext,0)


        def getRuleIndex(self):
            return SVAPropParser.RULE_exprAtom

        def enterRule(self, listener:ParseTreeListener):
            if hasattr( listener, "enterExprAtom" ):
                listener.enterExprAtom(self)

        def exitRule(self, listener:ParseTreeListener):
            if hasattr( listener, "exitExprAtom" ):
                listener.exitExprAtom(self)

        def accept(self, visitor:ParseTreeVisitor):
            if hasattr( visitor, "visitExprAtom" ):
                return visitor.visitExprAtom(self)
            else:
                return visitor.visitChildren(self)




    def exprAtom(self):

        localctx = SVAPropParser.ExprAtomContext(self, self._ctx, self.state)
        self.enterRule(localctx, 14, self.RULE_exprAtom)
        try:
            self.state = 79
            self._errHandler.sync(self)
            token = self._input.LA(1)
            if token in [44, 46, 48]:
                self.enterOuterAlt(localctx, 1)
                self.state = 75
                self.group()
                pass
            elif token in [7]:
                self.enterOuterAlt(localctx, 2)
                self.state = 76
                self.match(SVAPropParser.DELAY)
                self.state = 77
                self.match(SVAPropParser.DEC_NUMBER)
                pass
            elif token in [8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 50, 52, 53]:
                self.enterOuterAlt(localctx, 3)
                self.state = 78
                self.outerToken()
                pass
            else:
                raise NoViableAltException(self)

        except RecognitionException as re:
            localctx.exception = re
            self._errHandler.reportError(self, re)
            self._errHandler.recover(self, re)
        finally:
            self.exitRule()
        return localctx


    class GroupContext(ParserRuleContext):
        __slots__ = 'parser'

        def __init__(self, parser, parent:ParserRuleContext=None, invokingState:int=-1):
            super().__init__(parent, invokingState)
            self.parser = parser

        def LPAREN(self):
            return self.getToken(SVAPropParser.LPAREN, 0)

        def innerContent(self):
            return self.getTypedRuleContext(SVAPropParser.InnerContentContext,0)


        def RPAREN(self):
            return self.getToken(SVAPropParser.RPAREN, 0)

        def LBRACKET(self):
            return self.getToken(SVAPropParser.LBRACKET, 0)

        def RBRACKET(self):
            return self.getToken(SVAPropParser.RBRACKET, 0)

        def LBRACE(self):
            return self.getToken(SVAPropParser.LBRACE, 0)

        def RBRACE(self):
            return self.getToken(SVAPropParser.RBRACE, 0)

        def getRuleIndex(self):
            return SVAPropParser.RULE_group

        def enterRule(self, listener:ParseTreeListener):
            if hasattr( listener, "enterGroup" ):
                listener.enterGroup(self)

        def exitRule(self, listener:ParseTreeListener):
            if hasattr( listener, "exitGroup" ):
                listener.exitGroup(self)

        def accept(self, visitor:ParseTreeVisitor):
            if hasattr( visitor, "visitGroup" ):
                return visitor.visitGroup(self)
            else:
                return visitor.visitChildren(self)




    def group(self):

        localctx = SVAPropParser.GroupContext(self, self._ctx, self.state)
        self.enterRule(localctx, 16, self.RULE_group)
        try:
            self.state = 93
            self._errHandler.sync(self)
            token = self._input.LA(1)
            if token in [44]:
                self.enterOuterAlt(localctx, 1)
                self.state = 81
                self.match(SVAPropParser.LPAREN)
                self.state = 82
                self.innerContent()
                self.state = 83
                self.match(SVAPropParser.RPAREN)
                pass
            elif token in [46]:
                self.enterOuterAlt(localctx, 2)
                self.state = 85
                self.match(SVAPropParser.LBRACKET)
                self.state = 86
                self.innerContent()
                self.state = 87
                self.match(SVAPropParser.RBRACKET)
                pass
            elif token in [48]:
                self.enterOuterAlt(localctx, 3)
                self.state = 89
                self.match(SVAPropParser.LBRACE)
                self.state = 90
                self.innerContent()
                self.state = 91
                self.match(SVAPropParser.RBRACE)
                pass
            else:
                raise NoViableAltException(self)

        except RecognitionException as re:
            localctx.exception = re
            self._errHandler.reportError(self, re)
            self._errHandler.recover(self, re)
        finally:
            self.exitRule()
        return localctx


    class InnerContentContext(ParserRuleContext):
        __slots__ = 'parser'

        def __init__(self, parser, parent:ParserRuleContext=None, invokingState:int=-1):
            super().__init__(parent, invokingState)
            self.parser = parser

        def innerAtom(self, i:int=None):
            if i is None:
                return self.getTypedRuleContexts(SVAPropParser.InnerAtomContext)
            else:
                return self.getTypedRuleContext(SVAPropParser.InnerAtomContext,i)


        def getRuleIndex(self):
            return SVAPropParser.RULE_innerContent

        def enterRule(self, listener:ParseTreeListener):
            if hasattr( listener, "enterInnerContent" ):
                listener.enterInnerContent(self)

        def exitRule(self, listener:ParseTreeListener):
            if hasattr( listener, "exitInnerContent" ):
                listener.exitInnerContent(self)

        def accept(self, visitor:ParseTreeVisitor):
            if hasattr( visitor, "visitInnerContent" ):
                return visitor.visitInnerContent(self)
            else:
                return visitor.visitChildren(self)




    def innerContent(self):

        localctx = SVAPropParser.InnerContentContext(self, self._ctx, self.state)
        self.enterRule(localctx, 18, self.RULE_innerContent)
        self._la = 0 # Token type
        try:
            self.enterOuterAlt(localctx, 1)
            self.state = 98
            self._errHandler.sync(self)
            _la = self._input.LA(1)
            while (((_la) & ~0x3f) == 0 and ((1 << _la) & 15023726881931232) != 0):
                self.state = 95
                self.innerAtom()
                self.state = 100
                self._errHandler.sync(self)
                _la = self._input.LA(1)

        except RecognitionException as re:
            localctx.exception = re
            self._errHandler.reportError(self, re)
            self._errHandler.recover(self, re)
        finally:
            self.exitRule()
        return localctx


    class InnerAtomContext(ParserRuleContext):
        __slots__ = 'parser'

        def __init__(self, parser, parent:ParserRuleContext=None, invokingState:int=-1):
            super().__init__(parent, invokingState)
            self.parser = parser

        def group(self):
            return self.getTypedRuleContext(SVAPropParser.GroupContext,0)


        def DELAY(self):
            return self.getToken(SVAPropParser.DELAY, 0)

        def DEC_NUMBER(self):
            return self.getToken(SVAPropParser.DEC_NUMBER, 0)

        def innerToken(self):
            return self.getTypedRuleContext(SVAPropParser.InnerTokenContext,0)


        def getRuleIndex(self):
            return SVAPropParser.RULE_innerAtom

        def enterRule(self, listener:ParseTreeListener):
            if hasattr( listener, "enterInnerAtom" ):
                listener.enterInnerAtom(self)

        def exitRule(self, listener:ParseTreeListener):
            if hasattr( listener, "exitInnerAtom" ):
                listener.exitInnerAtom(self)

        def accept(self, visitor:ParseTreeVisitor):
            if hasattr( visitor, "visitInnerAtom" ):
                return visitor.visitInnerAtom(self)
            else:
                return visitor.visitChildren(self)




    def innerAtom(self):

        localctx = SVAPropParser.InnerAtomContext(self, self._ctx, self.state)
        self.enterRule(localctx, 20, self.RULE_innerAtom)
        try:
            self.state = 105
            self._errHandler.sync(self)
            token = self._input.LA(1)
            if token in [44, 46, 48]:
                self.enterOuterAlt(localctx, 1)
                self.state = 101
                self.group()
                pass
            elif token in [7]:
                self.enterOuterAlt(localctx, 2)
                self.state = 102
                self.match(SVAPropParser.DELAY)
                self.state = 103
                self.match(SVAPropParser.DEC_NUMBER)
                pass
            elif token in [5, 6, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 50, 52, 53]:
                self.enterOuterAlt(localctx, 3)
                self.state = 104
                self.innerToken()
                pass
            else:
                raise NoViableAltException(self)

        except RecognitionException as re:
            localctx.exception = re
            self._errHandler.reportError(self, re)
            self._errHandler.recover(self, re)
        finally:
            self.exitRule()
        return localctx


    class OuterTokenContext(ParserRuleContext):
        __slots__ = 'parser'

        def __init__(self, parser, parent:ParserRuleContext=None, invokingState:int=-1):
            super().__init__(parent, invokingState)
            self.parser = parser

        def ID(self):
            return self.getToken(SVAPropParser.ID, 0)

        def DEC_NUMBER(self):
            return self.getToken(SVAPropParser.DEC_NUMBER, 0)

        def LOGIC_NOT(self):
            return self.getToken(SVAPropParser.LOGIC_NOT, 0)

        def LOGIC_AND(self):
            return self.getToken(SVAPropParser.LOGIC_AND, 0)

        def LOGIC_OR(self):
            return self.getToken(SVAPropParser.LOGIC_OR, 0)

        def LOGIC_IMPL(self):
            return self.getToken(SVAPropParser.LOGIC_IMPL, 0)

        def BIT_AND(self):
            return self.getToken(SVAPropParser.BIT_AND, 0)

        def BIT_OR(self):
            return self.getToken(SVAPropParser.BIT_OR, 0)

        def BIT_NOT(self):
            return self.getToken(SVAPropParser.BIT_NOT, 0)

        def BIT_XOR(self):
            return self.getToken(SVAPropParser.BIT_XOR, 0)

        def BIT_XNOR(self):
            return self.getToken(SVAPropParser.BIT_XNOR, 0)

        def BIT_XNOR2(self):
            return self.getToken(SVAPropParser.BIT_XNOR2, 0)

        def CASE_EQ(self):
            return self.getToken(SVAPropParser.CASE_EQ, 0)

        def CASE_NEQ(self):
            return self.getToken(SVAPropParser.CASE_NEQ, 0)

        def WEQ(self):
            return self.getToken(SVAPropParser.WEQ, 0)

        def WNEQ(self):
            return self.getToken(SVAPropParser.WNEQ, 0)

        def EQ(self):
            return self.getToken(SVAPropParser.EQ, 0)

        def NEQ(self):
            return self.getToken(SVAPropParser.NEQ, 0)

        def GEQ(self):
            return self.getToken(SVAPropParser.GEQ, 0)

        def LEQ(self):
            return self.getToken(SVAPropParser.LEQ, 0)

        def ARSHIFT(self):
            return self.getToken(SVAPropParser.ARSHIFT, 0)

        def RSHIFT(self):
            return self.getToken(SVAPropParser.RSHIFT, 0)

        def LSHIFT_A(self):
            return self.getToken(SVAPropParser.LSHIFT_A, 0)

        def LSHIFT(self):
            return self.getToken(SVAPropParser.LSHIFT, 0)

        def LT(self):
            return self.getToken(SVAPropParser.LT, 0)

        def GT(self):
            return self.getToken(SVAPropParser.GT, 0)

        def PLUS(self):
            return self.getToken(SVAPropParser.PLUS, 0)

        def MINUS(self):
            return self.getToken(SVAPropParser.MINUS, 0)

        def STAR(self):
            return self.getToken(SVAPropParser.STAR, 0)

        def DIV(self):
            return self.getToken(SVAPropParser.DIV, 0)

        def PERCENT(self):
            return self.getToken(SVAPropParser.PERCENT, 0)

        def POWER(self):
            return self.getToken(SVAPropParser.POWER, 0)

        def DOT(self):
            return self.getToken(SVAPropParser.DOT, 0)

        def COMMA(self):
            return self.getToken(SVAPropParser.COMMA, 0)

        def QUESTION(self):
            return self.getToken(SVAPropParser.QUESTION, 0)

        def COLON(self):
            return self.getToken(SVAPropParser.COLON, 0)

        def TICK(self):
            return self.getToken(SVAPropParser.TICK, 0)

        def AT(self):
            return self.getToken(SVAPropParser.AT, 0)

        def HASH(self):
            return self.getToken(SVAPropParser.HASH, 0)

        def getRuleIndex(self):
            return SVAPropParser.RULE_outerToken

        def enterRule(self, listener:ParseTreeListener):
            if hasattr( listener, "enterOuterToken" ):
                listener.enterOuterToken(self)

        def exitRule(self, listener:ParseTreeListener):
            if hasattr( listener, "exitOuterToken" ):
                listener.exitOuterToken(self)

        def accept(self, visitor:ParseTreeVisitor):
            if hasattr( visitor, "visitOuterToken" ):
                return visitor.visitOuterToken(self)
            else:
                return visitor.visitChildren(self)




    def outerToken(self):

        localctx = SVAPropParser.OuterTokenContext(self, self._ctx, self.state)
        self.enterRule(localctx, 22, self.RULE_outerToken)
        self._la = 0 # Token type
        try:
            self.enterOuterAlt(localctx, 1)
            self.state = 107
            _la = self._input.LA(1)
            if not((((_la) & ~0x3f) == 0 and ((1 << _la) & 14654290974998272) != 0)):
                self._errHandler.recoverInline(self)
            else:
                self._errHandler.reportMatch(self)
                self.consume()
        except RecognitionException as re:
            localctx.exception = re
            self._errHandler.reportError(self, re)
            self._errHandler.recover(self, re)
        finally:
            self.exitRule()
        return localctx


    class InnerTokenContext(ParserRuleContext):
        __slots__ = 'parser'

        def __init__(self, parser, parent:ParserRuleContext=None, invokingState:int=-1):
            super().__init__(parent, invokingState)
            self.parser = parser

        def outerToken(self):
            return self.getTypedRuleContext(SVAPropParser.OuterTokenContext,0)


        def OVERLAP_IMP(self):
            return self.getToken(SVAPropParser.OVERLAP_IMP, 0)

        def NONOVERLAP_IMP(self):
            return self.getToken(SVAPropParser.NONOVERLAP_IMP, 0)

        def getRuleIndex(self):
            return SVAPropParser.RULE_innerToken

        def enterRule(self, listener:ParseTreeListener):
            if hasattr( listener, "enterInnerToken" ):
                listener.enterInnerToken(self)

        def exitRule(self, listener:ParseTreeListener):
            if hasattr( listener, "exitInnerToken" ):
                listener.exitInnerToken(self)

        def accept(self, visitor:ParseTreeVisitor):
            if hasattr( visitor, "visitInnerToken" ):
                return visitor.visitInnerToken(self)
            else:
                return visitor.visitChildren(self)




    def innerToken(self):

        localctx = SVAPropParser.InnerTokenContext(self, self._ctx, self.state)
        self.enterRule(localctx, 24, self.RULE_innerToken)
        try:
            self.state = 112
            self._errHandler.sync(self)
            token = self._input.LA(1)
            if token in [8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 50, 52, 53]:
                self.enterOuterAlt(localctx, 1)
                self.state = 109
                self.outerToken()
                pass
            elif token in [5]:
                self.enterOuterAlt(localctx, 2)
                self.state = 110
                self.match(SVAPropParser.OVERLAP_IMP)
                pass
            elif token in [6]:
                self.enterOuterAlt(localctx, 3)
                self.state = 111
                self.match(SVAPropParser.NONOVERLAP_IMP)
                pass
            else:
                raise NoViableAltException(self)

        except RecognitionException as re:
            localctx.exception = re
            self._errHandler.reportError(self, re)
            self._errHandler.recover(self, re)
        finally:
            self.exitRule()
        return localctx





