# Generated from SVAProp.g4 by ANTLR 4.13.2
from antlr4 import *
if "." in __name__:
    from .SVAPropParser import SVAPropParser
else:
    from SVAPropParser import SVAPropParser

# This class defines a complete generic visitor for a parse tree produced by SVAPropParser.

class SVAPropVisitor(ParseTreeVisitor):

    # Visit a parse tree produced by SVAPropParser#assertion.
    def visitAssertion(self, ctx:SVAPropParser.AssertionContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by SVAPropParser#label.
    def visitLabel(self, ctx:SVAPropParser.LabelContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by SVAPropParser#propSpec.
    def visitPropSpec(self, ctx:SVAPropParser.PropSpecContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by SVAPropParser#clockEvent.
    def visitClockEvent(self, ctx:SVAPropParser.ClockEventContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by SVAPropParser#disableIff.
    def visitDisableIff(self, ctx:SVAPropParser.DisableIffContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by SVAPropParser#overlapImplication.
    def visitOverlapImplication(self, ctx:SVAPropParser.OverlapImplicationContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by SVAPropParser#nonOverlapImplication.
    def visitNonOverlapImplication(self, ctx:SVAPropParser.NonOverlapImplicationContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by SVAPropParser#noImplication.
    def visitNoImplication(self, ctx:SVAPropParser.NoImplicationContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by SVAPropParser#expr.
    def visitExpr(self, ctx:SVAPropParser.ExprContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by SVAPropParser#exprAtom.
    def visitExprAtom(self, ctx:SVAPropParser.ExprAtomContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by SVAPropParser#group.
    def visitGroup(self, ctx:SVAPropParser.GroupContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by SVAPropParser#innerContent.
    def visitInnerContent(self, ctx:SVAPropParser.InnerContentContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by SVAPropParser#innerAtom.
    def visitInnerAtom(self, ctx:SVAPropParser.InnerAtomContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by SVAPropParser#outerToken.
    def visitOuterToken(self, ctx:SVAPropParser.OuterTokenContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by SVAPropParser#innerToken.
    def visitInnerToken(self, ctx:SVAPropParser.InnerTokenContext):
        return self.visitChildren(ctx)



del SVAPropParser