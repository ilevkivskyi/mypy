from typing import List, Optional, Dict, Callable, Tuple

from mypy.types import (
    Type, AnyType, UnboundType, TypeVisitor, ErrorType, FormalArgument, NoneTyp, function_type,
    Instance, TypeVarType, CallableType, TupleType, TypedDictType, UnionType, Overloaded,
    ErasedType, TypeList, PartialType, DeletedType, UninhabitedType, TypeType, is_named_instance,
    FunctionLike,
)
import mypy.applytype
import mypy.constraints
# Circular import; done in the function instead.
# import mypy.solve
from mypy import messages, sametypes
from mypy.nodes import (
    CONTRAVARIANT, COVARIANT, FuncBase, Var, Decorator, OverloadedFuncDef,
    ARG_POS, ARG_OPT, ARG_NAMED, ARG_NAMED_OPT, ARG_STAR, ARG_STAR2, TypeInfo
)
from mypy.maptype import map_instance_to_supertype
from mypy.expandtype import expand_type_by_instance

from mypy import experiments


TypeParameterChecker = Callable[[Type, Type, int], bool]


def check_type_parameter(lefta: Type, righta: Type, variance: int) -> bool:
    if variance == COVARIANT:
        return is_subtype(lefta, righta, check_type_parameter)
    elif variance == CONTRAVARIANT:
        return is_subtype(righta, lefta, check_type_parameter)
    else:
        return is_equivalent(lefta, righta, check_type_parameter)


def is_subtype(left: Type, right: Type,
               type_parameter_checker: TypeParameterChecker = check_type_parameter,
               *, ignore_pos_arg_names: bool = False) -> bool:
    """Is 'left' subtype of 'right'?

    Also consider Any to be a subtype of any type, and vice versa. This
    recursively applies to components of composite types (List[int] is subtype
    of List[Any], for example).

    type_parameter_checker is used to check the type parameters (for example,
    A with B in is_subtype(C[A], C[B]). The default checks for subtype relation
    between the type arguments (e.g., A and B), taking the variance of the
    type var into account.
    """
    if (isinstance(right, AnyType) or isinstance(right, UnboundType)
            or isinstance(right, ErasedType)):
        return True
    elif isinstance(right, UnionType) and not isinstance(left, UnionType):
        return any(is_subtype(left, item, type_parameter_checker,
                              ignore_pos_arg_names=ignore_pos_arg_names)
                   for item in right.items)
    # Treat builtins.type the same as Type[Any]
    elif is_named_instance(left, 'builtins.type'):
            return is_subtype(TypeType(AnyType()), right)
    elif is_named_instance(right, 'builtins.type'):
            return is_subtype(left, TypeType(AnyType()))
    else:
        result = left.accept(SubtypeVisitor(right, type_parameter_checker,
                                            ignore_pos_arg_names=ignore_pos_arg_names))
        # Useful for debugging
        # print(left, right, result)
        return result


def is_subtype_ignoring_tvars(left: Type, right: Type) -> bool:
    def ignore_tvars(s: Type, t: Type, v: int) -> bool:
        return True
    return is_subtype(left, right, ignore_tvars)


def is_equivalent(a: Type,
                  b: Type,
                  type_parameter_checker: TypeParameterChecker = check_type_parameter,
                  *,
                  ignore_pos_arg_names: bool = False
                  ) -> bool:
    return (
        is_subtype(a, b, type_parameter_checker, ignore_pos_arg_names=ignore_pos_arg_names)
        and is_subtype(b, a, type_parameter_checker, ignore_pos_arg_names=ignore_pos_arg_names))


