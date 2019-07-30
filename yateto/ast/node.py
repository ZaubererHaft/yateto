import re
from typing import Type, Any

from ..memory import DenseMemoryLayout
from .indices import BoundingBox, Indices, LoGCost
from abc import ABC, abstractmethod
from .. import aspp


class Node(ABC):
  def __init__(self):
    self.indices = None
    self._children = []
    self._eqspp = None
  
  def size(self):
    return self.indices.size()
  
  def shape(self):
    return self.indices.shape()
  
  @abstractmethod
  def nonZeroFlops(self):
    pass

  def __iter__(self):
    return iter(self._children)
  
  def __getitem__(self, key):
    return self._children[key]
  
  def __len__(self):
    return len(self._children)
  
  def setChildren(self, children):
    self._children = children

  def eqspp(self):
    return self._eqspp
  
  def setEqspp(self, spp):
    self._eqspp = spp

  def boundingBox(self):
    return BoundingBox.fromSpp(self._eqspp)
  
  @abstractmethod
  def memoryLayout(self):
    pass

  def argumentsCompatible(self, layouts):
    return True

  def resultCompatible(self, layout):
    return True

  def fixedIndexPermutation(self):
    return True

  @abstractmethod
  def setIndexPermutation(self, indices, permuteEqspp=True):
    pass

  def _checkMultipleScalarMults(self):
    """Checks whether the self object is not a type
    of ScalarMultiplication.

    If it is a case, the corresponding exception is raised

    Raises:
      ValueError: Multiple multiplications with scalars are not allowed
    """

    if isinstance(self, ScalarMultiplication):
      raise ValueError('Multiple multiplications with scalars are not allowed. Merge them into a single one.')


  def _binOp(self, other, opType):
    """Combines to objects (self and other) into one binary operation

    Args:
      other (Union[Type(Node), Type(Op)]):
      opType (Type(Op)): usually, it is an instance of Einsum class

    Returns:
      Type(Op): self, or other, or a new instance opType(self, other)

    """

    if isinstance(self, opType):
      if isinstance(other, opType):
        # Extend the children of the self object
        # by sticking the children of the OTHER object
        # if both "self" and "OTHER" belong to the opType
        # provided by the caller
        self._children.extend(other._children)
      else:
        # Attach the OTHER object to the list of children ot the self object
        # in case of the OTHER doesn't belong to the given opType
        self._children.append(other)

      # returb updated self object
      return self
    elif isinstance(other, opType):

      # Insert the self object to the list of children of the OTHER
      # object if only the OTHER object belongs to the provided opType
      other._children.insert(0, self)

      # return updated OTHER object
      return other

    # Create and return an object of the given type using the self and
    # OTHER objects in case of both objects don't belong to
    # the provided opType
    return opType(self, other)


  def __mul__(self, other):
    if not isinstance(other, Node):
      # if the OTHER operand doesn't belong to
      # any derived type of Node then we create an instance
      # of ScalarMultiplication class using passed parameters
      # i.e. other (usually a float number) and Node (any derived
      # type of node)


      # Check whether the derived type of the Node class (i.e. Node)
      # doesn't belong to ScalarMultiplication. Because
      # we prohibit multiple multiplications with scalars
      self._checkMultipleScalarMults()
      return ScalarMultiplication(other, self)

    if isinstance(self, ScalarMultiplication):

      # Check whether the OTHER operand doesn't
      # belong to ScalarMultiplication. Because we prohibit
      # multiple multiplications of ScalarMultiplication instances
      other._checkMultipleScalarMults()

      # At this point, we definetely know that the self object is an instance
      # of ScalarMultiplication and the OTHER belongs to any other derived type of Node.
      # Therefore, we substitute the first "Node" children of the ScalarMultiplication instance
      # (called "T" for simplicity) with the product of T and OTHER:
      self.setTerm(self.term() * other)
      return self

    elif isinstance(other, ScalarMultiplication):
      # The same as the previous case but "vice versa"

      self._checkMultipleScalarMults()
      other.setTerm(self * other.term())
      return other


    return self._binOp(other, Einsum)
  
  def __rmul__(self, other: Any):
    """Invokes when the left operand does not how to
    perform multiplication with an instance of the class Node.

    The function delegates multiplication to the function
    responsible for the left multiplication of Tensor with
    another python object.

    Args:
      other: any python object which doesn't know how to treat
             how to treat left multiplication with Type(Node)

    Returns:
      Type(Node): any derived instance of the class Node
                  e.g. ScalarMultiplication, self or binOp
    """
    return self.__mul__(other)
  
  def __add__(self, other):
    if not isinstance(other, Node):
      raise ValueError('Unsupported operation: Cannot add {} to {}.'.format(self, other))
    return self._binOp(other, Add)
  
  def __radd__(self, other):
    return self.__add__(other)
  
  def __neg__(self):
    self._checkMultipleScalarMults()
    return ScalarMultiplication(-1.0, self)

  def __sub__(self, other):
    return self._binOp(-other, Add)
    
  def __le__(self, other):
    if isinstance(other, IndexedTensor) and not (self == other):
      return Assign(self, Einsum(other))
    return Assign(self, other)


