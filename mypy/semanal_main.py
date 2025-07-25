"""Top-level logic for the semantic analyzer.

The semantic analyzer binds names, resolves imports, detects various
special constructs that don't have dedicated AST nodes after parse
(such as 'cast' which looks like a call), populates symbol tables, and
performs various simple consistency checks.

Semantic analysis of each SCC (strongly connected component; import
cycle) is performed in one unit. Each module is analyzed as multiple
separate *targets*; the module top level is one target and each function
is a target. Nested functions are not separate targets, however. This is
mostly identical to targets used by mypy daemon (but classes aren't
targets in semantic analysis).

We first analyze each module top level in an SCC. If we encounter some
names that we can't bind because the target of the name may not have
been processed yet, we *defer* the current target for further
processing. Deferred targets will be analyzed additional times until
everything can be bound, or we reach a maximum number of iterations.

We keep track of a set of incomplete namespaces, i.e. namespaces that we
haven't finished populating yet. References to these namespaces cause a
deferral if they can't be satisfied. Initially every module in the SCC
will be incomplete.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import nullcontext
from itertools import groupby
from typing import TYPE_CHECKING, Callable, Final, Optional, Union
from typing_extensions import TypeAlias as _TypeAlias

import mypy.build
import mypy.state
from mypy.checker import FineGrainedDeferredNode
from mypy.errors import Errors
from mypy.nodes import Decorator, FuncDef, MypyFile, OverloadedFuncDef, TypeInfo, Var
from mypy.options import Options
from mypy.plugin import ClassDefContext
from mypy.plugins import dataclasses as dataclasses_plugin
from mypy.semanal import (
    SemanticAnalyzer,
    apply_semantic_analyzer_patches,
    remove_imported_names_from_symtable,
)
from mypy.semanal_classprop import (
    add_type_promotion,
    calculate_class_abstract_status,
    calculate_class_vars,
    check_protocol_status,
)
from mypy.semanal_infer import infer_decorator_signature_if_simple
from mypy.semanal_shared import find_dataclass_transform_spec
from mypy.semanal_typeargs import TypeArgumentAnalyzer
from mypy.server.aststrip import SavedAttributes
from mypy.util import is_typeshed_file

if TYPE_CHECKING:
    from mypy.build import Graph, State


Patches: _TypeAlias = list[tuple[int, Callable[[], None]]]


# If we perform this many iterations, raise an exception since we are likely stuck.
MAX_ITERATIONS: Final = 20


# Number of passes over core modules before going on to the rest of the builtin SCC.
CORE_WARMUP: Final = 2
core_modules: Final = [
    "typing",
    "_collections_abc",
    "builtins",
    "abc",
    "collections",
    "collections.abc",
]


def semantic_analysis_for_scc(graph: Graph, scc: list[str], errors: Errors) -> None:
    """Perform semantic analysis for all modules in a SCC (import cycle).

    Assume that reachability analysis has already been performed.

    The scc will be processed roughly in the order the modules are included
    in the list.
    """
    patches: Patches = []
    # Note that functions can't define new module-level attributes
    # using 'global x', since module top levels are fully processed
    # before functions. This limitation is unlikely to go away soon.
    process_top_levels(graph, scc, patches)
    process_functions(graph, scc, patches)
    # We use patch callbacks to fix up things when we expect relatively few
    # callbacks to be required.
    apply_semantic_analyzer_patches(patches)
    # Run class decorator hooks (they requite complete MROs and no placeholders).
    apply_class_plugin_hooks(graph, scc, errors)
    # This pass might need fallbacks calculated above and the results of hooks.
    check_type_arguments(graph, scc, errors)
    calculate_class_properties(graph, scc, errors)
    check_blockers(graph, scc)
    # Clean-up builtins, so that TypeVar etc. are not accessible without importing.
    if "builtins" in scc:
        cleanup_builtin_scc(graph["builtins"])


def cleanup_builtin_scc(state: State) -> None:
    """Remove imported names from builtins namespace.

    This way names imported from typing in builtins.pyi aren't available
    by default (without importing them). We can only do this after processing
    the whole SCC is finished, when the imported names aren't needed for
    processing builtins.pyi itself.
    """
    assert state.tree is not None
    remove_imported_names_from_symtable(state.tree.names, "builtins")


def semantic_analysis_for_targets(
    state: State, nodes: list[FineGrainedDeferredNode], graph: Graph, saved_attrs: SavedAttributes
) -> None:
    """Semantically analyze only selected nodes in a given module.

    This essentially mirrors the logic of semantic_analysis_for_scc()
    except that we process only some targets. This is used in fine grained
    incremental mode, when propagating an update.

    The saved_attrs are implicitly declared instance attributes (attributes
    defined on self) removed by AST stripper that may need to be reintroduced
    here.  They must be added before any methods are analyzed.
    """
    patches: Patches = []
    if any(isinstance(n.node, MypyFile) for n in nodes):
        # Process module top level first (if needed).
        process_top_levels(graph, [state.id], patches)
    restore_saved_attrs(saved_attrs)
    analyzer = state.manager.semantic_analyzer
    for n in nodes:
        if isinstance(n.node, MypyFile):
            # Already done above.
            continue
        process_top_level_function(
            analyzer, state, state.id, n.node.fullname, n.node, n.active_typeinfo, patches
        )
    apply_semantic_analyzer_patches(patches)
    apply_class_plugin_hooks(graph, [state.id], state.manager.errors)
    check_type_arguments_in_targets(nodes, state, state.manager.errors)
    calculate_class_properties(graph, [state.id], state.manager.errors)


def restore_saved_attrs(saved_attrs: SavedAttributes) -> None:
    """Restore instance variables removed during AST strip that haven't been added yet."""
    for (cdef, name), sym in saved_attrs.items():
        info = cdef.info
        existing = info.get(name)
        defined_in_this_class = name in info.names
        assert isinstance(sym.node, Var)
        # This needs to mimic the logic in SemanticAnalyzer.analyze_member_lvalue()
        # regarding the existing variable in class body or in a superclass:
        # If the attribute of self is not defined in superclasses, create a new Var.
        if (
            existing is None
            or
            # (An abstract Var is considered as not defined.)
            (isinstance(existing.node, Var) and existing.node.is_abstract_var)
            or
            # Also an explicit declaration on self creates a new Var unless
            # there is already one defined in the class body.
            sym.node.explicit_self_type
            and not defined_in_this_class
        ):
            info.names[name] = sym