class SubtypeVisitor(TypeVisitor[bool]):

    def __init__(self, right: Type,
                 type_parameter_checker: TypeParameterChecker,
                 *, ignore_pos_arg_names: bool = False) -> None:
        self.right = right
        self.check_type_parameter = type_parameter_checker
        self.ignore_pos_arg_names = ignore_pos_arg_names

    # visit_x(left) means: is left (which is an instance of X) a subtype of
    # right?

    def visit_unbound_type(self, left: UnboundType) -> bool:
        return True

    def visit_error_type(self, left: ErrorType) -> bool:
        return False

    def visit_type_list(self, t: TypeList) -> bool:
        assert False, 'Not supported'

    def visit_any(self, left: AnyType) -> bool:
        return True

    def visit_none_type(self, left: NoneTyp) -> bool:
        if experiments.STRICT_OPTIONAL:
            return (isinstance(self.right, NoneTyp) or
                    is_named_instance(self.right, 'builtins.object'))
        else:
            return True

    def visit_uninhabited_type(self, left: UninhabitedType) -> bool:
        return True

    def visit_erased_type(self, left: ErasedType) -> bool:
        return True

    def visit_deleted_type(self, left: DeletedType) -> bool:
        return True

    def visit_instance(self, left: Instance) -> bool:
        if left.type.fallback_to_any:
            return True
        right = self.right
        if isinstance(right, TupleType) and right.fallback.type.is_enum:
            return is_subtype(left, right.fallback)
        if isinstance(right, Instance):
            if left.type._promote and is_subtype(
                    left.type._promote, self.right, self.check_type_parameter,
                    ignore_pos_arg_names=self.ignore_pos_arg_names):
                return True
            rname = right.type.fullname()
            if right.type.is_protocol:
                if is_protocol_implementation(left, right):
                    return True
            # always try a nominal check (even if structural returns False)
            # there might be errors that a user wants to silence *once*
            if not left.type.has_base(rname) and rname != 'builtins.object':
                return False

            # Map left type to corresponding right instances.
            t = map_instance_to_supertype(left, right.type)

            return all(self.check_type_parameter(lefta, righta, tvar.variance)
                       for lefta, righta, tvar in
                       zip(t.args, right.args, right.type.defn.type_vars))
        if isinstance(right, TypeType):
            item = right.item
            if isinstance(item, TupleType):
                item = item.fallback
            if isinstance(item, Instance):
                return is_subtype(left, item.type.metaclass_type)
            elif isinstance(item, AnyType):
                # Special case: all metaclasses are subtypes of Type[Any].
                mro = left.type.mro or []
                return any(base.fullname() == 'builtins.type' for base in mro)
            else:
                return False
        if isinstance(right, CallableType):
            # Special case: Instance can by a subtype of Callable.
            call = find_member('__call__', left, left)
            if call:
                return is_subtype(call, right)
            return False
        else:
            return False

    def visit_type_var(self, left: TypeVarType) -> bool:
        right = self.right
        if isinstance(right, TypeVarType) and left.id == right.id:
            return True
        return is_subtype(left.upper_bound, self.right)

    def visit_callable_type(self, left: CallableType) -> bool:
        right = self.right
        if isinstance(right, CallableType):
            return is_callable_subtype(
                left, right,
                ignore_pos_arg_names=self.ignore_pos_arg_names)
        elif isinstance(right, Overloaded):
            return all(is_subtype(left, item, self.check_type_parameter,
                                  ignore_pos_arg_names=self.ignore_pos_arg_names)
                       for item in right.items())
        elif isinstance(right, Instance):
            return is_subtype(left.fallback, right,
                              ignore_pos_arg_names=self.ignore_pos_arg_names)
        elif isinstance(right, TypeType):
            # This is unsound, we don't check the __init__ signature.
            return left.is_type_obj() and is_subtype(left.ret_type, right.item)
        else:
            return False

    def visit_tuple_type(self, left: TupleType) -> bool:
        right = self.right
        if isinstance(right, Instance):
            if is_named_instance(right, 'typing.Sized'):
                return True
            elif (is_named_instance(right, 'builtins.tuple') or
                  is_named_instance(right, 'typing.Iterable') or
                  is_named_instance(right, 'typing.Container') or
                  is_named_instance(right, 'typing.Sequence') or
                  is_named_instance(right, 'typing.Reversible')):
                if right.args:
                    iter_type = right.args[0]
                else:
                    iter_type = AnyType()
                return all(is_subtype(li, iter_type) for li in left.items)
            elif is_subtype(left.fallback, right, self.check_type_parameter):
                return True
            return False
        elif isinstance(right, TupleType):
            if len(left.items) != len(right.items):
                return False
            for l, r in zip(left.items, right.items):
                if not is_subtype(l, r, self.check_type_parameter):
                    return False
            if not is_subtype(left.fallback, right.fallback, self.check_type_parameter):
                return False
            return True
        else:
            return False

    def visit_typeddict_type(self, left: TypedDictType) -> bool:
        right = self.right
        if isinstance(right, Instance):
            return is_subtype(left.fallback, right, self.check_type_parameter)
        elif isinstance(right, TypedDictType):
            if not left.names_are_wider_than(right):
                return False
            for (_, l, r) in left.zip(right):
                if not is_equivalent(l, r, self.check_type_parameter):
                    return False
            # (NOTE: Fallbacks don't matter.)
            return True
        else:
            return False

    def visit_overloaded(self, left: Overloaded) -> bool:
        right = self.right
        if isinstance(right, Instance):
            return is_subtype(left.fallback, right)
        elif isinstance(right, CallableType):
            for item in left.items():
                if is_subtype(item, right, self.check_type_parameter,
                              ignore_pos_arg_names=self.ignore_pos_arg_names):
                    return True
            return False
        elif isinstance(right, Overloaded):
            # TODO: this may be too restrictive
            if len(left.items()) != len(right.items()):
                return False
            for i in range(len(left.items())):
                if not is_subtype(left.items()[i], right.items()[i], self.check_type_parameter,
                                  ignore_pos_arg_names=self.ignore_pos_arg_names):
                    return False
            return True
        elif isinstance(right, UnboundType):
            return True
        elif isinstance(right, TypeType):
            # All the items must have the same type object status, so
            # it's sufficient to query only (any) one of them.
            # This is unsound, we don't check the __init__ signature.
            return left.is_type_obj() and is_subtype(left.items()[0].ret_type, right.item)
        else:
            return False

    def visit_union_type(self, left: UnionType) -> bool:
        return all(is_subtype(item, self.right, self.check_type_parameter)
                   for item in left.items)

    def visit_partial_type(self, left: PartialType) -> bool:
        # This is indeterminate as we don't really know the complete type yet.
        raise RuntimeError

    def visit_type_type(self, left: TypeType) -> bool:
        right = self.right
        if isinstance(right, TypeType):
            return is_subtype(left.item, right.item)
        if isinstance(right, CallableType):
            # This is unsound, we don't check the __init__ signature.
            return is_subtype(left.item, right.ret_type)
        if isinstance(right, Instance):
            if right.type.fullname() == 'builtins.object':
                # treat builtins.object the same as Any.
                return True
            item = left.item
            return isinstance(item, Instance) and is_subtype(item, right.type.metaclass_type)
        return False


