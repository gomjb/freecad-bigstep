"""Turn off OCCT's shape healing during STEP transfer (OCCT 8.0).

During `STEPCAFControl_Reader::Transfer` OCCT runs ShapeFix on every shape, on
one core.  On large files that is most of the read time, and the BigStep
workers run BRepCheck + ShapeFix on every part afterwards anyway, in parallel.

OCCT 8 controls this only through
    STEPCAFControl_Reader::SetShapeProcessFlags(const std::bitset<18>&)
(empty flags + "is set" => no processing).  OCP cannot pass std::bitset, so
the exported C++ method is called through ctypes on the reader's C++ object.

Every step is verified; if anything does not match (other OCP/pybind11 build,
other OCCT version), the function returns False and the default behaviour stays.
"""
import ctypes

_SET_FLAGS = "_ZN21STEPCAFControl_Reader20SetShapeProcessFlagsERKSt6bitsetILm18EE"
_GET_FLAGS = "_ZNK21STEPCAFControl_Reader20GetShapeProcessFlagsEv"
_SET_NAME_MODE = "_ZN21STEPCAFControl_Reader11SetNameModeEb"


def _loaded_lib(name):
    with open("/proc/self/maps") as f:
        for line in f:
            path = line.split()[-1]
            if name in path and ".so" in path:
                return ctypes.CDLL(path)
    return None


def _cpp_pointer(pyobj):
    # pybind11 "simple layout" instance: PyObject header, then the value pointer.
    return ctypes.c_void_p.from_address(id(pyobj) + ctypes.sizeof(ctypes.c_void_p) * 2).value


def disable_transfer_healing(reader):
    """reader: OCP STEPCAFControl_Reader. Returns True if healing is now off."""
    try:
        lib = _loaded_lib("libTKDESTEP")
        if lib is None:
            return False
        set_flags, get_flags = getattr(lib, _SET_FLAGS), getattr(lib, _GET_FLAGS)
        set_name = getattr(lib, _SET_NAME_MODE)
        set_flags.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        get_flags.argtypes = [ctypes.c_void_p]
        get_flags.restype = ctypes.c_void_p
        set_name.argtypes = [ctypes.c_void_p, ctypes.c_bool]

        this = _cpp_pointer(reader)
        # 1. prove `this` is the reader: flip NameMode via C++, observe it from Python
        before = reader.GetNameMode()
        set_name(this, not before)
        ok = reader.GetNameMode() == (not before)
        set_name(this, before)
        if not ok or reader.GetNameMode() != before:
            return False

        # 2. set empty operation flags, then read back pair<bitset<18>, bool>
        empty = ctypes.c_uint64(0)
        set_flags(this, ctypes.byref(empty))
        res = get_flags(this)
        if not res:
            return False
        flags = ctypes.c_uint64.from_address(res).value & ((1 << 18) - 1)
        is_set = ctypes.c_bool.from_address(res + ctypes.sizeof(ctypes.c_uint64)).value
        return flags == 0 and is_set
    except (AttributeError, OSError, ValueError):
        return False
