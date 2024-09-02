import contextlib
import weakref

import torch
from torch.utils.weak import WeakTensorKeyDictionary
from typing import *  # noqa: F403


# Union find over versions of tensors
# -----------------------------------
# TensorUnionFind implements union-find over "versions of tensors". What this
# means is that if TensorUnionFind holds a set of two tensors {`a`, `b`} with
# `a` as the canonical entry, we should really think of it as holding {(`a`, 0),
# (`b`, 0)} with (`a`, 0) as the canonical entry, where the 0s are the versions
# of the tensors.
#
# If the tensor `a` is mutated, incrementing its version e.g. to 1, that would
# mean that if someone does uf.find(a), they are really querying for
# uf.find((`a`, 1)). That would return (`a`, 1) because (`a`, 1) is in a new set
# of its own and hence is the canonical entry of that set.
#
# After all of this, we expect the union-find to now hold two sets:
# {(`a`, 1)} and {(`a`, 0), (`b`, 0)}
#
# This situation can be problematic if someone does uf.find(b), i.e.,
# uf.find((`b`, 0)). Since the canonical entry of the original set continues to
# point to an older version of `a` i.e. (`a`, 0), there is no tensor for
# find to return, and we raise an error in this case.

# Storing metadata on canonical entries
# -------------------------------------
# We associate each set with exactly one metadata dict object. We maintain
# this invariant by storing a map from canonical tenosors to metadata dict
# objects. Extra logic is done on-merge in order to maintain this mapping.
#
# When two sets A, B are merged, one of the two canonical entries is chosen to
# be the canonical entry of the union. The other is no longer the canonical
# entry of any set. To maintain the canonical entry -> metadata dict mapping, we
# merge A and B's metadata dict objects and store the merged dict on the new
# canonical entry. The metadata of the no-longer-canonical entry is invalidated.
#
# In the case where the two dicts have different entries for
# the same key, we have a choice of which metadata to favor. In this class, we
# somewhat arbitrarily chose to favor the metadata of the NON-canonical tensor!
# This might be the opposite of what one would expect, but that shouldn't matter
# because which tensor between a and b union-find chooses to be canonical is an
# implementation detail anyway. The the user should careful to make sure that no
# such conflict can exist. We could raise an error in this case.

# Copy paste of torch/fx/experimental/optimization.py except size is not fixed
class UnionFind:
    def __init__(self):
        self.parent = dict()
        self.size = dict()

    def find(self, v: int) -> int:
        if v not in self.parent:
            self.parent[v] = v
            self.size[v] = 1

        par = self.parent[v]
        if v == par:
            return v
        assert par is not None
        self.parent[v] = self.find(par)
        return cast(int, self.parent[v])

    def merge(self, a: int, b: int):
        a, b = self.find(a), self.find(b)
        if a == b:
            return a
        if self.size[a] < self.size[b]:
            a, b = b, a
        self.parent[b] = a
        self.size[a] += self.size[b]

    # TODO(soulitzer): what does the default copy do?
    def copy(self):
        uf = UnionFind()
        uf.parent = self.parent.copy()
        uf.size = self.size.copy()
        return uf