def is_protocol_implementation(left: Instance, right: Instance, allow_any: bool = True) -> bool:
    """Check whether 'left' implements the protocol 'right'. If 'allow_any' is False, then
    check for a proper subtype. Treat recursive protocols by using a global 'assuming'
    structural subtype matrix (in sparse representation). If concurrent type checking
    will be implemented, then every thread/process should use its own matrix
    (see comment in nodes.TypeInfo).
    """
    assert right.type.is_protocol
    assuming = TypeInfo.assuming if allow_any else TypeInfo.assuming_proper
    for (l, r) in reversed(assuming):
        if sametypes.is_same_type(l, left) and sametypes.is_same_type(r, right):
            return True
    assuming.append((left, right))
    if right.type.protocol_members is None:
        # This type has not been yet analyzed, probably a call from make_simplified_union
        assuming.pop()
        return False
    for member in right.type.protocol_members:
        supertype = find_member(member, right, left)
        subtype = find_member(member, left, left)
        # Useful for debugging:
        # print(member, 'of', left, 'has type', subtype)
        # print(member, 'of', right, 'has type', supertype)
        if not subtype:
            assuming.pop()
            return False
        if allow_any:
            # nominal check currently ignore arg names
            is_compat = is_subtype(subtype, supertype, ignore_pos_arg_names=True)
        else:
            is_compat = is_proper_subtype(subtype, supertype)
        if not is_compat:
            assuming.pop()
            return False
    return True


