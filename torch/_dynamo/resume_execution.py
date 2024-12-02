# mypy: allow-untyped-defs
import copy
import dataclasses
import sys
import types
from typing import Any, cast, Dict, List, Optional, Tuple

from .bytecode_transformation import (
    bytecode_from_template,
    create_call_function,
    create_instruction,
    create_jump_absolute,
    create_load_const,
    Instruction,
    overwrite_instruction,
    transform_code_object,
    unique_id,
)
from .utils import ExactWeakKeyDictionary


# taken from code.h in cpython
CO_OPTIMIZED = 0x0001
CO_NEWLOCALS = 0x0002
CO_VARARGS = 0x0004
CO_VARKEYWORDS = 0x0008
CO_NESTED = 0x0010
CO_GENERATOR = 0x0020
CO_NOFREE = 0x0040
CO_COROUTINE = 0x0080
CO_ITERABLE_COROUTINE = 0x0100
CO_ASYNC_GENERATOR = 0x0200

# trace_rules.py import this constant for consistency
TORCH_DYNAMO_RESUME_IN_PREFIX = "torch_dynamo_resume_in"


def _initial_push_null(insts):
    if sys.version_info >= (3, 11):
        insts.append(create_instruction("PUSH_NULL"))
        if sys.version_info < (3, 13):
            insts.append(create_instruction("SWAP", arg=2))


# Generates bytecode from template and splits the code where LOAD_FAST dummy is present.
def _bytecode_from_template_with_split(template, stack_index, varname_map=None):
    template_code = bytecode_from_template(template, varname_map=varname_map)
    template_code.append(create_instruction("POP_TOP"))

    # adjust exception table entry depth
    for inst in template_code:
        if inst.exn_tab_entry:
            inst.exn_tab_entry.depth += stack_index

    # search for LOAD_FAST dummy and replace it with 2 NOPs (we can break up the bytecode between them)
    dummy_idx, dummy_inst = next(
        (
            (i, inst)
            for i, inst in enumerate(template_code)
            if inst.opname == "LOAD_FAST" and inst.argval == "dummy"
        ),
        (None, None),
    )
    assert dummy_idx is not None

    # replace LOAD_FAST dummy with first NOP marking exception area
    overwrite_instruction(dummy_inst, [create_instruction("NOP")])

    # POP_TOP follows LOAD_FAST dummy - replace with NOP marking end of exception area
    assert template_code[dummy_idx + 1].opname == "POP_TOP"
    overwrite_instruction(template_code[dummy_idx + 1], [create_instruction("NOP")])

    return template_code[: dummy_idx + 1], template_code[dummy_idx + 1 :]


def _try_except_tf_mode_template(dummy, stack_var_name):
    # NOTE: Make sure this name matches what is generated by symbolic_convert:import_source
    # on torch._dynamo.utils.
    global __import_torch_dot__dynamo_dot_utils
    try:
        dummy
    except:  # noqa: E722, B001
        __import_torch_dot__dynamo_dot_utils.set_torch_function_mode_stack(  # type: ignore[name-defined]
            stack_var_name
        )
        raise