class TensorIntMap:
    # Assigns Tensor objects to unique ints in an incrementing fashion.
    # The int given corresponds to a particular version of a Tensor.
    # If a Tensor has been mutated, its original int is invalidated, and
    # it will be assigned a new int upon the next get_int.
    # We try to be careful to NOT hold any owning references.
    _incrementing_id = 0
    _tensor_to_int_and_version = WeakTensorKeyDictionary()
    _int_to_tensor: Dict[int, weakref.ReferenceType] = dict()

    @torch._dynamo.allow_in_graph
    def get_int(self, t):
        mb_data = self._tensor_to_int_and_version.get(t)
        if mb_data is None or mb_data[1] != t._version:
            self._tensor_to_int_and_version[t] = (self._incrementing_id, t._version)
            self._int_to_tensor[self._incrementing_id] = weakref.ref(t)
            self._incrementing_id += 1
        return self._tensor_to_int_and_version[t][0]

    @torch._dynamo.allow_in_graph
    def get_opt_tensor(self, i):
        # This function may not always succeed. If that Tensor is no longer
        # alive or is no longer the same version i.e. it was mutated, None is
        # returned.
        mb_weak_t = self._int_to_tensor.get(i)
        if mb_weak_t is None:
            return None
        mb_t = mb_weak_t()
        if mb_t is None or (
            self._tensor_to_int_and_version[mb_t][1] != mb_t._version
            or self._tensor_to_int_and_version[mb_t][0] != i
        ):
            del self._int_to_tensor[i]
            return None
        return mb_t

    # Used during fakification
    def replace(self, old_tensor, new_tensor):
        old_int = self.get_int(old_tensor)
        self._tensor_to_int_and_version[new_tensor] = (old_int, new_tensor._version)
        self._int_to_tensor[old_int] = weakref.ref(new_tensor)

    def is_registered(self, t):
        return t in self._tensor_to_int_and_version

    def copy(self):
        new_map = TensorIntMap()
        new_map._incrementing_id = self._incrementing_id
        new_map._tensor_to_int_and_version = self._tensor_to_int_and_version.copy()
        new_map._int_to_tensor = self._int_to_tensor.copy()
        return new_map


def _get_union_find(x):
    from torch._subclasses.fake_tensor import FakeTensor
    from torch._subclasses.functional_tensor import mb_unwrap_functional_tensor

    # NB: Only FakeTensor is associated with a memo
    tensor = mb_unwrap_functional_tensor(x)
    if isinstance(tensor, FakeTensor):
        return tensor.fake_mode.fake_union_find
    return get_union_find()


lib = torch.library.Library("nested", "FRAGMENT")

lib.define("is_same_set(Tensor x, Tensor y) -> bool")

def is_same_set_impl(a, b):
    uf_a = _get_union_find(a)
    uf_b = _get_union_find(b)
    assert uf_a == uf_b
    return uf_a.find(a) is uf_a.find(a)

lib.impl("is_same_set", is_same_set_impl, "CPU")
lib.impl("is_same_set", is_same_set_impl, "CUDA")
lib.impl("is_same_set", is_same_set_impl, "Meta")

def is_same_set(a, b):
    return torch.ops.nested.is_same_set(a, b)

lib.define("merge(Tensor x, Tensor x) -> ()")

def merge_impl(a, b):
    uf_a = _get_union_find(a)
    uf_b = _get_union_find(a)
    assert uf_a == uf_b
    uf_a.merge(a, b)

lib.impl("merge", merge_impl, "CPU")
lib.impl("merge", merge_impl, "CUDA")
lib.impl("merge", merge_impl, "Meta")

def merge(a, b):
    torch.ops.nested.merge(a, b)

lib.define("get_max_seqlen(Tensor x) -> SymInt")

def get_max_seqlen_impl(x):
    uf = _get_union_find(x)
    cached_metadata = uf.get_metadata(x)
    return cached_metadata["_max_seqlen"]

lib.impl("get_max_seqlen", get_max_seqlen_impl, "CPU")
lib.impl("get_max_seqlen", get_max_seqlen_impl, "CUDA")
lib.impl("get_max_seqlen", get_max_seqlen_impl, "Meta")

def get_max_seqlen(x):
    return torch.ops.nested.get_max_seqlen(x)