def find_member(name: str, itype: Instance, subtype: Instance) -> Optional[Type]:
    """Find the type of member by 'name' in 'itype's TypeInfo. Apply type arguments
    from 'itype', and bind 'self' to 'subtype'. Return None if member was not found.
    """
    info = itype.type
    method = info.get_method(name)
    if method:
        if method.is_property:
            assert isinstance(method, OverloadedFuncDef)
            dec = method.items[0]
            assert isinstance(dec, Decorator)
            return find_var_type(dec.var, itype, subtype)
        return map_method(method, itype, subtype)
    else:
        # don't have such method, maybe variable or decorator?
        node = info.get(name)
        if not node:
            v = None
        else:
            v = node.node
        if isinstance(v, Decorator):
            v = v.var
        if isinstance(v, Var):
            return find_var_type(v, itype, subtype)
        if not v and name not in ['__getattr__', '__setattr__', '__getattribute__']:
            for method_name in ('__getattribute__', '__getattr__'):
                # Normally, mypy assumes that instances that define __getattr__ have all
                # attributes with the corresponding return type. If this will produce
                # many false negatives, then this could be prohibited for
                # structural subtyping.
                method = info.get_method(method_name)
                if method and method.info.fullname() != 'builtins.object':
                    getattr_type = map_method(method, itype, subtype)
                    if isinstance(getattr_type, CallableType):
                        return getattr_type.ret_type
        if itype.type.fallback_to_any:
            return AnyType()
    return None


def find_var_type(var: Var, itype: Instance, subtype: Instance) -> Type:
    """Find type of a variable 'var' (maybe also a decorated method).
    Apply type arguments from 'itype', and bind 'self' to 'subtype'.
    """
    from mypy.checkmember import bind_self
    itype = map_instance_to_supertype(itype, var.info)
    typ = var.type
    if typ is None:
        return AnyType()
    typ = expand_type_by_instance(typ, itype)
    # We don't need to bind 'self' for static methods, since there is no 'self'.
    if isinstance(typ, FunctionLike) and not var.is_staticmethod:
        signature = bind_self(typ, subtype)
        assert isinstance(signature, CallableType)
        if var.is_property:
            return signature.ret_type
        return signature
    return typ


def map_method(method: FuncBase, itype: Instance, subtype: Instance) -> Type:
    """Map 'method' to the base where it was defined. Apply type arguments
    from 'itype', and bind 'self' type to 'subtype'.
    This function should be used only for non-decorated methods. Decorated
    methods (including @staticmethod and @property) are treated
    by 'find_var_type'.
    """
    from mypy.checkmember import bind_self
    signature = function_type(method, Instance(itype.type.mro[-1], []))
    signature = bind_self(signature, subtype)
    itype = map_instance_to_supertype(itype, method.info)
    return expand_type_by_instance(signature, itype)


def get_missing_members(left: Instance, right: Instance) -> List[str]:
    """Find all protocol members of 'right' that are not implemented
    (i.e. completely missing) in 'left'.
    """
    assert right.type.is_protocol
    missing = []  # type: List[str]
    for member in right.type.protocol_members:
        if not find_member(member, left, left):
            missing.append(member)
    return sorted(missing)


