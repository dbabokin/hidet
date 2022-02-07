from typing import Optional, Union, Sequence
from hidet.ir.type import TypeNode, ScalarType, TensorType, Scope, Int, DataLayout
from hidet.ir.expr import Expr, TensorElement, Var, Constant


class VoidType(TypeNode):
    pass


class PointerType(TypeNode):
    def __init__(self, base_type):
        super().__init__()
        self.base_type = base_type


class ReferenceType(TypeNode):
    def __init__(self, base_type):
        super().__init__()
        self.base_type = base_type


class TensorPointerType(TypeNode):
    def __init__(self,
                 scope: Optional[Union[Scope, str]] = None,
                 dtype: Optional[Union[ScalarType, str]] = None,
                 shape: Optional[Sequence[Int]] = None,
                 layout: Optional[Union[Sequence[Int], DataLayout]] = None):
        self.tensor_type: TensorType = TensorType(scope, dtype, shape, layout)


class Cast(Expr):
    def __init__(self, expr, target_type):
        self.expr = expr
        self.target_type = target_type


class Dereference(Expr):
    def __init__(self, expr):
        self.expr = expr


class Address(Expr):
    def __init__(self, expr):
        self.expr = expr


class Reference(Expr):
    def __init__(self, expr):
        assert isinstance(expr, (TensorElement, Var)), "only l-value can be referenced."
        self.expr = expr


def pointer_type(base_type):
    return PointerType(base_type)