class IndexedTensor(Node):
  def __init__(self, tensor, indexNames: str):
    """Saves a passed tensor object as a data member of the class
    as well as generates an instance of Indices class
    for the following use.

    The default constructor of the supper class
    inits: self.indices = None, self._children = []
    and self._eqspp = None.

    Args:
      tensor (Tensor):
      indexNames:
    """

    super().__init__()
    self.tensor = tensor
    self.indices = Indices(indexNames, self.tensor.shape())
  
  def nonZeroFlops(self):
    return 0
  
  def setIndexPermutation(self, indices, permuteEqspp=True):
    assert str(indices) == str(self.indices)
  
  def spp(self, groupSpp=True):
    return self.tensor.spp(groupSpp)
  
  def name(self):
    return self.tensor.name()
  
  def memoryLayout(self):
    return self.tensor.memoryLayout()

  def __deepcopy__(self, memo):
    it = IndexedTensor(self.tensor, str(self.indices))
    if self._eqspp is not None:
      it._eqspp = self._eqspp.copy()
    return it

  def __str__(self):
    return '{}[{}]'.format(self.tensor.name(), str(self.indices))

class Op(Node):
  def __init__(self, *args):
    super().__init__()
    self._children = list(args)
    self._memoryLayout = None
    self.prefetch = None
  
  def memoryLayout(self):
    return self._memoryLayout

  def setMemoryLayout(self, memLayout):
    self._memoryLayout = memLayout

  def computeMemoryLayout(self):
    alignStride = False
    for child in self:
      if self.indices[0] in child.indices:
        position = child.indices.find(self.indices[0])
        if child.memoryLayout().mayVectorizeDim(position):
          alignStride = True
          break
    self._memoryLayout = DenseMemoryLayout.fromSpp(self.eqspp(), alignStride=alignStride)

  def fixedIndexPermutation(self):
    return False

  def setIndexPermutation(self, indices, permuteEqspp=True):
    if str(indices) == str(self.indices):
      return

    p = tuple([self.indices.find(idx) for idx in indices])
    if self._eqspp is not None:
      if permuteEqspp:
        self._eqspp = self._eqspp.transposed(p)
      else:
        self._eqspp = None
    if self._memoryLayout is not None:
      self._memoryLayout = self._memoryLayout.permuted(p)
    self.indices = self.indices.permuted(indices)
  
  def __str__(self):
    return '{}[{}]'.format(type(self).__name__, self.indices if self.indices != None else '<not deduced>')
  
  def computeSparsityPattern(self, *spps):
    raise NotImplementedError

class Einsum(Op):
  def nonZeroFlops(self):
    raise NotImplementedError
    
class Add(Op):
  def computeSparsityPattern(self, *spps):
    if len(spps) == 0:
      spps = [node.eqspp() for node in self]
    spp = spps[0]
    for i in range(1, len(spps)):
      spp = aspp.add(spp, spps[i])
    return spp
  
  def nonZeroFlops(self):
    nzFlops = 0
    for child in self:
      nzFlops += child.eqspp().count_nonzero()
    return nzFlops - self.eqspp().count_nonzero()