def is_callable_subtype(left: CallableType, right: CallableType,
                        ignore_return: bool = False,
                        ignore_pos_arg_names: bool = False) -> bool:
    """Is left a subtype of right?"""

    # If either function is implicitly typed, ignore positional arg names too
    if left.implicit or right.implicit:
        ignore_pos_arg_names = True

    # Non-type cannot be a subtype of type.
    if right.is_type_obj() and not left.is_type_obj():
        return False

    # A callable L is a subtype of a generic callable R if L is a
    # subtype of every type obtained from R by substituting types for
    # the variables of R. We can check this by simply leaving the
    # generic variables of R as type variables, effectively varying
    # over all possible values.

    # It's okay even if these variables share ids with generic
    # type variables of L, because generating and solving
    # constraints for the variables of L to make L a subtype of R
    # (below) treats type variables on the two sides as independent.

    if left.variables:
        # Apply generic type variables away in left via type inference.
        left = unify_generic_callable(left, right, ignore_return=ignore_return)
        if left is None:
            return False

    # Check return types.
    if not ignore_return and not is_subtype(left.ret_type, right.ret_type):
        return False

    if right.is_ellipsis_args:
        return True

    right_star_type = None   # type: Optional[Type]
    right_star2_type = None  # type: Optional[Type]

    # Match up corresponding arguments and check them for compatibility. In
    # every pair (argL, argR) of corresponding arguments from L and R, argL must
    # be "more general" than argR if L is to be a subtype of R.

    # Arguments are corresponding if they either share a name, share a position,
    # or both. If L's corresponding argument is ambiguous, L is not a subtype of
    # R.

    # If left has one corresponding argument by name and another by position,
    # consider them to be one "merged" argument (and not ambiguous) if they're
    # both optional, they're name-only and position-only respectively, and they
    # have the same type.  This rule allows functions with (*args, **kwargs) to
    # properly stand in for the full domain of formal arguments that they're
    # used for in practice.

    # Every argument in R must have a corresponding argument in L, and every
    # required argument in L must have a corresponding argument in R.
    done_with_positional = False
    for i in range(len(right.arg_types)):
        right_kind = right.arg_kinds[i]
        if right_kind in (ARG_STAR, ARG_STAR2, ARG_NAMED, ARG_NAMED_OPT):
            done_with_positional = True
        right_required = right_kind in (ARG_POS, ARG_NAMED)
        right_pos = None if done_with_positional else i

        right_arg = FormalArgument(
            right.arg_names[i],
            right_pos,
            right.arg_types[i],
            right_required)

        if right_kind == ARG_STAR:
            right_star_type = right_arg.typ
            # Right has an infinite series of optional positional arguments
            # here.  Get all further positional arguments of left, and make sure
            # they're more general than their corresponding member in this
            # series.  Also make sure left has its own inifite series of
            # optional positional arguments.
            if not left.is_var_arg:
                return False
            j = i
            while j < len(left.arg_kinds) and left.arg_kinds[j] in (ARG_POS, ARG_OPT):
                left_by_position = left.argument_by_position(j)
                assert left_by_position is not None
                # This fetches the synthetic argument that's from the *args
                right_by_position = right.argument_by_position(j)
                assert right_by_position is not None
                if not are_args_compatible(left_by_position, right_by_position,
                                           ignore_pos_arg_names):
                    return False
                j += 1
            continue

        if right_kind == ARG_STAR2:
            right_star2_type = right_arg.typ
            # Right has an infinite set of optional named arguments here.  Get
            # all further named arguments of left and make sure they're more
            # general than their corresponding member in this set.  Also make
            # sure left has its own infinite set of optional named arguments.
            if not left.is_kw_arg:
                return False
            left_names = {name for name in left.arg_names if name is not None}
            right_names = {name for name in right.arg_names if name is not None}
            left_only_names = left_names - right_names
            for name in left_only_names:
                left_by_name = left.argument_by_name(name)
                assert left_by_name is not None
                # This fetches the synthetic argument that's from the **kwargs
                right_by_name = right.argument_by_name(name)
                assert right_by_name is not None
                if not are_args_compatible(left_by_name, right_by_name,
                                           ignore_pos_arg_names):
                    return False
            continue

        # Left must have some kind of corresponding argument.
        left_arg = left.corresponding_argument(right_arg)
        if left_arg is None:
            return False

        if not are_args_compatible(left_arg, right_arg, ignore_pos_arg_names):
            return False

    done_with_positional = False
    for i in range(len(left.arg_types)):
        left_kind = left.arg_kinds[i]
        if left_kind in (ARG_STAR, ARG_STAR2, ARG_NAMED, ARG_NAMED_OPT):
            done_with_positional = True
        left_arg = FormalArgument(
            left.arg_names[i],
            None if done_with_positional else i,
            left.arg_types[i],
            left_kind in (ARG_POS, ARG_NAMED))

        # Check that *args and **kwargs types match in this loop
        if left_kind == ARG_STAR:
            if right_star_type is not None and not is_subtype(right_star_type, left_arg.typ):
                return False
            continue
        elif left_kind == ARG_STAR2:
            if right_star2_type is not None and not is_subtype(right_star2_type, left_arg.typ):
                return False
            continue

        right_by_name = (right.argument_by_name(left_arg.name)
                         if left_arg.name is not None
                         else None)

        right_by_pos = (right.argument_by_position(left_arg.pos)
                        if left_arg.pos is not None
                        else None)

        # If the left hand argument corresponds to two right-hand arguments,
        # neither of them can be required.
        if (right_by_name is not None
                and right_by_pos is not None
                and right_by_name != right_by_pos
                and (right_by_pos.required or right_by_name.required)):
            return False

        # All *required* left-hand arguments must have a corresponding
        # right-hand argument.  Optional args it does not matter.
        if left_arg.required and right_by_pos is None and right_by_name is None:
            return False

    return True