@dataclasses.dataclass(frozen=True)
class ReenterWith:
    stack_index: int
    target_values: Optional[Tuple[Any, ...]] = None

    def try_except_torch_function_mode(self, code_options, cleanup: List[Instruction]):
        """
        Codegen based off of:
        try:
            (rest)
        except:
            (restore previous tf mode stack)
            raise
        """
        from .variables.torch_function import get_prev_stack_var_name

        setup_try_except, epilogue = _bytecode_from_template_with_split(
            _try_except_tf_mode_template,
            self.stack_index,
            varname_map={"stack_var_name": get_prev_stack_var_name()},
        )
        cleanup[:] = epilogue + cleanup

        return setup_try_except

    # If we do not want to destroy the stack, we can do the same thing as a
    # `SETUP_WITH` block, only that we store the context manager in a local_symbol
    def try_finally(self, code_options, cleanup: List[Instruction]):
        """
        Codegen based off of:
        load args
        enter context
        try:
            (rest)
        finally:
            exit context
        """
        # NOTE: we assume that TOS is a context manager CLASS!
        load_args = []
        if self.target_values:
            load_args = [create_load_const(val) for val in self.target_values]
        ctx_name = unique_id(f"___context_manager_{self.stack_index}")
        if ctx_name not in code_options["co_varnames"]:
            code_options["co_varnames"] += (ctx_name,)
        for name in ["__enter__", "__exit__"]:
            if name not in code_options["co_names"]:
                code_options["co_names"] += (name,)

        create_ctx: List[Instruction] = []
        _initial_push_null(create_ctx)
        create_ctx.extend(
            [
                *load_args,
                *create_call_function(len(load_args), False),
                create_instruction("STORE_FAST", argval=ctx_name),
            ]
        )

        def _template(ctx, dummy):
            ctx.__enter__()
            try:
                dummy
            finally:
                ctx.__exit__(None, None, None)

        setup_try_finally, epilogue = _bytecode_from_template_with_split(
            _template, self.stack_index, varname_map={"ctx": ctx_name}
        )
        cleanup[:] = epilogue + cleanup
        return create_ctx + setup_try_finally

    def __call__(self, code_options, cleanup):
        """
        Codegen based off of:
        with ctx(args):
            (rest)
        """
        # NOTE: we assume that TOS is a context manager CLASS!
        load_args = []
        if self.target_values:
            load_args = [create_load_const(val) for val in self.target_values]

        create_ctx: List[Instruction] = []
        _initial_push_null(create_ctx)
        create_ctx.extend(
            [
                *load_args,
                *create_call_function(len(load_args), False),
            ]
        )

        def _template(ctx, dummy):
            with ctx:
                dummy

        setup_with, epilogue = _bytecode_from_template_with_split(
            _template, self.stack_index
        )
        cleanup[:] = epilogue + cleanup

        load_fast_ctx_inst = next(
            (
                inst
                for inst in setup_with
                if inst.opname == "LOAD_FAST" and inst.argval == "ctx"
            ),
            None,
        )
        assert load_fast_ctx_inst is not None
        # ctx already loaded on stack before the template - no need to LOAD_FAST
        overwrite_instruction(load_fast_ctx_inst, [create_instruction("NOP")])

        # 3.11+ only
        push_exc_info_gen = (
            inst for inst in epilogue if inst.opname == "PUSH_EXC_INFO"
        )
        push_exc_info_inst = next(push_exc_info_gen, None)
        # expect only 1 PUSH_EXC_INFO in epilogue
        assert next(push_exc_info_gen, None) is None

        return create_ctx + setup_with, push_exc_info_inst


@dataclasses.dataclass
class ResumeFunctionMetadata:
    code: types.CodeType
    instructions: List[Instruction] = dataclasses.field(default_factory=list)
    # Python 3.11+ fields
    # NOTE: Python 3.11 removed blocks, but for our purposes, a "block" consists
    # of instructions of all exception table entries that have the same target.

    # map from PUSH_EXC_INFO's in the prefix to original block target offset
    prefix_block_target_offset_remap: List[int] = dataclasses.field(
        default_factory=list
    )
    # map from new block target offsets to original block target offsets
    block_target_offset_remap: Optional[Dict[int, int]] = None


def _filter_iter(l1, l2, cond):
    """
    Two-pointer conditional filter.
    e.g. _filter_iter(insts, sorted_offsets, lambda i, o: i.offset == o)
    returns the instructions with offsets in sorted_offsets
    """
    it = iter(l2)
    res: List[Instruction] = []
    try:
        cur = next(it)
        for val in l1:
            if cond(val, cur):
                res.append(val)
                cur = next(it)
    except StopIteration:
        pass
    return res


def _load_tuple_and_call(tup):
    insts: List[Instruction] = []
    _initial_push_null(insts)
    insts.extend(create_load_const(val) for val in tup)
    insts.extend(create_call_function(len(tup), False))
    return insts