class UnaryOp(Op):
  def term(self) -> Type[Node]:
    """
    Returns:
      the left (first) child of an tensor unary operation
    """
    return self._children[0]


class ScalarMultiplication(UnaryOp):
  def __init__(self, scalar, term: Type[Node]):
    """
    Args:
      scalar (Scalar):
      term:
    """
    super().__init__(term)
    self._isConstant = isinstance(scalar, float) or isinstance(scalar, int)
    self._scalar = float(scalar) if self._isConstant else scalar
    self.setTerm(term)

  def fixedIndexPermutation(self):
    return self.term().fixedIndexPermutation()
  
  def setTerm(self, term):
    self._children[0] = term
    if self.fixedIndexPermutation():
      self.indices = self.term().indices
    else:
      self.indices = None

  def name(self):
    return str(self._scalar) if self._isConstant else self._scalar.name()
  
  def scalar(self):
    return self._scalar
  
  def computeSparsityPattern(self, *spps):
    if len(spps) == 0:
      return self.term().eqspp()
    assert len(spps) == 1
    return spps[0]

  def nonZeroFlops(self):
    if self._isConstant and self._scalar in [-1.0, 1.0]:
      return 0
    return self.eqspp().count_nonzero()
  
  def __str__(self):
    return '{}: {}'.format(super().__str__(), str(self._scalar))

class BinOp(Op):
  def __init__(self, lTerm, rTerm):
    super().__init__(lTerm, rTerm)
  
  def leftTerm(self):
    """
    Returns (Type[Node]): a left child of a local subtree i.e. a left operand
    """
    return self._children[0]
  
  def rightTerm(self):
    """
    Returns (Type[Node]): a right child of a local subtree i.e. a right operand

    """
    return self._children[1]
  
  def setChildren(self, children):
    if len(children) != 2:
      raise ValueError('BinOp node must have exactly 2 children.')
    super().setChildren(children)

class Assign(BinOp):
  def setChildren(self, children):
    if not isinstance(children[0], IndexedTensor):
      raise ValueError('First child of Assign node must be an IndexedTensor: ' + str(children[0]))
    super().setChildren(children)
    
  def nonZeroFlops(self):
    return 0
  
  def computeSparsityPattern(self, *spps):
    if len(spps) == 0:
      return self.rightTerm().eqspp()
    assert len(spps) == 2
    return spps[1]

def _productContractionLoGSparsityPattern(node, *spps):
  if len(spps) == 0:
    spps = (node.leftTerm().eqspp(), node.rightTerm().eqspp())
  assert len(spps) == 2
  einsumDescription = '{},{}->{}'.format(node.leftTerm().indices.tostring(), node.rightTerm().indices.tostring(), node.indices.tostring())
  return aspp.einsum(einsumDescription, spps[0], spps[1])

class Product(BinOp):
  def __init__(self, lTerm, rTerm):
    super().__init__(lTerm, rTerm)
    K = lTerm.indices & rTerm.indices
    assert lTerm.indices.subShape(K) == rTerm.indices.subShape(K)

    self.indices = lTerm.indices.merged(rTerm.indices - K)
  
  def nonZeroFlops(self):
    return self.eqspp().count_nonzero()
  
  def computeSparsityPattern(self, *spps):
    if len(spps) == 0:
      spps = [node.eqspp() for node in self]
    assert len(spps) == 2
    return _productContractionLoGSparsityPattern(self, *spps)

class IndexSum(UnaryOp):
  def __init__(self, term, sumIndex):
    super().__init__(term)
    self.indices = term.indices - set([sumIndex])
    self._sumIndex = term.indices.extract(sumIndex)
  
  def nonZeroFlops(self):
    return self.term().eqspp().count_nonzero() - self.eqspp().count_nonzero()
  
  def sumIndex(self):
    return self._sumIndex
  
  def computeSparsityPattern(self, *spps):
    assert len(spps) <= 1
    spp = spps[0] if len(spps) == 1 else self.term().eqspp()
    return spp.indexSum(self.term().indices, self.indices)

