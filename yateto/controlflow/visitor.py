from ..ast.visitor import Visitor
from .graph import *
from ..memory import DenseMemoryLayout
from typing import List

class AST2ControlFlow(Visitor):
    TEMPORARY_RESULT = '_tmp'

    def __init__(self, simpleMemoryLayout=False):
        self._tmp: int = 0
        self._cfg: List[ProgramPoint] = []
        self._writable = set()
        self._simpleMemoryLayout = simpleMemoryLayout

    def cfg(self):
        return self._cfg + [ProgramPoint(None)]

    def _ml(self, node):
        return DenseMemoryLayout(node.shape()) if self._simpleMemoryLayout else node.memoryLayout()

    def generic_visit(self, node):
        variables = [self.visit(child) for child in node]

        result = self._nextTemporary(node)
        action = ProgramAction(result=result,
                               term=Expression(node, self._ml(node), variables),
                               add=False)
        self._addAction(action)

        return result

    def visit_Add(self, node):
        variables = [self.visit(child) for child in node]
        assert len(variables) > 1

        variables.sort(key=lambda var: int(not var.writable) + int(not var.isGlobal()))

        tmp = self._nextTemporary(node)
        add = False
        for var in variables:
            action = ProgramAction(result=tmp, term=var, add=add)
            self._addAction(action)
            add = True

        return tmp

    def visit_ScalarMultiplication(self, node):
        variable = self.visit(node.term())

        result = self._nextTemporary(node)
        action = ProgramAction(result=result, term=variable, add=False, scalar=node.scalar())
        self._addAction(action)

        return result

    def visit_Assign(self, node : Type[Node]):
        self._writable = self._writable | {node[0].name()}
        variables = [self.visit(child) for child in node]

        action = ProgramAction(result=variables[0], term=variables[1], add=False)
        self._addAction(action)

        return variables[0]

    def visit_IndexedTensor(self, node):
        return Variable(name=node.name(),
                        writable=node.name() in self._writable,
                        memoryLayout=self._ml(node),
                        eqspp=node.eqspp(),
                        tensor=node.tensor)

    def _addAction(self, action):
        self._cfg.append(ProgramPoint(action))

    def _nextTemporary(self, node):
        name = '{}{}'.format(self.TEMPORARY_RESULT, self._tmp)
        self._tmp += 1

        return Variable(name=name,
                        writable=True,
                        memoryLayout=self._ml(node),
                        eqspp=node.eqspp())


class SortedGlobalsList(object):
    def visit(self, cfg):
        V = set()
        for pp in cfg:
            if pp.action:
                V = V | pp.action.result.variables() | pp.action.variables()
        return sorted([var for var in V if var.isGlobal()], key=lambda x: str(x))


class SortedPrefetchList(object):
    def visit(self, cfg):
        V = set()
        for pp in cfg:
            if pp.action and pp.action.isRHSExpression() and pp.action.term.node.prefetch is not None:
                V = V | {pp.action.term.node.prefetch}
        return sorted([v for v in V], key=lambda x: x.name())


class ScalarsSet(object):
    def visit(self, cfg):
        S = set()
        for pp in cfg:
            if pp.action:
                if isinstance(pp.action.scalar, Scalar):
                    S = S | {pp.action.scalar}
        return S


class PrettyPrinter(object):
    def __init__(self, printPPState=False):
        self._printPPState = printPPState

    def visit(self, cfg):
        for programming_point in cfg:

            if self._printPPState:
                if programming_point.live:
                    print('L =', programming_point.live)
                if programming_point.initBuffer:
                    print('Init =', programming_point.initBuffer)


            if programming_point.action:
                actionRepr = str(programming_point.action.term)

                # adjust printing result of the term in case of scalar multiplication
                if programming_point.action.scalar is not None:
                    actionRepr = str(programming_point.action.scalar) + ' * ' + actionRepr

                # print programming point on the screen
                print('  {} {} {}'.format(programming_point.action.result,
                                          '+=' if programming_point.action.add else '=',
                                          actionRepr))