def are_args_compatible(
        left: FormalArgument,
        right: FormalArgument,
        ignore_pos_arg_names: bool) -> bool:
    # If right has a specific name it wants this argument to be, left must
    # have the same.
    if right.name is not None and left.name != right.name:
        # But pay attention to whether we're ignoring positional arg names
        if not ignore_pos_arg_names or right.pos is None:
            return False
    # If right is at a specific position, left must have the same:
    if right.pos is not None and left.pos != right.pos:
        return False
    # Left must have a more general type
    if not is_subtype(right.typ, left.typ):
        return False
    # If right's argument is optional, left's must also be.
    if not right.required and left.required:
        return False
    return True


def unify_generic_callable(type: CallableType, target: CallableType,
                           ignore_return: bool) -> CallableType:
    """Try to unify a generic callable type with another callable type.

    Return unified CallableType if successful; otherwise, return None.
    """
    import mypy.solve
    constraints = []  # type: List[mypy.constraints.Constraint]
    for arg_type, target_arg_type in zip(type.arg_types, target.arg_types):
        c = mypy.constraints.infer_constraints(
            arg_type, target_arg_type, mypy.constraints.SUPERTYPE_OF)
        constraints.extend(c)
    if not ignore_return:
        c = mypy.constraints.infer_constraints(
            type.ret_type, target.ret_type, mypy.constraints.SUBTYPE_OF)
        constraints.extend(c)
    type_var_ids = [tvar.id for tvar in type.variables]
    inferred_vars = mypy.solve.solve_constraints(type_var_ids, constraints)
    if None in inferred_vars:
        return None
    msg = messages.temp_message_builder()
    applied = mypy.applytype.apply_generic_arguments(type, inferred_vars, msg, context=target)
    if msg.is_errors():
        return None
    return applied


def restrict_subtype_away(t: Type, s: Type) -> Type:
    """Return a supertype of (t intersect not s)

    Currently just remove elements of a union type.
    """
    if isinstance(t, UnionType):
        new_items = [item for item in t.items if (not is_subtype(item, s)
                                                  or isinstance(item, AnyType))]
        return UnionType.make_union(new_items)
    else:
        return t


def is_proper_subtype(t: Type, s: Type) -> bool:
    """Check if t is a proper subtype of s?

    For proper subtypes, there's no need to rely on compatibility due to
    Any types. Any instance type t is also a proper subtype of t.
    """
    # FIX tuple types
    if isinstance(s, UnionType):
        if isinstance(t, UnionType):
            return all([is_proper_subtype(item, s) for item in t.items])
        else:
            return any([is_proper_subtype(t, item) for item in s.items])

    if isinstance(t, Instance):
        if isinstance(s, Instance):
            if s.type.is_protocol:
                return is_protocol_implementation(t, s, allow_any=False)
            if not t.type.has_base(s.type.fullname()):
                return False

            def check_argument(left: Type, right: Type, variance: int) -> bool:
                if variance == COVARIANT:
                    return is_proper_subtype(left, right)
                elif variance == CONTRAVARIANT:
                    return is_proper_subtype(right, left)
                else:
                    return sametypes.is_same_type(left, right)

            # Map left type to corresponding right instances.
            t = map_instance_to_supertype(t, s.type)

            return all(check_argument(ta, ra, tvar.variance) for ta, ra, tvar in
                       zip(t.args, s.args, s.type.defn.type_vars))
        if isinstance(s, CallableType):
            call = find_member('__call__', t, t)
            if call:
                return is_proper_subtype(call, s)
            return False
        return False
    else:
        return sametypes.is_same_type(t, s)


def is_more_precise(t: Type, s: Type) -> bool:
    """Check if t is a more precise type than s.

    A t is a proper subtype of s, t is also more precise than s. Also, if
    s is Any, t is more precise than s for any t. Finally, if t is the same
    type as s, t is more precise than s.
    """
    # TODO Should List[int] be more precise than List[Any]?
    if isinstance(s, AnyType):
        return True
    if isinstance(s, Instance):
        if isinstance(t, CallableType):
            # Fall back to subclass check and ignore other properties of the callable.
            return is_proper_subtype(t.fallback, s)
        return is_proper_subtype(t, s)
    return sametypes.is_same_type(t, s)