def process_top_levels(graph: Graph, scc: list[str], patches: Patches) -> None:
    # Process top levels until everything has been bound.

    # Reverse order of the scc so the first modules in the original list will be
    # be processed first. This helps with performance.
    scc = list(reversed(scc))  # noqa: FURB187 intentional copy

    # Initialize ASTs and symbol tables.
    for id in scc:
        state = graph[id]
        assert state.tree is not None
        state.manager.semantic_analyzer.prepare_file(state.tree)

    # Initially all namespaces in the SCC are incomplete (well they are empty).
    state.manager.incomplete_namespaces.update(scc)

    worklist = scc.copy()
    # HACK: process core stuff first. This is mostly needed to support defining
    # named tuples in builtin SCC.
    if all(m in worklist for m in core_modules):
        worklist += list(reversed(core_modules)) * CORE_WARMUP
    final_iteration = False
    iteration = 0
    analyzer = state.manager.semantic_analyzer
    analyzer.deferral_debug_context.clear()

    while worklist:
        iteration += 1
        if iteration > MAX_ITERATIONS:
            # Just pick some module inside the current SCC for error context.
            assert state.tree is not None
            with analyzer.file_context(state.tree, state.options):
                analyzer.report_hang()
            break
        if final_iteration:
            # Give up. It's impossible to bind all names.
            state.manager.incomplete_namespaces.clear()
        all_deferred: list[str] = []
        any_progress = False
        while worklist:
            next_id = worklist.pop()
            state = graph[next_id]
            assert state.tree is not None
            deferred, incomplete, progress = semantic_analyze_target(
                next_id, next_id, state, state.tree, None, final_iteration, patches
            )
            all_deferred += deferred
            any_progress = any_progress or progress
            if not incomplete:
                state.manager.incomplete_namespaces.discard(next_id)
        if final_iteration:
            assert not all_deferred, "Must not defer during final iteration"
        # Reverse to process the targets in the same order on every iteration. This avoids
        # processing the same target twice in a row, which is inefficient.
        worklist = list(reversed(all_deferred))
        final_iteration = not any_progress


