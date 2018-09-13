import collections
from io import StringIO
from ..memory import DenseMemoryLayout
from ..controlflow.visitor import SortedGlobalsList
from ..controlflow.transformer import DetermineLocalInitialization
from .code import Cpp
from .factory import *

SUPPORT_LIBRARY_NAMESPACE = 'yateto'
MODIFIERS = 'static constexpr'

class KernelGenerator(object):  
  def __init__(self, arch):
    self._arch = arch

  def _name(self, var):
    raise NotImplementedError

  def _memoryLayout(self, term):
    raise NotImplementedError

  def _sizeFun(self, term):
    return self._memoryLayout(term).requiredReals()
  
  def generate(self, cpp, cfg, factory, routineCache=None):
    cfg = DetermineLocalInitialization().visit(cfg, self._sizeFun)
    localML = dict()
    for pp in cfg:
      for name, size in pp.initLocal.items():
        cpp('{} {}[{}] __attribute__((aligned({})));'.format(self._arch.typename, name, size, self._arch.alignment))
      action = pp.action
      if action:
        if action.isRHSExpression() or action.term.isGlobal():
          if action.isRHSExpression():
            termML = self._memoryLayout(action.term.node)
          else:
            termML = self._memoryLayout(action.term.tensor)
        else:
          termML = localML[action.term]
        if action.result.isLocal():
          localML[action.result] = termML
          resultML = termML
        else:
          resultML = self._memoryLayout(action.result.tensor)

        if action.isRHSExpression():
          factory.create(action.term.node, resultML, self._name(action.result), [self._name(var) for var in action.term.variableList()], action.add, routineCache)
        else:
          factory.simple(self._name(action.result), resultML, self._name(action.term), termML, action.add)
    return 0

class OptimisedKernelGenerator(KernelGenerator):
  NAMESPACE = 'kernel'
  EXECUTE_NAME = 'execute'
  NONZEROFLOPS_NAME = 'NonZeroFlops'
  HARDWAREFLOPS_NAME = 'HardwareFlops'
  
  def __init__(self, arch, routineCache):
    super().__init__(arch)
    self._routineCache = routineCache

  def _name(self, var):
    return str(var)

  def _memoryLayout(self, term):
    return term.memoryLayout()
  
  def generate(self, cpp, header, name, nonZeroFlops, cfg):
    variables = SortedGlobalsList().visit(cfg)
    tensors = collections.OrderedDict()
    for var in variables:
      bn = var.tensor.baseName()
      g = var.tensor.group()
      if bn in tensors:
        p = tensors[bn]
        if p is not None and g is not None:
          tensors[bn] = p | {g}
        elif not (p is None and g is None):
          raise ValueError('Grouped tensors ({}) and single tensors ({}) may not appear mixed in a kernel.'.format(node.name(), bn))        
      else:
        tensors[bn] = {g} if g is not None else None

    functionIO = StringIO()
    function = ''
    with Cpp(functionIO) as fcpp:
      factory = OptimisedKernelFactory(fcpp, self._arch)
      hwFlops = super().generate(fcpp, cfg, factory, self._routineCache)
      function = functionIO.getvalue()
    with header.Namespace(self.NAMESPACE):
      with header.Struct(name):
        header('{} {} {} = {};'.format(MODIFIERS, self._arch.uintTypename, self.NONZEROFLOPS_NAME, nonZeroFlops))
        header('{} {} {} = {};'.format(MODIFIERS, self._arch.uintTypename, self.HARDWAREFLOPS_NAME, hwFlops))
        header.emptyline()
        
        for baseName, groups in tensors.items():
          if groups:
            size = max(groups)+1
            header('{}* {}[{}] = {{{}}};'.format(self._arch.typename, baseName, size, ', '.join(['nullptr']*size)))
          else:
            header('{}* {} = nullptr;'.format(self._arch.typename, baseName))
        header.emptyline()
        header.functionDeclaration(self.EXECUTE_NAME)

      with cpp.Function('{}::{}::{}'.format(self.NAMESPACE, name, self.EXECUTE_NAME)):
        for baseName, groups in tensors.items():
          if groups:
            for g in groups:
              cpp('assert({}[{}] != nullptr);'.format(baseName, g))
          else:
            cpp('assert({} != nullptr);'.format(baseName))
        cpp(function)
    return hwFlops