class ContinueExecutionCache:
    cache = ExactWeakKeyDictionary()
    generated_code_metadata = ExactWeakKeyDictionary()

    @classmethod
    def lookup(cls, code, lineno, *key):
        if code not in cls.cache:
            cls.cache[code] = {}
        key = tuple(key)
        if key not in cls.cache[code]:
            cls.cache[code][key] = cls.generate(code, lineno, *key)
        return cls.cache[code][key]

    @classmethod
    def generate(
        cls,
        code,
        lineno,
        offset: int,
        setup_fn_target_offsets: Tuple[int, ...],  # only used in Python 3.11+
        nstack: int,
        argnames: Tuple[str, ...],
        argnames_null: Tuple[str, ...],
        setup_fns: Tuple[ReenterWith, ...],
        stack_ctx_vars: Tuple[Tuple[int, Tuple[Any]], ...],
        argnames_ctx_vars: Tuple[Tuple[str, Tuple[Any]], ...],
        null_idxes: Tuple[int, ...],
    ) -> types.CodeType:
        assert offset is not None
        assert not (
            code.co_flags
            & (CO_GENERATOR | CO_COROUTINE | CO_ITERABLE_COROUTINE | CO_ASYNC_GENERATOR)
        )
        assert code.co_flags & CO_OPTIMIZED
        if code in ContinueExecutionCache.generated_code_metadata:
            return cls.generate_based_on_original_code_object(
                code,
                lineno,
                offset,
                setup_fn_target_offsets,
                nstack,
                argnames,
                argnames_null,
                setup_fns,
                stack_ctx_vars,
                argnames_ctx_vars,
                null_idxes,
            )

        is_py311_plus = sys.version_info >= (3, 11)
        meta = ResumeFunctionMetadata(code)

        def update(instructions: List[Instruction], code_options: Dict[str, Any]):
            meta.instructions = copy.deepcopy(instructions)

            args = [f"___stack{i}" for i in range(nstack)]
            args.extend(v for v in argnames if v not in args)
            freevars = tuple(code_options["co_cellvars"] or []) + tuple(
                code_options["co_freevars"] or []
            )
            freevars = tuple(sorted(freevars))
            code_options[
                "co_name"
            ] = f"{TORCH_DYNAMO_RESUME_IN_PREFIX}_{code_options['co_name']}_at_{lineno}"
            if is_py311_plus:
                qualified_path = code_options["co_qualname"].rsplit(".", maxsplit=1)
                if len(qualified_path) == 1:
                    code_options["co_qualname"] = code_options["co_name"]
                else:
                    assert len(qualified_path) == 2
                    module_name, co_name = qualified_path
                    code_options[
                        "co_qualname"
                    ] = f"{module_name}.{TORCH_DYNAMO_RESUME_IN_PREFIX}_{co_name}_at_{lineno}"
            code_options["co_firstlineno"] = lineno
            code_options["co_cellvars"] = ()
            code_options["co_freevars"] = freevars
            code_options["co_argcount"] = len(args)
            code_options["co_posonlyargcount"] = 0
            code_options["co_kwonlyargcount"] = 0
            code_options["co_varnames"] = tuple(
                args
                + [v for v in argnames_null if v not in args]
                + [v for v in code_options["co_varnames"] if v not in args]
            )
            code_options["co_flags"] = code_options["co_flags"] & ~(
                CO_VARARGS | CO_VARKEYWORDS
            )
            target = next(i for i in instructions if i.offset == offset)

            prefix = []
            if is_py311_plus:
                if freevars:
                    prefix.append(
                        create_instruction("COPY_FREE_VARS", arg=len(freevars))
                    )
                prefix.append(create_instruction("RESUME", arg=0))

            cleanup: List[Instruction] = []
            hooks = {fn.stack_index: fn for fn in setup_fns}
            hook_target_offsets = {
                fn.stack_index: setup_fn_target_offsets[i]
                for i, fn in enumerate(setup_fns)
            }
            offset_to_inst = {inst.offset: inst for inst in instructions}
            # map old hook targets to new targets generated by the hook
            old_hook_target_remap = {}
            null_idxes_i = 0
            stack_ctx_vars_d = dict(stack_ctx_vars)  # type: ignore[var-annotated,arg-type]
            for i in range(nstack):
                while (
                    null_idxes_i < len(null_idxes)
                    and null_idxes[null_idxes_i] == i + null_idxes_i
                ):
                    prefix.append(create_instruction("PUSH_NULL"))
                    null_idxes_i += 1
                prefix.append(create_instruction("LOAD_FAST", argval=f"___stack{i}"))
                if i in hooks:
                    hook = hooks.pop(i)
                    hook_insts, exn_target = hook(code_options, cleanup)
                    prefix.extend(hook_insts)
                    if is_py311_plus:
                        hook_target_offset = hook_target_offsets.pop(i)
                        old_hook_target = offset_to_inst[hook_target_offset]
                        meta.prefix_block_target_offset_remap.append(hook_target_offset)
                        old_hook_target_remap[old_hook_target] = exn_target
                real_i = i + null_idxes_i
                if real_i in stack_ctx_vars_d:
                    # NOTE: we assume that current stack var is a context manager CLASS!
                    # Load args for context variable and construct it
                    prefix.extend(_load_tuple_and_call(stack_ctx_vars_d[real_i]))

            if is_py311_plus:
                # reverse the mapping since targets of later/nested contexts are inserted
                # into the mapping later, but show up earlier in the prefix.
                meta.prefix_block_target_offset_remap = list(
                    reversed(meta.prefix_block_target_offset_remap)
                )

            assert not hooks

            # NOTE: we assume that local var is a context manager CLASS!
            # initialize inactive context vars in argnames
            for name, vals in argnames_ctx_vars:
                prefix.append(create_instruction("LOAD_FAST", argval=name))
                prefix.extend(_load_tuple_and_call(vals))
                prefix.append(create_instruction("STORE_FAST", argval=name))

            # 3.12+: store NULL into variables that were NULL
            if argnames_null:
                assert sys.version_info >= (3, 12)
                for v in argnames_null:
                    assert v not in args
                    prefix.extend(
                        [
                            create_instruction("PUSH_NULL"),
                            create_instruction("STORE_FAST", argval=v),
                        ]
                    )

            prefix.append(create_jump_absolute(target))

            # because the line number table monotonically increases from co_firstlineno
            # remove starts_line for any instructions before the graph break instruction
            # this will ensure the instructions after the break have the correct line numbers
            for inst in instructions:
                if inst.offset == target.offset:
                    break
                inst.starts_line = None
                if sys.version_info >= (3, 11):
                    inst.positions = None

            if cleanup:
                prefix.extend(cleanup)
                prefix.extend(cls.unreachable_codes(code_options))

            # remap original instructions' exception table entries
            if old_hook_target_remap:
                assert is_py311_plus
                for inst in instructions:
                    if (
                        inst.exn_tab_entry
                        and inst.exn_tab_entry.target in old_hook_target_remap
                    ):
                        inst.exn_tab_entry.target = old_hook_target_remap[
                            inst.exn_tab_entry.target
                        ]

            # TODO(jansel): add dead code elimination here
            instructions[:] = prefix + instructions

        new_code = transform_code_object(code, update)
        ContinueExecutionCache.generated_code_metadata[new_code] = meta
        return new_code

    @staticmethod
    def unreachable_codes(code_options) -> List[Instruction]:
        """Codegen a `raise None` to make analysis work for unreachable code"""
        return [
            create_load_const(None),
            create_instruction("RAISE_VARARGS", arg=1),
        ]

    @classmethod
    def generate_based_on_original_code_object(
        cls, code, lineno, offset: int, setup_fn_target_offsets: Tuple[int, ...], *args
    ):
        """
        This handles the case of generating a resume into code generated
        to resume something else.  We want to always generate starting
        from the original code object so that if control flow paths
        converge we only generated 1 resume function (rather than 2^n
        resume functions).
        """

        meta: ResumeFunctionMetadata = ContinueExecutionCache.generated_code_metadata[
            code
        ]
        new_offset = None

        def find_new_offset(
            instructions: List[Instruction], code_options: Dict[str, Any]
        ):
            nonlocal new_offset
            (target,) = (i for i in instructions if i.offset == offset)
            # match the functions starting at the last instruction as we have added a prefix
            (new_target,) = (
                i2
                for i1, i2 in zip(reversed(instructions), reversed(meta.instructions))
                if i1 is target
            )
            assert target.opcode == new_target.opcode
            new_offset = new_target.offset

        transform_code_object(code, find_new_offset)

        if sys.version_info >= (3, 11):
            # setup_fn_target_offsets currently contains the target offset of
            # each setup_fn, based on `code`. When we codegen the resume function
            # based on the original code object, `meta.code`, the offsets in
            # setup_fn_target_offsets must be based on `meta.code` instead.
            if not meta.block_target_offset_remap:
                block_target_offset_remap = meta.block_target_offset_remap = {}

                def remap_block_offsets(
                    instructions: List[Instruction], code_options: Dict[str, Any]
                ):
                    # NOTE: each prefix block generates exactly one PUSH_EXC_INFO,
                    # so we can tell which block a prefix PUSH_EXC_INFO belongs to,
                    # by counting. Then we can use meta.prefix_block-target_offset_remap
                    # to determine where in the original code the PUSH_EXC_INFO offset
                    # replaced.
                    prefix_blocks: List[Instruction] = []
                    for inst in instructions:
                        if len(prefix_blocks) == len(
                            meta.prefix_block_target_offset_remap
                        ):
                            break
                        if inst.opname == "PUSH_EXC_INFO":
                            prefix_blocks.append(inst)

                    # offsets into prefix
                    for inst, o in zip(
                        prefix_blocks, meta.prefix_block_target_offset_remap
                    ):
                        block_target_offset_remap[cast(int, inst.offset)] = o

                    # old bytecode targets are after the prefix PUSH_EXC_INFO's
                    old_start_offset = (
                        cast(int, prefix_blocks[-1].offset) if prefix_blocks else -1
                    )
                    # offsets into old bytecode
                    old_inst_offsets = sorted(
                        n for n in setup_fn_target_offsets if n > old_start_offset
                    )
                    targets = _filter_iter(
                        instructions, old_inst_offsets, lambda inst, o: inst.offset == o
                    )
                    new_targets = _filter_iter(
                        zip(reversed(instructions), reversed(meta.instructions)),
                        targets,
                        lambda v1, v2: v1[0] is v2,
                    )
                    for new, old in zip(new_targets, targets):
                        block_target_offset_remap[old.offset] = new[1].offset

                transform_code_object(code, remap_block_offsets)

            # if offset is not in setup_fn_target_offsets, it is an error
            setup_fn_target_offsets = tuple(
                meta.block_target_offset_remap[n] for n in setup_fn_target_offsets
            )
        return ContinueExecutionCache.lookup(
            meta.code, lineno, new_offset, setup_fn_target_offsets, *args
        )