def order_by_subclassing(targets: list[FullTargetInfo]) -> Iterator[FullTargetInfo]:
    """Make sure that superclass methods are always processed before subclass methods.

    This algorithm is not very optimal, but it is simple and should work well for lists
    that are already almost correctly ordered.
    """

    # First, group the targets by their TypeInfo (since targets are sorted by line,
    # we know that each TypeInfo will appear as group key only once).
    grouped = [(k, list(g)) for k, g in groupby(targets, key=lambda x: x[3])]
    remaining_infos = {info for info, _ in grouped if info is not None}

    next_group = 0
    while grouped:
        if next_group >= len(grouped):
            # This should never happen, if there is an MRO cycle, it should be reported
            # and fixed during top-level processing.
            raise ValueError("Cannot order method targets by MRO")
        next_info, group = grouped[next_group]
        if next_info is None:
            # Trivial case, not methods but functions, process them straight away.
            yield from group
            grouped.pop(next_group)
            continue
        if any(parent in remaining_infos for parent in next_info.mro[1:]):
            # We cannot process this method group yet, try a next one.
            next_group += 1
            continue
        yield from group
        grouped.pop(next_group)
        remaining_infos.discard(next_info)
        # Each time after processing a method group we should retry from start,
        # since there may be some groups that are not blocked on parents anymore.
        next_group = 0


def process_functions(graph: Graph, scc: list[str], patches: Patches) -> None:
    # Process functions.
    all_targets = []
    for module in scc:
        tree = graph[module].tree
        assert tree is not None
        # In principle, functions can be processed in arbitrary order,
        # but _methods_ must be processed in the order they are defined,
        # because some features (most notably partial types) depend on
        # order of definitions on self.
        #
        # There can be multiple generated methods per line. Use target
        # name as the second sort key to get a repeatable sort order.
        targets = sorted(get_all_leaf_targets(tree), key=lambda x: (x[1].line, x[0]))
        all_targets.extend(
            [(module, target, node, active_type) for target, node, active_type in targets]
        )

    for module, target, node, active_type in order_by_subclassing(all_targets):
        analyzer = graph[module].manager.semantic_analyzer
        assert isinstance(node, (FuncDef, OverloadedFuncDef, Decorator)), node
        process_top_level_function(
            analyzer, graph[module], module, target, node, active_type, patches
        )


def process_top_level_function(
    analyzer: SemanticAnalyzer,
    state: State,
    module: str,
    target: str,
    node: FuncDef | OverloadedFuncDef | Decorator,
    active_type: TypeInfo | None,
    patches: Patches,
) -> None:
    """Analyze single top-level function or method.

    Process the body of the function (including nested functions) again and again,
    until all names have been resolved (or iteration limit reached).
    """
    # We need one more iteration after incomplete is False (e.g. to report errors, if any).
    final_iteration = False
    incomplete = True
    # Start in the incomplete state (no missing names will be reported on first pass).
    # Note that we use module name, since functions don't create qualified names.
    deferred = [module]
    analyzer.deferral_debug_context.clear()
    analyzer.incomplete_namespaces.add(module)
    iteration = 0
    while deferred:
        iteration += 1
        if iteration == MAX_ITERATIONS:
            # Just pick some module inside the current SCC for error context.
            assert state.tree is not None
            with analyzer.file_context(state.tree, state.options):
                analyzer.report_hang()
            break
        if not (deferred or incomplete) or final_iteration:
            # OK, this is one last pass, now missing names will be reported.
            analyzer.incomplete_namespaces.discard(module)
        deferred, incomplete, progress = semantic_analyze_target(
            target, module, state, node, active_type, final_iteration, patches
        )
        if not incomplete:
            state.manager.incomplete_namespaces.discard(module)
        if final_iteration:
            assert not deferred, "Must not defer during final iteration"
        if not progress:
            final_iteration = True

    analyzer.incomplete_namespaces.discard(module)
    # After semantic analysis is done, discard local namespaces
    # to avoid memory hoarding.
    analyzer.saved_locals.clear()