class UnitTestGenerator(KernelGenerator):
  KERNEL_VAR = 'krnl'
  CXXTEST_PREFIX = 'test'
  
  def __init__(self, arch):
    super().__init__(arch)
  
  def _tensorName(self, var):
    if var.isLocal():
      return str(var)
    baseName = var.tensor.baseName()
    group = var.tensor.group()
    return '_{}_{}'.format(baseName, group) if group is not None else baseName

  def _name(self, var):
    if var.isLocal():
      return str(var)
    return '_ut_' + self._tensorName(var)

  def _viewName(self, var):
    return '_view_' + self._name(var)

  def _groupTemplate(self, var):
    group = var.tensor.group()
    return '<{}>'.format(group) if group is not None else ''

  def _groupIndex(self, var):
    group = var.tensor.group()
    return '[{}]'.format(group) if group is not None else ''

  def _memoryLayout(self, term):
    return DenseMemoryLayout(term.shape())

  def _sizeFun(self, term):
    return self._memoryLayout(term).requiredReals()
  
  def generate(self, cpp, kernelName, cfg):
    variables = SortedGlobalsList().visit(cfg)
    with cpp.Function(self.CXXTEST_PREFIX + kernelName):
      for var in variables:
        self._factory = UnitTestFactory(cpp, self._arch)
        self._factory.tensor(var.tensor, self._tensorName(var))
        
        cpp('{} {}[{}] __attribute__((aligned({}))) = {{}};'.format(self._arch.typename, self._name(var), self._sizeFun(var.tensor), self._arch.alignment))
        
        shape = var.tensor.shape()
        cpp('{supportNS}::DenseTensorView<{dim},{arch.typename},{arch.uintTypename}> {viewName}({utName}, {{{shape}}}, {{{start}}}, {{{shape}}});'.format(
            supportNS = SUPPORT_LIBRARY_NAMESPACE,
            dim=len(shape),
            arch = self._arch,
            utName=self._name(var),
            viewName=self._viewName(var),
            shape=', '.join([str(s) for s in shape]),
            start=', '.join(['0']*len(shape))
          )
        )
        cpp( '{initNS}::{baseName}::view{groupTemplate}({name}).copyToView({viewName});'.format(
            initNS = InitializerGenerator.NAMESPACE,
            supportNS = SUPPORT_LIBRARY_NAMESPACE,
            groupTemplate=self._groupTemplate(var),
            baseName=var.tensor.baseName(),
            name=self._tensorName(var),
            viewName=self._viewName(var)
          )
        )
        cpp.emptyline()

      cpp( '{}::{} {};'.format(OptimisedKernelGenerator.NAMESPACE, kernelName, self.KERNEL_VAR) )
      for var in variables:
        cpp( '{}.{}{} = {};'.format(self.KERNEL_VAR, var.tensor.baseName(), self._groupIndex(var), self._tensorName(var)) )
      cpp( '{}.{}();'.format(self.KERNEL_VAR, OptimisedKernelGenerator.EXECUTE_NAME) )
      cpp.emptyline()

      factory = UnitTestFactory(cpp, self._arch)      
      super().generate(cpp, cfg, factory)

      for var in variables:
        if var.writable:
          factory.compare(self._name(var), self._memoryLayout(var.tensor), self._tensorName(var), var.tensor.memoryLayout())