"""
# partially finished support for with statements

def convert_locals_to_cells(
        instructions: List[Instruction],
        code_options: Dict[str, Any]):

    code_options["co_cellvars"] = tuple(
        var
        for var in code_options["co_varnames"]
        if var not in code_options["co_freevars"]
        and not var.startswith("___stack")
    )
    cell_and_free = code_options["co_cellvars"] + code_options["co_freevars"]
    for inst in instructions:
        if str(inst.argval).startswith("___stack"):
            continue
        elif inst.opname == "LOAD_FAST":
            inst.opname = "LOAD_DEREF"
        elif inst.opname == "STORE_FAST":
            inst.opname = "STORE_DEREF"
        elif inst.opname == "DELETE_FAST":
            inst.opname = "DELETE_DEREF"
        else:
            continue
        inst.opcode = dis.opmap[inst.opname]
        assert inst.argval in cell_and_free, inst.argval
        inst.arg = cell_and_free.index(inst.argval)

def patch_setup_with(
    instructions: List[Instruction],
    code_options: Dict[str, Any]
):
    nonlocal need_skip
    need_skip = True
    target_index = next(
        idx for idx, i in enumerate(instructions) if i.offset == offset
    )
    assert instructions[target_index].opname == "SETUP_WITH"
    convert_locals_to_cells(instructions, code_options)

    stack_depth_before = nstack + stack_effect(instructions[target_index].opcode,
                                               instructions[target_index].arg)

    inside_with = []
    inside_with_resume_at = None
    stack_depth = stack_depth_before
    idx = target_index + 1
    for idx in range(idx, len(instructions)):
        inst = instructions[idx]
        if inst.opname == "BEGIN_FINALLY":
            inside_with_resume_at = inst
            break
        elif inst.target is not None:
            unimplemented("jump from with not supported")
        elif inst.opname in ("BEGIN_FINALLY", "WITH_CLEANUP_START", "WITH_CLEANUP_FINISH", "END_FINALLY",
                             "POP_FINALLY", "POP_EXCEPT",
                             "POP_BLOCK", "END_ASYNC_FOR"):
            unimplemented("block ops not supported")
        inside_with.append(inst)
        stack_depth += stack_effect(inst.opcode, inst.arg)
    assert inside_with_resume_at

    instructions = [
        create_instruction("LOAD_FAST", f"___stack{i}") for i in range(nstack)
    ] + [
        create_instruction("SETUP_WITH", target=instructions[target_index].target)
        ... call the function ...
        unpack_tuple
    ] + [
        create_instruction("JUMP_ABSOLUTE", target=inside_with_resume_at)
    ]
"""