TargetInfo: _TypeAlias = tuple[
    str, Union[MypyFile, FuncDef, OverloadedFuncDef, Decorator], Optional[TypeInfo]
]

# Same as above but includes module as first item.
FullTargetInfo: _TypeAlias = tuple[
    str, str, Union[MypyFile, FuncDef, OverloadedFuncDef, Decorator], Optional[TypeInfo]
]


def get_all_leaf_targets(file: MypyFile) -> list[TargetInfo]:
    """Return all leaf targets in a symbol table (module-level and methods)."""
    result: list[TargetInfo] = []
    for fullname, node, active_type in file.local_definitions():
        if isinstance(node.node, (FuncDef, OverloadedFuncDef, Decorator)):
            result.append((fullname, node.node, active_type))
    return result


def semantic_analyze_target(
    target: str,
    module: str,
    state: State,
    node: MypyFile | FuncDef | OverloadedFuncDef | Decorator,
    active_type: TypeInfo | None,
    final_iteration: bool,
    patches: Patches,
) -> tuple[list[str], bool, bool]:
    """Semantically analyze a single target.

    Return tuple with these items:
    - list of deferred targets
    - was some definition incomplete (need to run another pass)
    - were any new names defined (or placeholders replaced)
    """
    state.manager.processed_targets.append((module, target))
    tree = state.tree
    assert tree is not None
    analyzer = state.manager.semantic_analyzer
    # TODO: Move initialization to somewhere else
    analyzer.global_decls = [set()]
    analyzer.nonlocal_decls = [set()]
    analyzer.globals = tree.names
    analyzer.imports = set()
    analyzer.progress = False
    with state.wrap_context(check_blockers=False):
        refresh_node = node
        if isinstance(refresh_node, Decorator):
            # Decorator expressions will be processed as part of the module top level.
            refresh_node = refresh_node.func
        analyzer.refresh_partial(
            refresh_node,
            patches,
            final_iteration,
            file_node=tree,
            options=state.options,
            active_type=active_type,
        )
        if isinstance(node, Decorator):
            infer_decorator_signature_if_simple(node, analyzer)
    for dep in analyzer.imports:
        state.add_dependency(dep)
        priority = mypy.build.PRI_LOW
        if priority <= state.priorities.get(dep, priority):
            state.priorities[dep] = priority

    # Clear out some stale data to avoid memory leaks and astmerge
    # validity check confusion
    analyzer.statement = None
    del analyzer.cur_mod_node

    if analyzer.deferred:
        return [target], analyzer.incomplete, analyzer.progress
    else:
        return [], analyzer.incomplete, analyzer.progress


def check_type_arguments(graph: Graph, scc: list[str], errors: Errors) -> None:
    for module in scc:
        state = graph[module]
        assert state.tree
        analyzer = TypeArgumentAnalyzer(
            errors,
            state.options,
            state.tree.is_typeshed_file(state.options),
            state.manager.semantic_analyzer.named_type,
        )
        with state.wrap_context():
            with mypy.state.state.strict_optional_set(state.options.strict_optional):
                state.tree.accept(analyzer)