class InitializerGenerator(object):
  SHAPE_NAME = 'Shape'
  START_NAME = 'Start'
  STOP_NAME = 'Stop'
  SIZE_NAME = 'Size'
  VALUES_BASENAME = 'Values'
  NAMESPACE = 'init'
  
  class TensorView(object):
    ARGUMENT_NAME = 'values'

    def typename(self, dim, arch):
      return '::{}::{}<{},{},{}>'.format(SUPPORT_LIBRARY_NAMESPACE, type(self).__name__, dim, arch.typename, arch.uintTypename)
    
    @classmethod
    def arguments(cls, arch):
      return '{}* {}'.format(arch.typename, cls.ARGUMENT_NAME)
    
    def generate(cpp, group, memLayout):
      raise NotImplementedError
  
  class DenseTensorView(TensorView):
    def generate(self, cpp, memLayout, arch, group):
      index = '[{}]'.format(group) if group is not None else ''
      cpp( 'return {}({}, {}, {}, {});'.format(
          self.typename(len(memLayout.shape()), arch),
          self.ARGUMENT_NAME,
          InitializerGenerator.SHAPE_NAME + index,
          InitializerGenerator.START_NAME + index,
          InitializerGenerator.STOP_NAME + index
        )
      )

  def __init__(self, cpp, arch):
    self._cpp = cpp
    self._arch = arch
  
  def _tensorViewGenerator(self, memoryLayout):
    memLayoutMap = {
      'DenseMemoryLayout': self.DenseTensorView
    }
    return memLayoutMap[type(memoryLayout).__name__]()
  
  def generate(self, tensors):
    collect = dict()
    for tensor in tensors:
      baseName = tensor.baseName()
      group = tensor.group()
      if baseName not in collect:
        collect[baseName] = {group: tensor}
      elif baseName in collect and group not in collect[baseName]:
        collect[baseName][group] = tensor
      else:
        assert collect[baseName][group] == tensor
    with self._cpp.Namespace(self.NAMESPACE):
      for baseName,tensorGroup in collect.items():
        with self._cpp.Namespace(baseName):
          self.visit(tensorGroup)

  def visit(self, group):
    maxIndex = max(group.keys())

    numberType = '{} {} const'.format(MODIFIERS, self._arch.uintTypename)
    self._array(numberType, self.SHAPE_NAME, {k: v.shape() for k,v in group.items()}, maxIndex)
    self._array(numberType, self.START_NAME, {k: [r.start for r in v.memoryLayout().bbox()] for k,v in group.items()}, maxIndex)
    self._array(numberType, self.STOP_NAME, {k: [r.stop for r in v.memoryLayout().bbox()] for k,v in group.items()}, maxIndex)
    self._array(numberType, self.SIZE_NAME, {k: [v.memoryLayout().requiredReals()] for k,v in group.items()}, maxIndex)
    
    realType = '{} {} const'.format(MODIFIERS, self._arch.typename)
    realPtrType = realType + '*'
    valueNames = dict()
    if maxIndex is not None:
      for k,v in group.items():
        values = v.values()
        memLayout = v.memoryLayout()
        if values is not None:
          memory = ['0.']*memLayout.requiredReals()
          for idx,x in values.items():
            memory[memLayout.address(idx)] = x
          name = '{}{}'.format(self.VALUES_BASENAME, k if k is not None else '')
          valueNames[k] = ['&{}[0]'.format(name)]
          self._cpp('{} {}[] = {{{}}};'.format(realType, name, ', '.join(memory)))
      if len(valueNames) > 0:
        self._array(realPtrType, self.VALUES_BASENAME, valueNames, maxIndex)

    viewArgs = self.TensorView.arguments(self._arch)
    if maxIndex is None:
      ml = next(iter(group.values())).memoryLayout()
      tv = self._tensorViewGenerator(ml)
      with self._cpp.Function('view', arguments=viewArgs, returnType=tv.typename(len(ml.shape()), self._arch)):
        tv.generate(self._cpp, ml, self._arch, None)
    else:
      self._cpp('template<int n> struct view_type {};')
      self._cpp('template<int n> static typename view_type<n>::type view({});'.format(viewArgs))
      for k,v in group.items():
        ml = v.memoryLayout()
        tv = self._tensorViewGenerator(ml)
        typename = tv.typename(len(ml.shape()), self._arch)
        self._cpp( 'template<> struct view_type<{}> {{ typedef {} type; }};'.format(k, typename) )
        with self._cpp.Function('view<{}>'.format(k), arguments=viewArgs, returnType='template<> {}'.format(typename)):
          tv.generate(self._cpp, ml, self._arch, k)
  
  def _array(self, typ, name, group, maxIndex):
    dummy = [0]
    formatArray = lambda L: ', '.join([str(x) for x in L])
    maxLen = max(map(len, group.values())) if len(group.values()) > 0 else 0
    if maxIndex is None:
      init = [formatArray(next(iter(group.values())) if len(group.values()) > 0 else dummy)]
    else:
      groupSize = maxIndex+1
      init = [None]*groupSize
      for idx in range(groupSize):
        init[idx] = formatArray(group[idx] if idx in group else dummy)

    arrayIndices = ''
    if maxLen > 1:
      arrayIndices = '[{}]'.format(maxLen)
      init = ['{{{}}}'.format(i) for i in init]
    
    initStr = ', '.join(init)
    groupIndices = ''
    if maxIndex is not None:
      groupIndices = '[]'
      initStr = '{{{}}}'.format(initStr)

    self._cpp('{} {}{}{} = {};'.format(typ, name, groupIndices, arrayIndices, initStr))
