# Generated from SVAProp.g4 by ANTLR 4.13.2
from antlr4 import *
if "." in __name__:
    from .SVAPropParser import SVAPropParser
else:
    from SVAPropParser import SVAPropParser

# This class defines a complete listener for a parse tree produced by SVAPropParser.
class SVAPropListener(ParseTreeListener):

    # Enter a parse tree produced by SVAPropParser#assertion.
    def enterAssertion(self, ctx:SVAPropParser.AssertionContext):
        pass

    # Exit a parse tree produced by SVAPropParser#assertion.
    def exitAssertion(self, ctx:SVAPropParser.AssertionContext):
        pass


    # Enter a parse tree produced by SVAPropParser#label.
    def enterLabel(self, ctx:SVAPropParser.LabelContext):
        pass

    # Exit a parse tree produced by SVAPropParser#label.
    def exitLabel(self, ctx:SVAPropParser.LabelContext):
        pass


    # Enter a parse tree produced by SVAPropParser#propSpec.
    def enterPropSpec(self, ctx:SVAPropParser.PropSpecContext):
        pass

    # Exit a parse tree produced by SVAPropParser#propSpec.
    def exitPropSpec(self, ctx:SVAPropParser.PropSpecContext):
        pass


    # Enter a parse tree produced by SVAPropParser#clockEvent.
    def enterClockEvent(self, ctx:SVAPropParser.ClockEventContext):
        pass

    # Exit a parse tree produced by SVAPropParser#clockEvent.
    def exitClockEvent(self, ctx:SVAPropParser.ClockEventContext):
        pass


    # Enter a parse tree produced by SVAPropParser#disableIff.
    def enterDisableIff(self, ctx:SVAPropParser.DisableIffContext):
        pass

    # Exit a parse tree produced by SVAPropParser#disableIff.
    def exitDisableIff(self, ctx:SVAPropParser.DisableIffContext):
        pass


    # Enter a parse tree produced by SVAPropParser#overlapImplication.
    def enterOverlapImplication(self, ctx:SVAPropParser.OverlapImplicationContext):
        pass

    # Exit a parse tree produced by SVAPropParser#overlapImplication.
    def exitOverlapImplication(self, ctx:SVAPropParser.OverlapImplicationContext):
        pass


    # Enter a parse tree produced by SVAPropParser#nonOverlapImplication.
    def enterNonOverlapImplication(self, ctx:SVAPropParser.NonOverlapImplicationContext):
        pass

    # Exit a parse tree produced by SVAPropParser#nonOverlapImplication.
    def exitNonOverlapImplication(self, ctx:SVAPropParser.NonOverlapImplicationContext):
        pass


    # Enter a parse tree produced by SVAPropParser#noImplication.
    def enterNoImplication(self, ctx:SVAPropParser.NoImplicationContext):
        pass

    # Exit a parse tree produced by SVAPropParser#noImplication.
    def exitNoImplication(self, ctx:SVAPropParser.NoImplicationContext):
        pass


    # Enter a parse tree produced by SVAPropParser#expr.
    def enterExpr(self, ctx:SVAPropParser.ExprContext):
        pass

    # Exit a parse tree produced by SVAPropParser#expr.
    def exitExpr(self, ctx:SVAPropParser.ExprContext):
        pass


    # Enter a parse tree produced by SVAPropParser#exprAtom.
    def enterExprAtom(self, ctx:SVAPropParser.ExprAtomContext):
        pass

    # Exit a parse tree produced by SVAPropParser#exprAtom.
    def exitExprAtom(self, ctx:SVAPropParser.ExprAtomContext):
        pass


    # Enter a parse tree produced by SVAPropParser#group.
    def enterGroup(self, ctx:SVAPropParser.GroupContext):
        pass

    # Exit a parse tree produced by SVAPropParser#group.
    def exitGroup(self, ctx:SVAPropParser.GroupContext):
        pass


    # Enter a parse tree produced by SVAPropParser#innerContent.
    def enterInnerContent(self, ctx:SVAPropParser.InnerContentContext):
        pass

    # Exit a parse tree produced by SVAPropParser#innerContent.
    def exitInnerContent(self, ctx:SVAPropParser.InnerContentContext):
        pass


    # Enter a parse tree produced by SVAPropParser#innerAtom.
    def enterInnerAtom(self, ctx:SVAPropParser.InnerAtomContext):
        pass

    # Exit a parse tree produced by SVAPropParser#innerAtom.
    def exitInnerAtom(self, ctx:SVAPropParser.InnerAtomContext):
        pass


    # Enter a parse tree produced by SVAPropParser#outerToken.
    def enterOuterToken(self, ctx:SVAPropParser.OuterTokenContext):
        pass

    # Exit a parse tree produced by SVAPropParser#outerToken.
    def exitOuterToken(self, ctx:SVAPropParser.OuterTokenContext):
        pass


    # Enter a parse tree produced by SVAPropParser#innerToken.
    def enterInnerToken(self, ctx:SVAPropParser.InnerTokenContext):
        pass

    # Exit a parse tree produced by SVAPropParser#innerToken.
    def exitInnerToken(self, ctx:SVAPropParser.InnerTokenContext):
        pass



del SVAPropParser