class Contraction(BinOp):
  def __init__(self, indices, lTerm, rTerm, sumIndices):
    super().__init__(lTerm, rTerm)
    li = lTerm.indices - sumIndices
    lr = (rTerm.indices - sumIndices) - li
    self.indices = li.merged(lr)
    self.sumIndices = sumIndices
    self.setIndexPermutation(indices)

  def nonZeroFlops(self):
    raise NotImplementedError
  
  def computeSparsityPattern(self, *spps):
    if len(spps) == 0:
      spps = [node.eqspp() for node in self]
    assert len(spps) == 2
    return _productContractionLoGSparsityPattern(self, *spps)


class LoopOverGEMM(BinOp):
  def __init__(self, indices, aTerm, bTerm, m, n, k):
    super().__init__(aTerm, bTerm)
    self.indices = indices
    self._m = m
    self._n = n
    self._k = k
    self._transA = aTerm.indices.find(m[0]) > aTerm.indices.find(k[0])
    self._transB = not self.isGEMV() and bTerm.indices.find(k[0]) > bTerm.indices.find(n[0])

  def isGEMV(self):
    return len(self._n) == 0

  def nonZeroFlops(self):
    p = Product(self.leftTerm(), self.rightTerm())
    p.setEqspp( p.computeSparsityPattern() )
    return 2*p.nonZeroFlops() - self.eqspp().count_nonzero()
  
  def computeSparsityPattern(self, *spps):
    if len(spps) == 0:
      spps = [node.eqspp() for node in self]
    assert len(spps) == 2
    return _productContractionLoGSparsityPattern(self, *spps)
  
  def cost(self):
    A = self.leftTerm().indices
    B = self.rightTerm().indices
    AstrideOne = (A.find(self._m[0]) == 0) if not self._transA else (A.find(self._k[0]) == 0)
    BstrideOne = (B.find(self._k[0]) == 0) if not self._transB else (B.find(self._n[0]) == 0)
    cost = LoGCost(int(not AstrideOne) + int(not BstrideOne), int(self._transA), int(self._transB), len(self._m) + len(self._n) + len(self._k))
    return cost
  
  def loopIndices(self):
    """ TODO
    Returns:
      Indices: TODO
    """
    i1 = self.indices - (self._m + self._n)
    i2 = (self.leftTerm().indices - (self._m + self._k)) - i1
    i3 = ((self.rightTerm().indices - (self._k + self._n)) - i1) - i2
    return i1.merged(i2).merged(i3)
  
  def transA(self):
    return self._transA

  def transB(self):
    return self._transB

  def argumentsCompatible(self, layouts):
    super().argumentsCompatible(layouts)
    m = self.leftTerm().indices.positions(self._m)
    k1 = self.leftTerm().indices.positions(self._k)
    k2 = self.rightTerm().indices.positions(self._k)
    n = self.rightTerm().indices.positions(self._n)
    return layouts[0].mayFuse(m) and layouts[0].mayFuse(k1) and layouts[1].mayFuse(k2) and layouts[1].mayFuse(n)

  def resultCompatible(self, layout):
    super().resultCompatible(layout)
    m = self.indices.positions(self._m)
    n = self.indices.positions(self._n)
    return layout.mayFuse(m) and layout.mayFuse(n)

  @staticmethod
  def indexString(name, fused, indices, transpose=False):
    indexStr = str(indices)
    batchedIndices = set(indices)
    for fs in fused:
      if len(fs) > 1:
        indexStr = re.sub(r'([{0}]{{{1},{1}}})'.format(fs, len(fs)), r'(\1)', indexStr)
      batchedIndices = batchedIndices - set(fs)
    if batchedIndices:
      indexStr = re.sub(r'([{}])'.format(''.join(batchedIndices)), r'[\1]', indexStr)
    return '{}{}_{{{}}}'.format(name, '^T' if transpose else '', indexStr)
  
  def __str__(self):
    Astr = self.indexString('A', [self._m, self._k], self.leftTerm().indices, self._transA)
    Bstr = self.indexString('B', [self._k, self._n], self.rightTerm().indices, self._transB)
    Cstr = self.indexString('C', [self._m, self._n], self.indices)
    return '{} [{}]: {} = {} {}'.format(type(self).__name__, self.indices, Cstr, Astr, Bstr)