def check_type_arguments_in_targets(
    targets: list[FineGrainedDeferredNode], state: State, errors: Errors
) -> None:
    """Check type arguments against type variable bounds and restrictions.

    This mirrors the logic in check_type_arguments() except that we process only
    some targets. This is used in fine grained incremental mode.
    """
    analyzer = TypeArgumentAnalyzer(
        errors,
        state.options,
        is_typeshed_file(state.options.abs_custom_typeshed_dir, state.path or ""),
        state.manager.semantic_analyzer.named_type,
    )
    with state.wrap_context():
        with mypy.state.state.strict_optional_set(state.options.strict_optional):
            for target in targets:
                func: FuncDef | OverloadedFuncDef | None = None
                if isinstance(target.node, (FuncDef, OverloadedFuncDef)):
                    func = target.node
                saved = (state.id, target.active_typeinfo, func)  # module, class, function
                with errors.scope.saved_scope(saved) if errors.scope else nullcontext():
                    analyzer.recurse_into_functions = func is not None
                    target.node.accept(analyzer)


def apply_class_plugin_hooks(graph: Graph, scc: list[str], errors: Errors) -> None:
    """Apply class plugin hooks within a SCC.

    We run these after to the main semantic analysis so that the hooks
    don't need to deal with incomplete definitions such as placeholder
    types.

    Note that some hooks incorrectly run during the main semantic
    analysis pass, for historical reasons.
    """
    num_passes = 0
    incomplete = True
    # If we encounter a base class that has not been processed, we'll run another
    # pass. This should eventually reach a fixed point.
    while incomplete:
        assert num_passes < 10, "Internal error: too many class plugin hook passes"
        num_passes += 1
        incomplete = False
        for module in scc:
            state = graph[module]
            tree = state.tree
            assert tree
            for _, node, _ in tree.local_definitions():
                if isinstance(node.node, TypeInfo):
                    if not apply_hooks_to_class(
                        state.manager.semantic_analyzer,
                        module,
                        node.node,
                        state.options,
                        tree,
                        errors,
                    ):
                        incomplete = True


def apply_hooks_to_class(
    self: SemanticAnalyzer,
    module: str,
    info: TypeInfo,
    options: Options,
    file_node: MypyFile,
    errors: Errors,
) -> bool:
    # TODO: Move more class-related hooks here?
    defn = info.defn
    ok = True
    for decorator in defn.decorators:
        with self.file_context(file_node, options, info):
            hook = None

            decorator_name = self.get_fullname_for_hook(decorator)
            if decorator_name:
                hook = self.plugin.get_class_decorator_hook_2(decorator_name)
            # Special case: if the decorator is itself decorated with
            # typing.dataclass_transform, apply the hook for the dataclasses plugin
            # TODO: remove special casing here
            if hook is None and find_dataclass_transform_spec(decorator):
                hook = dataclasses_plugin.dataclass_class_maker_callback

            if hook:
                ok = ok and hook(ClassDefContext(defn, decorator, self))

    # Check if the class definition itself triggers a dataclass transform (via a parent class/
    # metaclass)
    spec = find_dataclass_transform_spec(info)
    if spec is not None:
        with self.file_context(file_node, options, info):
            # We can't use the normal hook because reason = defn, and ClassDefContext only accepts
            # an Expression for reason
            ok = ok and dataclasses_plugin.DataclassTransformer(defn, defn, spec, self).transform()

    return ok


def calculate_class_properties(graph: Graph, scc: list[str], errors: Errors) -> None:
    builtins = graph["builtins"].tree
    assert builtins
    for module in scc:
        state = graph[module]
        tree = state.tree
        assert tree
        for _, node, _ in tree.local_definitions():
            if isinstance(node.node, TypeInfo):
                with state.manager.semantic_analyzer.file_context(tree, state.options, node.node):
                    calculate_class_abstract_status(node.node, tree.is_stub, errors)
                    check_protocol_status(node.node, errors)
                    calculate_class_vars(node.node)
                    add_type_promotion(
                        node.node, tree.names, graph[module].options, builtins.names
                    )


def check_blockers(graph: Graph, scc: list[str]) -> None:
    for module in scc:
        graph[module].check_blockers()