class TensorUnionFind:
    # Union-find over tensors with some extra functionality
    def __init__(self, tensor_int_map=None):
        # TensorUnionFind is a wrapper around UnionFind on ints
        self._union_find_int = UnionFind()
        self._tensor_int_map = TensorIntMap()
        # 1) Maintains a metadata dict object for each set (see note
        #   "Storing metadata on canonical entries")
        self._metadata: Dict[int, Dict[Any, Any]] = DefaultDict(dict)
        self._equiv_sets: Dict[int, Set[weakref.ReferenceType]] = DefaultDict(set)
        # Sentinel value to indicate that an entry has been invalidated
        self._INVALID_ENTRY = object()

    def merge(self, x, y):
        x_root, y_root = self.find(x), self.find(y)
        if x_root is y_root:
            return
        x_root_int = self._tensor_int_map.get_int(x_root)
        y_root_int = self._tensor_int_map.get_int(y_root)

        self._union_find_int.merge(x_root_int, y_root_int)

        # src and tgt depend on which direction we merged in the actual impl
        tgt, src = (x_root_int, y_root_int) if self._union_find_int.find(x_root_int) is x_root_int else (y_root_int, x_root_int)
        # Store merged metadata onto new canonical entry and invalidate non-canonical
        self._metadata[tgt].update(self._metadata[src])
        self._metadata[src] = self._INVALID_ENTRY
        # Maintain set of weakrefs to all tensors in set
        self._get_equiv_set(tgt).update(self._get_equiv_set(src))

        self._equiv_sets[src] = self._INVALID_ENTRY

    def is_registered(self, tensor):
        return self._tensor_int_map.is_registered(tensor)

    def find(self, tensor):
        assert isinstance(tensor, torch.Tensor)
        canonical_id = self._union_find_int.find(self._tensor_int_map.get_int(tensor))
        mb_tensor = self._tensor_int_map.get_opt_tensor(canonical_id)
        if mb_tensor is None:
            # See note "Union find over versions of tensors".
            raise RuntimeError("The canonical tensor of this set has been mutated.")
        return mb_tensor

    def _get_equiv_set(self, canonical_int):
        canonical_tensor = self._tensor_int_map.get_opt_tensor(canonical_int)
        self._equiv_sets[canonical_int].add(weakref.ref(canonical_tensor))
        return self._equiv_sets[canonical_int]

    def get_metadata(self, tensor):
        ret = self._metadata[self._union_find_int.find(self._tensor_int_map.get_int(tensor))]
        assert ret is not self._INVALID_ENTRY
        return ret

    def get_equiv_tensors(self, tensor):
        equiv_set = self._get_equiv_set(self._union_find_int.find(self._tensor_int_map.get_int(tensor)))
        assert equiv_set is not self._INVALID_ENTRY
        to_remove = set()
        for weak_tensor in equiv_set:
            mb_tensor = weak_tensor()
            if mb_tensor is not None:
                yield mb_tensor
            else:
                to_remove.add(weak_tensor)
        equiv_set -= to_remove

    def validate_invariants(self):
        # for testing only
        for t_id, v in self._metadata.items():
            mb_t = self._tensor_int_map.get_opt_tensor(t_id)
            if mb_t is None:
                # Do we do anything here?
                continue
            assert (self.find(mb_t) is mb_t) == (v is not self._INVALID_ENTRY)
        for t_id, v in self._equiv_sets.items():
            mb_t = self._tensor_int_map.get_opt_tensor(t_id)
            if mb_t is None:
                # Do we do anything here?
                continue
            assert (self.find(mb_t) is mb_t) == (v is not self._INVALID_ENTRY)

    def print_state(self):
        # useful for debugging
        for t_id in self._tensor_int_map._int_to_tensor.keys():
            mb_t = self._tensor_int_map.get_opt_tensor(t_id)
            if mb_t is None:
                continue
            def fn(t):
                return f"{id(t)}:{str(t.__class__)}:{t.device}"
            print(fn(mb_t), fn(self.find(mb_t)))

    def copy(self):
        new_uf = TensorUnionFind()
        new_uf._tensor_int_map = self._tensor_int_map.copy()
        new_uf._union_find_int = self._union_find_int.copy()
        new_uf._metadata = DefaultDict(
            self._metadata.default_factory,  # Preserves the default factory of the original defaultdict
            {k: (new_uf._INVALID_ENTRY if v is self._INVALID_ENTRY else v.copy()) for (k, v) in self._metadata.items()}
        )
        new_uf._equiv_sets = self._equiv_sets.copy()
        return new_uf

_union_find = None

def get_union_find():
    global _union_find
    if _union_find is None:
        _union_find = TensorUnionFind()
    return _union_find
